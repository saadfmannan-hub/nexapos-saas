"""Sale lifecycle services.

complete_sale() is the single transactional entry point that turns a
validated cart into an immutable Sale with items, payments, stock
movements, customer balance changes and an invoice number.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.audit import services as audit
from apps.core.money import D, money, round_to_precision
from apps.customers import services as customer_services
from apps.inventory import services as inventory
from apps.subscriptions import services as subscriptions

from .models import (
    InvoiceSequence,
    PaymentMethod,
    Sale,
    SaleItem,
    SalePayment,
    SaleReturn,
    SaleReturnItem,
)

ZERO = Decimal("0")


class SaleError(Exception):
    pass


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


def next_invoice_number(business, branch):
    """Concurrency-safe invoice number driven by Business Settings.

    The base prefix always comes from BusinessSettings.invoice_prefix
    (the configurable value). When invoice_include_branch_code is on, the
    branch code is inserted and each branch is numbered independently;
    otherwise numbering is global per business.

        INV-2026-000001                (global, default)
        INV-HK-2026-000001             (per-branch)

    Historical invoice numbers are never touched — only new ones use the
    current configuration.
    """
    from django.db.models import Max

    settings_obj = business.settings
    year = timezone.now().year
    base = (settings_obj.invoice_prefix or "INV").strip() or "INV"
    include_branch = settings_obj.invoice_include_branch_code

    seq_branch = branch if include_branch else None
    seq, created = InvoiceSequence.objects.get_or_create(
        business=business, branch=seq_branch, year=year
    )
    if created and seq_branch is None:
        # When switching to global numbering, continue above any existing
        # per-branch sequence for the year so a new global counter can't
        # mint a number that an old branch sequence already used.
        highest = (
            InvoiceSequence.objects.for_business(business)
            .filter(year=year).exclude(pk=seq.pk)
            .aggregate(m=Max("last_number"))["m"]
        )
        if highest:
            seq.last_number = highest
            seq.save(update_fields=["last_number"])

    # select_for_update serializes concurrent finalizations on this counter
    seq = InvoiceSequence.objects.select_for_update().get(pk=seq.pk)
    seq.last_number += 1
    seq.save(update_fields=["last_number"])

    if include_branch:
        segment = (branch.invoice_prefix or branch.code or "").strip()
        if segment:
            return f"{base}-{segment}-{year}-{seq.last_number:06d}"
    return f"{base}-{year}-{seq.last_number:06d}"


def _resolve_price(product, variant):
    if variant is not None and variant.sale_price > 0:
        return variant.sale_price
    return product.sale_price


def _resolve_cost(product, variant):
    if variant is not None:
        return variant.average_cost or variant.purchase_price
    return product.average_cost or product.purchase_price


def compute_line(product, variant, quantity, unit_price, discount_amount, prices_include_tax):
    """Compute one cart line. Returns dict of money parts (tax-exclusive base)."""
    quantity = D(quantity)
    unit_price = money(unit_price)
    discount_amount = money(discount_amount)
    rate = D(product.effective_tax_rate())

    gross = unit_price * quantity - discount_amount
    if gross < 0:
        raise SaleError("Line discount cannot exceed the line amount.")

    include = (
        product.price_includes_tax
        if product.price_includes_tax is not None
        else prices_include_tax
    )
    if rate > 0:
        if include:
            base = gross / (1 + rate / 100)
            tax = gross - base
        else:
            base = gross
            tax = gross * rate / 100
    else:
        base, tax = gross, ZERO
    return {
        "quantity": quantity,
        "unit_price": unit_price,
        "discount_amount": discount_amount,
        "tax_rate": rate,
        "base": money(base),
        "tax": money(tax),
        "total": money(base + tax),
    }


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
    request=None,
):
    """Finalize a sale.

    items:    [{product, variant, quantity, unit_price, discount_amount}]
    payments: [{method (PaymentMethod), amount, reference}]
    """
    subscriptions.require_operational(business)
    subscriptions.check_limit(business, "monthly_invoices")

    if not items:
        raise SaleError("Cannot complete a sale with no items.")

    settings_obj = business.settings
    prices_include_tax = settings_obj.prices_include_tax
    invoice_discount = money(invoice_discount)

    # ---- compute totals --------------------------------------------------
    computed = []
    subtotal = tax_total = line_discounts = ZERO
    for line in items:
        product, variant = line["product"], line.get("variant")
        if product.business_id != business.id:
            raise SaleError("Product does not belong to this business.")
        if variant is not None and variant.product_id != product.id:
            raise SaleError("Variant does not match product.")
        qty = D(line["quantity"])
        if qty <= 0:
            raise SaleError("Quantity must be positive.")
        unit_price = money(line.get("unit_price", _resolve_price(product, variant)))
        if unit_price < 0:
            raise SaleError("Price cannot be negative.")
        min_price = product.minimum_sale_price or ZERO
        if min_price > 0 and unit_price < min_price:
            if not (membership and membership.has_perm("sales.price_override")):
                raise SaleError(
                    f"Price for {product.name} is below the minimum sale price."
                )
        discount = money(line.get("discount_amount", ZERO))
        if discount > 0 and not product.allow_discount:
            raise SaleError(f"Discounts are not allowed on {product.name}.")
        parts = compute_line(product, variant, qty, unit_price, discount, prices_include_tax)
        computed.append((line, parts))
        subtotal += parts["base"]
        tax_total += parts["tax"]
        line_discounts += discount

    if invoice_discount < 0:
        raise SaleError("Invoice discount cannot be negative.")
    if invoice_discount > subtotal + tax_total:
        raise SaleError("Invoice discount cannot exceed the sale amount.")

    # Discount permission / cap check
    total_discount = line_discounts + invoice_discount
    if total_discount > 0:
        if membership and not membership.has_perm("sales.discount"):
            raise SaleError("You do not have permission to apply discounts.")
        gross_before = subtotal + tax_total + line_discounts
        cap = settings_obj.max_discount_percent
        if cap < 100 and gross_before > 0:
            pct = total_discount / gross_before * 100
            if pct > cap:
                raise SaleError(
                    f"Total discount {pct:.1f}% exceeds the allowed maximum of {cap}%."
                )

    # Grand total is rounded to the business's display precision so that
    # what the cashier sees IS the amount owed (no hidden 3rd decimal that
    # would make an exact payment look insufficient). The delta is stored
    # on the sale as `rounding`.
    raw_total = money(subtotal + tax_total - invoice_discount)
    if settings_obj.price_rounding == "nearest":
        precision_total = money(
            round_to_precision(raw_total, business.currency_precision)
        )
    else:
        precision_total = raw_total
    rounding_delta = money(precision_total - raw_total)

    # ---- payments --------------------------------------------------------
    pay_total = ZERO
    credit_amount = ZERO
    store_credit_amount = ZERO
    cash_tendered = ZERO
    clean_payments = []
    for p in payments:
        amount = money(p["amount"])
        if amount <= 0:
            raise SaleError("Payment amounts must be positive.")
        method = p["method"]
        if method.business_id != business.id:
            raise SaleError("Invalid payment method.")
        if method.kind == PaymentMethod.Kind.CUSTOMER_CREDIT:
            credit_amount += amount
        elif method.kind == PaymentMethod.Kind.STORE_CREDIT:
            store_credit_amount += amount
        elif method.kind == PaymentMethod.Kind.CASH:
            cash_tendered += amount
        pay_total += amount
        clean_payments.append({"method": method, "amount": amount,
                               "reference": p.get("reference", "")})

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
        status=Sale.Status.COMPLETED,
        sale_date=timezone.now(),
        subtotal=money(subtotal),
        discount_amount=total_discount,
        tax_amount=money(tax_total),
        rounding=rounding_delta,
        total=precision_total,
        amount_paid=money(pay_total - change_due - credit_amount),
        change_due=change_due,
        notes=notes,
        delivery_date=delivery_date,
        delivery_status=(Sale.DeliveryStatus.PENDING if delivery_date else ""),
    )

    total_cost = ZERO
    for line, parts in computed:
        product, variant = line["product"], line.get("variant")
        unit_cost = money(_resolve_cost(product, variant))
        line_cost = money(unit_cost * parts["quantity"])
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
        )
        if product.is_stocked:
            inventory.record_movement(
                business=business,
                warehouse=warehouse,
                product=product,
                variant=variant,
                movement_type="sale",
                quantity=-parts["quantity"],
                unit_cost=unit_cost,
                reference_type="Sale",
                reference_id=sale.invoice_number,
                user=cashier,
            )

    sale.total_cost = money(total_cost)
    sale.gross_profit = money(subtotal - invoice_discount - total_cost)

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
def void_sale(*, sale, user, reason, request=None):
    if sale.status in (Sale.Status.VOIDED,):
        raise SaleError("Sale is already voided.")
    if sale.returns.exists():
        raise SaleError("A sale with returns cannot be voided.")

    # Restore stock
    for item in sale.items.select_related("product", "variant"):
        if item.product.is_stocked:
            inventory.record_movement(
                business=sale.business,
                warehouse=sale.warehouse,
                product=item.product,
                variant=item.variant,
                movement_type="sale_return",
                quantity=item.quantity,
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

    if sale.status == Sale.Status.VOIDED:
        raise SaleError("Cannot return items from a voided sale.")
    if not items:
        raise SaleError("Select at least one item to return.")

    business = sale.business
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
        item = entry["sale_item"]
        if item.sale_id != sale.id:
            raise SaleError("Return item does not belong to this sale.")
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
        if do_restock and item.product.is_stocked:
            inventory.record_movement(
                business=business,
                warehouse=sale.warehouse,
                product=item.product,
                variant=item.variant,
                movement_type="sale_return",
                quantity=qty,
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
