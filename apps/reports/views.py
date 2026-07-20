from datetime import date as date_cls
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.db.models import Avg, Count, F, Q, Sum
from django.db.models.functions import ExtractHour, TruncDate
from django.http import Http404
from django.shortcuts import redirect, render

from apps.audit import services as audit
from apps.core.date_ranges import (
    business_localdate,
    business_timezone,
    date_range_querystring,
    filter_business_date_range,
    resolve_date_range,
)
from apps.core.decorators import business_required
from apps.subscriptions.access import (
    AccessAction,
    evaluate_access,
    require_access,
)
from apps.subscriptions.decorators import module_permission_required

from . import exports
from .queries import (
    REPORT_GROUPS,
    REPORT_REQUIRED_MODULES,
    REPORTS,
    SALES_REPORT_KEYS,
    current_year_financial_summary,
)

ZERO = Decimal("0")


def _parse_filters(request):
    from apps.branches.models import Branch, Warehouse
    from apps.suppliers.models import SupplierPayment

    f = {}
    f["date_from"], f["date_to"] = resolve_date_range(
        request.GET,
        request.business,
    )
    branch = request.GET.get("branch", "")
    if branch and not branch.isdigit():
        raise Http404
    branch_id = int(branch) if branch.isdigit() else None
    warehouse = request.GET.get("warehouse", "")
    if warehouse and not warehouse.isdigit():
        raise Http404
    warehouse_id = int(warehouse) if warehouse.isdigit() else None
    allowed_branches = request.membership.allowed_branch_ids
    allowed_warehouses = request.membership.allowed_warehouse_ids
    if branch_id is not None:
        branch_qs = Branch.objects.for_business(request.business)
        if allowed_branches is not None:
            branch_qs = branch_qs.filter(pk__in=allowed_branches)
        if not branch_qs.filter(pk=branch_id).exists():
            raise Http404
    if warehouse_id is not None:
        warehouse_qs = Warehouse.objects.for_business(request.business)
        if allowed_branches is not None:
            warehouse_qs = warehouse_qs.filter(branch_id__in=allowed_branches)
        elif allowed_warehouses is not None:
            warehouse_qs = warehouse_qs.filter(pk__in=allowed_warehouses)
        warehouse_obj = warehouse_qs.filter(pk=warehouse_id).first()
        if warehouse_obj is None or (
            branch_id is not None and warehouse_obj.branch_id != branch_id
        ):
            raise Http404
    f["branch_id"] = branch_id
    f["warehouse_id"] = warehouse_id
    product = request.GET.get("product", "")
    f["product_id"] = int(product) if product.isdigit() else None
    brand = request.GET.get("brand", "")
    f["brand_id"] = int(brand) if brand.isdigit() else None
    classification = request.GET.get("garment_classification", "").strip().lower()
    f["garment_classification"] = (
        classification if classification in ("adult", "child") else None
    )
    supplier = request.GET.get("supplier", "")
    f["supplier_id"] = int(supplier) if supplier.isdigit() else None
    payment_method = request.GET.get("method", "").strip().lower()
    valid_methods = {value for value, _label in SupplierPayment.Method.choices}
    f["payment_method"] = payment_method if payment_method in valid_methods else None
    cheque_status = request.GET.get("cheque_status", "").strip().lower()
    valid_statuses = {value for value, _label in SupplierPayment.ChequeStatus.choices}
    f["cheque_status"] = cheque_status if cheque_status in valid_statuses else None
    return f


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@module_permission_required(
    "pos_core",
    "dashboard.view",
    action=AccessAction.READ,
)
def dashboard(request):
    from apps.core.money import money
    from apps.customers.models import Customer
    from apps.expenses.models import Expense
    from apps.inventory import services as inventory
    from apps.inventory.models import StockLevel
    from apps.sales.models import PaymentMethod, Sale, SaleItem, SalePayment, SaleReturn
    from apps.suppliers.models import Supplier

    business = request.business
    inventory_decision = evaluate_access(
        request,
        "inventory",
        permission_code="inventory.view",
        action=AccessAction.READ,
    )
    inventory_access = inventory_decision.allowed
    suppliers_access = evaluate_access(
        request,
        "suppliers",
        permission_code="suppliers.view",
        action=AccessAction.READ,
    ).allowed
    purchases_access = evaluate_access(
        request,
        "purchases",
        permission_code="purchases.view",
        action=AccessAction.READ,
    ).allowed
    expenses_access = evaluate_access(
        request,
        "expenses",
        permission_code="expenses.view",
        action=AccessAction.READ,
    ).allowed
    customer_credit_access = evaluate_access(
        request,
        "customer_credit",
        permission_code="customers.view",
        action=AccessAction.READ,
    ).allowed
    tailoring_access = evaluate_access(
        request,
        "tailoring",
        action=AccessAction.READ,
    ).allowed
    advanced_reports_access = evaluate_access(
        request,
        "advanced_reports",
        permission_code="reports.view",
        action=AccessAction.READ,
    ).allowed
    inventory_allowed_branches = request.membership.allowed_branch_ids
    if inventory_access and inventory_allowed_branches is not None:
        from apps.branches.models import Warehouse

        inventory_warehouse_ids = set(
            Warehouse.objects.for_business(business)
            .filter(branch_id__in=inventory_allowed_branches)
            .values_list("id", flat=True)
        )
    elif inventory_access:
        inventory_warehouse_ids = None
    else:
        inventory_warehouse_ids = frozenset()
    today = business_localdate(business)
    month_start = today.replace(day=1)
    week_start = today - timedelta(days=today.weekday())
    date_from, date_to = resolve_date_range(request.GET, business)
    branch_id = request.GET.get("branch", "")

    allowed_branch_ids = request.membership.allowed_branch_ids
    if branch_id:
        from apps.branches.models import Branch

        if not branch_id.isdigit():
            raise Http404
        branch_qs = Branch.objects.for_business(business).filter(is_active=True)
        if allowed_branch_ids is not None:
            branch_qs = branch_qs.filter(pk__in=allowed_branch_ids)
        if not branch_qs.filter(pk=int(branch_id)).exists():
            raise Http404
    allowed_warehouse_ids = request.membership.allowed_warehouse_ids
    sales = (
        Sale.objects.for_business(business)
        .exclude(status__in=["draft", "voided"])
        .filter(warehouse__business=business)
    )
    if allowed_branch_ids is not None:
        sales = sales.filter(branch_id__in=allowed_branch_ids)
    if allowed_warehouse_ids is not None:
        sales = sales.filter(warehouse_id__in=allowed_warehouse_ids)
    period = filter_business_date_range(
        sales,
        business,
        field_name="sale_date",
        date_from=date_from,
        date_to=date_to,
    )
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
        qs = qs.filter(sale__warehouse__business=business)
        if allowed_branch_ids is not None:
            qs = qs.filter(sale__branch_id__in=allowed_branch_ids)
        if allowed_warehouse_ids is not None:
            qs = qs.filter(sale__warehouse_id__in=allowed_warehouse_ids)
        if not customer_credit_access:
            qs = qs.exclude(method__kind=PaymentMethod.Kind.CUSTOMER_CREDIT)
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

    today_sales_qs = filter_business_date_range(
        sales, business, field_name="sale_date", date_from=today, date_to=today
    )
    today_returns_qs = filter_business_date_range(
        SaleReturn.objects.for_business(business),
        business,
        field_name="created_at",
        date_from=today,
        date_to=today,
    )
    if allowed_branch_ids is not None:
        today_returns_qs = today_returns_qs.filter(branch_id__in=allowed_branch_ids)
    if allowed_warehouse_ids is not None:
        today_returns_qs = today_returns_qs.filter(
            warehouse_id__in=allowed_warehouse_ids
        )
    if branch_id.isdigit():
        today_sales_qs = today_sales_qs.filter(branch_id=branch_id)
        today_returns_qs = today_returns_qs.filter(branch_id=branch_id)
    today_sales = today_sales_qs.aggregate(t=Sum("total"))["t"] or ZERO
    today_returns = today_returns_qs.aggregate(t=Sum("refund_amount"))["t"] or ZERO
    today_receivable = (
        sum((sale.balance for sale in today_sales_qs), ZERO)
        if customer_credit_access
        else None
    )
    today_payments = payment_totals(payments_for_range(today, today))
    period_payments = payment_totals(payments_for_range(date_from, date_to))

    show_profit = request.membership.has_perm("profit.view")
    selected_branch_id = int(branch_id) if branch_id.isdigit() else None
    current_year = current_year_financial_summary(
        business,
        request.membership,
        branch_id=selected_branch_id,
        today=today,
        include_profit=show_profit,
        include_expenses=expenses_access,
        include_customer_credit=customer_credit_access,
    )
    agg = period.aggregate(
        sum_total=Sum("total"), count=Count("id"), avg=Avg("total"),
        profit=Sum("gross_profit"), sum_subtotal=Sum("subtotal"),
        paid=Sum("amount_paid"),
    )
    agg["total"] = agg.pop("sum_total")
    agg["subtotal"] = agg.pop("sum_subtotal")
    credit_outstanding = None
    if customer_credit_access:
        customer_qs = Customer.objects.for_business(business)
        if allowed_branch_ids is not None:
            customer_qs = customer_qs.filter(home_branch_id__in=allowed_branch_ids)
        if branch_id.isdigit():
            customer_qs = customer_qs.filter(home_branch_id=int(branch_id))
        credit_outstanding = customer_qs.aggregate(t=Sum("balance"))["t"] or ZERO
    payables = ZERO
    if suppliers_access:
        payables = (
            Supplier.objects.for_business(business).aggregate(t=Sum("balance"))["t"]
            or ZERO
        )
    expenses_qs = Expense.objects.none()
    expenses_total = None
    if expenses_access:
        expenses_qs = Expense.objects.for_business(business).exclude(
            status__in=["rejected", "cancelled"]
        ).filter(expense_date__gte=date_from, expense_date__lte=date_to)
        if branch_id.isdigit():
            expenses_qs = expenses_qs.filter(branch_id=branch_id)
        if allowed_branch_ids is not None:
            expenses_qs = expenses_qs.filter(branch_id__in=allowed_branch_ids)
        expenses_total = expenses_qs.aggregate(t=Sum("amount"))["t"] or ZERO
    period_returns_qs = filter_business_date_range(
        SaleReturn.objects.for_business(business),
        business,
        field_name="created_at",
        date_from=date_from,
        date_to=date_to,
    )
    if allowed_branch_ids is not None:
        period_returns_qs = period_returns_qs.filter(branch_id__in=allowed_branch_ids)
    if allowed_warehouse_ids is not None:
        period_returns_qs = period_returns_qs.filter(
            warehouse_id__in=allowed_warehouse_ids
        )
    if branch_id.isdigit():
        period_returns_qs = period_returns_qs.filter(branch_id=branch_id)
    returns_total = period_returns_qs.aggregate(t=Sum("refund_amount"))["t"] or ZERO
    if inventory_access:
        low_stock_qs = StockLevel.objects.for_business(business).filter(
            product__reorder_level__gt=0,
            quantity__lte=F("product__reorder_level"),
        )
        if not tailoring_access:
            low_stock_qs = low_stock_qs.filter(product__is_tailoring_item=False)
        if inventory_warehouse_ids is not None:
            low_stock_qs = low_stock_qs.filter(
                warehouse_id__in=inventory_warehouse_ids
            )
        if branch_id.isdigit():
            low_stock_qs = low_stock_qs.filter(warehouse__branch_id=branch_id)
        low_stock_count = low_stock_qs.count()
        stock_value = (
            inventory.stock_value(
                business,
                allowed_warehouse_ids=inventory_warehouse_ids,
                include_tailoring=tailoring_access,
            )
            if show_profit
            else None
        )
    else:
        low_stock_qs = StockLevel.objects.none()
        low_stock_count = 0
        stock_value = None
    gross = agg["profit"] or ZERO
    margin = (gross / agg["subtotal"] * 100) if (agg["subtotal"] or 0) > 0 else ZERO

    if tailoring_access:
        delivery_counts = {
            "booked": period.count(),
            "in_process": period.filter(
                delivery_status=Sale.DeliveryStatus.IN_PRODUCTION
            ).count(),
            "finished": period.filter(
                delivery_status=Sale.DeliveryStatus.READY
            ).count(),
            "ready": period.filter(
                delivery_status=Sale.DeliveryStatus.READY
            ).count(),
            "pending_delivery": period.filter(
                delivery_status__in=[
                    Sale.DeliveryStatus.PENDING,
                    Sale.DeliveryStatus.IN_PRODUCTION,
                    Sale.DeliveryStatus.READY,
                ]
            ).count(),
        }
    else:
        delivery_counts = {
            "booked": 0,
            "in_process": 0,
            "finished": 0,
            "ready": 0,
            "pending_delivery": 0,
        }

    # ---- chart datasets (real data) ---------------------------------------
    trend = (
        period.annotate(
            day=TruncDate("sale_date", tzinfo=business_timezone(business))
        ).values("day")
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
    ]
    if customer_credit_access:
        method_rows.append(("Customer Credit", period_payments["credit"]))
    chart_methods = {
        "labels": [name for name, _amount in method_rows],
        "data": [float(amount or 0) for _name, amount in method_rows],
    }
    top_products = []
    if advanced_reports_access:
        for item in (
            SaleItem.objects.for_business(business)
            .filter(sale__in=period)
            .values("product_name", "sku")
            .annotate(
                qty=Sum("quantity"),
                sales=Sum("line_total"),
                profit=Sum("gross_profit"),
            )
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
    prev = filter_business_date_range(
        sales,
        business,
        field_name="sale_date",
        date_from=prev_from,
        date_to=prev_to,
    )
    if branch_id.isdigit():
        prev = prev.filter(branch_id=branch_id)
    prev_agg = prev.aggregate(t=Sum("total"), p=Sum("gross_profit"), c=Count("id"))
    prev_expenses = None
    if expenses_access:
        prev_expenses_qs = Expense.objects.for_business(business).exclude(
            status__in=["rejected", "cancelled"]
        ).filter(expense_date__gte=prev_from, expense_date__lte=prev_to)
        if branch_id.isdigit():
            prev_expenses_qs = prev_expenses_qs.filter(branch_id=branch_id)
        if allowed_branch_ids is not None:
            prev_expenses_qs = prev_expenses_qs.filter(
                branch_id__in=allowed_branch_ids
            )
        prev_expenses = prev_expenses_qs.aggregate(t=Sum("amount"))["t"] or ZERO
    prev_total = prev_agg["t"] or ZERO
    prev_profit = prev_agg["p"] or ZERO
    yesterday = today - timedelta(days=1)
    yesterday_sales = filter_business_date_range(
        sales,
        business,
        field_name="sale_date",
        date_from=yesterday,
        date_to=yesterday,
    ).aggregate(t=Sum("total"))["t"] or ZERO
    trends = {
        "today_sales": _pct(today_sales, yesterday_sales),
        "period_sales": _pct(agg["total"] or ZERO, prev_total),
        "gross_profit": _pct(gross, prev_profit),
        "net_profit": (
            _pct(gross - expenses_total, prev_profit - prev_expenses)
            if expenses_access
            else None
        ),
        "invoices": _pct(Decimal(agg["count"] or 0), Decimal(prev_agg["c"] or 0)),
    }

    # ---- sparklines (daily series for the period) --------------------------
    spark_sales = chart_trend["sales"]
    spark_profit = chart_trend["profit"]
    spark_expenses = []
    if expenses_access:
        exp_daily = {
            str(r["expense_date"]): float(r["t"] or 0)
            for r in expenses_qs.values("expense_date").annotate(t=Sum("amount"))
        }
        spark_expenses = [exp_daily.get(label, 0) for label in iso_labels]

    # ---- extra interactive charts ------------------------------------------
    hourly = (
        period.annotate(
            h=ExtractHour("sale_date", tzinfo=business_timezone(business))
        ).values("h")
        .annotate(t=Sum("total")).order_by("h")
    )
    hour_map = {r["h"]: float(r["t"] or 0) for r in hourly}
    chart_hourly = {"labels": [f"{h:02d}" for h in range(24)],
                    "data": [hour_map.get(h, 0) for h in range(24)]}

    chart_movement = {"labels": [], "stock_in": [], "stock_out": []}
    if inventory_access:
        from apps.inventory.models import StockMovement

        move_start = today - timedelta(days=13)
        movements = StockMovement.objects.for_business(business).filter(
            created_at__date__gte=move_start
        )
        if not tailoring_access:
            movements = movements.filter(product__is_tailoring_item=False)
        if inventory_warehouse_ids is not None:
            movements = movements.filter(
                warehouse_id__in=inventory_warehouse_ids
            )
        movements = (
            movements.annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(
                qin=Sum("quantity", filter=Q(quantity__gt=0)),
                qout=Sum("quantity", filter=Q(quantity__lt=0)),
            )
            .order_by("day")
        )
        chart_movement = {
            "labels": [str(r["day"]) for r in movements],
            "stock_in": [float(r["qin"] or 0) for r in movements],
            "stock_out": [abs(float(r["qout"] or 0)) for r in movements],
        }
    top_customers = []
    if advanced_reports_access:
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

    pending_payables = ()
    if suppliers_access:
        pending_payables = SupplierModel.objects.for_business(business).filter(
            balance__gt=0
        ).order_by("-balance")[:5]
    awaiting_pos = ()
    if purchases_access:
        awaiting_pos = Purchase.objects.for_business(business).filter(
            status__in=["order", "partially_received"]
        )
        allowed_purchase_branches = request.membership.allowed_branch_ids
        allowed_purchase_warehouses = request.membership.allowed_warehouse_ids
        if allowed_purchase_branches is not None:
            awaiting_pos = awaiting_pos.filter(branch_id__in=allowed_purchase_branches)
        if allowed_purchase_warehouses is not None:
            awaiting_pos = awaiting_pos.filter(
                warehouse_id__in=allowed_purchase_warehouses
            )
        awaiting_pos = awaiting_pos.select_related("supplier").order_by(
            "-purchase_date"
        )[:5]

    recent_expenses = ()
    if expenses_access:
        recent_expenses = (
            Expense.objects.for_business(business)
            .exclude(status__in=["rejected", "cancelled"])
            .select_related("category")
        )
        if allowed_branch_ids is not None:
            recent_expenses = recent_expenses.filter(
                branch_id__in=allowed_branch_ids
            )
        recent_expenses = recent_expenses.order_by(
            "-expense_date", "-created_at"
        )[:5]
    pending_receivables = ()
    if customer_credit_access:
        pending_receivables = CustomerModel.objects.for_business(business).filter(
            balance__gt=0
        )
        if allowed_branch_ids is not None:
            pending_receivables = pending_receivables.filter(
                home_branch_id__in=allowed_branch_ids
            )
        if branch_id.isdigit():
            pending_receivables = pending_receivables.filter(
                home_branch_id=int(branch_id)
            )
        pending_receivables = pending_receivables.order_by("-balance")[:5]

    widgets = {
        "recent_sales": sales.select_related("customer").order_by("-sale_date")[:8],
        "recent_expenses": recent_expenses,
        "pending_receivables": pending_receivables,
        "pending_payables": pending_payables,
        "low_stock_items": low_stock_qs.select_related("product", "warehouse")[:8],
        "awaiting_pos": awaiting_pos,
    }

    from apps.branches.models import Branch

    branches = Branch.objects.for_business(business).filter(is_active=True)
    allowed_branch_ids = request.membership.allowed_branch_ids
    if allowed_branch_ids is not None:
        branches = branches.filter(id__in=allowed_branch_ids)

    return render(request, "dashboard/index.html", {
        "active_nav": "dashboard",
        "suppliers_access": suppliers_access,
        "purchases_access": purchases_access,
        "expenses_access": expenses_access,
        "customer_credit_access": customer_credit_access,
        "advanced_reports_access": advanced_reports_access,
        "tailoring_access": tailoring_access,
        "date_from": date_from, "date_to": date_to,
        "range_presets": {
            "today": {"from": str(today), "to": str(today)},
            "week": {"from": str(week_start), "to": str(today)},
            "month": {"from": str(month_start), "to": str(today)},
        },
        "branches": branches.order_by("name"),
        "current_year": current_year,
        "kpis": {
            "today_sales": today_sales,
            "today_income": today_payments["income"],
            "today_receivable": (
                money(today_receivable) if today_receivable is not None else None
            ),
            "today_returns": today_returns,
            "today_net_sales": money(today_sales - today_returns),
            "cash": today_payments["cash"],
            "card": today_payments["card"],
            "bank": today_payments["bank"],
            "period_sales": agg["total"] or ZERO,
            "period_income": period_payments["income"],
            "period_credit": (
                period_payments["credit"] if customer_credit_access else None
            ),
            "invoices": agg["count"] or 0,
            "avg_invoice": agg["avg"] or ZERO,
            "collected": agg["paid"] or ZERO,
            "gross_profit": gross if show_profit else None,
            "margin": margin if show_profit else None,
            "expenses": expenses_total,
            "net_profit": (
                (gross - expenses_total)
                if show_profit and expenses_total is not None
                else None
            ),
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
        "inventory_access": inventory_access,
        "show_profit": show_profit,
        "onboarding_pending": not request.business.onboarding_completed,
    })


# ---------------------------------------------------------------------------
# Reports center
# ---------------------------------------------------------------------------
@module_permission_required(
    "pos_core",
    "reports.view",
    action=AccessAction.READ,
)
def index(request):
    groups = []
    for group_name, keys in REPORT_GROUPS:
        items = []
        for key in keys:
            title, _fn, perm = REPORTS[key]
            decision = evaluate_access(
                request,
                REPORT_REQUIRED_MODULES[key],
                permission_code=perm,
                action=AccessAction.READ,
            )
            sales_allowed = (
                key not in SALES_REPORT_KEYS
                or request.membership.has_perm("reports.sales")
            )
            if decision.allowed and sales_allowed:
                items.append({"key": key, "title": title})
        if items:
            groups.append({"name": group_name, "items": items})
    return render(request, "reports/index.html",
                  {"groups": groups, "active_nav": "reports"})


def _run_report(request, key):
    if key not in REPORTS:
        raise Http404
    title, fn, perm = REPORTS[key]
    require_access(
        request,
        REPORT_REQUIRED_MODULES[key],
        permission_code="reports.view",
        action=AccessAction.READ,
    )
    if key in SALES_REPORT_KEYS:
        require_access(
            request,
            "pos_core",
            permission_code="reports.sales",
            action=AccessAction.READ,
        )
    require_access(
        request,
        REPORT_REQUIRED_MODULES[key],
        permission_code=perm,
        action=AccessAction.READ,
    )
    filters = _parse_filters(request)
    filters["allowed_branch_ids"] = request.membership.allowed_branch_ids
    if key in {"sales_detailed", "current_stock", "low_stock", "stock_movements"}:
        filters["tailoring_enabled"] = evaluate_access(
            request, "tailoring", action=AccessAction.READ
        ).allowed
    if key == "sales_detailed":
        filters["customer_credit_enabled"] = evaluate_access(
            request, "customer_credit", action=AccessAction.READ
        ).allowed
    data = fn(request.business, filters)
    return title, data, filters


@business_required
def report_view(request, key):
    from apps.branches.models import Branch, Warehouse
    from apps.catalog.models import Brand, Product
    from apps.suppliers.models import Supplier, SupplierPayment

    export = request.GET.get("export", "")
    if key not in REPORTS:
        raise Http404
    if export:
        require_access(
            request,
            REPORT_REQUIRED_MODULES[key],
            permission_code="reports.export",
            action=AccessAction.READ,
        )
    title, data, filters = _run_report(request, key)
    if export:
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
    branches = Branch.objects.for_business(request.business)
    warehouses = Warehouse.objects.for_business(request.business)
    allowed_branch_ids = request.membership.allowed_branch_ids
    if allowed_branch_ids is not None:
        branches = branches.filter(id__in=allowed_branch_ids)
        warehouses = warehouses.filter(branch_id__in=allowed_branch_ids)
    if key != "supplier_payments_cheques":
        branches = branches.filter(is_active=True)
        warehouses = warehouses.filter(is_active=True)
    return render(request, "reports/report.html", {
        "key": key, "title": title, "data": data, "filters": filters,
        "active_nav": "reports",
        "branches": branches.order_by("name"),
        "warehouses": warehouses.order_by("name"),
        "suppliers": (
            Supplier.objects.for_business(request.business).order_by("name")
            if key == "supplier_payments_cheques" else []
        ),
        "supplier_payment_methods": (
            SupplierPayment.Method.choices if key == "supplier_payments_cheques" else []
        ),
        "supplier_cheque_statuses": (
            SupplierPayment.ChequeStatus.choices
            if key == "supplier_payments_cheques" else []
        ),
        "products": (
            Product.objects.for_business(request.business)
            .filter(
                **(
                    {}
                    if filters.get("tailoring_enabled", True)
                    else {"is_tailoring_item": False}
                )
            )
            .only("id", "name")
            .order_by("name")
            if key == "sales_detailed" else []
        ),
        "brands": (
            Brand.objects.for_business(request.business)
            .filter(
                products__business=request.business,
                products__is_tailoring_item=True,
                products__unit__business=request.business,
                products__unit__is_meter=True,
                products__track_inventory=True,
                products__product_type__in=(Product.Type.STANDARD, Product.Type.VARIANT),
                products__is_archived=False,
            )
            .distinct()
            .order_by("name")
            if key == "fabric_history" else []
        ),
        "filter_querystring": date_range_querystring(
            request.GET,
            filters["date_from"],
            filters["date_to"],
        ),
        "can_export": request.membership.has_perm("reports.export"),
    })
