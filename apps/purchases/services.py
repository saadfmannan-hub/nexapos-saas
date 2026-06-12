"""Purchase lifecycle: ordering, receiving, payment, returns.

Stock only increases when goods are received; supplier payable only
increases when goods are received (the payable follows the received
value, with the full total recorded on the purchase itself).
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Sum

from apps.audit import services as audit
from apps.core.money import D, money
from apps.inventory import services as inventory
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


@transaction.atomic
def pay_purchase(*, purchase, amount, method, user, reference="", notes="",
                 request=None):
    amount = money(amount)
    if amount <= 0:
        raise ValidationError("Payment must be positive.")
    if amount > purchase.outstanding:
        raise ValidationError("Payment exceeds the outstanding amount.")
    payment = SupplierPayment.objects.create(
        business=purchase.business,
        payment_number=_next_number(SupplierPayment, purchase.business,
                                    "payment_number", "SPY"),
        supplier=purchase.supplier, purchase=purchase, amount=amount,
        payment_method=method, reference=reference[:120], notes=notes[:300],
        paid_by=user,
    )
    Purchase.objects.filter(pk=purchase.pk).update(
        amount_paid=F("amount_paid") + amount)
    Supplier.objects.filter(pk=purchase.supplier_id).update(
        balance=F("balance") - amount)
    audit.log("purchase.paid", business=purchase.business, user=user,
              request=request, module="purchases", obj=payment,
              description=f"Paid {amount} on {purchase.purchase_number}.")
    return payment


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
