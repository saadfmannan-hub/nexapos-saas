"""Report queries.

Each report function takes (business, filters) and returns:
  {"columns": [...], "rows": [[...], ...], "totals": [...] or None}
Filters: date_from, date_to (date objects or None), branch_id, warehouse_id.
The same data feeds HTML tables and CSV/Excel/PDF exports, so exported
numbers always match what is on screen.
"""
from decimal import Decimal

from django.db.models import Avg, Count, F, Sum

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
        "tax": _money(item.tax_amount * ratio),
        "cost": _money(item.unit_cost * net_qty),
        "profit": _money(item.gross_profit * ratio),
    }


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
    qs = (
        _sales_base(business, f, exclude_voided=False)
        .select_related("customer", "branch", "cashier")
        .prefetch_related("returns")
        .order_by("-sale_date")[:2000]
    )
    rows = [[s.invoice_number, s.sale_date.strftime("%Y-%m-%d %H:%M"),
             s.customer.full_name, s.branch.name, s.cashier.full_name,
             s.net_total, s.net_amount_paid, s.balance,
             s.get_status_display()] for s in qs]
    return {"columns": ["Invoice", "Date", "Customer", "Branch", "Cashier",
                        "Total", "Paid", "Balance", "Status"],
            "rows": rows, "totals": None}


def product_sales(business, f):
    from apps.sales.models import SaleItem

    qs = SaleItem.objects.for_business(business).filter(
        sale__in=_sales_base(business, f))
    if f.get("category_id"):
        qs = qs.filter(product__category_id=f["category_id"])
    by_product = {}
    for item in qs.select_related("product"):
        values = _net_item_values(item)
        row = by_product.setdefault(item.product.name, {
            "qty": ZERO,
            "returned": ZERO,
            "revenue": ZERO,
            "profit": ZERO,
        })
        row["qty"] += values["qty"]
        row["returned"] += values["returned"]
        row["revenue"] += values["revenue"]
        row["profit"] += values["profit"]
    rows = [[name, r["qty"], r["returned"], _money(r["revenue"]),
             _money(r["profit"])]
            for name, r in sorted(
                by_product.items(), key=lambda item: item[1]["revenue"],
                reverse=True)]
    totals = ["TOTAL", sum((r[1] or ZERO) for r in rows),
              sum((r[2] or ZERO) for r in rows),
              sum((r[3] or ZERO) for r in rows), sum((r[4] or ZERO) for r in rows)]
    return {"columns": ["Product", "Qty sold", "Qty returned", "Revenue", "Gross profit"],
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
    from apps.sales.models import SalePayment

    qs = (
        SalePayment.objects.for_business(business)
        .filter(sale__in=_sales_base(business, f))
        .values("method__name")
        .annotate(count=Count("id"), total=Sum("amount"))
        .order_by("-total")
    )
    rows = [[r["method__name"], r["count"], r["total"]] for r in qs]
    totals = ["TOTAL", sum(r[1] for r in rows), sum((r[2] or ZERO) for r in rows)]
    return {"columns": ["Payment method", "Transactions", "Amount"],
            "rows": rows, "totals": totals if rows else None}


def voided_sales(business, f):
    qs = _sales_base(business, f, exclude_voided=False).filter(status="voided")
    rows = [[s.invoice_number, s.sale_date.strftime("%Y-%m-%d %H:%M"),
             s.total, s.voided_by.full_name if s.voided_by else "",
             s.void_reason] for s in qs]
    return {"columns": ["Invoice", "Date", "Total", "Voided by", "Reason"],
            "rows": rows, "totals": None}


def returns_report(business, f):
    from apps.sales.models import SaleReturn

    qs = SaleReturn.objects.for_business(business).select_related(
        "sale", "customer", "processed_by")
    if f.get("date_from"):
        qs = qs.filter(created_at__date__gte=f["date_from"])
    if f.get("date_to"):
        qs = qs.filter(created_at__date__lte=f["date_to"])
    if f.get("branch_id"):
        qs = qs.filter(branch_id=f["branch_id"])
    rows = [[r.return_number, r.sale.invoice_number, r.customer.full_name,
             r.get_refund_method_display(), r.refund_amount,
             r.created_at.strftime("%Y-%m-%d")] for r in qs]
    totals = ["TOTAL", "", "", "", sum((r[4] or ZERO) for r in rows), ""]
    return {"columns": ["Return #", "Invoice", "Customer", "Refund method",
                        "Refund", "Date"],
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
    return {"columns": ["Tax rate", "Taxable amount", "Tax collected"],
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
    from apps.customers.models import Customer

    qs = Customer.objects.for_business(business).filter(balance__gt=0).order_by("-balance")
    rows = [[c.full_name, c.code, c.mobile, c.credit_limit, c.balance] for c in qs]
    totals = ["TOTAL", "", "", "", sum((r[4] or ZERO) for r in rows)]
    return {"columns": ["Customer", "Code", "Mobile", "Credit limit", "Balance owed"],
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
        .prefetch_related("returns", "items")
    ):
        key = (sale.customer.full_name, sale.customer.code)
        row = by_customer.setdefault(key, {
            "invoices": 0,
            "total": ZERO,
            "paid": ZERO,
            "balance": ZERO,
            "profit": ZERO,
        })
        row["invoices"] += 1
        row["total"] += sale.net_total
        row["paid"] += sale.net_amount_paid
        row["balance"] += sale.balance
        row["profit"] += _net_sale_profit(sale)
    rows = [[name, code, r["invoices"], _money(r["total"]), _money(r["paid"]),
             _money(r["balance"]), _money(r["profit"])]
            for (name, code), r in sorted(
                by_customer.items(), key=lambda item: item[1]["total"],
                reverse=True)[:1000]]
    totals = ["TOTAL", "", sum(r[2] for r in rows),
              sum((r[3] or ZERO) for r in rows), sum((r[4] or ZERO) for r in rows),
              sum((r[5] or ZERO) for r in rows), sum((r[6] or ZERO) for r in rows)]
    return {"columns": ["Customer", "Code", "Invoices", "Total", "Paid",
                        "Balance", "Gross profit"],
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
