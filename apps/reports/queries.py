"""Report queries.

Each report function takes (business, filters) and returns:
  {"columns": [...], "rows": [[...], ...], "totals": [...] or None}
Filters: date_from, date_to (date objects or None), branch_id, warehouse_id.
The same data feeds HTML tables and CSV/Excel/PDF exports, so exported
numbers always match what is on screen.
"""
from datetime import date
from decimal import Decimal

from django.db.models import (
    Avg,
    Case,
    Count,
    DecimalField,
    ExpressionWrapper,
    F,
    OuterRef,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Greatest, Round
from django.utils import timezone

ZERO = Decimal("0")


def _money(value):
    from apps.core.money import money

    return money(value or ZERO)


def _net_item_values(item):
    net_qty = item.quantity - item.returned_quantity
    if not item.quantity:
        return {
            "qty": ZERO,
            "returned": item.returned_quantity,
            "revenue": ZERO,
            "tax": ZERO,
            "cost": ZERO,
            "profit": ZERO,
        }
    ratio = net_qty / item.quantity
    return {
        "qty": net_qty,
        "returned": item.returned_quantity,
        "revenue": _money(item.line_total * ratio),
        "discount": _money(item.discount_amount * ratio),
        "tax": _money(item.tax_amount * ratio),
        "cost": _money(item.unit_cost * net_qty),
        "profit": _money(item.gross_profit * ratio),
    }


def _payment_breakdown(sale):
    from apps.sales.models import PaymentMethod

    amounts = {"bank": ZERO, "card": ZERO, "cash": ZERO, "credit": ZERO}
    for payment in sale.payments.all():
        if payment.method.kind == PaymentMethod.Kind.BANK:
            amounts["bank"] += payment.amount
        elif payment.method.kind == PaymentMethod.Kind.CARD:
            amounts["card"] += payment.amount
        elif payment.method.kind == PaymentMethod.Kind.CASH:
            amounts["cash"] += payment.amount
        elif payment.method.kind == PaymentMethod.Kind.CUSTOMER_CREDIT:
            amounts["credit"] += payment.amount
    return {key: _money(value) for key, value in amounts.items()}


def _payment_method_summary(sale):
    names = []
    for payment in sale.payments.all():
        if payment.method.name not in names:
            names.append(payment.method.name)
    return " + ".join(names) or "-"


def _real_received(sale):
    from apps.sales.models import SaleReturn

    payments = _payment_breakdown(sale)
    received = payments["bank"] + payments["card"] + payments["cash"]
    cash_refunds = sum(
        (
            r.refund_amount
            for r in sale.returns.all()
            if r.refund_method in (
                SaleReturn.RefundMethod.BANK,
                SaleReturn.RefundMethod.CARD,
                SaleReturn.RefundMethod.CASH,
            )
        ),
        ZERO,
    )
    return _money(received - cash_refunds)


def _net_sale_tax(sale):
    return sum((_net_item_values(item)["tax"] for item in sale.items.all()), ZERO)


def _net_sale_profit(sale):
    return sum((_net_item_values(item)["profit"] for item in sale.items.all()), ZERO)


def _sales_base(business, f, exclude_voided=True):
    from apps.sales.models import Sale

    qs = Sale.objects.for_business(business).exclude(status="draft")
    if exclude_voided:
        qs = qs.exclude(status="voided")
    if f.get("date_from"):
        qs = qs.filter(sale_date__date__gte=f["date_from"])
    if f.get("date_to"):
        qs = qs.filter(sale_date__date__lte=f["date_to"])
    if f.get("branch_id"):
        qs = qs.filter(branch_id=f["branch_id"])
    return qs


def _scope_to_membership_branches(
    queryset,
    *,
    business,
    membership,
    branch_id=None,
    branch_field="branch_id",
):
    """Limit a tenant queryset to the membership's permitted branches."""
    if membership is None or membership.business_id != business.id:
        return queryset.none()

    allowed = membership.allowed_branch_ids
    if branch_id is not None:
        if allowed is not None and branch_id not in allowed:
            return queryset.none()
        return queryset.filter(**{branch_field: branch_id})
    if allowed is not None:
        return queryset.filter(**{f"{branch_field}__in": allowed})
    return queryset


def current_year_financial_summary(
    business,
    membership,
    *,
    branch_id=None,
    today=None,
    include_profit=True,
):
    """Current-calendar-year dashboard totals, independent of date filters.

    Receivable follows ``customer_receivables``: the current outstanding
    amount on valid invoices created in the requested range. Real payments
    are cash, card, and bank entries; cash/card/bank refunds are backed out
    before the per-invoice balance is clamped at zero.
    """
    from apps.core.money import money
    from apps.expenses.models import Expense
    from apps.sales.models import PaymentMethod, Sale, SaleItem, SalePayment, SaleReturn

    today = today or timezone.localdate()
    year_start = date(today.year, 1, 1)
    try:
        selected_branch_id = int(branch_id) if branch_id is not None else None
    except (TypeError, ValueError):
        selected_branch_id = None

    receivable_payment_kinds = (
        PaymentMethod.Kind.CASH,
        PaymentMethod.Kind.CARD,
        PaymentMethod.Kind.BANK,
    )
    income_payment_kinds = receivable_payment_kinds + (
        PaymentMethod.Kind.ONLINE,
        PaymentMethod.Kind.OTHER,
    )
    amount_field = DecimalField(max_digits=30, decimal_places=3)
    calculation_field = DecimalField(max_digits=38, decimal_places=12)
    zero = Value(ZERO, output_field=amount_field)

    valid_sales = _sales_base(
        business,
        {"date_from": year_start, "date_to": today},
    )
    valid_sales = _scope_to_membership_branches(
        valid_sales,
        business=business,
        membership=membership,
        branch_id=selected_branch_id,
    )

    payment_subquery = (
        SalePayment.objects.for_business(business)
        .filter(
            sale_id=OuterRef("pk"),
            method__kind__in=receivable_payment_kinds,
        )
        .values("sale_id")
        .annotate(total=Sum("amount"))
        .values("total")[:1]
    )
    return_subquery = (
        SaleReturn.objects.for_business(business)
        .filter(sale_id=OuterRef("pk"))
        .values("sale_id")
        .annotate(total=Sum("refund_amount"))
        .values("total")[:1]
    )
    paid_refund_subquery = (
        SaleReturn.objects.for_business(business)
        .filter(
            sale_id=OuterRef("pk"),
            refund_method__in=receivable_payment_kinds,
        )
        .values("sale_id")
        .annotate(total=Sum("refund_amount"))
        .values("total")[:1]
    )
    sales_with_balance = valid_sales.annotate(
        _real_received=Coalesce(
            Subquery(payment_subquery, output_field=amount_field),
            zero,
            output_field=amount_field,
        ),
        _returned_total=Coalesce(
            Subquery(return_subquery, output_field=amount_field),
            zero,
            output_field=amount_field,
        ),
        _paid_refunds=Coalesce(
            Subquery(paid_refund_subquery, output_field=amount_field),
            zero,
            output_field=amount_field,
        ),
    ).annotate(
        _receivable=Greatest(
            ExpressionWrapper(
                F("total")
                - F("_returned_total")
                - F("_real_received")
                + F("_paid_refunds"),
                output_field=amount_field,
            ),
            zero,
            output_field=amount_field,
        )
    )
    sales_totals = sales_with_balance.aggregate(
        total_sales=Sum("total", output_field=amount_field),
        total_receivable=Sum("_receivable", output_field=amount_field),
    )

    payments = SalePayment.objects.for_business(business).filter(
        payment_date__gte=year_start,
        payment_date__lte=today,
        method__kind__in=income_payment_kinds,
    ).exclude(sale__status__in=[Sale.Status.DRAFT, Sale.Status.VOIDED])
    payments = _scope_to_membership_branches(
        payments,
        business=business,
        membership=membership,
        branch_id=selected_branch_id,
        branch_field="sale__branch_id",
    )
    total_income = payments.aggregate(total=Sum("amount"))["total"] or ZERO

    expenses = Expense.objects.for_business(business).filter(
        expense_date__gte=year_start,
        expense_date__lte=today,
        status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
    )
    expenses = _scope_to_membership_branches(
        expenses,
        business=business,
        membership=membership,
        branch_id=selected_branch_id,
    )
    total_expenses = expenses.aggregate(total=Sum("amount"))["total"] or ZERO

    returns = SaleReturn.objects.for_business(business).filter(
        created_at__date__gte=year_start,
        created_at__date__lte=today,
    )
    returns = _scope_to_membership_branches(
        returns,
        business=business,
        membership=membership,
        branch_id=selected_branch_id,
    )
    total_returns = returns.aggregate(total=Sum("refund_amount"))["total"] or ZERO

    gross_profit = None
    if include_profit:
        net_quantity = ExpressionWrapper(
            F("quantity") - F("returned_quantity"),
            output_field=calculation_field,
        )
        proportional_profit = ExpressionWrapper(
            F("gross_profit") * net_quantity / F("quantity"),
            output_field=calculation_field,
        )
        rounded_profit = ExpressionWrapper(
            Round(proportional_profit, precision=3),
            output_field=amount_field,
        )
        gross_profit = (
            SaleItem.objects.for_business(business)
            .filter(sale__in=valid_sales)
            .aggregate(
                total=Sum(
                    Case(
                        When(quantity=0, then=zero),
                        default=rounded_profit,
                        output_field=amount_field,
                    ),
                    output_field=amount_field,
                )
            )["total"]
            or ZERO
        )

    total_sales = money(sales_totals["total_sales"] or ZERO)
    total_receivable = money(sales_totals["total_receivable"] or ZERO)
    total_income = money(total_income)
    total_expenses = money(total_expenses)
    total_returns = money(total_returns)
    net_sales = money(total_sales - total_returns)
    gross_profit = money(gross_profit) if gross_profit is not None else None

    return {
        "year": today.year,
        "start_date": year_start,
        "end_date": today,
        "total_sales": total_sales,
        "total_income": total_income,
        "total_receivable": total_receivable,
        "total_expenses": total_expenses,
        "total_returns": total_returns,
        "net_sales": net_sales,
        "gross_profit": gross_profit,
        "estimated_net_profit": (
            money(gross_profit - total_expenses)
            if gross_profit is not None
            else None
        ),
    }


def sales_summary(business, f):
    from apps.sales.models import PaymentMethod

    qs = (
        _sales_base(business, f)
        .prefetch_related("returns", "items", "payments__method")
        .order_by("sale_date", "invoice_number")
    )
    rows = []
    for sale in qs:
        payments = {"bank": ZERO, "card": ZERO, "cash": ZERO}
        for payment in sale.payments.all():
            if payment.method.kind == PaymentMethod.Kind.BANK:
                payments["bank"] += payment.amount
            elif payment.method.kind == PaymentMethod.Kind.CARD:
                payments["card"] += payment.amount
            elif payment.method.kind == PaymentMethod.Kind.CASH:
                payments["cash"] += payment.amount
        received = payments["bank"] + payments["card"] + payments["cash"]
        receivable = _money(sale.net_total - received)
        rows.append([
            sale.sale_date.date(),
            sale.invoice_number,
            _money(sale.net_total),
            _money(payments["bank"]),
            _money(payments["card"]),
            _money(payments["cash"]),
            receivable,
            _money(sale.discount_amount),
            _money(_net_sale_tax(sale)),
            _money(_net_sale_profit(sale)),
        ])
    totals = [
        "TOTAL", "",
        sum((r[2] or ZERO) for r in rows),
        sum((r[3] or ZERO) for r in rows),
        sum((r[4] or ZERO) for r in rows),
        sum((r[5] or ZERO) for r in rows),
        sum((r[6] or ZERO) for r in rows),
        sum((r[7] or ZERO) for r in rows),
        sum((r[8] or ZERO) for r in rows),
        sum((r[9] or ZERO) for r in rows),
    ]
    return {"columns": ["Date", "Invoice No", "Sales Amount", "Bank Transfer",
                        "Card", "Cash", "Credit / Receivable", "Discount",
                        "VAT", "Gross"],
            "rows": rows, "totals": totals if rows else None}


def sales_detailed(business, f):
    from apps.sales.models import Sale, SaleItem

    qs = (
        SaleItem.objects.for_business(business)
        .filter(sale__in=_sales_base(business, f, exclude_voided=False))
        .select_related("sale__customer", "sale__branch", "sale__cashier", "product")
        .prefetch_related("sale__returns", "sale__payments__method")
        .order_by("-sale__sale_date", "sale__invoice_number", "id")
    )
    if f.get("product_id"):
        qs = qs.filter(product_id=f["product_id"])
    if f.get("garment_classification") in ("adult", "child"):
        qs = qs.filter(garment_classification=f["garment_classification"])

    rows = []
    pieces = {"adult": ZERO, "child": ZERO, "legacy": ZERO}
    fabric_totals = {"estimated": ZERO, "actual": ZERO, "variance": ZERO}
    has_fabric = {"estimated": False, "actual": False, "variance": False}
    for item in qs[:2000]:
        sale = item.sale
        quantity = item.quantity - item.returned_quantity
        if sale.status == Sale.Status.VOIDED:
            quantity = ZERO
        classification = item.garment_classification_label or "Not Applicable"
        if item.is_tailoring_line:
            key = item.garment_classification or "legacy"
            pieces[key] += quantity
        estimated_fabric = item.estimated_fabric
        actual_fabric = item.actual_fabric_used
        variance = item.fabric_variance
        for key, value in (
            ("estimated", estimated_fabric),
            ("actual", actual_fabric),
            ("variance", variance),
        ):
            if value is not None:
                fabric_totals[key] += value
                has_fabric[key] = True
        rows.append([
            sale.invoice_number,
            sale.sale_date.strftime("%Y-%m-%d %H:%M"),
            sale.customer.full_name,
            sale.branch.name,
            sale.cashier.full_name,
            item.product_name,
            classification,
            quantity,
            estimated_fabric,
            actual_fabric,
            variance,
            _payment_method_summary(sale),
            sale.net_total,
            sale.net_amount_paid,
            sale.balance,
            sale.get_status_display(),
        ])
    return {
        "columns": [
            "Invoice", "Date", "Customer", "Branch", "Cashier", "Product",
            "Garment Classification", "Quantity", "Estimated Fabric",
            "Actual Fabric", "Variance", "Payment Method", "Total", "Paid",
            "Balance", "Status",
        ],
        "rows": rows,
        "totals": None,
        "summary": [
            ("Total Adult Pieces", pieces["adult"]),
            ("Total Child Pieces", pieces["child"]),
            ("Total Legacy/Unclassified Pieces", pieces["legacy"]),
            (
                "Estimated Total",
                fabric_totals["estimated"] if has_fabric["estimated"] else None,
            ),
            (
                "Actual Total",
                fabric_totals["actual"] if has_fabric["actual"] else None,
            ),
            (
                "Variance Total",
                fabric_totals["variance"] if has_fabric["variance"] else None,
            ),
        ],
    }


def product_sales(business, f):
    from apps.sales.models import SaleItem

    qs = SaleItem.objects.for_business(business).filter(
        sale__in=_sales_base(business, f))
    if f.get("category_id"):
        qs = qs.filter(product__category_id=f["category_id"])
    by_product = {}
    for item in qs.select_related("product__category"):
        values = _net_item_values(item)
        key = (
            item.product_name,
            item.sku,
            item.product.category.name if item.product.category else "",
        )
        row = by_product.setdefault(key, {
            "qty": ZERO,
            "revenue": ZERO,
            "discount": ZERO,
            "tax": ZERO,
            "cost": ZERO,
            "profit": ZERO,
        })
        row["qty"] += values["qty"]
        row["revenue"] += values["revenue"]
        row["discount"] += values["discount"]
        row["tax"] += values["tax"]
        row["cost"] += values["cost"]
        row["profit"] += values["profit"]
    rows = [[name, sku or "-", category or "-", r["qty"], _money(r["revenue"]),
             _money(r["discount"]), _money(r["tax"]), _money(r["cost"]),
             _money(r["profit"])]
            for (name, sku, category), r in sorted(
                by_product.items(), key=lambda item: item[1]["revenue"],
                reverse=True)]
    totals = ["TOTAL", "", "", sum((r[3] or ZERO) for r in rows),
              sum((r[4] or ZERO) for r in rows), sum((r[5] or ZERO) for r in rows),
              sum((r[6] or ZERO) for r in rows), sum((r[7] or ZERO) for r in rows),
              sum((r[8] or ZERO) for r in rows)]
    return {"columns": ["Product Name", "SKU", "Category", "Qty Sold",
                        "Sales Amount", "Discount", "VAT", "Cost", "Gross Profit"],
            "rows": rows, "totals": totals if rows else None}


def category_sales(business, f):
    from apps.sales.models import SaleItem

    qs = (
        SaleItem.objects.for_business(business)
        .filter(sale__in=_sales_base(business, f))
        .select_related("product__category")
    )
    by_category = {}
    for item in qs:
        values = _net_item_values(item)
        name = item.product.category.name if item.product.category else "(Uncategorized)"
        row = by_category.setdefault(name, {"qty": ZERO, "revenue": ZERO, "profit": ZERO})
        row["qty"] += values["qty"]
        row["revenue"] += values["revenue"]
        row["profit"] += values["profit"]
    rows = [[name, r["qty"], _money(r["revenue"]), _money(r["profit"])]
            for name, r in sorted(
                by_category.items(), key=lambda item: item[1]["revenue"],
                reverse=True)]
    return {"columns": ["Category", "Qty", "Revenue", "Gross profit"],
            "rows": rows, "totals": None}


def cashier_sales(business, f):
    by_cashier = {}
    for sale in (
        _sales_base(business, f)
        .select_related("cashier")
        .prefetch_related("returns", "items")
    ):
        name = sale.cashier.full_name
        row = by_cashier.setdefault(name, {
            "invoices": 0,
            "total": ZERO,
            "profit": ZERO,
        })
        row["invoices"] += 1
        row["total"] += sale.net_total
        row["profit"] += _net_sale_profit(sale)
    rows = [[name, r["invoices"], _money(r["total"]),
             round((_money(r["total"]) / r["invoices"]) if r["invoices"] else ZERO, 3),
             _money(r["profit"])]
            for name, r in sorted(
                by_cashier.items(), key=lambda item: item[1]["total"],
                reverse=True)]
    return {"columns": ["Cashier", "Invoices", "Sales", "Avg invoice", "Gross profit"],
            "rows": rows, "totals": None}


def payment_methods_report(business, f):
    qs = (
        _sales_base(business, f)
        .select_related("customer")
        .prefetch_related("payments__method")
        .order_by("sale_date", "invoice_number")
    )
    rows = []
    for sale in qs:
        payments = _payment_breakdown(sale)
        total_received = payments["cash"] + payments["card"] + payments["bank"]
        rows.append([
            sale.sale_date.date(),
            sale.invoice_number,
            sale.customer.full_name,
            sale.customer.mobile or "-",
            payments["cash"],
            payments["card"],
            payments["bank"],
            payments["credit"],
            _money(total_received),
        ])
    totals = [
        "TOTAL", "", "", "",
        sum((r[4] or ZERO) for r in rows),
        sum((r[5] or ZERO) for r in rows),
        sum((r[6] or ZERO) for r in rows),
        sum((r[7] or ZERO) for r in rows),
        sum((r[8] or ZERO) for r in rows),
    ]
    return {"columns": ["Date", "Invoice No", "Customer", "Phone Number",
                        "Cash", "Card", "Bank Transfer", "Customer Credit",
                        "Total Received"],
            "rows": rows, "totals": totals if rows else None}


def voided_sales(business, f):
    qs = _sales_base(business, f, exclude_voided=False).filter(status="voided")
    rows = [[s.invoice_number, s.sale_date.strftime("%Y-%m-%d %H:%M"),
             s.total, s.voided_by.full_name if s.voided_by else "",
             s.void_reason] for s in qs]
    return {"columns": ["Invoice", "Date", "Total", "Voided by", "Reason"],
            "rows": rows, "totals": None}


def returns_report(business, f):
    from apps.sales.models import SaleReturnItem

    qs = SaleReturnItem.objects.for_business(business).select_related(
        "sale_return__sale", "sale_return__customer", "sale_return__processed_by",
        "sale_item")
    if f.get("date_from"):
        qs = qs.filter(sale_return__created_at__date__gte=f["date_from"])
    if f.get("date_to"):
        qs = qs.filter(sale_return__created_at__date__lte=f["date_to"])
    if f.get("branch_id"):
        qs = qs.filter(sale_return__branch_id=f["branch_id"])
    rows = []
    for item in qs.order_by("-sale_return__created_at", "sale_item__product_name"):
        sale_return = item.sale_return
        rows.append([
            sale_return.created_at.strftime("%Y-%m-%d"),
            sale_return.return_number,
            sale_return.sale.invoice_number,
            sale_return.customer.full_name,
            sale_return.customer.mobile or "-",
            item.sale_item.product_name,
            item.sale_item.sku or "-",
            item.quantity,
            item.sale_item.unit_price,
            item.line_refund,
            sale_return.get_refund_method_display(),
            sale_return.reason or "-",
            sale_return.processed_by.full_name if sale_return.processed_by else "-",
        ])
    totals = ["TOTAL", "", "", "", "", "", "", "", "",
              sum((r[9] or ZERO) for r in rows), "", "", ""]
    return {"columns": ["Return Date", "Return No", "Invoice No", "Customer",
                        "Phone Number", "Product", "SKU", "Returned Qty",
                        "Unit Price", "Returned Amount", "Refund Method",
                        "Reason", "Processed By"],
            "rows": rows, "totals": totals if rows else None}


def tax_report(business, f):
    from apps.sales.models import SaleItem

    qs = (
        SaleItem.objects.for_business(business)
        .filter(sale__in=_sales_base(business, f), tax_rate__gt=0)
    )
    by_rate = {}
    for item in qs:
        values = _net_item_values(item)
        row = by_rate.setdefault(item.tax_rate, {"base": ZERO, "tax": ZERO})
        row["base"] += values["revenue"] - values["tax"]
        row["tax"] += values["tax"]
    rows = [[f"{rate}%", _money(r["base"]), _money(r["tax"])]
            for rate, r in sorted(by_rate.items())]
    totals = ["TOTAL", sum((r[1] or ZERO) for r in rows),
              sum((r[2] or ZERO) for r in rows)]
    return {"columns": ["VAT Rate", "Taxable Amount", "VAT Amount"],
            "rows": rows, "totals": totals if rows else None}


def current_stock(business, f):
    from apps.inventory.models import StockLevel

    qs = (
        StockLevel.objects.for_business(business)
        .select_related("product", "variant", "warehouse")
        .filter(product__is_archived=False)
        .order_by("product__name")
    )
    if f.get("warehouse_id"):
        qs = qs.filter(warehouse_id=f["warehouse_id"])
    rows = []
    total_value = ZERO
    for level in qs[:5000]:
        target = level.variant or level.product
        cost = target.average_cost or getattr(target, "purchase_price", ZERO)
        value = level.quantity * cost
        total_value += value
        rows.append([level.product.name,
                     level.variant.name if level.variant else "",
                     level.warehouse.name, level.quantity, cost, value])
    return {"columns": ["Product", "Variant", "Warehouse", "Quantity",
                        "Avg cost", "Value"],
            "rows": rows,
            "totals": ["TOTAL", "", "", "", "", total_value] if rows else None}


def low_stock(business, f):
    from apps.inventory.models import StockLevel

    qs = (
        StockLevel.objects.for_business(business)
        .select_related("product", "warehouse")
        .filter(product__reorder_level__gt=0,
                quantity__lte=F("product__reorder_level"),
                product__is_archived=False)
    )
    if f.get("warehouse_id"):
        qs = qs.filter(warehouse_id=f["warehouse_id"])
    rows = [[level.product.name, level.warehouse.name, level.quantity,
             level.product.reorder_level] for level in qs[:2000]]
    return {"columns": ["Product", "Warehouse", "Current stock", "Reorder level"],
            "rows": rows, "totals": None}


def stock_movements_report(business, f):
    from apps.inventory.models import StockMovement

    qs = (
        StockMovement.objects.for_business(business)
        .select_related("product", "warehouse", "user")
        .order_by("-created_at")
    )
    if f.get("date_from"):
        qs = qs.filter(created_at__date__gte=f["date_from"])
    if f.get("date_to"):
        qs = qs.filter(created_at__date__lte=f["date_to"])
    if f.get("warehouse_id"):
        qs = qs.filter(warehouse_id=f["warehouse_id"])
    rows = [[m.created_at.strftime("%Y-%m-%d %H:%M"), m.product.name,
             m.get_movement_type_display(), m.warehouse.name, m.quantity,
             m.balance_after, f"{m.reference_type} {m.reference_id}".strip(),
             m.user.full_name if m.user else ""] for m in qs[:2000]]
    return {"columns": ["Date", "Product", "Type", "Warehouse", "Qty",
                        "Balance after", "Reference", "User"],
            "rows": rows, "totals": None}


def purchases_summary(business, f):
    from apps.purchases.models import Purchase

    qs = Purchase.objects.for_business(business).select_related("supplier")
    if f.get("date_from"):
        qs = qs.filter(purchase_date__gte=f["date_from"])
    if f.get("date_to"):
        qs = qs.filter(purchase_date__lte=f["date_to"])
    if f.get("branch_id"):
        qs = qs.filter(branch_id=f["branch_id"])
    rows = [[p.purchase_number, str(p.purchase_date), p.supplier.name,
             p.total, p.amount_paid, p.outstanding, p.get_status_display()]
            for p in qs[:2000]]
    totals = ["TOTAL", "", "", sum((r[3] or ZERO) for r in rows),
              sum((r[4] or ZERO) for r in rows), sum((r[5] or ZERO) for r in rows), ""]
    return {"columns": ["Number", "Date", "Supplier", "Total", "Paid",
                        "Outstanding", "Status"],
            "rows": rows, "totals": totals if rows else None}


def supplier_balances(business, f):
    from apps.suppliers.models import Supplier

    qs = Supplier.objects.for_business(business).filter(balance__gt=0).order_by("-balance")
    rows = [[s.name, s.code, s.mobile, s.balance] for s in qs]
    totals = ["TOTAL", "", "", sum((r[3] or ZERO) for r in rows)]
    return {"columns": ["Supplier", "Code", "Mobile", "Payable"],
            "rows": rows, "totals": totals if rows else None}


def customer_receivables(business, f):
    qs = (
        _sales_base(business, f)
        .select_related("customer")
        .prefetch_related("payments__method", "returns")
        .order_by("sale_date", "invoice_number")
    )
    rows = []
    for sale in qs:
        paid = _real_received(sale)
        receivable = _money(sale.net_total - paid)
        if receivable <= 0:
            continue
        rows.append([
            sale.customer.full_name,
            sale.customer.mobile or "-",
            sale.invoice_number,
            sale.sale_date.date(),
            _money(sale.net_total),
            paid,
            receivable,
            sale.delivery_date or "-",
            sale.payment_state,
        ])
    totals = ["TOTAL", "", "", "",
              sum((r[4] or ZERO) for r in rows),
              sum((r[5] or ZERO) for r in rows),
              sum((r[6] or ZERO) for r in rows), "", ""]
    return {"columns": ["Customer Name", "Phone Number", "Invoice No",
                        "Invoice Date", "Sales Amount", "Paid Amount",
                        "Credit / Receivable", "Due Date / Delivery Date",
                        "Status"],
            "rows": rows, "totals": totals if rows else None}


def top_customers(business, f):
    by_customer = {}
    for sale in (
        _sales_base(business, f)
        .exclude(customer__is_walk_in=True)
        .select_related("customer")
        .prefetch_related("returns")
    ):
        key = (sale.customer.full_name, sale.customer.mobile)
        row = by_customer.setdefault(key, {"invoices": 0, "total": ZERO})
        row["invoices"] += 1
        row["total"] += sale.net_total
    rows = [[name, mobile, r["invoices"], _money(r["total"])]
            for (name, mobile), r in sorted(
                by_customer.items(), key=lambda item: item[1]["total"],
                reverse=True)[:100]]
    return {"columns": ["Customer", "Mobile", "Invoices", "Total purchases"],
            "rows": rows, "totals": None}


def expenses_report(business, f):
    from apps.expenses.models import Expense

    qs = (
        Expense.objects.for_business(business)
        .exclude(status__in=["rejected", "cancelled"])
        .select_related("category", "branch")
    )
    if f.get("date_from"):
        qs = qs.filter(expense_date__gte=f["date_from"])
    if f.get("date_to"):
        qs = qs.filter(expense_date__lte=f["date_to"])
    if f.get("branch_id"):
        qs = qs.filter(branch_id=f["branch_id"])
    rows = [[e.expense_number, str(e.expense_date), e.category.name,
             e.payee or (e.supplier.name if e.supplier else ""), e.branch.name,
             e.amount, e.get_status_display()] for e in qs[:2000]]
    totals = ["TOTAL", "", "", "", "", sum((r[5] or ZERO) for r in rows), ""]
    return {"columns": ["Number", "Date", "Category", "Payee", "Branch",
                        "Amount", "Status"],
            "rows": rows, "totals": totals if rows else None}


def profit_summary(business, f):
    """Estimated net profit = gross profit − operating expenses."""
    from apps.expenses.models import Expense

    revenue = cost = gross = ZERO
    for sale in _sales_base(business, f).prefetch_related("items"):
        for item in sale.items.all():
            values = _net_item_values(item)
            revenue += values["revenue"] - values["tax"]
            cost += values["cost"]
            gross += values["profit"]
    exp_qs = Expense.objects.for_business(business).exclude(
        status__in=["rejected", "cancelled"])
    if f.get("date_from"):
        exp_qs = exp_qs.filter(expense_date__gte=f["date_from"])
    if f.get("date_to"):
        exp_qs = exp_qs.filter(expense_date__lte=f["date_to"])
    if f.get("branch_id"):
        exp_qs = exp_qs.filter(branch_id=f["branch_id"])
    expenses = exp_qs.aggregate(t=Sum("amount"))["t"] or ZERO
    rows = [
        ["Revenue (net of tax)", _money(revenue)],
        ["Cost of goods sold", _money(cost)],
        ["Gross profit", _money(gross)],
        ["Operating expenses", expenses],
        ["Estimated net profit", _money(gross - expenses)],
    ]
    return {"columns": ["Measure", "Amount"], "rows": rows, "totals": None}


def shifts_report(business, f):
    from apps.registers.models import Shift

    qs = Shift.objects.for_business(business).select_related("register", "cashier")
    if f.get("date_from"):
        qs = qs.filter(opened_at__date__gte=f["date_from"])
    if f.get("date_to"):
        qs = qs.filter(opened_at__date__lte=f["date_to"])
    if f.get("branch_id"):
        qs = qs.filter(branch_id=f["branch_id"])
    rows = [[s.register.name, s.cashier.full_name,
             s.opened_at.strftime("%Y-%m-%d %H:%M"),
             s.closed_at.strftime("%Y-%m-%d %H:%M") if s.closed_at else "",
             s.expected_cash, s.actual_cash if s.actual_cash is not None else "",
             s.difference, s.get_status_display()] for s in qs[:1000]]
    return {"columns": ["Register", "Cashier", "Opened", "Closed",
                        "Expected cash", "Actual cash", "Difference", "Status"],
            "rows": rows, "totals": None}


def profit_loss(business, f):
    """Profit & Loss: revenue → COGS → gross profit → expenses by
    category → estimated net profit."""
    from apps.expenses.models import Expense

    revenue = cost = gross = discount = ZERO
    for sale in _sales_base(business, f).prefetch_related("items"):
        discount += sale.discount_amount
        for item in sale.items.all():
            values = _net_item_values(item)
            revenue += values["revenue"] - values["tax"]
            cost += values["cost"]
            gross += values["profit"]
    exp_qs = Expense.objects.for_business(business).exclude(
        status__in=["rejected", "cancelled"])
    if f.get("date_from"):
        exp_qs = exp_qs.filter(expense_date__gte=f["date_from"])
    if f.get("date_to"):
        exp_qs = exp_qs.filter(expense_date__lte=f["date_to"])
    if f.get("branch_id"):
        exp_qs = exp_qs.filter(branch_id=f["branch_id"])
    by_category = exp_qs.values("category__name").annotate(
        t=Sum("amount")).order_by("-t")

    revenue = _money(revenue)
    cost = _money(cost)
    gross = _money(gross)
    rows = [
        ["INCOME", ""],
        ["Revenue (net of tax)", revenue],
        ["Sales discounts given", -discount],
        ["Cost of goods sold", -cost],
        ["GROSS PROFIT", gross],
        ["", ""],
        ["OPERATING EXPENSES", ""],
    ]
    total_expenses = ZERO
    for row in by_category:
        rows.append([f"  {row['category__name']}", -(row["t"] or ZERO)])
        total_expenses += row["t"] or ZERO
    rows += [
        ["Total operating expenses", -total_expenses],
        ["", ""],
        ["ESTIMATED NET PROFIT", gross - total_expenses],
    ]
    return {"columns": ["Line", "Amount"], "rows": rows, "totals": None}


def cash_flow(business, f):
    """Cash in (sale payments + collections) vs cash out (supplier
    payments, expenses, cash refunds), grouped by payment kind."""
    from apps.customers.models import CustomerPayment
    from apps.expenses.models import Expense
    from apps.sales.models import SalePayment, SaleReturn
    from apps.suppliers.models import SupplierPayment

    def ranged(qs, field):
        if f.get("date_from"):
            qs = qs.filter(**{f"{field}__gte": f["date_from"]})
        if f.get("date_to"):
            qs = qs.filter(**{f"{field}__lte": f["date_to"]})
        return qs

    sale_pay = (
        ranged(SalePayment.objects.for_business(business), "created_at__date")
        .exclude(method__kind__in=["customer_credit", "store_credit"])
        .values("method__name").annotate(t=Sum("amount")).order_by("-t")
    )
    collections = ranged(
        CustomerPayment.objects.for_business(business).filter(kind="collection"),
        "created_at__date").aggregate(t=Sum("amount"))["t"] or ZERO
    supplier_pay = ranged(
        SupplierPayment.objects.for_business(business),
        "created_at__date").aggregate(t=Sum("amount"))["t"] or ZERO
    expenses = ranged(
        Expense.objects.for_business(business)
        .exclude(status__in=["rejected", "cancelled"]),
        "expense_date").aggregate(t=Sum("amount"))["t"] or ZERO
    refunds = ranged(
        SaleReturn.objects.for_business(business)
        .filter(refund_method__in=["cash", "card", "bank"]),
        "created_at__date").aggregate(t=Sum("refund_amount"))["t"] or ZERO

    rows = [["CASH IN", ""]]
    total_in = ZERO
    for row in sale_pay:
        rows.append([f"  Sales — {row['method__name']}", row["t"] or ZERO])
        total_in += row["t"] or ZERO
    rows.append(["  Customer collections", collections])
    total_in += collections
    rows += [
        ["Total in", total_in],
        ["", ""],
        ["CASH OUT", ""],
        ["  Supplier payments", -supplier_pay],
        ["  Expenses", -expenses],
        ["  Refunds paid out", -refunds],
        ["Total out", -(supplier_pay + expenses + refunds)],
        ["", ""],
        ["NET CASH FLOW", total_in - supplier_pay - expenses - refunds],
    ]
    return {"columns": ["Line", "Amount"], "rows": rows, "totals": None}


def expense_analysis(business, f):
    from apps.expenses.models import Expense

    qs = Expense.objects.for_business(business).exclude(
        status__in=["rejected", "cancelled"])
    if f.get("date_from"):
        qs = qs.filter(expense_date__gte=f["date_from"])
    if f.get("date_to"):
        qs = qs.filter(expense_date__lte=f["date_to"])
    if f.get("branch_id"):
        qs = qs.filter(branch_id=f["branch_id"])
    data = qs.values("category__name").annotate(
        count=Count("id"), total=Sum("amount"), avg=Avg("amount")
    ).order_by("-total")
    grand = sum((r["total"] or ZERO) for r in data)
    rows = []
    for r in data:
        share = (r["total"] / grand * 100) if grand else ZERO
        rows.append([r["category__name"], r["count"], r["total"],
                     round(r["avg"] or 0, 3), f"{share:.1f}%"])
    return {"columns": ["Category", "Count", "Total", "Average", "Share"],
            "rows": rows,
            "totals": ["TOTAL", sum(r[1] for r in rows), grand, "", "100%"]
                      if rows else None}


def customer_sales(business, f):
    by_customer = {}
    for sale in (
        _sales_base(business, f)
        .select_related("customer")
        .prefetch_related("returns", "items", "payments__method")
    ):
        key = (sale.customer.full_name, sale.customer.mobile)
        row = by_customer.setdefault(key, {
            "invoices": 0,
            "total": ZERO,
            "paid": ZERO,
            "receivable": ZERO,
            "discount": ZERO,
            "tax": ZERO,
            "profit": ZERO,
        })
        paid = _real_received(sale)
        row["invoices"] += 1
        row["total"] += sale.net_total
        row["paid"] += paid
        row["receivable"] += _money(sale.net_total - paid)
        row["discount"] += sale.discount_amount
        row["tax"] += _net_sale_tax(sale)
        row["profit"] += _net_sale_profit(sale)
    rows = [[name, mobile or "-", r["invoices"], _money(r["total"]),
             _money(r["paid"]), _money(r["receivable"]), _money(r["discount"]),
             _money(r["tax"]), _money(r["profit"])]
            for (name, mobile), r in sorted(
                by_customer.items(), key=lambda item: item[1]["total"],
                reverse=True)[:1000]]
    totals = ["TOTAL", "", sum(r[2] for r in rows),
              sum((r[3] or ZERO) for r in rows), sum((r[4] or ZERO) for r in rows),
              sum((r[5] or ZERO) for r in rows), sum((r[6] or ZERO) for r in rows),
              sum((r[7] or ZERO) for r in rows), sum((r[8] or ZERO) for r in rows)]
    return {"columns": ["Customer Name", "Phone Number", "Invoices",
                        "Sales Amount", "Paid Amount", "Credit / Receivable",
                        "Discount", "VAT", "Gross Profit"],
            "rows": rows, "totals": totals if rows else None}


# Registry: key -> (title, function, default_permission)
REPORTS = {
    "sales_summary": ("Daily Sales Report", sales_summary, "reports.view"),
    "sales_detailed": ("Detailed sales / invoices", sales_detailed, "reports.view"),
    "product_sales": ("Product sales", product_sales, "reports.view"),
    "category_sales": ("Category sales", category_sales, "reports.view"),
    "cashier_sales": ("Sales by cashier", cashier_sales, "reports.view"),
    "payment_methods": ("Payment method breakdown", payment_methods_report, "reports.view"),
    "voids": ("Voided sales", voided_sales, "reports.view"),
    "returns": ("Sales returns", returns_report, "reports.view"),
    "tax": ("Sales tax report", tax_report, "reports.financial"),
    "profit": ("Profit summary (estimated)", profit_summary, "reports.financial"),
    "profit_loss": ("Profit & Loss (estimated)", profit_loss, "reports.financial"),
    "cash_flow": ("Cash flow", cash_flow, "reports.financial"),
    "expense_analysis": ("Expense analysis", expense_analysis, "reports.financial"),
    "customer_sales": ("Sales by customer", customer_sales, "reports.view"),
    "current_stock": ("Current stock & valuation", current_stock, "reports.view"),
    "low_stock": ("Low stock / reorder", low_stock, "reports.view"),
    "stock_movements": ("Stock movements", stock_movements_report, "reports.view"),
    "purchases": ("Purchases", purchases_summary, "reports.view"),
    "supplier_balances": ("Outstanding supplier balances", supplier_balances, "reports.financial"),
    "receivables": ("Outstanding receivables", customer_receivables, "reports.financial"),
    "top_customers": ("Top customers", top_customers, "reports.view"),
    "expenses": ("Expenses", expenses_report, "reports.financial"),
    "shifts": ("Shifts & cash differences", shifts_report, "reports.view"),
}

REPORT_GROUPS = [
    ("Sales", ["sales_summary", "sales_detailed", "product_sales", "category_sales",
               "customer_sales", "cashier_sales", "payment_methods", "voids",
               "returns", "tax"]),
    ("Inventory", ["current_stock", "low_stock", "stock_movements"]),
    ("Purchasing", ["purchases", "supplier_balances"]),
    ("Customers", ["receivables", "top_customers"]),
    ("Financial", ["profit_loss", "cash_flow", "expense_analysis", "profit",
                   "expenses"]),
    ("Registers", ["shifts"]),
]
