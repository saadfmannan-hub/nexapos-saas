"""Purchase lifecycle: ordering, receiving, payment, returns.

Stock only increases when goods are received; supplier payable only
increases when goods are received (the payable follows the received
value, with the full total recorded on the purchase itself).
"""
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.audit import services as audit
from apps.core.date_ranges import business_localdate
from apps.core.money import D, money
from apps.inventory import services as inventory
from apps.sales.models import PaymentMethod
from apps.suppliers.models import Supplier, SupplierPayment

from .models import Purchase, PurchaseItem, PurchaseReturn, PurchaseReturnItem

ZERO = Decimal("0")


def _next_number(model, business, field, prefix):
    n = model.objects.for_business(business).count() + 1
    while model.objects.for_business(business).filter(**{field: f"{prefix}-{n:06d}"}).exists():
        n += 1
    return f"{prefix}-{n:06d}"


@transaction.atomic
def create_purchase(*, business, supplier, branch, warehouse, rows, user,
                    purchase_date, due_date=None, supplier_invoice_number="",
                    discount=ZERO, shipping=ZERO, other=ZERO, notes="",
                    attachment=None, request=None):
    """rows: [{product, variant, quantity, unit_cost}]"""
    purchase = Purchase.objects.create(
        business=business,
        purchase_number=_next_number(Purchase, business, "purchase_number", "PUR"),
        supplier=supplier, branch=branch, warehouse=warehouse,
        supplier_invoice_number=supplier_invoice_number[:60],
        purchase_date=purchase_date, due_date=due_date,
        discount_amount=money(discount), shipping_cost=money(shipping),
        other_charges=money(other), notes=notes, created_by=user,
        attachment=attachment,
    )
    subtotal = ZERO
    for row in rows:
        qty, cost = D(row["quantity"]), money(row.get("unit_cost", 0))
        if qty <= 0:
            raise ValidationError("Purchase quantities must be positive.")
        line_total = money(qty * cost)
        subtotal += line_total
        PurchaseItem.objects.create(
            business=business, purchase=purchase,
            product=row["product"], variant=row["variant"],
            product_name=str(row["variant"] or row["product"])[:240],
            quantity_ordered=qty, unit_cost=cost, line_total=line_total,
        )
    purchase.subtotal = money(subtotal)
    purchase.total = money(subtotal - purchase.discount_amount +
                           purchase.shipping_cost + purchase.other_charges)
    if purchase.total < 0:
        raise ValidationError("Purchase total cannot be negative.")
    purchase.save()
    audit.log("purchase.created", business=business, user=user, request=request,
              module="purchases", obj=purchase,
              description=f"Purchase order {purchase.purchase_number} created "
                          f"for {supplier.name} ({purchase.total}).")
    return purchase


@transaction.atomic
def receive_purchase(*, purchase, quantities, user, request=None):
    """quantities: {purchase_item_id: qty_to_receive_now}. Partial receiving
    supported; stock and supplier payable increase by the received value."""
    if purchase.status == Purchase.Status.CANCELLED:
        raise ValidationError("Cancelled purchases cannot be received.")
    received_value = ZERO
    any_received = False
    for item in purchase.items.select_related("product", "variant"):
        qty = D(quantities.get(item.pk, 0))
        if qty <= 0:
            continue
        if qty > item.quantity_pending:
            raise ValidationError(
                f"Cannot receive {qty} of {item.product_name}; only "
                f"{item.quantity_pending} pending."
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
        received_value += money(qty * item.unit_cost)
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
    Supplier.objects.filter(pk=purchase.supplier_id).update(
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


def _normalise_payment_row(*, business, row):
    method = str(row.get("method", "")).strip()
    allowed = {choice for choice, _label in SupplierPayment.Method.choices}
    if method not in allowed:
        raise ValidationError("Select Cash, Bank Transfer, Card or Cheque.")

    amount = money(row.get("amount"))
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
        "due_date": None,
        "cheque_status": "",
    }
    if method == SupplierPayment.Method.CHEQUE:
        cheque_number = str(row.get("cheque_number", "")).strip()
        bank_name = str(row.get("bank_name", "")).strip()
        raw_due_date = row.get("due_date")
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
        if due_date is None:
            raise ValidationError("Due Date is required for a cheque.")
        if due_date <= business_localdate(business):
            raise ValidationError("Cheque Due Date must be in the future.")
        normalised.update({
            "cheque_number": cheque_number[:100],
            "bank_name": bank_name[:120],
            "due_date": due_date,
            "cheque_status": SupplierPayment.ChequeStatus.PENDING,
        })
        return normalised

    payment_method = row.get("payment_method")
    expected_kind = IMMEDIATE_METHOD_KINDS[method]
    if payment_method is not None:
        if (
            payment_method.business_id != business.pk
            or payment_method.kind != expected_kind
            or not payment_method.is_active
        ):
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
def record_purchase_payments(*, purchase, rows, user, request=None):
    """Record one or more immediate or post-dated purchase payments safely."""
    locked_purchase = (
        Purchase.objects.select_for_update()
        .select_related("supplier")
        .get(pk=purchase.pk, business=purchase.business)
    )
    if locked_purchase.status == Purchase.Status.CANCELLED:
        raise ValidationError("Payments cannot be added to a cancelled purchase.")
    Supplier.objects.select_for_update().get(
        pk=locked_purchase.supplier_id, business=locked_purchase.business,
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
        Purchase.objects.filter(pk=locked_purchase.pk).update(
            amount_paid=F("amount_paid") + settled_total,
        )
        Supplier.objects.filter(pk=locked_purchase.supplier_id).update(
            balance=F("balance") - settled_total,
        )
    return created


def pay_purchase(*, purchase, amount, method, user, reference="", notes="",
                 request=None):
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
        request=request,
    )[0]


@transaction.atomic
def update_cheque_status(*, payment, status, user, request=None):
    locked_payment = (
        SupplierPayment.objects.select_for_update()
        .select_related("purchase", "supplier")
        .get(pk=payment.pk, business=payment.business)
    )
    if not locked_payment.is_cheque or locked_payment.purchase_id is None:
        raise ValidationError("Only purchase cheques have a cheque status.")

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

    purchase = Purchase.objects.select_for_update().get(
        pk=locked_payment.purchase_id, business=locked_payment.business,
    )
    Supplier.objects.select_for_update().get(
        pk=locked_payment.supplier_id, business=locked_payment.business,
    )
    if status == SupplierPayment.ChequeStatus.CLEARED:
        Purchase.objects.filter(pk=purchase.pk).update(
            amount_paid=F("amount_paid") + locked_payment.amount,
        )
        Supplier.objects.filter(pk=locked_payment.supplier_id).update(
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
def return_purchase(*, purchase, quantities, user, reason="", request=None):
    """quantities: {purchase_item_id: qty_to_return}. Reduces stock and
    supplier payable."""
    items = []
    total = ZERO
    for item in purchase.items.select_related("product", "variant"):
        qty = D(quantities.get(item.pk, 0))
        if qty <= 0:
            continue
        already_returned = (
            PurchaseReturnItem.objects.for_business(purchase.business)
            .filter(purchase_item=item)
            .aggregate(t=Sum("quantity"))["t"] or ZERO
        )
        returnable = item.quantity_received - already_returned
        if qty > returnable:
            raise ValidationError(
                f"Cannot return {qty} of {item.product_name}; only "
                f"{returnable} were received and not yet returned."
            )
        items.append((item, qty))
        total += money(qty * item.unit_cost)
    if not items:
        raise ValidationError("Enter at least one quantity to return.")

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
            line_total=money(qty * item.unit_cost),
        )
        if item.product.is_stocked:
            inventory.record_movement(
                business=purchase.business, warehouse=purchase.warehouse,
                product=item.product, variant=item.variant,
                movement_type="purchase_return", quantity=-qty,
                unit_cost=item.unit_cost, reference_type="PurchaseReturn",
                reference_id=purchase_return.return_number, user=user,
            )
    Supplier.objects.filter(pk=purchase.supplier_id).update(
        balance=F("balance") - total)
    audit.log("purchase.returned", business=purchase.business, user=user,
              request=request, module="purchases", obj=purchase_return,
              description=f"Purchase return {purchase_return.return_number} "
                          f"({total}) against {purchase.purchase_number}.")
    return purchase_return
