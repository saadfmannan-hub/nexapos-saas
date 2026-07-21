"""Authoritative net financial calculations for sales and returns.

Sale and payment rows remain immutable accounting inputs.  Every reporting
consumer should derive net figures here so returns are applied consistently
without changing sale, payment, refund, or inventory posting.
"""
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import (
    Case,
    DecimalField,
    ExpressionWrapper,
    F,
    FloatField,
    Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Cast, Round

from apps.core.money import money, qty

ZERO = Decimal("0")

CASH = "cash"
CARD = "card"
BANK = "bank"
ONLINE = "online"
CUSTOMER_CREDIT = "customer_credit"
CUSTOMER_ACCOUNT = "customer_account"
STORE_CREDIT = "store_credit"
OTHER = "other"

ALL_TENDER_KINDS = (
    CASH,
    CARD,
    BANK,
    ONLINE,
    CUSTOMER_CREDIT,
    STORE_CREDIT,
    OTHER,
)
REAL_PAYMENT_KINDS = (CASH, CARD, BANK)
INCOME_PAYMENT_KINDS = REAL_PAYMENT_KINDS + (ONLINE, OTHER)

# SaleReturn.refund_method is the persisted, user-selected allocation.  Do not
# infer or redistribute refunds across the original tenders.
REFUND_TO_TENDER_KIND = {
    CASH: CASH,
    CARD: CARD,
    BANK: BANK,
    CUSTOMER_ACCOUNT: CUSTOMER_CREDIT,
    STORE_CREDIT: STORE_CREDIT,
}

# These refund types reverse value that Sale.amount_paid treats as settled.
# Customer-account refunds reduce the credit/receivable instead.
SETTLED_REFUND_METHODS = (CASH, CARD, BANK, STORE_CREDIT)


def _blank_amounts():
    return {kind: ZERO for kind in ALL_TENDER_KINDS}


@dataclass(frozen=True)
class TenderSummary:
    gross: dict
    refunds: dict
    net: dict

    def amount(self, kind):
        return self.net.get(kind, ZERO)

    def refunded(self, kind):
        return self.refunds.get(kind, ZERO)

    def received(self, kinds=REAL_PAYMENT_KINDS):
        return money(sum((self.amount(kind) for kind in kinds), ZERO))


def tender_summary_from_totals(gross, refunds):
    """Build a tender summary from already-grouped kind/amount mappings."""
    gross_values = _blank_amounts()
    refund_values = _blank_amounts()
    for kind, value in gross.items():
        if kind in gross_values:
            gross_values[kind] += value or ZERO
    for kind, value in refunds.items():
        mapped_kind = REFUND_TO_TENDER_KIND.get(kind)
        if mapped_kind is not None:
            refund_values[mapped_kind] += value or ZERO
    gross_values = {
        kind: money(value) for kind, value in gross_values.items()
    }
    refund_values = {
        kind: money(value) for kind, value in refund_values.items()
    }
    net_values = {
        kind: money(gross_values[kind] - refund_values[kind])
        for kind in ALL_TENDER_KINDS
    }
    return TenderSummary(gross_values, refund_values, net_values)


def tender_summary_from_records(payments, returns):
    """Net tender amounts from already-loaded model instances."""
    gross = _blank_amounts()
    refunds = {}
    for payment in payments:
        gross[payment.method.kind] = (
            gross.get(payment.method.kind, ZERO) + payment.amount
        )
    for sale_return in returns:
        method = sale_return.refund_method
        refunds[method] = refunds.get(method, ZERO) + sale_return.refund_amount
    return tender_summary_from_totals(gross, refunds)


def tender_summary_from_querysets(payments, returns):
    """Net tender amounts from tenant/scoping-filtered querysets."""
    gross = {
        row["method__kind"]: row["total"] or ZERO
        for row in payments.values("method__kind").annotate(total=Sum("amount"))
    }
    refunds = {
        row["refund_method"]: row["total"] or ZERO
        for row in returns.values("refund_method").annotate(
            total=Sum("refund_amount")
        )
    }
    return tender_summary_from_totals(gross, refunds)


@dataclass(frozen=True)
class SaleFinancialSummary:
    gross_sales: Decimal
    returned: Decimal
    net_sales: Decimal
    net_paid: Decimal
    receivable: Decimal
    tenders: TenderSummary


def financial_summary_for_sale(sale):
    """Return one invoice's internally consistent net financial state."""
    returns = list(sale.returns.all())
    payments = list(sale.payments.all())
    tenders = tender_summary_from_records(payments, returns)
    if sale.status == "voided":
        return SaleFinancialSummary(
            gross_sales=money(sale.total),
            returned=ZERO,
            net_sales=ZERO,
            net_paid=ZERO,
            receivable=ZERO,
            tenders=TenderSummary(
                gross=tenders.gross,
                refunds=tenders.refunds,
                net=_blank_amounts(),
            ),
        )
    returned = money(sum((item.refund_amount for item in returns), ZERO))
    net_sales = money(sale.total - returned)
    settled_refunds = sum(
        (
            item.refund_amount
            for item in returns
            if item.refund_method in SETTLED_REFUND_METHODS
        ),
        ZERO,
    )
    net_paid = money(sale.amount_paid - settled_refunds)
    receivable = money(max(net_sales - net_paid, ZERO))
    return SaleFinancialSummary(
        gross_sales=money(sale.total),
        returned=returned,
        net_sales=net_sales,
        net_paid=net_paid,
        receivable=receivable,
        tenders=tenders,
    )


@dataclass(frozen=True)
class SalesActivitySummary:
    gross_sales: Decimal
    returns: Decimal
    net_sales: Decimal
    gross_paid: Decimal
    settled_refunds: Decimal
    net_paid: Decimal


def sales_activity_summary(sales, returns=None):
    """Aggregate sales less returns for already-scoped querysets.

    When ``returns`` is omitted, all returns against the selected invoice
    cohort are used.  Supplying a date-filtered return queryset preserves
    transaction-period reporting (for example dashboard daily activity).
    """
    from .models import SaleReturn

    valid_sales = sales.exclude(status__in=("draft", "voided"))
    sale_totals = valid_sales.aggregate(
        total=Sum("total"),
        paid=Sum("amount_paid"),
    )
    gross_sales = sale_totals["total"] or ZERO
    gross_paid = sale_totals["paid"] or ZERO
    if returns is None:
        returns = SaleReturn.objects.filter(sale__in=valid_sales)
    return_totals = returns.aggregate(
        total=Sum("refund_amount"),
        settled=Sum(
            "refund_amount",
            filter=Q(refund_method__in=SETTLED_REFUND_METHODS),
        ),
    )
    returned = return_totals["total"] or ZERO
    settled_refunds = return_totals["settled"] or ZERO
    gross_sales = money(gross_sales)
    gross_paid = money(gross_paid)
    returned = money(returned)
    settled_refunds = money(settled_refunds)
    return SalesActivitySummary(
        gross_sales=gross_sales,
        returns=returned,
        net_sales=money(gross_sales - returned),
        gross_paid=gross_paid,
        settled_refunds=settled_refunds,
        net_paid=money(gross_paid - settled_refunds),
    )


@dataclass(frozen=True)
class ItemFinancialAmounts:
    quantity: Decimal = ZERO
    revenue: Decimal = ZERO
    tax: Decimal = ZERO
    discount: Decimal = ZERO
    cost: Decimal = ZERO
    profit: Decimal = ZERO

    @property
    def revenue_excluding_tax(self):
        return money(self.revenue - self.tax)


@dataclass(frozen=True)
class ItemFinancialActivity:
    booked: ItemFinancialAmounts
    returned: ItemFinancialAmounts
    net: ItemFinancialAmounts


def _item_amounts(item, quantity, invoice_discount_share=ZERO):
    if not item.quantity:
        return ItemFinancialAmounts()
    ratio = quantity / item.quantity
    revenue = money(item.line_total * ratio)
    tax = money(item.tax_amount * ratio)
    cost = money(item.unit_cost * item.inventory_quantity * ratio)
    return ItemFinancialAmounts(
        quantity=qty(quantity),
        revenue=revenue,
        tax=tax,
        discount=money(
            (item.discount_amount + invoice_discount_share) * ratio
        ),
        cost=cost,
        profit=money(revenue - tax - cost),
    )


def _returned_item_amounts(return_item, invoice_discount_share=ZERO):
    item = return_item.sale_item
    if not item.quantity:
        return ItemFinancialAmounts()
    ratio = return_item.quantity / item.quantity
    revenue = money(return_item.line_refund)
    tax = money(item.tax_amount * ratio)
    cost = money(item.unit_cost * item.inventory_quantity * ratio)
    return ItemFinancialAmounts(
        quantity=qty(return_item.quantity),
        revenue=revenue,
        tax=tax,
        discount=money(
            (item.discount_amount + invoice_discount_share) * ratio
        ),
        cost=cost,
        profit=money(revenue - tax - cost),
    )


def _sum_item_amounts(values):
    totals = {
        "quantity": ZERO,
        "revenue": ZERO,
        "tax": ZERO,
        "discount": ZERO,
        "cost": ZERO,
        "profit": ZERO,
    }
    for value in values:
        for field in totals:
            totals[field] += getattr(value, field)
    return ItemFinancialAmounts(
        quantity=qty(totals["quantity"]),
        revenue=money(totals["revenue"]),
        tax=money(totals["tax"]),
        discount=money(totals["discount"]),
        cost=money(totals["cost"]),
        profit=money(totals["profit"]),
    )


def _subtract_item_amounts(booked, returned):
    return ItemFinancialAmounts(
        quantity=qty(booked.quantity - returned.quantity),
        revenue=money(booked.revenue - returned.revenue),
        tax=money(booked.tax - returned.tax),
        discount=money(booked.discount - returned.discount),
        cost=money(booked.cost - returned.cost),
        profit=money(booked.profit - returned.profit),
    )


def _invoice_discount_shares(sale, items):
    """Reconstruct each immutable line's allocated invoice discount."""
    items = list(items)
    gross_line_discount = sum(
        (item.discount_amount for item in items), ZERO
    )
    invoice_discount = money(
        max(sale.discount_amount - gross_line_discount, ZERO)
    )
    if not items or invoice_discount <= 0:
        return {item.pk: ZERO for item in items}

    # Invoice discounts were allocated proportionally before the immutable
    # line snapshots were written. Post-discount taxable bases preserve that
    # same proportion; the gross-line fallback covers a fully discounted sale.
    weights = [max(item.line_total - item.tax_amount, ZERO) for item in items]
    total_weight = sum(weights, ZERO)
    if total_weight <= 0:
        weights = [
            max(
                item.unit_price * item.quantity - item.discount_amount,
                ZERO,
            )
            for item in items
        ]
        total_weight = sum(weights, ZERO)
    if total_weight <= 0:
        return {item.pk: ZERO for item in items}

    shares = {}
    allocated = ZERO
    last_index = len(items) - 1
    for index, (item, weight) in enumerate(zip(items, weights, strict=True)):
        if index == last_index:
            share = money(invoice_discount - allocated)
        else:
            share = money(invoice_discount * weight / total_weight)
            allocated += share
        shares[item.pk] = share
    return shares


def item_financial_summary_for_sale(sale, items=None, return_items=None):
    """Return booked, returned, and net item economics for one invoice."""
    items = list(sale.items.all()) if items is None else list(items)
    shares = _invoice_discount_shares(sale, items)
    booked = _sum_item_amounts(
        _item_amounts(item, item.quantity, shares.get(item.pk, ZERO))
        for item in items
    )
    if sale.status == "voided":
        return ItemFinancialActivity(
            booked=booked,
            returned=booked,
            net=ItemFinancialAmounts(),
        )
    if return_items is None:
        net = _sum_item_amounts(
            _item_amounts(
                item,
                item.quantity - item.returned_quantity,
                shares.get(item.pk, ZERO),
            )
            for item in items
        )
        returned = _subtract_item_amounts(booked, net)
    else:
        returned = _sum_item_amounts(
            _returned_item_amounts(
                return_item,
                shares.get(return_item.sale_item_id, ZERO),
            )
            for return_item in return_items
        )
        net = _subtract_item_amounts(booked, returned)
    return ItemFinancialActivity(
        booked=booked,
        returned=returned,
        net=net,
    )


def net_item_values(item, return_items=None):
    """Proportional net quantity and financial values for one sale line."""
    if return_items is not None:
        booked = _item_amounts(item, item.quantity)
        returned = _sum_item_amounts(
            _returned_item_amounts(return_item)
            for return_item in return_items
        )
        net = _subtract_item_amounts(booked, returned)
        return {
            "qty": net.quantity,
            "returned": returned.quantity,
            "revenue": net.revenue,
            "discount": net.discount,
            "tax": net.tax,
            "cost": net.cost,
            "profit": net.profit,
        }
    net_quantity = item.quantity - item.returned_quantity
    if not item.quantity:
        return {
            "qty": ZERO,
            "returned": item.returned_quantity,
            "revenue": ZERO,
            "discount": ZERO,
            "tax": ZERO,
            "cost": ZERO,
            "profit": ZERO,
        }
    ratio = net_quantity / item.quantity
    revenue = money(item.line_total * ratio)
    tax = money(item.tax_amount * ratio)
    cost = money(item.unit_cost * item.inventory_quantity * ratio)
    return {
        "qty": qty(net_quantity),
        "returned": qty(item.returned_quantity),
        "revenue": revenue,
        "discount": money(item.discount_amount * ratio),
        "tax": tax,
        "cost": cost,
        "profit": money(revenue - tax - cost),
    }


def net_sale_discount(sale, items=None):
    """Return line and invoice discounts attributable to unreturned units."""
    return item_financial_summary_for_sale(sale, items=items).net.discount


def net_item_quantity_expression():
    return ExpressionWrapper(
        F("quantity") - F("returned_quantity"),
        output_field=DecimalField(max_digits=38, decimal_places=12),
    )


def net_item_value_expression(value):
    """ORM expression for a money value proportional to unreturned quantity."""
    amount_field = DecimalField(max_digits=30, decimal_places=3)
    calculation_field = DecimalField(max_digits=38, decimal_places=12)
    value_expression = F(value) if isinstance(value, str) else value
    ratio = ExpressionWrapper(
        Cast(net_item_quantity_expression(), FloatField())
        / Cast(F("quantity"), FloatField()),
        output_field=FloatField(),
    )
    proportional = ExpressionWrapper(
        value_expression * ratio,
        output_field=calculation_field,
    )
    return Case(
        When(quantity=0, then=Value(ZERO, output_field=amount_field)),
        default=ExpressionWrapper(
            Round(proportional, precision=3),
            output_field=amount_field,
        ),
        output_field=amount_field,
    )


def returned_item_value_expression(value):
    """ORM expression for a return line's proportional source-line value."""
    amount_field = DecimalField(max_digits=30, decimal_places=3)
    calculation_field = DecimalField(max_digits=38, decimal_places=12)
    value_expression = (
        F(f"sale_item__{value}") if isinstance(value, str) else value
    )
    ratio = ExpressionWrapper(
        Cast(F("quantity"), FloatField())
        / Cast(F("sale_item__quantity"), FloatField()),
        output_field=FloatField(),
    )
    proportional = ExpressionWrapper(
        value_expression * ratio,
        output_field=calculation_field,
    )
    return Case(
        When(
            sale_item__quantity=0,
            then=Value(ZERO, output_field=amount_field),
        ),
        default=ExpressionWrapper(
            Round(proportional, precision=3),
            output_field=amount_field,
        ),
        output_field=amount_field,
    )


def item_cost_expression(field_prefix=""):
    """ORM expression matching a persisted sale line's rounded cost."""
    amount_field = DecimalField(max_digits=30, decimal_places=3)
    calculation_field = DecimalField(max_digits=38, decimal_places=12)
    fabric_field = f"{field_prefix}fabric_meter_used"
    quantity_field = f"{field_prefix}quantity"
    inventory_quantity = Case(
        When(
            **{f"{fabric_field}__isnull": False},
            then=F(fabric_field),
        ),
        default=F(quantity_field),
        output_field=calculation_field,
    )
    raw_cost = ExpressionWrapper(
        F(f"{field_prefix}unit_cost") * inventory_quantity,
        output_field=calculation_field,
    )
    return ExpressionWrapper(
        Round(raw_cost, precision=3),
        output_field=amount_field,
    )


def _returned_discount_total(returns):
    from .models import SaleItem, SaleReturnItem

    return_items = list(
        SaleReturnItem.objects.filter(sale_return__in=returns)
        .select_related("sale_item__sale")
        .order_by("sale_item__sale_id", "sale_item_id", "id")
    )
    sale_ids = {item.sale_item.sale_id for item in return_items}
    if not sale_ids:
        return money(ZERO)
    items_by_sale = {}
    for item in (
        SaleItem.objects.filter(sale_id__in=sale_ids)
        .select_related("sale")
        .order_by("sale_id", "id")
    ):
        items_by_sale.setdefault(item.sale_id, []).append(item)
    shares_by_sale = {
        sale_id: _invoice_discount_shares(items[0].sale, items)
        for sale_id, items in items_by_sale.items()
    }
    total = ZERO
    for return_item in return_items:
        source = return_item.sale_item
        if not source.quantity:
            continue
        invoice_share = shares_by_sale.get(source.sale_id, {}).get(
            source.pk, ZERO
        )
        total += money(
            (source.discount_amount + invoice_share)
            * return_item.quantity
            / source.quantity
        )
    return money(total)


def item_activity_summary(sales, returns, *, include_discount=True):
    """Net item economics for sale-date and return-date scoped querysets."""
    from .models import SaleItem, SaleReturnItem

    valid_sales = sales.exclude(status__in=("draft", "voided"))
    amount_field = DecimalField(max_digits=30, decimal_places=3)
    booked_cost = item_cost_expression()
    returned_cost = item_cost_expression("sale_item__")
    booked_totals = (
        SaleItem.objects.filter(sale__in=valid_sales).aggregate(
            quantity_total=Sum("quantity"),
            revenue=Sum("line_total", output_field=amount_field),
            tax=Sum("tax_amount", output_field=amount_field),
            cost=Sum(booked_cost, output_field=amount_field),
        )
    )
    returned_totals = (
        SaleReturnItem.objects.filter(sale_return__in=returns).aggregate(
            quantity_total=Sum("quantity"),
            revenue=Sum("line_refund", output_field=amount_field),
            tax=Sum(
                returned_item_value_expression("tax_amount"),
                output_field=amount_field,
            ),
            cost=Sum(
                returned_item_value_expression(returned_cost),
                output_field=amount_field,
            ),
        )
    )
    booked_discount = ZERO
    returned_discount = ZERO
    if include_discount:
        booked_discount = (
            valid_sales.aggregate(total=Sum("discount_amount"))["total"]
            or ZERO
        )
        returned_discount = _returned_discount_total(returns)

    booked_revenue = money(booked_totals["revenue"] or ZERO)
    booked_tax = money(booked_totals["tax"] or ZERO)
    booked_cost_total = money(booked_totals["cost"] or ZERO)
    returned_revenue = money(returned_totals["revenue"] or ZERO)
    returned_tax = money(returned_totals["tax"] or ZERO)
    returned_cost_total = money(returned_totals["cost"] or ZERO)
    booked = ItemFinancialAmounts(
        quantity=qty(booked_totals["quantity_total"] or ZERO),
        revenue=booked_revenue,
        tax=booked_tax,
        discount=money(booked_discount),
        cost=booked_cost_total,
        profit=money(booked_revenue - booked_tax - booked_cost_total),
    )
    returned = ItemFinancialAmounts(
        quantity=qty(returned_totals["quantity_total"] or ZERO),
        revenue=returned_revenue,
        tax=returned_tax,
        discount=money(returned_discount),
        cost=returned_cost_total,
        profit=money(
            returned_revenue - returned_tax - returned_cost_total
        ),
    )
    return ItemFinancialActivity(
        booked=booked,
        returned=returned,
        net=_subtract_item_amounts(booked, returned),
    )


def subtract_grouped_totals(gross_rows, return_rows, *, key, total="total"):
    """Combine grouped sale/refund rows into a key -> net amount mapping."""
    values = {}
    for row in gross_rows:
        group = row[key]
        values[group] = money(
            values.get(group, ZERO) + (row.get(total) or ZERO)
        )
    for row in return_rows:
        group = row[key]
        values[group] = money(
            values.get(group, ZERO) - (row.get(total) or ZERO)
        )
    return values
