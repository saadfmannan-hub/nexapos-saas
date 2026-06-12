from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Avg, Count, F, Sum
from django.db.models.functions import TruncDate
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
    from apps.customers.models import Customer
    from apps.expenses.models import Expense
    from apps.inventory import services as inventory
    from apps.inventory.models import StockLevel
    from apps.sales.models import Sale, SaleItem, SalePayment, SaleReturn
    from apps.suppliers.models import Supplier

    business = request.business
    today = timezone.localdate()
    month_start = today.replace(day=1)
    date_from = request.GET.get("from") or str(month_start)
    date_to = request.GET.get("to") or str(today)
    branch_id = request.GET.get("branch", "")

    sales = Sale.objects.for_business(business).exclude(
        status__in=["draft", "voided"])
    period = sales.filter(sale_date__date__gte=date_from,
                          sale_date__date__lte=date_to)
    if branch_id.isdigit():
        period = period.filter(branch_id=branch_id)

    show_profit = request.membership.has_perm("profit.view")
    agg = period.aggregate(
        sum_total=Sum("total"), count=Count("id"), avg=Avg("total"),
        profit=Sum("gross_profit"), sum_subtotal=Sum("subtotal"),
        paid=Sum("amount_paid"),
    )
    agg["total"] = agg.pop("sum_total")
    agg["subtotal"] = agg.pop("sum_subtotal")
    today_sales = sales.filter(sale_date__date=today).aggregate(t=Sum("total"))["t"] or ZERO
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
    returns_total = (
        SaleReturn.objects.for_business(business)
        .filter(created_at__date__gte=date_from, created_at__date__lte=date_to)
        .aggregate(t=Sum("refund_amount"))["t"] or ZERO
    )
    low_stock_count = (
        StockLevel.objects.for_business(business)
        .filter(product__reorder_level__gt=0,
                quantity__lte=F("product__reorder_level")).count()
    )
    stock_value = inventory.stock_value(business) if show_profit else None
    gross = agg["profit"] or ZERO
    margin = (gross / agg["subtotal"] * 100) if (agg["subtotal"] or 0) > 0 else ZERO

    # ---- chart datasets (real data) ---------------------------------------
    trend = (
        period.annotate(day=TruncDate("sale_date")).values("day")
        .annotate(total=Sum("total"), profit=Sum("gross_profit"))
        .order_by("day")
    )
    chart_trend = {
        "labels": [str(r["day"]) for r in trend],
        "sales": [float(r["total"] or 0) for r in trend],
        "profit": [float(r["profit"] or 0) for r in trend] if show_profit else [],
    }
    by_method = (
        SalePayment.objects.for_business(business)
        .filter(sale__in=period).values("method__name")
        .annotate(t=Sum("amount")).order_by("-t")
    )
    chart_methods = {
        "labels": [r["method__name"] for r in by_method],
        "data": [float(r["t"] or 0) for r in by_method],
    }
    top_products = (
        SaleItem.objects.for_business(business)
        .filter(sale__in=period).values("product__name")
        .annotate(t=Sum("line_total")).order_by("-t")[:8]
    )
    chart_products = {
        "labels": [r["product__name"][:24] for r in top_products],
        "data": [float(r["t"] or 0) for r in top_products],
    }
    by_branch = (
        period.values("branch__name").annotate(t=Sum("total")).order_by("-t")
    )
    chart_branches = {
        "labels": [r["branch__name"] for r in by_branch],
        "data": [float(r["t"] or 0) for r in by_branch],
    }

    from apps.branches.models import Branch

    return render(request, "dashboard/index.html", {
        "active_nav": "dashboard",
        "date_from": date_from, "date_to": date_to,
        "branches": Branch.objects.for_business(business).filter(is_active=True),
        "kpis": {
            "today_sales": today_sales,
            "period_sales": agg["total"] or ZERO,
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
        "can_export": request.membership.has_perm("reports.export"),
    })
