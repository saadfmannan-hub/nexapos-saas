from datetime import date as date_cls
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Avg, Count, F, Q, Sum
from django.db.models.functions import ExtractHour, TruncDate
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.audit import services as audit
from apps.core.decorators import business_required, require_permission

from . import exports
from .queries import REPORT_GROUPS, REPORTS

ZERO = Decimal("0")


def _parse_filters(request):
    f = {}
    date_from = request.GET.get("from", "")
    date_to = request.GET.get("to", "")
    f["date_from"] = date_from or None
    f["date_to"] = date_to or None
    branch = request.GET.get("branch", "")
    f["branch_id"] = int(branch) if branch.isdigit() else None
    warehouse = request.GET.get("warehouse", "")
    f["warehouse_id"] = int(warehouse) if warehouse.isdigit() else None
    product = request.GET.get("product", "")
    f["product_id"] = int(product) if product.isdigit() else None
    classification = request.GET.get("garment_classification", "").strip().lower()
    f["garment_classification"] = (
        classification if classification in ("adult", "child") else None
    )
    return f


def _default_range(request):
    """Default: this month."""
    today = timezone.localdate()
    return today.replace(day=1), today


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@require_permission("dashboard.view")
def dashboard(request):
    from apps.core.money import money
    from apps.customers.models import Customer
    from apps.expenses.models import Expense
    from apps.inventory import services as inventory
    from apps.inventory.models import StockLevel
    from apps.sales.models import PaymentMethod, Sale, SaleItem, SalePayment, SaleReturn
    from apps.suppliers.models import Supplier

    business = request.business
    today = timezone.localdate()
    month_start = today.replace(day=1)
    week_start = today - timedelta(days=today.weekday())
    date_from = request.GET.get("from") or str(month_start)
    date_to = request.GET.get("to") or str(today)
    branch_id = request.GET.get("branch", "")

    sales = Sale.objects.for_business(business).exclude(
        status__in=["draft", "voided"])
    period = sales.filter(sale_date__date__gte=date_from,
                          sale_date__date__lte=date_to)
    if branch_id.isdigit():
        period = period.filter(branch_id=branch_id)

    real_payment_kinds = (
        PaymentMethod.Kind.CASH,
        PaymentMethod.Kind.CARD,
        PaymentMethod.Kind.BANK,
    )

    def payments_for_range(start, end):
        qs = SalePayment.objects.for_business(business).filter(
            payment_date__gte=start,
            payment_date__lte=end,
        )
        qs = qs.exclude(sale__status__in=["draft", "voided"])
        if branch_id.isdigit():
            qs = qs.filter(sale__branch_id=branch_id)
        return qs

    def payment_totals(payment_qs):
        totals = {"cash": ZERO, "card": ZERO, "bank": ZERO, "credit": ZERO}
        for row in (
            payment_qs.values("method__kind")
            .annotate(total=Sum("amount"))
        ):
            kind = row["method__kind"]
            amount = row["total"] or ZERO
            if kind == PaymentMethod.Kind.CASH:
                totals["cash"] += amount
            elif kind == PaymentMethod.Kind.CARD:
                totals["card"] += amount
            elif kind == PaymentMethod.Kind.BANK:
                totals["bank"] += amount
            elif kind == PaymentMethod.Kind.CUSTOMER_CREDIT:
                totals["credit"] += amount
        totals["income"] = totals["cash"] + totals["card"] + totals["bank"]
        return {key: money(value) for key, value in totals.items()}

    today_sales_qs = sales.filter(sale_date__date=today)
    today_returns_qs = SaleReturn.objects.for_business(business).filter(
        created_at__date=today)
    if branch_id.isdigit():
        today_sales_qs = today_sales_qs.filter(branch_id=branch_id)
        today_returns_qs = today_returns_qs.filter(branch_id=branch_id)
    today_sales = today_sales_qs.aggregate(t=Sum("total"))["t"] or ZERO
    today_returns = today_returns_qs.aggregate(t=Sum("refund_amount"))["t"] or ZERO
    today_receivable = sum((sale.balance for sale in today_sales_qs), ZERO)
    today_payments = payment_totals(payments_for_range(today, today))
    period_payments = payment_totals(payments_for_range(date_from, date_to))

    show_profit = request.membership.has_perm("profit.view")
    agg = period.aggregate(
        sum_total=Sum("total"), count=Count("id"), avg=Avg("total"),
        profit=Sum("gross_profit"), sum_subtotal=Sum("subtotal"),
        paid=Sum("amount_paid"),
    )
    agg["total"] = agg.pop("sum_total")
    agg["subtotal"] = agg.pop("sum_subtotal")
    credit_outstanding = (
        Customer.objects.for_business(business).aggregate(t=Sum("balance"))["t"] or ZERO
    )
    payables = (
        Supplier.objects.for_business(business).aggregate(t=Sum("balance"))["t"] or ZERO
    )
    expenses_qs = Expense.objects.for_business(business).exclude(
        status__in=["rejected", "cancelled"]
    ).filter(expense_date__gte=date_from, expense_date__lte=date_to)
    if branch_id.isdigit():
        expenses_qs = expenses_qs.filter(branch_id=branch_id)
    expenses_total = expenses_qs.aggregate(t=Sum("amount"))["t"] or ZERO
    period_returns_qs = SaleReturn.objects.for_business(business).filter(
        created_at__date__gte=date_from, created_at__date__lte=date_to)
    if branch_id.isdigit():
        period_returns_qs = period_returns_qs.filter(branch_id=branch_id)
    returns_total = period_returns_qs.aggregate(t=Sum("refund_amount"))["t"] or ZERO
    low_stock_qs = StockLevel.objects.for_business(business).filter(
        product__reorder_level__gt=0,
        quantity__lte=F("product__reorder_level"),
    )
    if branch_id.isdigit():
        low_stock_qs = low_stock_qs.filter(warehouse__branch_id=branch_id)
    low_stock_count = low_stock_qs.count()
    stock_value = inventory.stock_value(business) if show_profit else None
    gross = agg["profit"] or ZERO
    margin = (gross / agg["subtotal"] * 100) if (agg["subtotal"] or 0) > 0 else ZERO

    delivery_counts = {
        "booked": period.count(),
        "in_process": period.filter(delivery_status=Sale.DeliveryStatus.IN_PRODUCTION).count(),
        "finished": period.filter(delivery_status=Sale.DeliveryStatus.READY).count(),
        "ready": period.filter(delivery_status=Sale.DeliveryStatus.READY).count(),
        "pending_delivery": period.filter(
            delivery_status__in=[
                Sale.DeliveryStatus.PENDING,
                Sale.DeliveryStatus.IN_PRODUCTION,
                Sale.DeliveryStatus.READY,
            ]
        ).count(),
    }

    # ---- chart datasets (real data) ---------------------------------------
    trend = (
        period.annotate(day=TruncDate("sale_date")).values("day")
        .annotate(total=Sum("total"), profit=Sum("gross_profit"))
        .order_by("day")
    )
    income_trend = (
        payments_for_range(date_from, date_to)
        .filter(method__kind__in=real_payment_kinds)
        .values("payment_date")
        .annotate(total=Sum("amount"))
        .order_by("payment_date")
    )
    # Zero-fill the full selected range so the trend is a daily series
    # (days without sales plot as 0), not one dot per day that had sales.
    d_from = date_cls.fromisoformat(str(date_from))
    d_to = date_cls.fromisoformat(str(date_to))
    by_day = {str(r["day"]): r for r in trend}
    income_by_day = {str(r["payment_date"]): r["total"] or ZERO for r in income_trend}
    iso_labels, pretty_labels = [], []
    sales_series, income_series, profit_series = [], [], []
    day = d_from
    # Safety cap: ranges beyond ~2 years fall back to sales-days only.
    fill_daily = (d_to - d_from).days <= 750
    if fill_daily:
        while day <= d_to:
            key = day.isoformat()
            row = by_day.get(key)
            iso_labels.append(key)
            pretty_labels.append(day.strftime("%b %d"))
            sales_series.append(float(row["total"] or 0) if row else 0.0)
            income_series.append(float(income_by_day.get(key, ZERO)))
            profit_series.append(float(row["profit"] or 0) if row else 0.0)
            day += timedelta(days=1)
    else:
        for r in trend:
            key = str(r["day"])
            iso_labels.append(key)
            pretty_labels.append(r["day"].strftime("%b %d"))
            sales_series.append(float(r["total"] or 0))
            income_series.append(float(income_by_day.get(key, ZERO)))
            profit_series.append(float(r["profit"] or 0))
    chart_trend = {
        "labels": pretty_labels,
        "sales": sales_series,
        "income": income_series,
        "profit": profit_series if show_profit else [],
    }
    method_rows = [
        ("Cash", period_payments["cash"]),
        ("Card", period_payments["card"]),
        ("Bank Transfer", period_payments["bank"]),
        ("Customer Credit", period_payments["credit"]),
    ]
    chart_methods = {
        "labels": [name for name, _amount in method_rows],
        "data": [float(amount or 0) for _name, amount in method_rows],
    }
    top_products = []
    for item in (
        SaleItem.objects.for_business(business)
        .filter(sale__in=period)
        .values("product_name", "sku")
        .annotate(qty=Sum("quantity"), sales=Sum("line_total"),
                  profit=Sum("gross_profit"))
        .order_by("-sales")[:8]
    ):
        top_products.append({
            "product": item["product_name"],
            "sku": item["sku"] or "-",
            "qty": item["qty"] or ZERO,
            "sales": item["sales"] or ZERO,
            "profit": item["profit"] or ZERO,
        })
    chart_products = {
        "labels": [r["product"][:24] for r in top_products],
        "data": [float(r["sales"] or 0) for r in top_products],
    }
    by_branch = (
        period.values("branch__name").annotate(t=Sum("total")).order_by("-t")
    )
    chart_branches = {
        "labels": [r["branch__name"] for r in by_branch],
        "data": [float(r["t"] or 0) for r in by_branch],
    }

    # ---- previous-period comparison (trend %) ------------------------------
    def _pct(cur, prev):
        if prev in (None, 0) or prev == ZERO:
            return None
        return round(float((cur - prev) / prev * 100), 1)

    span = (d_to - d_from).days + 1
    prev_from, prev_to = d_from - timedelta(days=span), d_from - timedelta(days=1)
    prev = sales.filter(sale_date__date__gte=prev_from, sale_date__date__lte=prev_to)
    if branch_id.isdigit():
        prev = prev.filter(branch_id=branch_id)
    prev_agg = prev.aggregate(t=Sum("total"), p=Sum("gross_profit"), c=Count("id"))
    prev_expenses = Expense.objects.for_business(business).exclude(
        status__in=["rejected", "cancelled"]
    ).filter(expense_date__gte=prev_from, expense_date__lte=prev_to).aggregate(
        t=Sum("amount"))["t"] or ZERO
    prev_total = prev_agg["t"] or ZERO
    prev_profit = prev_agg["p"] or ZERO
    yesterday_sales = sales.filter(
        sale_date__date=today - timedelta(days=1)
    ).aggregate(t=Sum("total"))["t"] or ZERO
    trends = {
        "today_sales": _pct(today_sales, yesterday_sales),
        "period_sales": _pct(agg["total"] or ZERO, prev_total),
        "gross_profit": _pct(gross, prev_profit),
        "net_profit": _pct(gross - expenses_total, prev_profit - prev_expenses),
        "invoices": _pct(Decimal(agg["count"] or 0), Decimal(prev_agg["c"] or 0)),
    }

    # ---- sparklines (daily series for the period) --------------------------
    spark_sales = chart_trend["sales"]
    spark_profit = chart_trend["profit"]
    exp_daily = {
        str(r["expense_date"]): float(r["t"] or 0)
        for r in expenses_qs.values("expense_date").annotate(t=Sum("amount"))
    }
    spark_expenses = [exp_daily.get(label, 0) for label in iso_labels]

    # ---- extra interactive charts ------------------------------------------
    hourly = (
        period.annotate(h=ExtractHour("sale_date")).values("h")
        .annotate(t=Sum("total")).order_by("h")
    )
    hour_map = {r["h"]: float(r["t"] or 0) for r in hourly}
    chart_hourly = {"labels": [f"{h:02d}" for h in range(24)],
                    "data": [hour_map.get(h, 0) for h in range(24)]}

    from apps.inventory.models import StockMovement

    move_start = today - timedelta(days=13)
    movements = (
        StockMovement.objects.for_business(business)
        .filter(created_at__date__gte=move_start)
        .annotate(day=TruncDate("created_at")).values("day")
        .annotate(qin=Sum("quantity", filter=Q(quantity__gt=0)),
                  qout=Sum("quantity", filter=Q(quantity__lt=0)))
        .order_by("day")
    )
    chart_movement = {
        "labels": [str(r["day"]) for r in movements],
        "stock_in": [float(r["qin"] or 0) for r in movements],
        "stock_out": [abs(float(r["qout"] or 0)) for r in movements],
    }
    top_customers = []
    for customer in (
        period.exclude(customer__is_walk_in=True)
        .values("customer__full_name", "customer__mobile")
        .annotate(sales=Sum("total"), paid=Sum("amount_paid"))
        .order_by("-sales")[:8]
    ):
        sales_total = customer["sales"] or ZERO
        paid_total = customer["paid"] or ZERO
        top_customers.append({
            "customer": customer["customer__full_name"],
            "phone": customer["customer__mobile"] or "-",
            "sales": sales_total,
            "paid": paid_total,
            "receivable": money(sales_total - paid_total),
        })
    chart_customers = {
        "labels": [r["customer"][:22] for r in top_customers],
        "data": [float(r["sales"] or 0) for r in top_customers],
    }

    # ---- activity widgets ---------------------------------------------------
    from apps.customers.models import Customer as CustomerModel
    from apps.purchases.models import Purchase
    from apps.suppliers.models import Supplier as SupplierModel

    widgets = {
        "recent_sales": sales.select_related("customer").order_by("-sale_date")[:8],
        "recent_expenses": Expense.objects.for_business(business)
            .exclude(status__in=["rejected", "cancelled"])
            .select_related("category").order_by("-expense_date", "-created_at")[:5],
        "pending_receivables": CustomerModel.objects.for_business(business)
            .filter(balance__gt=0).order_by("-balance")[:5],
        "pending_payables": SupplierModel.objects.for_business(business)
            .filter(balance__gt=0).order_by("-balance")[:5],
        "low_stock_items": low_stock_qs.select_related("product", "warehouse")[:8],
        "awaiting_pos": Purchase.objects.for_business(business)
            .filter(status__in=["order", "partially_received"])
            .select_related("supplier").order_by("-purchase_date")[:5],
    }

    from apps.branches.models import Branch

    return render(request, "dashboard/index.html", {
        "active_nav": "dashboard",
        "date_from": date_from, "date_to": date_to,
        "range_presets": {
            "today": {"from": str(today), "to": str(today)},
            "week": {"from": str(week_start), "to": str(today)},
            "month": {"from": str(month_start), "to": str(today)},
        },
        "branches": Branch.objects.for_business(business).filter(is_active=True),
        "kpis": {
            "today_sales": today_sales,
            "today_income": today_payments["income"],
            "today_receivable": money(today_receivable),
            "today_returns": today_returns,
            "today_net_sales": money(today_sales - today_returns),
            "cash": today_payments["cash"],
            "card": today_payments["card"],
            "bank": today_payments["bank"],
            "period_sales": agg["total"] or ZERO,
            "period_income": period_payments["income"],
            "period_credit": period_payments["credit"],
            "invoices": agg["count"] or 0,
            "avg_invoice": agg["avg"] or ZERO,
            "collected": agg["paid"] or ZERO,
            "gross_profit": gross if show_profit else None,
            "margin": margin if show_profit else None,
            "expenses": expenses_total,
            "net_profit": (gross - expenses_total) if show_profit else None,
            "receivables": credit_outstanding,
            "payables": payables,
            "stock_value": stock_value,
            "low_stock": low_stock_count,
            "returns": returns_total,
        },
        "chart_trend": chart_trend, "chart_methods": chart_methods,
        "chart_products": chart_products, "chart_branches": chart_branches,
        "chart_hourly": chart_hourly, "chart_movement": chart_movement,
        "chart_customers": chart_customers,
        "delivery_counts": delivery_counts,
        "top_products_table": top_products,
        "top_customers_table": top_customers,
        "trends": trends,
        "sparks": {"sales": spark_sales, "profit": spark_profit,
                   "expenses": spark_expenses},
        "widgets": widgets,
        "show_profit": show_profit,
        "onboarding_pending": not request.business.onboarding_completed,
    })


# ---------------------------------------------------------------------------
# Reports center
# ---------------------------------------------------------------------------
@require_permission("reports.view")
def index(request):
    groups = []
    for group_name, keys in REPORT_GROUPS:
        items = []
        for key in keys:
            title, _fn, perm = REPORTS[key]
            if request.membership.has_perm(perm):
                items.append({"key": key, "title": title})
        if items:
            groups.append({"name": group_name, "items": items})
    return render(request, "reports/index.html",
                  {"groups": groups, "active_nav": "reports"})


def _run_report(request, key):
    if key not in REPORTS:
        raise Http404
    title, fn, perm = REPORTS[key]
    if not request.membership.has_perm(perm):
        raise PermissionDenied
    filters = _parse_filters(request)
    if filters["date_from"] is None and key not in (
        "current_stock", "low_stock", "supplier_balances", "receivables"
    ):
        start, end = _default_range(request)
        filters["date_from"], filters["date_to"] = str(start), str(end)
    data = fn(request.business, filters)
    return title, data, filters


@business_required
def report_view(request, key):
    from apps.branches.models import Branch, Warehouse
    from apps.catalog.models import Product

    title, data, filters = _run_report(request, key)
    export = request.GET.get("export", "")
    if export:
        if not request.membership.has_perm("reports.export"):
            raise PermissionDenied
        audit.log("report.exported", request=request, module="reports",
                  description=f"Exported report '{key}' as {export}.")
        label = f"{filters.get('date_from') or ''} → {filters.get('date_to') or ''}"
        if export == "csv":
            return exports.export_csv(key, data)
        if export == "xlsx":
            return exports.export_xlsx(key, data)
        if export == "pdf":
            return exports.export_pdf(title, data, request.business, label)
        messages.error(request, "Unknown export format.")
        return redirect("reports:view", key=key)
    return render(request, "reports/report.html", {
        "key": key, "title": title, "data": data, "filters": filters,
        "active_nav": "reports",
        "branches": Branch.objects.for_business(request.business).filter(is_active=True),
        "warehouses": Warehouse.objects.for_business(request.business).filter(is_active=True),
        "products": (
            Product.objects.for_business(request.business).only("id", "name").order_by("name")
            if key == "sales_detailed" else []
        ),
        "can_export": request.membership.has_perm("reports.export"),
    })
