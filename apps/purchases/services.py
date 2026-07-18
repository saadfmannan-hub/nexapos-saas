"""Purchase lifecycle: ordering, receiving, payment, returns.

Stock only increases when goods are received; supplier payable only
increases when goods are received (the payable follows the received
value, with the full total recorded on the purchase itself).
"""
from datetime import date
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import Http404
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.audit import services as audit
from apps.branches.models import Branch, Warehouse
from apps.catalog.models import Category, Product, ProductVariant, TaxRate, Unit
from apps.core.date_ranges import business_localdate
from apps.core.money import money
from apps.inventory import services as inventory
from apps.sales.models import PaymentMethod
from apps.subscriptions import services as subscription_services
from apps.subscriptions.access import AccessAction, require_actor_access
from apps.suppliers.models import Supplier, SupplierPayment

from .models import Purchase, PurchaseItem, PurchaseReturn, PurchaseReturnItem

ZERO = Decimal("0")
DECIMAL_FIELD_LIMIT = Decimal("100000000000")


def _validated_decimal(value, label):
    """Parse untrusted numeric input and reject NaN/infinity explicitly."""

    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        parsed = value
    else:
        raw_value = repr(value) if isinstance(value, float) else str(value)
        try:
            parsed = Decimal(raw_value)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValidationError(f"{label} must be a valid number.") from exc
    if not parsed.is_finite():
        raise ValidationError(f"{label} must be a finite number.")
    if abs(parsed) >= DECIMAL_FIELD_LIMIT:
        raise ValidationError(f"{label} is outside the supported range.")
    return parsed


def _validated_money(value, label):
    try:
        return money(_validated_decimal(value, label))
    except InvalidOperation as exc:
        raise ValidationError(f"{label} must be a valid money amount.") from exc


def _validated_date(value, label, *, allow_empty=False):
    if isinstance(value, date):
        return value
    if allow_empty and (value is None or value == ""):
        return None
    try:
        parsed = parse_date(str(value or ""))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Enter a valid {label}.") from exc
    if parsed is None:
        raise ValidationError(f"Enter a valid {label}.")
    return parsed


def _ensure_purchases_scope(
    context,
    *,
    business,
    branch=None,
    warehouse=None,
    tenant_objects=(),
):
    membership = context.membership
    allowed_branches = membership.allowed_branch_ids
    allowed_warehouses = membership.allowed_warehouse_ids
    if branch is not None and (
        branch.business_id != business.pk
        or (allowed_branches is not None and branch.pk not in allowed_branches)
    ):
        raise Http404
    if warehouse is not None and (
        warehouse.business_id != business.pk
        or (allowed_warehouses is not None and warehouse.pk not in allowed_warehouses)
    ):
        raise Http404
    if branch is not None and warehouse is not None and warehouse.branch_id not in (
        None,
        branch.pk,
    ):
        raise Http404
    if any(
        getattr(obj, "business_id", None) != business.pk
        for obj in tenant_objects
        if obj is not None
    ):
        raise Http404


def require_purchases_write(
    *,
    business,
    user,
    permission_code="purchases.manage",
    membership=None,
    request=None,
    branch=None,
    warehouse=None,
    tenant_objects=(),
    lock_business=False,
):
    """Authorize one Purchasing mutation and its tenant/location scope."""

    request_business = getattr(request, "business", None)
    if request_business is not None and request_business.pk != getattr(
        business, "pk", None
    ):
        raise Http404
    business_queryset = business.__class__.objects
    if lock_business:
        # PostgreSQL NO KEY UPDATE still serializes Phase 2C workflows while
        # remaining compatible with FK KEY SHARE locks from unrelated inserts.
        business_queryset = business_queryset.select_for_update(no_key=True)
    business = business_queryset.filter(pk=business.pk).first()
    if business is None:
        raise Http404
    context = require_actor_access(
        user,
        business,
        "purchases",
        permission_code=permission_code,
        action=AccessAction.WRITE,
        membership=membership,
        # Critical service guards must not reuse a request-level entitlement
        # cache after business/subscription state changes concurrently.
        request=None,
    )
    _ensure_purchases_scope(
        context,
        business=business,
        branch=branch,
        warehouse=warehouse,
        tenant_objects=tenant_objects,
    )
    return context


def _business_for_canonical_object(obj, *, user, membership=None, request=None):
    """Resolve the actor's business without trusting the supplied ORM object.

    Callers pass an object that has already been reloaded canonically.  A
    request or explicit membership identifies the intended tenant; without
    either, require an exact active actor/object-business membership before
    entitlement evaluation.  Cross-tenant object probes therefore remain a
    not-found result instead of leaking membership state through a 403.
    """

    request_business = getattr(request, "business", None)
    if request_business is not None:
        if request_business.pk != obj.business_id:
            raise Http404
        return request_business

    if membership is not None:
        if (
            getattr(membership, "user_id", None) != getattr(user, "pk", None)
            or getattr(membership, "business_id", None) != obj.business_id
        ):
            raise Http404
        return obj.business

    from apps.accounts.models import Membership

    if not Membership.objects.filter(
        business_id=obj.business_id,
        user=user,
        is_active=True,
    ).exists():
        raise Http404
    return obj.business


def _authorized_purchase(
    *,
    purchase,
    user,
    membership=None,
    request=None,
    permission_code="purchases.manage",
    lock=True,
    lock_business=False,
):
    candidate = Purchase.objects.select_related("business").filter(
        pk=getattr(purchase, "pk", None)
    ).first()
    if candidate is None:
        raise Http404
    business = _business_for_canonical_object(
        candidate,
        user=user,
        membership=membership,
        request=request,
    )
    context = require_purchases_write(
        business=business,
        user=user,
        permission_code=permission_code,
        membership=membership,
        request=request,
        lock_business=lock_business,
    )
    business = context.business
    queryset = Purchase.objects.select_related(
        "business", "supplier", "branch", "warehouse"
    )
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    locked_purchase = queryset.filter(
        pk=candidate.pk,
        business=business,
    ).first()
    if locked_purchase is None:
        raise Http404
    _ensure_purchases_scope(
        context,
        business=business,
        branch=locked_purchase.branch,
        warehouse=locked_purchase.warehouse,
        tenant_objects=(locked_purchase.supplier,),
    )
    return locked_purchase, context


def authorize_purchase_write(
    *,
    purchase,
    user,
    permission_code="purchases.view",
    membership=None,
    request=None,
):
    """Authorize an unsafe Purchase output action before external effects."""

    authorized_purchase, _context = _authorized_purchase(
        purchase=purchase,
        user=user,
        membership=membership,
        request=request,
        permission_code=permission_code,
        lock=False,
    )
    return authorized_purchase


def _positive_requested_ids(quantities):
    requested = set()
    for item_id, raw_quantity in quantities.items():
        try:
            normalized_id = int(item_id)
            quantity = _validated_decimal(raw_quantity, "Quantity")
        except (TypeError, ValueError):
            raise Http404 from None
        if quantity > 0:
            requested.add(normalized_id)
    return requested


def _locked_purchase_items(purchase):
    """Lock and validate every child item, including inconsistent tenants."""

    items = list(
        PurchaseItem.objects.select_for_update(of=("self",))
        .select_related("product__unit", "variant")
        .filter(purchase=purchase)
        .order_by("pk")
    )
    for item in items:
        if (
            item.business_id != purchase.business_id
            or item.product.business_id != purchase.business_id
            or (
                item.product.unit_id is not None
                and item.product.unit.business_id != purchase.business_id
            )
            or (
                item.variant_id is not None
                and (
                    item.variant.business_id != purchase.business_id
                    or item.variant.product_id != item.product_id
                )
            )
        ):
            raise Http404
    return items


def _locked_purchase_payments(purchase):
    payments = list(
        SupplierPayment.objects.select_for_update(of=("self",))
        .select_related("supplier", "payment_method")
        .filter(purchase=purchase)
        .order_by("pk")
    )
    for payment in payments:
        if (
            payment.business_id != purchase.business_id
            or payment.supplier_id != purchase.supplier_id
            or payment.supplier.business_id != purchase.business_id
            or (
                payment.payment_method_id is not None
                and payment.payment_method.business_id != purchase.business_id
            )
        ):
            raise Http404
    return payments


def _locked_returned_quantities(purchase, purchase_items):
    purchase_returns = list(
        PurchaseReturn.objects.select_for_update(of=("self",))
        .select_related("supplier", "warehouse")
        .filter(purchase=purchase)
        .order_by("pk")
    )
    for purchase_return in purchase_returns:
        if (
            purchase_return.business_id != purchase.business_id
            or purchase_return.supplier_id != purchase.supplier_id
            or purchase_return.supplier.business_id != purchase.business_id
            or purchase_return.warehouse_id != purchase.warehouse_id
            or purchase_return.warehouse.business_id != purchase.business_id
        ):
            raise Http404

    item_ids = {item.pk for item in purchase_items}
    return_ids = {purchase_return.pk for purchase_return in purchase_returns}
    returned_quantities = {}
    return_items = list(
        PurchaseReturnItem.objects.select_for_update(of=("self",))
        .select_related("purchase_return", "purchase_item")
        .filter(Q(purchase_return_id__in=return_ids) | Q(purchase_item_id__in=item_ids))
        .order_by("pk")
    )
    for return_item in return_items:
        if (
            return_item.business_id != purchase.business_id
            or return_item.purchase_return_id not in return_ids
            or return_item.purchase_item_id not in item_ids
            or return_item.purchase_item.business_id != purchase.business_id
            or return_item.purchase_item.purchase_id != purchase.pk
        ):
            raise Http404
        quantity = _validated_decimal(
            return_item.quantity, "Existing purchase return quantity"
        )
        if quantity <= 0:
            raise Http404
        returned_quantities[return_item.purchase_item_id] = (
            returned_quantities.get(return_item.purchase_item_id, ZERO) + quantity
        )
    return returned_quantities


def _next_number(model, business, field, prefix):
    # Numbered workflows lock Business before any purchase/supplier/product
    # row, providing one global lock order for the count-based identifier.
    n = model.objects.for_business(business).count() + 1
    while model.objects.for_business(business).filter(**{field: f"{prefix}-{n:06d}"}).exists():
        n += 1
    return f"{prefix}-{n:06d}"


@transaction.atomic
def quick_add_product(
    *,
    business,
    form,
    user,
    membership=None,
    request=None,
):
    """Create a standard Product inside an authorized Purchase workflow."""

    context = require_purchases_write(
        business=business,
        user=user,
        membership=membership,
        request=request,
        lock_business=True,
    )
    business = context.business
    require_actor_access(
        user,
        business,
        "pos_core",
        permission_code="products.manage",
        action=AccessAction.WRITE,
        membership=context.membership,
        request=None,
    )
    product = form.save(commit=False)
    category = None
    if product.category_id is not None:
        category = Category.objects.select_for_update(no_key=True).filter(
            pk=product.category_id,
            business=business,
            is_active=True,
        ).first()
        if category is None:
            raise Http404
    unit = Unit.objects.select_for_update(no_key=True).filter(
        pk=product.unit_id,
        business=business,
        is_active=True,
    ).first()
    if unit is None:
        raise Http404
    tax_rate = None
    if product.tax_rate_id is not None:
        tax_rate = TaxRate.objects.select_for_update(no_key=True).filter(
            pk=product.tax_rate_id,
            business=business,
            is_active=True,
        ).first()
        if tax_rate is None:
            raise Http404
    _ensure_purchases_scope(
        context,
        business=business,
        tenant_objects=(category, unit, tax_rate),
    )
    if unit.is_meter:
        require_actor_access(
            user,
            business,
            "tailoring",
            permission_code="products.manage",
            action=AccessAction.WRITE,
            membership=context.membership,
            request=request,
        )
    subscription_services.check_limit(business, "products")
    product.business = business
    product.category = category
    product.brand = None
    product.unit = unit
    product.tax_rate = tax_rate
    product.preferred_supplier = None
    product.product_type = Product.Type.STANDARD
    if product.unit.is_meter:
        product.is_tailoring_item = True
        product.track_inventory = True
        product.allow_discount = False
    product.is_active = True
    product.save()
    audit.log(
        "product.saved",
        business=business,
        user=user,
        request=request,
        module="catalog",
        obj=product,
        description=f"Product '{product.name}' quick-added from a purchase.",
    )
    return product


@transaction.atomic
def create_purchase(*, business, supplier, branch, warehouse, rows, user,
                    purchase_date, due_date=None, supplier_invoice_number="",
                    discount=ZERO, shipping=ZERO, other=ZERO, notes="",
                    attachment=None, membership=None, request=None):
    """rows: [{product, variant, quantity, unit_cost}]"""
    context = require_purchases_write(
        business=business,
        user=user,
        membership=membership,
        request=request,
        lock_business=True,
    )
    business = context.business
    rows = list(rows)
    if not rows:
        raise ValidationError("Enter at least one purchase item.")
    supplier = Supplier.objects.select_for_update(no_key=True).filter(
        pk=getattr(supplier, "pk", None), business=business
    ).first()
    branch = Branch.objects.select_for_update(no_key=True).filter(
        pk=getattr(branch, "pk", None), business=business
    ).first()
    warehouse = Warehouse.objects.select_for_update(no_key=True).filter(
        pk=getattr(warehouse, "pk", None), business=business
    ).first()
    if supplier is None or branch is None or warehouse is None:
        raise Http404
    if not supplier.is_active or not branch.is_active or not warehouse.is_active:
        raise ValidationError(
            "Purchase supplier and location must be active and belong to this business."
        )
    requested_product_ids = {
        getattr(row.get("product"), "pk", None) for row in rows
    }
    if None in requested_product_ids:
        raise Http404
    product_ids = sorted(requested_product_ids)
    locked_products = {
        product.pk: product
        for product in (
            Product.objects.select_for_update(of=("self",), no_key=True)
            .select_related("unit")
            .filter(pk__in=product_ids, business=business)
            .order_by("pk")
        )
    }
    if set(locked_products) != set(product_ids):
        raise Http404
    variant_ids = {
        row["variant"].pk
        for row in rows
        if row.get("variant") is not None
    }
    locked_variants = {
        variant.pk: variant
        for variant in ProductVariant.objects.select_for_update(no_key=True).filter(
            pk__in=variant_ids,
            business=business,
        )
    }
    if set(locked_variants) != variant_ids:
        raise Http404
    _ensure_purchases_scope(
        context,
        business=business,
        branch=branch,
        warehouse=warehouse,
        tenant_objects=(supplier, *locked_products.values(), *locked_variants.values()),
    )

    purchase_date = _validated_date(purchase_date, "purchase date")
    due_date = _validated_date(due_date, "due date", allow_empty=True)

    discount_value = _validated_money(discount, "Discount")
    shipping_value = _validated_money(shipping, "Shipping cost")
    other_value = _validated_money(other, "Other charges")
    if discount_value < 0 or shipping_value < 0 or other_value < 0:
        raise ValidationError("Purchase adjustments cannot be negative.")

    validated_rows = []
    subtotal = ZERO
    for row in rows:
        product = locked_products.get(row["product"].pk)
        variant = (
            locked_variants.get(row["variant"].pk)
            if row.get("variant") is not None
            else None
        )
        if product is None:
            raise Http404
        if variant is not None and variant.product_id != product.id:
            raise Http404
        qty = _validated_decimal(row["quantity"], "Purchase quantity")
        cost = _validated_money(row.get("unit_cost", 0), "Purchase unit cost")
        if qty <= 0:
            raise ValidationError("Purchase quantities must be positive.")
        if cost < 0:
            raise ValidationError("Purchase unit costs cannot be negative.")
        if product.is_meter_tailoring and product.has_variants and variant is None:
            raise ValidationError(
                f"Select a variant/color for {product.name}."
            )
        line_total = _validated_money(qty * cost, "Purchase line total")
        subtotal += line_total
        validated_rows.append((product, variant, qty, cost, line_total))

    if any(product.is_tailoring_item for product, *_rest in validated_rows):
        require_actor_access(
            user,
            business,
            "tailoring",
            permission_code="purchases.manage",
            action=AccessAction.WRITE,
            membership=context.membership,
            request=request,
        )

    subtotal = _validated_money(subtotal, "Purchase subtotal")
    total = _validated_money(
        subtotal - discount_value + shipping_value + other_value,
        "Purchase total",
    )
    if total < 0:
        raise ValidationError("Purchase total cannot be negative.")

    purchase = Purchase.objects.create(
        business=business,
        purchase_number=_next_number(Purchase, business, "purchase_number", "PUR"),
        supplier=supplier,
        branch=branch,
        warehouse=warehouse,
        supplier_invoice_number=supplier_invoice_number[:60],
        purchase_date=purchase_date,
        due_date=due_date,
        subtotal=subtotal,
        discount_amount=discount_value,
        shipping_cost=shipping_value,
        other_charges=other_value,
        total=total,
        notes=notes,
        created_by=user,
        attachment=attachment,
    )
    for product, variant, qty, cost, line_total in validated_rows:
        PurchaseItem.objects.create(
            business=business, purchase=purchase,
            product=product, variant=variant,
            product_name=str(variant or product)[:240],
            quantity_ordered=qty, unit_cost=cost, line_total=line_total,
        )
    audit.log("purchase.created", business=business, user=user, request=request,
              module="purchases", obj=purchase,
              description=f"Purchase order {purchase.purchase_number} created "
                          f"for {supplier.name} ({purchase.total}).")
    return purchase


@transaction.atomic
def receive_purchase(
    *, purchase, quantities, user, membership=None, request=None
):
    """quantities: {purchase_item_id: qty_to_receive_now}. Partial receiving
    supported; stock and supplier payable increase by the received value."""
    purchase, context = _authorized_purchase(
        purchase=purchase,
        user=user,
        membership=membership,
        request=request,
        lock_business=True,
    )
    if purchase.status == Purchase.Status.CANCELLED:
        raise ValidationError("Cancelled purchases cannot be received.")
    received_value = ZERO
    any_received = False
    locked_items = _locked_purchase_items(purchase)
    locked_item_ids = {item.pk for item in locked_items}
    requested_item_ids = _positive_requested_ids(quantities)
    if not requested_item_ids.issubset(locked_item_ids):
        raise Http404
    _ensure_purchases_scope(
        context,
        business=purchase.business,
        branch=purchase.branch,
        warehouse=purchase.warehouse,
        tenant_objects=(
            purchase.supplier,
            *(item.product for item in locked_items),
            *(item.variant for item in locked_items if item.variant is not None),
        ),
    )
    if any(
        item.pk in requested_item_ids and item.product.is_tailoring_item
        for item in locked_items
    ):
        require_actor_access(
            user,
            purchase.business,
            "tailoring",
            permission_code="purchases.manage",
            action=AccessAction.WRITE,
            membership=context.membership,
            request=request,
        )
    for item in locked_items:
        qty = _validated_decimal(
            quantities.get(item.pk, 0), "Receive quantity"
        )
        if qty <= 0:
            continue
        if qty > item.quantity_pending:
            raise ValidationError(
                f"Cannot receive {qty} of {item.product_name}; only "
                f"{item.quantity_pending} pending."
            )
        if (
            item.product.is_meter_tailoring
            and item.product.has_variants
            and item.variant is None
        ):
            raise ValidationError(
                f"Select a variant/color for {item.product.name} before receipt."
            )
        if item.product.is_stocked:
            inventory.record_movement(
                business=purchase.business, warehouse=purchase.warehouse,
                product=item.product, variant=item.variant,
                movement_type="purchase", quantity=qty, unit_cost=item.unit_cost,
                reference_type="Purchase", reference_id=purchase.purchase_number,
                user=user,
            )
        # Keep latest purchase price on the product/variant
        target = item.variant or item.product
        if target.purchase_price != item.unit_cost:
            target.purchase_price = item.unit_cost
            target.save(update_fields=["purchase_price"])
        item.quantity_received = item.quantity_received + qty
        item.save(update_fields=["quantity_received"])
        received_value += _validated_money(
            qty * item.unit_cost, "Received value"
        )
        any_received = True
    if not any_received:
        raise ValidationError("Enter at least one quantity to receive.")

    # Supplier payable rises with the received goods value (plus a share of
    # charges when fully received — kept simple: charges added on completion).
    pending = purchase.items.aggregate(
        o=Sum("quantity_ordered"), r=Sum("quantity_received"))
    fully = pending["r"] >= pending["o"]
    extra = ZERO
    if fully:
        extra = (purchase.shipping_cost + purchase.other_charges
                 - purchase.discount_amount)
    Supplier.objects.filter(
        pk=purchase.supplier_id,
        business=purchase.business,
    ).update(
        balance=F("balance") + received_value + extra
    )
    purchase.status = Purchase.Status.RECEIVED if fully else Purchase.Status.PARTIAL
    purchase.save(update_fields=["status", "updated_at"])
    audit.log("purchase.received", business=purchase.business, user=user,
              request=request, module="purchases", obj=purchase,
              description=f"Goods received on {purchase.purchase_number} "
                          f"(value {received_value}).")
    return purchase


IMMEDIATE_METHOD_KINDS = {
    SupplierPayment.Method.CASH: PaymentMethod.Kind.CASH,
    SupplierPayment.Method.BANK: PaymentMethod.Kind.BANK,
    SupplierPayment.Method.CARD: PaymentMethod.Kind.CARD,
}


def with_pending_cheques(queryset):
    """Annotate purchases with the pending amount used by model totals."""
    return queryset.annotate(
        _cheques_pending=Coalesce(
            Sum(
                "payments__amount",
                filter=Q(
                    payments__business_id=F("business_id"),
                    payments__method=SupplierPayment.Method.CHEQUE,
                    payments__cheque_status=SupplierPayment.ChequeStatus.PENDING,
                ),
            ),
            Value(ZERO),
            output_field=DecimalField(max_digits=14, decimal_places=3),
        )
    )


def _normalise_payment_row(*, business, row):
    method = str(row.get("method", "")).strip()
    allowed = {choice for choice, _label in SupplierPayment.Method.choices}
    if method not in allowed:
        raise ValidationError("Select Cash, Bank Transfer, Card or Cheque.")

    amount = _validated_money(row.get("amount"), "Payment amount")
    if amount <= 0:
        raise ValidationError("Payment amount must be greater than zero.")

    normalised = {
        "method": method,
        "amount": amount,
        "reference": str(row.get("reference", "")).strip()[:120],
        "notes": str(row.get("notes", "")).strip()[:300],
        "payment_method": None,
        "cheque_number": "",
        "bank_name": "",
        "cheque_issue_date": None,
        "due_date": None,
        "cheque_status": "",
    }
    if method == SupplierPayment.Method.CHEQUE:
        cheque_number = str(row.get("cheque_number", "")).strip()
        bank_name = str(row.get("bank_name", "")).strip()
        raw_issue_date = row.get("cheque_issue_date")
        raw_due_date = row.get("due_date")
        try:
            cheque_issue_date = (
                raw_issue_date
                if isinstance(raw_issue_date, date)
                else parse_date(str(raw_issue_date or ""))
            )
        except ValueError:
            cheque_issue_date = None
        try:
            due_date = raw_due_date if isinstance(raw_due_date, date) else parse_date(
                str(raw_due_date or "")
            )
        except ValueError:
            due_date = None
        if not cheque_number:
            raise ValidationError("Cheque Number is required.")
        if not bank_name:
            raise ValidationError("Bank Name is required.")
        if cheque_issue_date is None:
            raise ValidationError("Cheque Issue Date is required.")
        if due_date is None:
            raise ValidationError("Cheque Payment Date is required.")
        if due_date <= business_localdate(business):
            raise ValidationError("Cheque Payment Date must be in the future.")
        normalised.update({
            "cheque_number": cheque_number[:100],
            "bank_name": bank_name[:120],
            "cheque_issue_date": cheque_issue_date,
            "due_date": due_date,
            "cheque_status": SupplierPayment.ChequeStatus.PENDING,
        })
        return normalised

    payment_method = row.get("payment_method")
    expected_kind = IMMEDIATE_METHOD_KINDS[method]
    if payment_method is not None:
        payment_method_pk = getattr(payment_method, "pk", None)
        canonical_business_id = (
            PaymentMethod.objects.filter(pk=payment_method_pk)
            .values_list("business_id", flat=True)
            .first()
        )
        if canonical_business_id != business.pk:
            raise Http404
        payment_method = (
            PaymentMethod.objects.for_business(business)
            .filter(
                pk=payment_method_pk,
                kind=expected_kind,
                is_active=True,
            )
            .first()
        )
        if payment_method is None:
            raise ValidationError("Invalid payment method for this business.")
    else:
        payment_method = (
            PaymentMethod.objects.for_business(business)
            .filter(kind=expected_kind, is_active=True)
            .order_by("id")
            .first()
        )
    if payment_method is None:
        raise ValidationError(
            f"{dict(SupplierPayment.Method.choices)[method]} is not available."
        )
    normalised["payment_method"] = payment_method
    return normalised


@transaction.atomic
def record_purchase_payments(
    *, purchase, rows, user, membership=None, request=None
):
    """Record one or more immediate or post-dated purchase payments safely."""
    locked_purchase, context = _authorized_purchase(
        purchase=purchase,
        user=user,
        membership=membership,
        request=request,
        lock_business=True,
    )
    if locked_purchase.status == Purchase.Status.CANCELLED:
        raise ValidationError("Payments cannot be added to a cancelled purchase.")
    _locked_purchase_payments(locked_purchase)
    locked_supplier = Supplier.objects.select_for_update().filter(
        pk=locked_purchase.supplier_id, business=locked_purchase.business,
    ).first()
    if locked_supplier is None:
        raise Http404
    _ensure_purchases_scope(
        context,
        business=locked_purchase.business,
        branch=locked_purchase.branch,
        warehouse=locked_purchase.warehouse,
        tenant_objects=(locked_supplier,),
    )

    normalised_rows = [
        _normalise_payment_row(business=locked_purchase.business, row=row)
        for row in rows
    ]
    if not normalised_rows:
        raise ValidationError("Add at least one payment row.")

    pending = (
        SupplierPayment.objects.for_business(locked_purchase.business)
        .filter(
            purchase=locked_purchase,
            method=SupplierPayment.Method.CHEQUE,
            cheque_status=SupplierPayment.ChequeStatus.PENDING,
        )
        .aggregate(total=Sum("amount"))["total"]
        or ZERO
    )
    new_allocation = sum((row["amount"] for row in normalised_rows), ZERO)
    if locked_purchase.amount_paid + pending + new_allocation > locked_purchase.total:
        raise ValidationError(
            "Paid plus Pending Cheques cannot exceed Purchase Total."
        )

    created = []
    settled_total = ZERO
    for row in normalised_rows:
        payment = SupplierPayment.objects.create(
            business=locked_purchase.business,
            payment_number=_next_number(
                SupplierPayment, locked_purchase.business,
                "payment_number", "SPY",
            ),
            supplier=locked_purchase.supplier,
            purchase=locked_purchase,
            paid_by=user,
            **row,
        )
        created.append(payment)
        if row["method"] != SupplierPayment.Method.CHEQUE:
            settled_total += row["amount"]
        audit.log(
            "purchase.payment_recorded",
            business=locked_purchase.business,
            user=user,
            request=request,
            module="purchases",
            obj=payment,
            description=(
                f"{payment.method_label} {payment.amount} recorded on "
                f"{locked_purchase.purchase_number}."
            ),
        )

    if settled_total:
        Purchase.objects.filter(
            pk=locked_purchase.pk,
            business=locked_purchase.business,
        ).update(
            amount_paid=F("amount_paid") + settled_total,
        )
        Supplier.objects.filter(
            pk=locked_purchase.supplier_id,
            business=locked_purchase.business,
        ).update(
            balance=F("balance") - settled_total,
        )
    return created


def pay_purchase(*, purchase, amount, method, user, reference="", notes="",
                 membership=None, request=None):
    """Backward-compatible entry point for an immediate supplier payment."""
    kind_to_method = {value: key for key, value in IMMEDIATE_METHOD_KINDS.items()}
    payment_kind = kind_to_method.get(getattr(method, "kind", None))
    if payment_kind is None:
        raise ValidationError("Select Cash, Bank Transfer or Card.")
    return record_purchase_payments(
        purchase=purchase,
        rows=[{
            "method": payment_kind,
            "payment_method": method,
            "amount": amount,
            "reference": reference,
            "notes": notes,
        }],
        user=user,
        membership=membership,
        request=request,
    )[0]


@transaction.atomic
def update_cheque_status(
    *, payment, status, user, membership=None, request=None
):
    candidate = (
        SupplierPayment.objects.select_related("business")
        .filter(pk=getattr(payment, "pk", None))
        .first()
    )
    if candidate is None:
        raise Http404
    business = _business_for_canonical_object(
        candidate,
        user=user,
        membership=membership,
        request=request,
    )
    context = require_purchases_write(
        business=business,
        user=user,
        membership=membership,
        request=request,
    )
    business = context.business
    if not candidate.is_cheque or candidate.purchase_id is None:
        raise ValidationError("Only purchase cheques have a cheque status.")

    purchase = (
        Purchase.objects.select_for_update(of=("self",))
        .select_related("supplier", "branch", "warehouse")
        .filter(pk=candidate.purchase_id, business=business)
        .first()
    )
    if purchase is None:
        raise Http404
    locked_payment = (
        SupplierPayment.objects.select_for_update(of=("self",))
        .select_related("supplier")
        .filter(
            pk=candidate.pk,
            business=business,
            purchase=purchase,
        )
        .first()
    )
    if locked_payment is None:
        raise Http404
    supplier = Supplier.objects.select_for_update().filter(
        pk=locked_payment.supplier_id, business=business,
    ).first()
    if (
        supplier is None
        or purchase.supplier_id != supplier.pk
        or locked_payment.supplier_id != purchase.supplier_id
    ):
        raise Http404
    _ensure_purchases_scope(
        context,
        business=business,
        branch=purchase.branch,
        warehouse=purchase.warehouse,
        tenant_objects=(purchase.supplier, supplier),
    )
    allowed = {choice for choice, _label in SupplierPayment.ChequeStatus.choices}
    if status not in allowed:
        raise ValidationError("Invalid cheque status.")
    old_status = locked_payment.cheque_status
    if status == old_status:
        return locked_payment
    if old_status != SupplierPayment.ChequeStatus.PENDING:
        raise ValidationError("This cheque status can no longer be changed.")
    if status == SupplierPayment.ChequeStatus.PENDING:
        raise ValidationError("The cheque is already Pending.")
    if status == SupplierPayment.ChequeStatus.CLEARED:
        Purchase.objects.filter(pk=purchase.pk, business=business).update(
            amount_paid=F("amount_paid") + locked_payment.amount,
        )
        Supplier.objects.filter(
            pk=locked_payment.supplier_id,
            business=business,
        ).update(
            balance=F("balance") - locked_payment.amount,
        )
        locked_payment.cleared_at = timezone.now()

    locked_payment.cheque_status = status
    locked_payment.save(update_fields=["cheque_status", "cleared_at", "updated_at"])
    audit.log(
        "purchase.cheque_status_updated",
        business=locked_payment.business,
        user=user,
        request=request,
        module="purchases",
        obj=locked_payment,
        description=(
            f"Cheque {locked_payment.cheque_number} changed from "
            f"{old_status} to {status} on {purchase.purchase_number}."
        ),
        old_values={"cheque_status": old_status},
        new_values={"cheque_status": status},
    )
    return locked_payment


@transaction.atomic
def return_purchase(
    *, purchase, quantities, user, reason="", membership=None, request=None
):
    """quantities: {purchase_item_id: qty_to_return}. Reduces stock and
    supplier payable."""
    purchase, context = _authorized_purchase(
        purchase=purchase,
        user=user,
        membership=membership,
        request=request,
        lock_business=True,
    )
    items = []
    total = ZERO
    locked_items = _locked_purchase_items(purchase)
    locked_item_ids = {item.pk for item in locked_items}
    if not _positive_requested_ids(quantities).issubset(locked_item_ids):
        raise Http404
    _ensure_purchases_scope(
        context,
        business=purchase.business,
        branch=purchase.branch,
        warehouse=purchase.warehouse,
        tenant_objects=(
            purchase.supplier,
            *(item.product for item in locked_items),
            *(item.variant for item in locked_items if item.variant is not None),
        ),
    )
    returned_quantities = _locked_returned_quantities(purchase, locked_items)
    for item in locked_items:
        qty = _validated_decimal(
            quantities.get(item.pk, 0), "Return quantity"
        )
        if qty <= 0:
            continue
        already_returned = returned_quantities.get(item.pk, ZERO)
        returnable = item.quantity_received - already_returned
        if qty > returnable:
            raise ValidationError(
                f"Cannot return {qty} of {item.product_name}; only "
                f"{returnable} were received and not yet returned."
            )
        items.append((item, qty))
        total += _validated_money(qty * item.unit_cost, "Purchase return value")
    if not items:
        raise ValidationError("Enter at least one quantity to return.")
    if any(item.product.is_tailoring_item for item, _qty in items):
        require_actor_access(
            user,
            purchase.business,
            "tailoring",
            permission_code="purchases.manage",
            action=AccessAction.WRITE,
            membership=context.membership,
            request=request,
        )

    purchase_return = PurchaseReturn.objects.create(
        business=purchase.business,
        return_number=_next_number(PurchaseReturn, purchase.business,
                                   "return_number", "PRT"),
        purchase=purchase, supplier=purchase.supplier,
        warehouse=purchase.warehouse, reason=reason[:255], total=total,
        processed_by=user,
    )
    for item, qty in items:
        PurchaseReturnItem.objects.create(
            business=purchase.business, purchase_return=purchase_return,
            purchase_item=item, quantity=qty, unit_cost=item.unit_cost,
            line_total=_validated_money(
                qty * item.unit_cost, "Purchase return line total"
            ),
        )
        if item.product.is_stocked:
            inventory.record_movement(
                business=purchase.business, warehouse=purchase.warehouse,
                product=item.product, variant=item.variant,
                movement_type="purchase_return", quantity=-qty,
                unit_cost=item.unit_cost, reference_type="PurchaseReturn",
                reference_id=purchase_return.return_number, user=user,
            )
    Supplier.objects.filter(
        pk=purchase.supplier_id,
        business=purchase.business,
    ).update(
        balance=F("balance") - total)
    audit.log("purchase.returned", business=purchase.business, user=user,
              request=request, module="purchases", obj=purchase_return,
              description=f"Purchase return {purchase_return.return_number} "
                          f"({total}) against {purchase.purchase_number}.")
    return purchase_return


@transaction.atomic
def cancel_purchase(*, purchase, user, membership=None, request=None):
    """Cancel an untouched purchase while serialized with receive/payment paths."""
    locked_purchase, _context = _authorized_purchase(
        purchase=purchase,
        user=user,
        membership=membership,
        request=request,
    )
    if locked_purchase.status == Purchase.Status.CANCELLED:
        raise ValidationError("Purchase is already cancelled.")
    locked_items = _locked_purchase_items(locked_purchase)
    _locked_returned_quantities(locked_purchase, locked_items)
    locked_payments = _locked_purchase_payments(locked_purchase)
    if any(item.quantity_received > 0 for item in locked_items):
        raise ValidationError(
            "Purchases with received goods cannot be cancelled — use a "
            "purchase return instead."
        )
    has_pending_cheque = any(
        payment.method == SupplierPayment.Method.CHEQUE
        and payment.cheque_status == SupplierPayment.ChequeStatus.PENDING
        for payment in locked_payments
    )
    if locked_purchase.amount_paid > 0 or has_pending_cheque:
        raise ValidationError(
            "Purchases with Paid or Pending Cheques cannot be cancelled."
        )
    locked_purchase.status = Purchase.Status.CANCELLED
    locked_purchase.save(update_fields=["status", "updated_at"])
    audit.log(
        "purchase.cancelled",
        business=locked_purchase.business,
        user=user,
        request=request,
        module="purchases",
        obj=locked_purchase,
        description=f"Purchase {locked_purchase.purchase_number} cancelled.",
    )
    return locked_purchase
