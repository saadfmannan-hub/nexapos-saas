"""Sale lifecycle services.

complete_sale() is the single transactional entry point that turns a
validated cart into an immutable Sale with items, payments, stock
movements, customer balance changes and an invoice number.
"""
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.audit import services as audit
from apps.catalog.models import ProductVariant
from apps.core.money import D, money, qty
from apps.customers import services as customer_services
from apps.inventory import services as inventory
from apps.subscriptions import services as subscriptions

from . import calculations
from .models import (
    MAX_FABRIC_TOTAL,
    InvoiceSequence,
    PaymentMethod,
    Sale,
    SaleItem,
    SalePayment,
    SaleReturn,
    SaleReturnItem,
)

ZERO = Decimal("0")
ONE = Decimal("1")
MAX_POS_FABRIC_METER = Decimal("1000.000")
TAILORING_FIELDS = {
    "design_type",
    "daraz_details",
    "vip_3d_design",
    "computer_design",
    "customer_notes",
    "workshop_notes",
}
TAILORING_DESIGN_TYPES = {
    "Daraz",
    "VIP 3D",
    "VIP 3D Design",
    "Computer Design",
}
TAILORING_FIELD_LIMITS = {
    "design_type": 50,
    "daraz_details": 200,
    "vip_3d_design": 200,
    "computer_design": 200,
    "customer_notes": 500,
    "workshop_notes": 500,
}


class SaleError(Exception):
    def __init__(self, message, *, errors=None):
        super().__init__(message)
        self.errors = errors or {}


DEFAULT_PAYMENT_METHODS = [
    ("Cash", PaymentMethod.Kind.CASH),
    ("Card", PaymentMethod.Kind.CARD),
    ("Bank Transfer", PaymentMethod.Kind.BANK),
    ("Customer Credit", PaymentMethod.Kind.CUSTOMER_CREDIT),
    ("Store Credit", PaymentMethod.Kind.STORE_CREDIT),
]


def create_default_payment_methods(business):
    for name, kind in DEFAULT_PAYMENT_METHODS:
        PaymentMethod.objects.get_or_create(
            business=business, name=name, defaults={"kind": kind, "is_system": True}
        )


# Sentinel "year" for the lifetime (non-resetting) invoice counter. The
# number format carries no year, so the counter must never reset — using a
# fixed key keeps a single ongoing sequence per scope and guarantees the
# 3-digit running number stays unique across years.
LIFETIME_SEQUENCE = 0


def next_invoice_number(business, branch):
    """Concurrency-safe invoice number driven by Business Settings.

    Format is the configured prefix + a simple zero-padded running number
    (minimum 3 digits, growing past 999 naturally). No year, no second
    sequence:

        INV B-001, INV B-002, ... INV B-999, INV B-1000   (default)
        INV B-HK-001                                       (per-branch opt-in)

    The counter is lifetime (does not reset per year). Historical invoice
    numbers are never touched — only new ones use this configuration.
    """
    settings_obj = business.settings
    base = (settings_obj.invoice_prefix or "INV").strip() or "INV"
    include_branch = settings_obj.invoice_include_branch_code

    seq_branch = branch if include_branch else None
    seq, _ = InvoiceSequence.objects.get_or_create(
        business=business, branch=seq_branch, year=LIFETIME_SEQUENCE
    )
    # select_for_update serializes concurrent finalizations on this counter
    seq = InvoiceSequence.objects.select_for_update().get(pk=seq.pk)
    seq.last_number += 1
    seq.save(update_fields=["last_number"])

    number = f"{seq.last_number:03d}"  # 001..999, then 1000, 1001, ...
    if include_branch:
        segment = (branch.invoice_prefix or branch.code or "").strip()
        if segment:
            return f"{base}-{segment}-{number}"
    return f"{base}-{number}"


def _resolve_price(product, variant):
    if variant is not None and variant.sale_price > 0:
        return variant.sale_price
    return product.sale_price


def _resolve_cost(product, variant):
    if variant is not None:
        return variant.average_cost or variant.purchase_price
    return product.average_cost or product.purchase_price


def compute_line(
    product, variant, quantity, unit_price, discount_amount,
    prices_include_tax, tax_rate=None,
):
    """Backward-compatible wrapper around the commercial calculation engine."""
    try:
        return calculations.compute_line(
            product,
            variant,
            quantity,
            unit_price,
            discount_amount,
            prices_include_tax=prices_include_tax,
            tax_rate=tax_rate,
        )
    except calculations.CalculationError as exc:
        raise SaleError(str(exc)) from exc


def _clean_tailoring_details(raw, *, field_prefix="tailoring_details"):
    if not isinstance(raw, dict):
        raise SaleError(
            "Invalid tailoring details.",
            errors={field_prefix: "Tailoring details must be an object."},
        )
    details = {}
    for key in TAILORING_FIELDS:
        value = str(raw.get(key, "") or "").strip()
        if not value:
            continue
        if len(value) > TAILORING_FIELD_LIMITS[key]:
            label = key.replace("_", " ").title()
            message = f"{label} must be {TAILORING_FIELD_LIMITS[key]} characters or fewer."
            raise SaleError(message, errors={f"{field_prefix}.{key}": message})
        details[key] = value
    design_type = details.get("design_type")
    if design_type and design_type not in TAILORING_DESIGN_TYPES:
        message = "Select a valid design type."
        raise SaleError(
            message,
            errors={f"{field_prefix}.design_type": message},
        )
    return details


def _fabric_estimate(product, classification, quantity, *, field_prefix):
    field_name = (
        "estimated_adult_fabric"
        if classification == SaleItem.GarmentClassification.ADULT
        else "estimated_child_fabric"
    )
    per_garment = getattr(product, field_name)
    if per_garment is None:
        label = classification.title()
        message = (
            f"Configure Estimated {label} Fabric for {product.name} before selling it."
        )
        raise SaleError(message, errors={f"{field_prefix}.garment_classification": message})
    estimate = qty(quantity * per_garment)
    if estimate > MAX_FABRIC_TOTAL:
        message = f"Estimated fabric for {product.name} is too large."
        raise SaleError(message, errors={field_prefix: message})
    return estimate


def _clean_actual_fabric(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SaleError(
            "Enter a valid actual fabric amount.",
            errors={"actual_fabric_used": "Enter a valid decimal amount."},
        ) from exc
    if not amount.is_finite():
        raise SaleError(
            "Enter a valid actual fabric amount.",
            errors={"actual_fabric_used": "Enter a valid decimal amount."},
        )
    amount = qty(amount)
    if amount < 0:
        raise SaleError(
            "Actual fabric used cannot be negative.",
            errors={"actual_fabric_used": "Actual fabric used cannot be negative."},
        )
    if amount > MAX_FABRIC_TOTAL:
        raise SaleError(
            "Actual fabric used is too large.",
            errors={"actual_fabric_used": "Actual fabric used is too large."},
        )
    return amount


def _clean_fabric_meter(value, *, field_prefix):
    """Validate the immutable POS meter quantity without silent rounding."""
    field = f"{field_prefix}.fabric_meter_used"
    if value is None or str(value).strip() == "":
        message = "Enter Meter for every tailoring garment."
        raise SaleError(message, errors={field: message})
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        message = "Enter a valid Meter amount."
        raise SaleError(message, errors={field: message}) from exc
    if not amount.is_finite():
        message = "Enter a valid Meter amount."
        raise SaleError(message, errors={field: message})
    if amount <= 0:
        message = "Meter must be greater than zero."
        raise SaleError(message, errors={field: message})
    if amount > MAX_POS_FABRIC_METER:
        message = "Meter cannot exceed 1000.000."
        raise SaleError(message, errors={field: message})
    if amount.as_tuple().exponent < -3:
        message = "Meter can have at most 3 decimal places."
        raise SaleError(message, errors={field: message})
    return qty(amount)


def _clean_checkout_token(value):
    if value is None:
        return None
    token = str(value).strip()
    if not token:
        return None
    if len(token) > 64:
        raise SaleError("Invalid checkout token.")
    return token


def _validate_checkout_replay(existing, *, cashier, branch, customer):
    """Prevent a token from being reused for a different sale context."""
    if (
        existing.cashier_id != cashier.id
        or existing.branch_id != branch.id
        or existing.customer_id != customer.id
    ):
        raise SaleError("Invalid checkout token.")
    return existing


def _validate_sale_context(
    *, business, branch, warehouse, cashier, customer, membership, register, shift
):
    if branch.business_id != business.id or not branch.is_active:
        raise SaleError("Invalid branch.")
    if warehouse.business_id != business.id or not warehouse.is_active:
        raise SaleError("Invalid warehouse.")
    if warehouse.branch_id not in (None, branch.id):
        raise SaleError("Warehouse does not belong to this branch.")
    if customer.business_id != business.id or not customer.is_active:
        raise SaleError("Invalid customer.")
    if membership is not None:
        if (
            membership.business_id != business.id
            or membership.user_id != cashier.id
            or not membership.is_active
        ):
            raise SaleError("Invalid business membership.")
        if not membership.has_perm("sales.create"):
            raise SaleError("You do not have permission to complete sales.")
        if not membership.can_access_branch(branch):
            raise SaleError("You cannot sell from this branch.")
    if register is not None:
        if (
            register.business_id != business.id
            or register.branch_id != branch.id
            or not register.is_active
        ):
            raise SaleError("Invalid or inactive register.")
    if shift is None:
        return
    if (
        shift.business_id != business.id
        or shift.cashier_id != cashier.id
        or shift.branch_id != branch.id
        or shift.status != "open"
        or register is None
        or shift.register_id != register.id
    ):
        raise SaleError("Invalid open shift for this branch.")


@transaction.atomic
def complete_sale(
    *,
    business,
    branch,
    warehouse,
    cashier,
    customer,
    items,
    payments,
    membership=None,
    register=None,
    shift=None,
    invoice_discount=ZERO,
    notes="",
    salesperson=None,
    delivery_date=None,
    priority=Sale.Priority.NORMAL,
    checkout_token=None,
    request=None,
):
    """Finalize a sale.

    items:    [{product, variant, quantity, unit_price, discount_amount}]
    payments: [{method (PaymentMethod), amount, reference}]
    """
    checkout_token = _clean_checkout_token(checkout_token)
    _validate_sale_context(
        business=business,
        branch=branch,
        warehouse=warehouse,
        cashier=cashier,
        customer=customer,
        membership=membership,
        register=register,
        shift=shift,
    )

    if checkout_token is not None:
        existing = (
            Sale.objects.for_business(business)
            .filter(checkout_token=checkout_token)
            .first()
        )
        if existing is not None:
            return _validate_checkout_replay(
                existing,
                cashier=cashier,
                branch=branch,
                customer=customer,
            )

    subscriptions.require_operational(business)
    subscriptions.check_limit(business, "monthly_invoices")

    if not items:
        raise SaleError("Cannot complete a sale with no items.")

    # Lock every product in a stable order before evaluating its Meter shape.
    # Product edits use the same row lock, so a checkout cannot race a unit or
    # Standard/Variant transition and write stock using stale semantics.
    product_model = items[0]["product"].__class__
    product_ids = sorted({line["product"].pk for line in items})
    locked_products = {
        product.pk: product
        for product in product_model.objects.select_for_update()
        .select_related("unit")
        .filter(pk__in=product_ids, business=business)
        .order_by("pk")
    }
    variant_ids = sorted({
        line["variant"].pk
        for line in items
        if line.get("variant") is not None and line["variant"].pk is not None
    })
    locked_variants = {
        variant.pk: variant
        for variant in ProductVariant.objects.select_for_update()
        .filter(pk__in=variant_ids, business=business)
        .order_by("pk")
    }

    priority = str(priority or Sale.Priority.NORMAL).strip().lower()
    if priority not in dict(Sale.Priority.choices):
        message = "Select a valid order priority."
        raise SaleError(message, errors={"priority": message})

    settings_obj = business.settings
    invoice_discount = money(invoice_discount)

    # ---- validate cart ---------------------------------------------------
    normalized_items = []
    has_tailoring_items = False
    for index, line in enumerate(items):
        product = locked_products.get(line["product"].pk)
        supplied_variant = line.get("variant")
        variant = (
            locked_variants.get(supplied_variant.pk)
            if supplied_variant is not None and supplied_variant.pk is not None
            else None
        )
        if product is None:
            raise SaleError("Invalid product in cart.")
        if (
            product.business_id != business.id
            or not product.is_active
            or product.is_archived
        ):
            raise SaleError("Invalid product in cart.")
        if supplied_variant is not None and (
            variant is None
            or variant.business_id != business.id
            or variant.product_id != product.id
            or not variant.is_active
        ):
            raise SaleError("Invalid variant in cart.")
        is_meter_tailoring = bool(
            product.is_tailoring_item
            and product.unit_id is not None
            and product.unit.is_meter
        )
        qty = D(line["quantity"])
        if qty <= 0:
            raise SaleError("Quantity must be positive.")
        default_price = ZERO if is_meter_tailoring else _resolve_price(product, variant)
        unit_price = money(line.get("unit_price", default_price))
        if unit_price < 0:
            raise SaleError("Price cannot be negative.")
        min_price = product.minimum_sale_price or ZERO
        if not is_meter_tailoring and min_price > 0 and unit_price < min_price:
            if not (membership and membership.has_perm("sales.price_override")):
                raise SaleError(
                    f"Price for {product.name} is below the minimum sale price."
                )
        discount = money(line.get("discount_amount", ZERO))
        if discount > 0 and (is_meter_tailoring or not product.allow_discount):
            raise SaleError(f"Discounts are not allowed on {product.name}.")
        field_prefix = f"items.{index}"
        if product.unit_id is not None and product.unit.business_id != business.id:
            raise SaleError("Invalid product unit in cart.")
        tailoring_details = _clean_tailoring_details(
            line.get("tailoring_details", {}),
            field_prefix=f"{field_prefix}.tailoring_details",
        )
        classification = str(
            line.get("garment_classification", "") or ""
        ).strip().lower()
        raw_collection_type = line.get("collection_type")
        collection_type = str(raw_collection_type or "").strip().lower()
        estimated_fabric = None
        fabric_meter_used = None
        meter_key_present = "fabric_meter_used" in line
        is_legacy_tailoring = bool(
            product.is_tailoring_item
            and product.unit_id is None
            and not meter_key_present
        )

        if is_meter_tailoring:
            has_tailoring_items = True
            if not product.is_stocked:
                message = f"{product.name} must track inventory before it can be sold."
                raise SaleError(message, errors={field_prefix: message})
            if product.has_variants and variant is None:
                message = f"Select a fabric color for {product.name}."
                raise SaleError(
                    message,
                    errors={f"{field_prefix}.variant_id": message},
                )
            if qty != ONE:
                message = "Quantity must be 1 for meter tailoring garments."
                raise SaleError(
                    message,
                    errors={f"{field_prefix}.quantity": message},
                )
            fabric_meter_used = _clean_fabric_meter(
                line.get("fabric_meter_used"),
                field_prefix=field_prefix,
            )
            if classification not in dict(SaleItem.GarmentClassification.choices):
                message = "Select Adult or Child for every garment."
                raise SaleError(
                    message,
                    errors={f"{field_prefix}.garment_classification": message},
                )
            if collection_type not in dict(SaleItem.CollectionType.choices):
                message = "Select Normal or Premium for every garment."
                raise SaleError(
                    message,
                    errors={f"{field_prefix}.collection_type": message},
                )
        elif is_legacy_tailoring:
            has_tailoring_items = True
            if classification not in dict(SaleItem.GarmentClassification.choices):
                message = "Select Adult or Child for every garment."
                raise SaleError(
                    message,
                    errors={f"{field_prefix}.garment_classification": message},
                )
            # Calls made before collection types existed omitted the key entirely.
            if raw_collection_type is None:
                collection_type = SaleItem.CollectionType.NORMAL
            if collection_type not in dict(SaleItem.CollectionType.choices):
                message = "Select Normal or Premium for every garment."
                raise SaleError(
                    message,
                    errors={f"{field_prefix}.collection_type": message},
                )
            estimated_fabric = _fabric_estimate(
                product,
                classification,
                qty,
                field_prefix=field_prefix,
            )
        elif product.is_tailoring_item and product.unit_id is None and meter_key_present:
            message = f"Select the Meter unit for {product.name} before entering Meter."
            raise SaleError(
                message,
                errors={f"{field_prefix}.fabric_meter_used": message},
            )
        elif classification or collection_type or tailoring_details:
            message = f"{product.name} is not configured as a tailoring garment."
            raise SaleError(message, errors={field_prefix: message})
        normalized_items.append({
            "product": product,
            "variant": variant,
            "quantity": qty,
            "unit_price": unit_price,
            "discount_amount": discount,
            "garment_classification": classification,
            "collection_type": collection_type,
            "estimated_fabric": estimated_fabric,
            "fabric_meter_used": fabric_meter_used,
            "tailoring_details": tailoring_details,
        })

    if has_tailoring_items and delivery_date is None:
        message = "Please select delivery date before completing the tailoring booking."
        raise SaleError(message, errors={"delivery_date": message})
    if delivery_date is not None:
        if not hasattr(delivery_date, "year"):
            message = "Invalid delivery date."
            raise SaleError(message, errors={"delivery_date": message})

    try:
        totals = calculations.calculate_sale_totals(
            business=business,
            items=normalized_items,
            invoice_discount=invoice_discount,
        )
    except calculations.CalculationError as exc:
        raise SaleError(str(exc)) from exc

    # Discount permission / cap check
    total_discount = totals["discount_total"]
    if total_discount > 0:
        if membership and not membership.has_perm("sales.discount"):
            raise SaleError("You do not have permission to apply discounts.")
        gross_before = totals["subtotal"] + totals["line_discounts"]
        cap = settings_obj.max_discount_percent
        if cap < 100 and gross_before > 0:
            pct = total_discount / gross_before * 100
            if pct > cap:
                raise SaleError(
                    f"Total discount {pct:.1f}% exceeds the allowed maximum of {cap}%."
                )

    # ---- payments --------------------------------------------------------
    for p in payments:
        method = p["method"]
        if method.business_id != business.id or not method.is_active:
            raise SaleError("Invalid payment method.")
    try:
        clean_payments, payment_totals = calculations.calculate_payment_totals(
            payments,
            lambda method: method.kind,
        )
    except calculations.CalculationError as exc:
        raise SaleError(str(exc)) from exc
    pay_total = payment_totals["pay_total"]
    credit_amount = payment_totals["credit_amount"]
    store_credit_amount = payment_totals["store_credit_amount"]
    cash_tendered = payment_totals["cash_tendered"]
    precision_total = totals["total"]

    change_due = ZERO
    if pay_total > precision_total:
        overpay = pay_total - precision_total
        if cash_tendered >= overpay:
            change_due = overpay
        else:
            raise SaleError("Overpayment is only allowed for cash (change due).")
    elif pay_total < precision_total:
        raise SaleError(
            "Payments do not cover the total. Use Customer Credit for the "
            "unpaid balance."
        )

    # ---- credit validation -----------------------------------------------
    if credit_amount > 0:
        if customer.is_walk_in and settings_obj.require_customer_for_credit:
            raise SaleError("Credit sales require a named customer.")
        if membership and not membership.has_perm("sales.credit"):
            raise SaleError("You do not have permission to make credit sales.")
        if customer.credit_limit > 0:
            projected = customer.balance + credit_amount
            if projected > customer.credit_limit:
                if not (membership and membership.has_perm("credit.approve")):
                    raise SaleError(
                        "This sale would exceed the customer's credit limit."
                    )
    if store_credit_amount > 0 and customer.store_credit < store_credit_amount:
        raise SaleError("Customer does not have enough store credit.")

    # ---- shift requirement -----------------------------------------------
    if shift is None and not settings_obj.allow_sale_without_shift:
        raise SaleError("An open shift is required before selling.")

    # ---- create records ---------------------------------------------------
    try:
        # Keep invoice-sequence mutation and the token-unique insert in one
        # savepoint. A concurrent replay rolls both back before we return the
        # already committed sale.
        with transaction.atomic():
            sale = Sale.objects.create(
                business=business,
                branch=branch,
                warehouse=warehouse,
                register=register,
                shift=shift,
                cashier=cashier,
                salesperson=salesperson,
                customer=customer,
                invoice_number=next_invoice_number(business, branch),
                checkout_token=checkout_token,
                status=Sale.Status.COMPLETED,
                priority=priority,
                sale_date=timezone.now(),
                subtotal=totals["subtotal"],
                discount_amount=total_discount,
                tax_amount=totals["tax_total"],
                rounding=totals["rounding"],
                total=precision_total,
                amount_paid=money(pay_total - change_due - credit_amount),
                change_due=change_due,
                notes=notes,
                delivery_date=delivery_date,
                delivery_status=(Sale.DeliveryStatus.PENDING if delivery_date else ""),
            )
    except IntegrityError:
        if checkout_token is not None:
            existing = (
                Sale.objects.for_business(business)
                .filter(checkout_token=checkout_token)
                .first()
            )
            if existing is not None:
                return _validate_checkout_replay(
                    existing,
                    cashier=cashier,
                    branch=branch,
                    customer=customer,
                )
        raise

    total_cost = ZERO
    for line, parts in totals["lines"]:
        product, variant = line["product"], line.get("variant")
        unit_cost = money(_resolve_cost(product, variant))
        inventory_quantity = (
            line["fabric_meter_used"]
            if line.get("fabric_meter_used") is not None
            else parts["quantity"]
        )
        line_cost = money(unit_cost * inventory_quantity)
        total_cost += line_cost
        SaleItem.objects.create(
            business=business,
            sale=sale,
            product=product,
            variant=variant,
            product_name=(variant.__str__() if variant else product.name)[:240],
            sku=(variant.sku if variant else product.sku) or "",
            quantity=parts["quantity"],
            unit_price=parts["unit_price"],
            discount_amount=parts["discount_amount"],
            tax_rate=parts["tax_rate"],
            tax_amount=parts["tax"],
            line_total=parts["total"],
            unit_cost=unit_cost,
            gross_profit=money(parts["base"] - line_cost),
            garment_classification=line.get("garment_classification", ""),
            collection_type=line.get("collection_type", ""),
            estimated_fabric=line.get("estimated_fabric"),
            fabric_meter_used=line.get("fabric_meter_used"),
            tailoring_details=line.get("tailoring_details", {}),
        )
        if product.is_stocked:
            inventory.record_movement(
                business=business,
                warehouse=warehouse,
                product=product,
                variant=variant,
                movement_type="sale",
                quantity=-inventory_quantity,
                unit_cost=unit_cost,
                reference_type="Sale",
                reference_id=sale.invoice_number,
                user=cashier,
            )

    sale.total_cost = money(total_cost)
    sale.gross_profit = money(totals["subtotal"] - invoice_discount - total_cost)

    for p in clean_payments:
        amount = p["amount"]
        if p["method"].kind == PaymentMethod.Kind.CASH and change_due > 0:
            amount = money(amount - change_due)  # store net cash received
            change_due = ZERO
            if amount <= 0:
                continue
        SalePayment.objects.create(
            business=business, sale=sale, method=p["method"],
            amount=amount, payment_date=timezone.localdate(),
            reference=p["reference"], received_by=cashier, shift=shift,
        )

    if credit_amount > 0:
        customer_services.apply_balance_change(customer.id, credit_amount)
        sale.status = (
            Sale.Status.CREDIT
            if credit_amount >= precision_total
            else Sale.Status.PARTIAL
        )
    if store_credit_amount > 0:
        customer_services.apply_store_credit_change(customer.id, -store_credit_amount)

    sale.save()

    audit.log("sale.completed", business=business, user=cashier, request=request,
              module="sales", obj=sale,
              description=f"Sale {sale.invoice_number} completed for {sale.total}.")
    return sale


@transaction.atomic
def add_sale_payment(
    *, sale, amount, method, user, payment_date=None, reference="", notes="",
    shift=None, request=None,
):
    """Record a later payment against a credit / partially-paid sale.

    Updates sale.amount_paid, the sale status, and (because the unpaid
    portion of a sale sits on the customer's receivable balance) reduces
    the customer balance by the same amount.
    """
    amount = money(amount)
    if amount <= 0:
        raise SaleError("Payment amount must be positive.")
    if sale.status == Sale.Status.VOIDED:
        raise SaleError("Voided sales cannot receive payments.")
    if sale.status == Sale.Status.DRAFT:
        raise SaleError("Draft sales cannot receive payments.")
    if method.business_id != sale.business_id:
        raise SaleError("Invalid payment method.")
    if method.kind in (PaymentMethod.Kind.CUSTOMER_CREDIT,
                       PaymentMethod.Kind.STORE_CREDIT):
        raise SaleError("Use a real payment method to settle a balance.")
    if amount > sale.balance:
        raise SaleError(
            f"Payment {amount} exceeds the outstanding balance {sale.balance}."
        )

    payment = SalePayment.objects.create(
        business=sale.business,
        sale=sale,
        method=method,
        amount=amount,
        payment_date=payment_date or timezone.localdate(),
        reference=reference[:120],
        notes=notes[:300],
        received_by=user,
        shift=shift,
    )
    sale.amount_paid = money(sale.amount_paid + amount)
    if sale.status in (Sale.Status.CREDIT, Sale.Status.PARTIAL):
        sale.status = (
            Sale.Status.COMPLETED if sale.balance <= 0 else Sale.Status.PARTIAL
        )
    sale.save(update_fields=["amount_paid", "status", "updated_at"])

    # The unpaid balance was carried on the customer account — settle it.
    customer_services.apply_balance_change(sale.customer_id, -amount)

    audit.log("sale.payment_added", business=sale.business, user=user,
              request=request, module="sales", obj=payment,
              description=(f"Payment {amount} ({method.name}) received on "
                           f"{sale.invoice_number}; balance now {sale.balance}."),
              new_values={"amount": str(amount), "method": method.name,
                          "payment_date": str(payment.payment_date)})
    return payment


@transaction.atomic
def delete_sale(*, sale, user, request=None):
    """Hard-delete a sale ONLY when it has zero business impact:
    a draft with no payments, no stock movements and no returns.
    Anything else must be voided so the audit trail survives."""
    from apps.inventory.models import StockMovement

    if sale.status != Sale.Status.DRAFT:
        raise SaleError(
            "Only draft sales can be deleted. Completed sales must be "
            "voided so the invoice number and audit trail are preserved."
        )
    if sale.payments.exists():
        raise SaleError("Sales with recorded payments cannot be deleted.")
    if sale.returns.exists():
        raise SaleError("Sales with returns cannot be deleted.")
    if sale.invoice_number and StockMovement.objects.for_business(
        sale.business
    ).filter(reference_type="Sale", reference_id=sale.invoice_number).exists():
        raise SaleError("Sales with stock movements cannot be deleted — void instead.")

    description = f"Draft sale #{sale.pk} ({sale.invoice_number or 'no invoice'}) deleted."
    audit.log("sale.deleted", business=sale.business, user=user, request=request,
              module="sales", obj=sale, description=description)
    sale.delete()


def set_delivery_status(*, sale, status, user, request=None):
    if status not in dict(Sale.DeliveryStatus.choices):
        raise SaleError("Invalid delivery status.")
    if sale.status == Sale.Status.VOIDED:
        raise SaleError("Voided sales cannot change delivery status.")
    old = sale.delivery_status
    sale.delivery_status = status
    sale.save(update_fields=["delivery_status", "updated_at"])
    audit.log("sale.delivery_status", business=sale.business, user=user,
              request=request, module="sales", obj=sale,
              old_values={"delivery_status": old},
              new_values={"delivery_status": status},
              description=(f"Delivery status of {sale.invoice_number} "
                           f"changed {old or '—'} → {status}."))
    return sale


@transaction.atomic
def update_actual_fabric(
    *, sale_item, actual_fabric_used, user, membership, request=None
):
    if (
        membership is None
        or not membership.is_active
        or membership.business_id != sale_item.business_id
        or membership.user_id != user.id
        or not membership.has_perm("workshop.fabric_actual")
    ):
        raise PermissionDenied

    item = (
        SaleItem.objects.select_for_update()
        .select_related("sale__branch", "product")
        .get(pk=sale_item.pk, business_id=membership.business_id)
    )
    if not membership.can_access_branch(item.sale.branch):
        raise PermissionDenied
    if not item.is_tailoring_line:
        raise SaleError("Actual fabric can only be recorded for tailoring items.")
    if item.fabric_meter_used is not None:
        raise SaleError(
            "Meter was recorded at POS for this garment and cannot be replaced "
            "by workshop actual fabric."
        )

    amount = _clean_actual_fabric(actual_fabric_used)
    old = item.actual_fabric_used
    item.actual_fabric_used = amount
    item.save(update_fields=["actual_fabric_used", "updated_at"])
    audit.log(
        "sale.fabric_actual_updated",
        business=item.business,
        user=user,
        request=request,
        module="sales",
        obj=item,
        old_values={"actual_fabric_used": None if old is None else str(old)},
        new_values={"actual_fabric_used": None if amount is None else str(amount)},
        description=(
            f"Actual fabric for {item.product_name} on "
            f"{item.sale.invoice_number} updated."
        ),
    )
    return item


@transaction.atomic
def void_sale(*, sale, user, reason, request=None):
    sale = (
        Sale.objects.select_for_update()
        .select_related("business", "customer", "warehouse")
        .get(pk=sale.pk, business_id=sale.business_id)
    )
    if sale.status in (Sale.Status.VOIDED,):
        raise SaleError("Sale is already voided.")
    if sale.returns.exists():
        raise SaleError("A sale with returns cannot be voided.")

    # Restore stock
    items = list(
        SaleItem.objects.select_for_update()
        .filter(sale=sale, business=sale.business)
        .select_related("product", "variant")
        .order_by("pk")
    )
    for item in items:
        deducted_meter = item.fabric_meter_used is not None
        if deducted_meter or item.product.is_stocked:
            inventory.record_movement(
                business=sale.business,
                warehouse=sale.warehouse,
                product=item.product,
                variant=item.variant,
                movement_type="sale_return",
                quantity=item.inventory_quantity,
                unit_cost=item.unit_cost,
                reference_type="Void",
                reference_id=sale.invoice_number,
                user=user,
                notes=f"Void: {reason}"[:300],
            )
    # Reverse customer balance effects
    credit_paid = sale.total - sale.amount_paid
    if credit_paid > 0:
        customer_services.apply_balance_change(sale.customer_id, -credit_paid)
    store_credit_used = sale.payments.filter(
        method__kind=PaymentMethod.Kind.STORE_CREDIT
    ).aggregate(t=Sum("amount"))["t"] or ZERO
    if store_credit_used > 0:
        customer_services.apply_store_credit_change(sale.customer_id, store_credit_used)

    sale.status = Sale.Status.VOIDED
    sale.voided_at = timezone.now()
    sale.voided_by = user
    sale.void_reason = reason[:255]
    sale.save()
    audit.log("sale.voided", business=sale.business, user=user, request=request,
              module="sales", obj=sale,
              description=f"Sale {sale.invoice_number} voided: {reason}")
    return sale


@transaction.atomic
def process_return(
    *,
    sale,
    items,
    refund_method,
    user,
    reason="",
    restock=True,
    shift=None,
    request=None,
):
    """items: [{sale_item, quantity, restock(optional)}]"""
    from apps.customers.models import Customer

    items = list(items)
    if not items:
        raise SaleError("Select at least one item to return.")

    # Lock Sale first, then SaleItems. Void follows the same lock order so a
    # return and a void cannot both restore the same inventory.
    sale = (
        Sale.objects.select_for_update()
        .select_related("business", "customer", "branch", "warehouse")
        .get(pk=sale.pk, business_id=sale.business_id)
    )
    if sale.status == Sale.Status.VOIDED:
        raise SaleError("Cannot return items from a voided sale.")

    if shift is not None and (
        shift.business_id != sale.business_id
        or shift.cashier_id != user.id
        or shift.branch_id != sale.branch_id
        or shift.status != "open"
    ):
        raise SaleError("Invalid open shift for this return.")

    business = sale.business
    requested_ids = []
    for entry in items:
        sale_item = entry.get("sale_item") if isinstance(entry, dict) else None
        if sale_item is None or sale_item.pk is None:
            raise SaleError("Invalid return item.")
        requested_ids.append(sale_item.pk)
    locked_items = {
        item.pk: item
        for item in (
            SaleItem.objects.select_for_update()
            .filter(
                business=business,
                sale=sale,
                pk__in=requested_ids,
            )
            .select_related("product", "variant")
            .order_by("pk")
        )
    }
    if len(locked_items) != len(set(requested_ids)):
        raise SaleError("Return item does not belong to this sale.")

    settings_obj = business.settings
    if settings_obj.return_window_days:
        deadline = sale.sale_date + timezone.timedelta(
            days=settings_obj.return_window_days
        )
        if timezone.now() > deadline:
            raise SaleError("The return window for this sale has expired.")

    n = SaleReturn.objects.for_business(business).count() + 1
    while SaleReturn.objects.for_business(business).filter(
        return_number=f"RET-{n:06d}"
    ).exists():
        n += 1

    sale_return = SaleReturn.objects.create(
        business=business,
        return_number=f"RET-{n:06d}",
        sale=sale,
        customer=sale.customer,
        branch=sale.branch,
        warehouse=sale.warehouse,
        reason=reason[:255],
        refund_method=refund_method,
        restock=restock,
        processed_by=user,
        shift=shift,
    )

    refund_total = ZERO
    for entry in items:
        item = locked_items[entry["sale_item"].pk]
        qty = D(entry["quantity"])
        if qty <= 0:
            continue
        if qty > item.returnable_quantity:
            raise SaleError(
                f"Cannot return {qty} of {item.product_name}; only "
                f"{item.returnable_quantity} remain."
            )
        # Refund proportionally: line_total includes tax minus discounts
        per_unit = money(item.line_total / item.quantity) if item.quantity else ZERO
        line_refund = money(per_unit * qty)
        do_restock = restock and entry.get("restock", True)
        if item.fabric_meter_used is not None:
            if qty != item.quantity:
                raise SaleError(
                    "A meter tailoring garment must be fully returned to "
                    "process its refund safely."
                )
        if item.fabric_meter_used is not None and do_restock:
            if item.return_items.filter(restocked=True).exists():
                raise SaleError("Fabric stock has already been restored for this garment.")
        SaleReturnItem.objects.create(
            business=business,
            sale_return=sale_return,
            sale_item=item,
            quantity=qty,
            refund_per_unit=per_unit,
            line_refund=line_refund,
            restocked=do_restock,
        )
        item.returned_quantity += qty
        item.save(update_fields=["returned_quantity"])
        refund_total += line_refund
        deducted_meter = item.fabric_meter_used is not None
        if do_restock and (deducted_meter or item.product.is_stocked):
            restore_quantity = item.fabric_meter_used if deducted_meter else qty
            inventory.record_movement(
                business=business,
                warehouse=sale.warehouse,
                product=item.product,
                variant=item.variant,
                movement_type="sale_return",
                quantity=restore_quantity,
                unit_cost=item.unit_cost,
                reference_type="SaleReturn",
                reference_id=sale_return.return_number,
                user=user,
            )

    if refund_total <= 0:
        raise SaleError("Nothing to return.")

    sale_return.refund_amount = refund_total
    sale_return.save(update_fields=["refund_amount"])

    # Apply refund financially
    if refund_method == SaleReturn.RefundMethod.STORE_CREDIT:
        customer_services.apply_store_credit_change(sale.customer_id, refund_total)
    elif refund_method == SaleReturn.RefundMethod.CUSTOMER_ACCOUNT:
        outstanding = Customer.objects.get(pk=sale.customer_id).balance
        applied = min(outstanding, refund_total)
        if applied > 0:
            customer_services.apply_balance_change(sale.customer_id, -applied)
        leftover = refund_total - applied
        if leftover > 0:
            customer_services.apply_store_credit_change(sale.customer_id, leftover)
    # cash/card/bank: money leaves the drawer — reflected in shift totals.

    # Update sale status
    remaining = sale.items.aggregate(q=Sum("quantity"), r=Sum("returned_quantity"))
    if remaining["r"] and remaining["q"] and remaining["r"] >= remaining["q"]:
        sale.status = Sale.Status.RETURNED
    else:
        sale.status = Sale.Status.PART_RETURNED
    sale.save(update_fields=["status"])

    audit.log("sale.returned", business=business, user=user, request=request,
              module="sales", obj=sale_return,
              description=(f"Return {sale_return.return_number} for invoice "
                           f"{sale.invoice_number}: {refund_total} via {refund_method}."))
    return sale_return
