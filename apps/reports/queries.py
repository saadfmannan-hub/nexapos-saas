"""Report queries.

Each report function takes (business, filters) and returns:
  {"columns": [...], "rows": [[...], ...], "totals": [...] or None}
Filters: date_from, date_to (date objects or None), branch_id, warehouse_id.
The same data feeds HTML tables and CSV/Excel/PDF exports, so exported
numbers always match what is on screen.
"""
import calendar
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import (
    Avg,
    Case,
    Count,
    DecimalField,
    ExpressionWrapper,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Greatest, Round
from django.utils.dateparse import parse_date

from apps.core.date_ranges import (
    business_localdate,
    business_localtime,
    business_timezone,
    filter_business_date_range,
)

ZERO = Decimal("0")


def _money(value):
    from apps.core.money import money

    return money(value or ZERO)


def _quantity(value):
    from apps.core.money import qty

    return qty(value or ZERO)


def _filter_business_datetime_dates(
    queryset,
    business,
    filters,
    *,
    field_name="created_at",
):
    """Apply half-open business-local date bounds to a datetime field."""
    date_from = filters.get("date_from")
    date_to = filters.get("date_to")
    if date_from and not isinstance(date_from, date):
        date_from = parse_date(str(date_from))
    if date_to and not isinstance(date_to, date):
        date_to = parse_date(str(date_to))

    local_timezone = business_timezone(business)
    if date_from:
        start = datetime.combine(date_from, time.min, tzinfo=local_timezone)
        queryset = queryset.filter(**{f"{field_name}__gte": start})
    if date_to:
        end = datetime.combine(
            date_to + timedelta(days=1),
            time.min,
            tzinfo=local_timezone,
        )
        queryset = queryset.filter(**{f"{field_name}__lt": end})
    return queryset


def _net_pos_meter(item, *, is_voided=False):
    """Return the meter quantity still deducted from inventory for one line.

    The persisted POS meter remains the immutable entered value.  Voids restore
    the full movement, while returns restore stock only when their individual
    return line was marked as restocked.  Although new meter-tailoring lines are
    fixed at quantity one, retaining a proportional calculation keeps legacy
    or manually-created rows safe and predictable.
    """
    entered = item.fabric_meter_used
    if entered is None:
        return None
    if is_voided or item.quantity <= 0:
        return ZERO

    restocked_quantity = sum(
        (
            return_item.quantity
            for return_item in item.return_items.all()
            if return_item.restocked
        ),
        ZERO,
    )
    remaining_quantity = max(item.quantity - restocked_quantity, ZERO)
    if remaining_quantity > item.quantity:
        remaining_quantity = item.quantity
    return _quantity(entered * remaining_quantity / item.quantity)


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
        "cost": _money(item.unit_cost * item.inventory_quantity * ratio),
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


def _payment_method_summary(sale, *, include_customer_credit=True):
    from apps.sales.models import PaymentMethod

    names = []
    for payment in sale.payments.all():
        if (
            not include_customer_credit
            and payment.method.kind
            in {
                PaymentMethod.Kind.CUSTOMER_CREDIT,
                PaymentMethod.Kind.STORE_CREDIT,
            }
        ):
            continue
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
    qs = filter_business_date_range(
        qs,
        business,
        field_name="sale_date",
        date_from=f.get("date_from"),
        date_to=f.get("date_to"),
    )
    if f.get("branch_id"):
        qs = qs.filter(branch_id=f["branch_id"])
    allowed_branch_ids = f.get("allowed_branch_ids")
    if allowed_branch_ids is not None:
        qs = qs.filter(
            branch_id__in=allowed_branch_ids,
            warehouse__business=business,
        ).filter(
            Q(warehouse__branch_id__in=allowed_branch_ids)
            | Q(warehouse__branch__isnull=True)
        )
    if f.get("warehouse_id"):
        qs = qs.filter(warehouse_id=f["warehouse_id"])
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
    include_expenses=True,
    include_customer_credit=True,
):
    """Current-calendar-year dashboard totals, independent of date filters.

    Receivable follows ``customer_receivables``: the current outstanding
    amount on valid invoices created in the requested range. Real payments
    are cash, card, and bank entries; cash/card/bank refunds are backed out
    before the per-invoice balance is clamped at zero.
    """
    from apps.core.money import money
    from apps.sales.models import PaymentMethod, Sale, SaleItem, SalePayment, SaleReturn

    today = today or business_localdate(business)
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
        {
            "date_from": year_start,
            "date_to": today,
            "allowed_branch_ids": membership.allowed_branch_ids,
        },
    )
    valid_sales = _scope_to_membership_branches(
        valid_sales,
        business=business,
        membership=membership,
        branch_id=selected_branch_id,
    )

    if include_customer_credit:
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
    else:
        sales_totals = valid_sales.aggregate(
            total_sales=Sum("total", output_field=amount_field),
        )
        sales_totals["total_receivable"] = None

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
    allowed_branch_ids = membership.allowed_branch_ids
    if allowed_branch_ids is not None:
        payments = payments.filter(
            sale__warehouse__business=business,
        ).filter(
            Q(sale__warehouse__branch_id__in=allowed_branch_ids)
            | Q(sale__warehouse__branch__isnull=True)
        )
    total_income = payments.aggregate(total=Sum("amount"))["total"] or ZERO

    total_expenses = None
    if include_expenses:
        from apps.expenses.models import Expense

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

    returns = filter_business_date_range(
        SaleReturn.objects.for_business(business),
        business,
        field_name="created_at",
        date_from=year_start,
        date_to=today,
    )
    returns = _scope_to_membership_branches(
        returns,
        business=business,
        membership=membership,
        branch_id=selected_branch_id,
    )
    if allowed_branch_ids is not None:
        returns = returns.filter(
            warehouse__business=business,
            sale__branch_id__in=allowed_branch_ids,
            sale__warehouse__business=business,
        ).filter(
            Q(warehouse__branch_id__in=allowed_branch_ids)
            | Q(warehouse__branch__isnull=True)
        ).filter(
            Q(sale__warehouse__branch_id__in=allowed_branch_ids)
            | Q(sale__warehouse__branch__isnull=True)
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
    total_receivable = (
        money(sales_totals["total_receivable"] or ZERO)
        if include_customer_credit
        else None
    )
    total_income = money(total_income)
    total_expenses = money(total_expenses) if total_expenses is not None else None
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
            if gross_profit is not None and total_expenses is not None
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
            business_localtime(business, value=sale.sale_date).date(),
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
        .select_related(
            "sale__customer",
            "sale__branch",
            "sale__warehouse",
            "sale__cashier",
            "product__brand",
            "product__unit",
            "variant",
        )
        .prefetch_related(
            "return_items",
            "sale__returns",
            "sale__payments__method",
        )
        .order_by("-sale__sale_date", "sale__invoice_number", "id")
    )
    if f.get("product_id"):
        qs = qs.filter(product_id=f["product_id"])
    tailoring_enabled = f.get("tailoring_enabled", True)
    customer_credit_enabled = f.get("customer_credit_enabled", True)
    if tailoring_enabled and f.get("garment_classification") in ("adult", "child"):
        qs = qs.filter(garment_classification=f["garment_classification"])

    rows = []
    pieces = {"adult": ZERO, "child": ZERO, "legacy": ZERO}
    fabric_totals = {"estimated": ZERO, "actual": ZERO, "variance": ZERO}
    has_fabric = {"estimated": False, "actual": False, "variance": False}
    net_pos_meter_total = ZERO
    has_pos_meter = False
    for item in qs[:2000]:
        sale = item.sale
        quantity = item.quantity - item.returned_quantity
        if sale.status == Sale.Status.VOIDED:
            quantity = ZERO
        classification = item.garment_classification_label or "Not Applicable"
        collection_type = item.collection_type_label or "Not Applicable"
        if item.is_tailoring_line:
            key = item.garment_classification or "legacy"
            pieces[key] += quantity
        estimated_fabric = item.estimated_fabric
        actual_fabric = item.actual_fabric_used
        variance = item.fabric_variance
        pos_meter = item.fabric_meter_used
        net_pos_meter = _net_pos_meter(
            item,
            is_voided=sale.status == Sale.Status.VOIDED,
        )
        if pos_meter is not None:
            has_pos_meter = True
            net_pos_meter_total += net_pos_meter or ZERO
        for key, value in (
            ("estimated", estimated_fabric),
            ("actual", actual_fabric),
            ("variance", variance),
        ):
            if value is not None:
                fabric_totals[key] += value
                has_fabric[key] = True
        brand = item.product.brand
        brand_name = (
            brand.name
            if brand is not None and brand.business_id == business.id
            else None
        )
        variant_name = (
            item.variant.name
            if (
                item.variant is not None
                and item.variant.business_id == business.id
                and item.variant.product_id == item.product_id
            )
            else None
        )
        warehouse_name = (
            sale.warehouse.name
            if sale.warehouse.business_id == business.id
            else None
        )
        rows.append([
            sale.invoice_number,
            business_localtime(
                business, value=sale.sale_date
            ).strftime("%Y-%m-%d %H:%M"),
            sale.customer.full_name,
            sale.branch.name,
            sale.cashier.full_name,
            item.product_name,
            classification,
            collection_type,
            quantity,
            estimated_fabric,
            actual_fabric,
            variance,
            _payment_method_summary(
                sale,
                include_customer_credit=customer_credit_enabled,
            ),
            sale.net_total,
            sale.net_amount_paid,
            sale.balance,
            sale.get_status_display(),
            pos_meter,
            net_pos_meter,
            brand_name,
            variant_name,
            warehouse_name,
        ])
    columns = [
            "Invoice", "Date", "Customer", "Branch", "Cashier", "Product",
            "Garment Classification", "Collection", "Quantity",
            "Legacy Estimated Fabric", "Legacy Workshop Actual",
            "Legacy Variance", "Payment Method", "Total", "Paid",
            "Balance", "Status", "POS Meter", "Net Meter Deducted", "Brand",
            "Variant / Color", "Warehouse",
        ]
    if not tailoring_enabled:
        tailoring_indexes = {6, 7, 9, 10, 11, 17, 18}
        columns = [
            column for index, column in enumerate(columns)
            if index not in tailoring_indexes
        ]
        rows = [
            [value for index, value in enumerate(row) if index not in tailoring_indexes]
            for row in rows
        ]
    if not customer_credit_enabled:
        balance_index = columns.index("Balance")
        columns.pop(balance_index)
        rows = [
            [value for index, value in enumerate(row) if index != balance_index]
            for row in rows
        ]
    return {
        "columns": columns,
        "rows": rows,
        "totals": None,
        "summary": [
            ("Total Adult Pieces", pieces["adult"]),
            ("Total Child Pieces", pieces["child"]),
            ("Total Legacy/Unclassified Pieces", pieces["legacy"]),
            (
                "Net POS Meter Total",
                net_pos_meter_total if has_pos_meter else None,
            ),
            (
                "Legacy Estimated Total",
                fabric_totals["estimated"] if has_fabric["estimated"] else None,
            ),
            (
                "Legacy Workshop Actual Total",
                fabric_totals["actual"] if has_fabric["actual"] else None,
            ),
            (
                "Legacy Variance Total",
                fabric_totals["variance"] if has_fabric["variance"] else None,
            ),
        ] if tailoring_enabled else [],
        "wide_pdf": True,
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
            business_localtime(business, value=sale.sale_date).date(),
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
    rows = [[s.invoice_number, business_localtime(
              business, value=s.sale_date).strftime("%Y-%m-%d %H:%M"),
             s.total, s.voided_by.full_name if s.voided_by else "",
             s.void_reason] for s in qs]
    return {"columns": ["Invoice", "Date", "Total", "Voided by", "Reason"],
            "rows": rows, "totals": None}


def returns_report(business, f):
    from apps.sales.models import SaleReturnItem

    qs = SaleReturnItem.objects.for_business(business).select_related(
        "sale_return__sale", "sale_return__customer", "sale_return__processed_by",
        "sale_item")
    qs = filter_business_date_range(
        qs,
        business,
        field_name="sale_return__created_at",
        date_from=f.get("date_from"),
        date_to=f.get("date_to"),
    )
    if f.get("branch_id"):
        qs = qs.filter(sale_return__branch_id=f["branch_id"])
    allowed_branch_ids = f.get("allowed_branch_ids")
    if allowed_branch_ids is not None:
        qs = qs.filter(
            sale_return__branch_id__in=allowed_branch_ids,
            sale_return__warehouse__business=business,
        ).filter(
            Q(sale_return__warehouse__branch_id__in=allowed_branch_ids)
            | Q(sale_return__warehouse__branch__isnull=True)
        )
    rows = []
    for item in qs.order_by("-sale_return__created_at", "sale_item__product_name"):
        sale_return = item.sale_return
        rows.append([
            business_localtime(
                business, value=sale_return.created_at
            ).strftime("%Y-%m-%d"),
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
    if not f.get("tailoring_enabled", True):
        qs = qs.filter(product__is_tailoring_item=False)
    allowed_branch_ids = f.get("allowed_branch_ids")
    if allowed_branch_ids is not None:
        qs = qs.filter(
            warehouse__business=business,
            warehouse__branch_id__in=allowed_branch_ids,
        )
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
    if not f.get("tailoring_enabled", True):
        qs = qs.filter(product__is_tailoring_item=False)
    allowed_branch_ids = f.get("allowed_branch_ids")
    if allowed_branch_ids is not None:
        qs = qs.filter(
            warehouse__business=business,
            warehouse__branch_id__in=allowed_branch_ids,
        )
    rows = [[level.product.name, level.warehouse.name, level.quantity,
             level.product.reorder_level] for level in qs[:2000]]
    return {"columns": ["Product", "Warehouse", "Current stock", "Reorder level"],
            "rows": rows, "totals": None}


def stock_movements_report(business, f):
    from apps.inventory.models import StockMovement

    qs = (
        StockMovement.objects.for_business(business)
        .select_related("product__brand", "product__unit", "variant", "warehouse", "user")
        .order_by("-created_at")
    )
    if f.get("date_from"):
        qs = qs.filter(created_at__date__gte=f["date_from"])
    if f.get("date_to"):
        qs = qs.filter(created_at__date__lte=f["date_to"])
    if f.get("warehouse_id"):
        qs = qs.filter(warehouse_id=f["warehouse_id"])
    if not f.get("tailoring_enabled", True):
        qs = qs.filter(product__is_tailoring_item=False)
    if f.get("branch_id"):
        qs = qs.filter(warehouse__branch_id=f["branch_id"])
    allowed_branch_ids = f.get("allowed_branch_ids")
    if allowed_branch_ids is not None:
        qs = qs.filter(warehouse__branch_id__in=allowed_branch_ids)
    rows = [[
        m.created_at.strftime("%Y-%m-%d %H:%M"),
        m.product.name,
        m.get_movement_type_display(),
        m.warehouse.name,
        m.quantity,
        m.balance_after,
        f"{m.reference_type} {m.reference_id}".strip(),
        m.user.full_name if m.user else "",
        (
            m.product.brand.name
            if (
                m.product.brand is not None
                and m.product.brand.business_id == business.id
            )
            else None
        ),
        (
            m.variant.name
            if (
                m.variant is not None
                and m.variant.business_id == business.id
                and m.variant.product_id == m.product_id
            )
            else None
        ),
        (
            (m.product.unit.abbreviation or m.product.unit.name)
            if m.product.unit is not None
            else None
        ),
    ] for m in qs[:2000]]
    return {"columns": ["Date", "Product", "Type", "Warehouse", "Qty",
                        "Balance after", "Reference", "User", "Brand",
                        "Variant / Color", "Unit"],
            "rows": rows, "totals": None}


def fabric_history(business, f):
    """Return authoritative fabric movement grouped by product/variant.

    Brand and color are presentation fields only. Opening stock and purchases
    come from their distinct inventory-ledger movements, usage comes from the
    immutable POS meter field, and remaining stock comes from the inventory
    module's cached balance.
    """
    from apps.catalog.models import Product, ProductVariant
    from apps.inventory.models import StockLevel, StockMovement
    from apps.sales.models import Sale, SaleItem, SaleReturnItem

    quantity_field = DecimalField(max_digits=14, decimal_places=3)
    calculation_field = DecimalField(max_digits=38, decimal_places=12)
    zero_quantity = Value(ZERO, output_field=quantity_field)
    zero_calculation = Value(ZERO, output_field=calculation_field)
    zero_count = Value(0, output_field=IntegerField())
    product_types = (Product.Type.STANDARD, Product.Type.VARIANT)

    def inventory_scope(queryset):
        queryset = queryset.filter(
            warehouse__business=business,
            product__business=business,
            product__is_tailoring_item=True,
            product__unit__business=business,
            product__unit__is_meter=True,
            product__track_inventory=True,
            product__product_type__in=product_types,
            product__is_archived=False,
        ).filter(
            Q(variant__isnull=True)
            | Q(variant__business=business, variant__product_id=F("product_id"))
        )
        allowed_branch_ids = f.get("allowed_branch_ids")
        if allowed_branch_ids is not None:
            queryset = queryset.filter(
                warehouse__branch_id__in=allowed_branch_ids
            )
        if f.get("branch_id"):
            queryset = queryset.filter(warehouse__branch_id=f["branch_id"])
        if f.get("warehouse_id"):
            queryset = queryset.filter(warehouse_id=f["warehouse_id"])
        if f.get("brand_id"):
            queryset = queryset.filter(
                product__brand_id=f["brand_id"],
                product__brand__business=business,
            )
        return queryset

    remaining_rows = (
        inventory_scope(StockLevel.objects.for_business(business))
        .values("product_id", "variant_id")
        .annotate(
            remaining=Coalesce(
                Sum("quantity"),
                zero_quantity,
                output_field=quantity_field,
            )
        )
    )

    opening_rows = (
        inventory_scope(
            StockMovement.objects.for_business(business).filter(
                movement_type=StockMovement.Type.OPENING,
            )
        )
        .values("product_id", "variant_id")
        .annotate(
            opening=Coalesce(
                Sum("quantity"),
                zero_quantity,
                output_field=quantity_field,
            )
        )
    )

    purchase_rows = inventory_scope(
        StockMovement.objects.for_business(business).filter(
            movement_type__in=(
                StockMovement.Type.PURCHASE,
                StockMovement.Type.PURCHASE_RETURN,
            )
        )
    )
    purchase_rows = (
        _filter_business_datetime_dates(purchase_rows, business, f)
        .values("product_id", "variant_id")
        .annotate(
            purchased=Coalesce(
                Sum("quantity"),
                zero_quantity,
                output_field=quantity_field,
            )
        )
    )

    restocked_quantity = (
        SaleReturnItem.objects.for_business(business)
        .filter(
            sale_item_id=OuterRef("pk"),
            sale_item__business=business,
            sale_return__business=business,
            sale_return__sale_id=OuterRef("sale_id"),
            restocked=True,
        )
        .values("sale_item_id")
        .annotate(total=Sum("quantity"))
        .values("total")[:1]
    )
    usage_rows = (
        SaleItem.objects.for_business(business)
        .filter(
            sale__business=business,
            sale__branch__business=business,
            sale__warehouse__business=business,
            product__business=business,
            product__is_tailoring_item=True,
            product__unit__business=business,
            product__unit__is_meter=True,
            product__track_inventory=True,
            product__product_type__in=product_types,
            product__is_archived=False,
            fabric_meter_used__isnull=False,
            fabric_meter_used__gt=ZERO,
        )
        .exclude(sale__status__in=(Sale.Status.DRAFT, Sale.Status.VOIDED))
        .filter(
            Q(variant__isnull=True)
            | Q(variant__business=business, variant__product_id=F("product_id"))
        )
    )
    allowed_branch_ids = f.get("allowed_branch_ids")
    if allowed_branch_ids is not None:
        usage_rows = usage_rows.filter(
            sale__branch_id__in=allowed_branch_ids,
        ).filter(
            Q(sale__warehouse__branch_id__in=allowed_branch_ids)
            | Q(sale__warehouse__branch__isnull=True)
        )
    if f.get("branch_id"):
        usage_rows = usage_rows.filter(sale__branch_id=f["branch_id"])
    if f.get("warehouse_id"):
        usage_rows = usage_rows.filter(sale__warehouse_id=f["warehouse_id"])
    if f.get("brand_id"):
        usage_rows = usage_rows.filter(
            product__brand_id=f["brand_id"],
            product__brand__business=business,
        )
    usage_rows = _filter_business_datetime_dates(
        usage_rows,
        business,
        f,
        field_name="sale__sale_date",
    )

    remaining_line_quantity = Greatest(
        ExpressionWrapper(
            F("quantity")
            - Coalesce(
                Subquery(restocked_quantity, output_field=quantity_field),
                zero_quantity,
                output_field=quantity_field,
            ),
            output_field=calculation_field,
        ),
        zero_calculation,
    )
    proportional_meter = ExpressionWrapper(
        F("fabric_meter_used") * remaining_line_quantity / F("quantity"),
        output_field=calculation_field,
    )
    rounded_meter = ExpressionWrapper(
        Round(proportional_meter, precision=3),
        output_field=quantity_field,
    )
    usage_rows = (
        usage_rows.annotate(_remaining_line_quantity=remaining_line_quantity)
        .annotate(
            _net_meter=Case(
                When(quantity__lte=ZERO, then=zero_quantity),
                default=rounded_meter,
                output_field=quantity_field,
            ),
            _order_count=Case(
                When(_remaining_line_quantity__gt=ZERO, then=Value(1)),
                default=zero_count,
                output_field=IntegerField(),
            ),
        )
        .values("product_id", "variant_id")
        .annotate(
            used=Coalesce(
                Sum("_net_meter"),
                zero_quantity,
                output_field=quantity_field,
            ),
            orders=Coalesce(
                Sum("_order_count"),
                zero_count,
                output_field=IntegerField(),
            ),
        )
    )

    opening_by_item = {
        (row["product_id"], row["variant_id"]): _quantity(row["opening"])
        for row in opening_rows
    }
    purchased_by_item = {
        (row["product_id"], row["variant_id"]): _quantity(row["purchased"])
        for row in purchase_rows
    }
    used_by_item = {
        (row["product_id"], row["variant_id"]): (
            _quantity(row["used"]),
            int(row["orders"] or 0),
        )
        for row in usage_rows
    }
    remaining_by_item = {
        (row["product_id"], row["variant_id"]): _quantity(row["remaining"])
        for row in remaining_rows
    }
    item_keys = (
        set(opening_by_item)
        | set(purchased_by_item)
        | set(used_by_item)
        | set(remaining_by_item)
    )

    product_ids = {product_id for product_id, _variant_id in item_keys}
    variant_ids = {variant_id for _product_id, variant_id in item_keys if variant_id}
    products = Product.objects.for_business(business).filter(
        id__in=product_ids,
        is_tailoring_item=True,
        unit__business=business,
        unit__is_meter=True,
        track_inventory=True,
        product_type__in=product_types,
        is_archived=False,
    ).select_related("brand")
    products_by_id = {product.id: product for product in products}
    variants_by_id = ProductVariant.objects.for_business(business).filter(
        id__in=variant_ids,
        product_id__in=product_ids,
    ).in_bulk()

    detail_rows = []
    for product_id, variant_id in item_keys:
        product = products_by_id.get(product_id)
        if product is None:
            continue
        variant = variants_by_id.get(variant_id) if variant_id else None
        if variant_id and (variant is None or variant.product_id != product_id):
            continue

        opening = opening_by_item.get((product_id, variant_id), ZERO)
        purchased = purchased_by_item.get((product_id, variant_id), ZERO)
        used, orders = used_by_item.get((product_id, variant_id), (ZERO, 0))
        remaining = remaining_by_item.get((product_id, variant_id), ZERO)
        if not (opening or purchased or used or remaining or orders):
            continue

        brand = product.brand
        if brand is not None and brand.business_id == business.id:
            brand_id = brand.id
            brand_name = brand.name
        else:
            brand_id = None
            brand_name = "No Brand"

        if variant is not None:
            color = str((variant.attributes or {}).get("Color") or "").strip()
            color = color or variant.name or "No Color"
            item_name = f"{product.name} - {variant.name}"
        else:
            color = product.name
            item_name = product.name

        detail_rows.append({
            "brand_id": brand_id,
            "brand": brand_name,
            "color": color,
            "item": item_name,
            "opening": _quantity(opening),
            "purchased": _quantity(purchased),
            "used": _quantity(used),
            "remaining": _quantity(remaining),
            "orders": orders,
        })

    detail_rows.sort(
        key=lambda row: (
            row["brand"].casefold(),
            row["brand_id"] or 0,
            row["color"].casefold(),
            row["item"].casefold(),
        )
    )
    grouped = {}
    for row in detail_rows:
        grouped.setdefault((row["brand_id"], row["brand"]), []).append(row)

    rows = []
    brand_totals = []
    for (_brand_id, brand_name), children in grouped.items():
        rows.extend([
            [
                child["brand"],
                child["color"],
                child["item"],
                child["opening"],
                child["purchased"],
                child["used"],
                child["remaining"],
                child["orders"],
            ]
            for child in children
        ])
        total = {
            "brand": brand_name,
            "opening": _quantity(sum((row["opening"] for row in children), ZERO)),
            "purchased": _quantity(sum((row["purchased"] for row in children), ZERO)),
            "used": _quantity(sum((row["used"] for row in children), ZERO)),
            "remaining": _quantity(sum((row["remaining"] for row in children), ZERO)),
            "orders": sum(row["orders"] for row in children),
        }
        brand_totals.append(total)
        rows.append([
            f"Brand Total - {brand_name}",
            "",
            "",
            total["opening"],
            total["purchased"],
            total["used"],
            total["remaining"],
            total["orders"],
        ])

    grand_total = {
        "opening": _quantity(sum((row["opening"] for row in detail_rows), ZERO)),
        "purchased": _quantity(sum((row["purchased"] for row in detail_rows), ZERO)),
        "used": _quantity(sum((row["used"] for row in detail_rows), ZERO)),
        "remaining": _quantity(sum((row["remaining"] for row in detail_rows), ZERO)),
        "orders": sum(row["orders"] for row in detail_rows),
    }
    totals = [
        "GRAND TOTAL",
        "",
        "",
        grand_total["opening"],
        grand_total["purchased"],
        grand_total["used"],
        grand_total["remaining"],
        grand_total["orders"],
    ] if detail_rows else None

    return {
        "columns": [
            "Brand",
            "Color",
            "Product / Variant",
            "Opening Stock (Meters)",
            "Purchased (Meters)",
            "Used (Meters)",
            "Remaining (Meters)",
            "Orders Count",
        ],
        "rows": rows,
        "totals": totals,
        "brand_totals": brand_totals,
        "detail_count": len(detail_rows),
        "summary": [(
            "Period",
            f"{f.get('date_from') or ''} to {f.get('date_to') or ''}",
        )],
        "column_formats": {
            3: "0.000",
            4: "0.000",
            5: "0.000",
            6: "0.000",
        },
    }


def purchases_summary(business, f):
    from apps.purchases import services as purchase_services
    from apps.purchases.models import Purchase

    qs = purchase_services.with_pending_cheques(
        Purchase.objects.for_business(business)
        .select_related("supplier")
    )
    if f.get("date_from"):
        qs = qs.filter(purchase_date__gte=f["date_from"])
    if f.get("date_to"):
        qs = qs.filter(purchase_date__lte=f["date_to"])
    if f.get("branch_id"):
        qs = qs.filter(branch_id=f["branch_id"])
    allowed_branch_ids = f.get("allowed_branch_ids")
    if allowed_branch_ids is not None:
        qs = qs.filter(
            branch_id__in=allowed_branch_ids,
            warehouse__business=business,
        ).filter(
            Q(warehouse__branch_id__in=allowed_branch_ids)
            | Q(warehouse__branch__isnull=True)
        )
    rows = [[p.purchase_number, str(p.purchase_date), p.supplier.name,
             p.total, p.amount_paid, p.cheques_pending, p.remaining_balance,
             p.supplier_balance, p.get_status_display()]
            for p in qs[:2000]]
    totals = ["TOTAL", "", "", sum((r[3] or ZERO) for r in rows),
              sum((r[4] or ZERO) for r in rows), sum((r[5] or ZERO) for r in rows),
              sum((r[6] or ZERO) for r in rows), sum((r[7] or ZERO) for r in rows), ""]
    return {"columns": ["Number", "Date", "Supplier", "Purchase Total", "Paid",
                        "Cheques Pending", "Remaining Balance", "Supplier Balance",
                        "Status"],
            "rows": rows, "totals": totals if rows else None}


def _filter_payment_record_dates(queryset, business, filters):
    """Apply business-local date boundaries to UTC payment timestamps."""
    return _filter_business_datetime_dates(queryset, business, filters)


def supplier_payments_cheques(business, f):
    """Purchase-side supplier payments with one row per payment record."""
    from apps.purchases import services as purchase_services
    from apps.purchases.models import Purchase
    from apps.suppliers.models import SupplierPayment

    payments_qs = (
        SupplierPayment.objects.for_business(business)
        .filter(supplier__business=business)
        .filter(Q(purchase__isnull=True) | Q(purchase__business=business))
        .filter(Q(payment_method__isnull=True) | Q(payment_method__business=business))
        .filter(
            Q(purchase__isnull=True)
            | Q(
                purchase__branch__business=business,
                purchase__warehouse__business=business,
            )
        )
        .select_related(
            "supplier", "purchase", "purchase__branch", "purchase__warehouse",
            "payment_method",
        )
    )
    payments_qs = _filter_payment_record_dates(payments_qs, business, f)
    if f.get("supplier_id"):
        payments_qs = payments_qs.filter(supplier_id=f["supplier_id"])
    if f.get("branch_id"):
        payments_qs = payments_qs.filter(purchase__branch_id=f["branch_id"])
    if f.get("warehouse_id"):
        payments_qs = payments_qs.filter(purchase__warehouse_id=f["warehouse_id"])
    allowed_branch_ids = f.get("allowed_branch_ids")
    if allowed_branch_ids is not None:
        payments_qs = payments_qs.filter(
            Q(purchase__isnull=True)
            | Q(
                purchase__branch_id__in=allowed_branch_ids,
                purchase__warehouse__business=business,
            )
            & (
                Q(purchase__warehouse__branch_id__in=allowed_branch_ids)
                | Q(purchase__warehouse__branch__isnull=True)
            )
        )

    method = f.get("payment_method")
    if method in (
        SupplierPayment.Method.CASH,
        SupplierPayment.Method.BANK,
        SupplierPayment.Method.CARD,
    ):
        payments_qs = payments_qs.filter(
            Q(method=method)
            | Q(
                method="",
                payment_method__business=business,
                payment_method__kind=method,
            )
        )
    elif method == SupplierPayment.Method.CHEQUE:
        payments_qs = payments_qs.filter(method=method)
    if f.get("cheque_status"):
        payments_qs = payments_qs.filter(
            method=SupplierPayment.Method.CHEQUE,
            cheque_status=f["cheque_status"],
        )

    payments = list(payments_qs.order_by("-created_at", "-pk"))
    purchase_ids = {payment.purchase_id for payment in payments if payment.purchase_id}
    purchases = purchase_services.with_pending_cheques(
        Purchase.objects.for_business(business).filter(pk__in=purchase_ids)
    )
    purchase_by_id = {purchase.pk: purchase for purchase in purchases}

    rows = []
    total_payment_amount = ZERO
    total_pending_cheques = ZERO
    total_cleared_cheques = ZERO
    for payment in payments:
        purchase = purchase_by_id.get(payment.purchase_id)
        is_cheque = payment.is_cheque
        rows.append([
            business_localdate(business, now=payment.created_at),
            payment.supplier.name,
            purchase.purchase_number if purchase else None,
            purchase.purchase_date if purchase else None,
            payment.method_label,
            payment.amount,
            payment.cheque_number if is_cheque else None,
            payment.bank_name if is_cheque else None,
            payment.cheque_issue_date if is_cheque else None,
            payment.due_date if is_cheque else None,
            payment.get_cheque_status_display() if is_cheque else None,
            purchase.amount_paid if purchase else None,
            purchase.cheques_pending if purchase else None,
            purchase.remaining_balance if purchase else None,
            payment.supplier.balance,
        ])
        total_payment_amount += payment.amount
        if payment.cheque_status == SupplierPayment.ChequeStatus.PENDING:
            total_pending_cheques += payment.amount
        elif payment.cheque_status == SupplierPayment.ChequeStatus.CLEARED:
            total_cleared_cheques += payment.amount

    return {
        "columns": [
            "Date", "Supplier", "Purchase No.", "Purchase Date", "Pmt Medium",
            "Amount", "Cheque Number", "Bank Name", "Cheque Issue Date",
            "Cheque Payment Date", "Cheque Status", "Paid", "Cheques Pending",
            "Remaining Balance", "Supplier Balance",
        ],
        "rows": rows,
        "totals": None,
        "summary": [
            ("Total Payment Amount", _money(total_payment_amount)),
            ("Total Pending Cheques", _money(total_pending_cheques)),
            ("Total Cleared Cheques", _money(total_cleared_cheques)),
        ],
        "wide_pdf": True,
    }


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
            business_localtime(business, value=sale.sale_date).date(),
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


def _fixed_expense_occurrences(business, f):
    """Return applicable, not-yet-generated monthly fixed expenses."""
    from apps.branches.models import Branch
    from apps.expenses.models import Expense, RecurringExpenseTemplate

    date_from = f.get("date_from")
    date_to = f.get("date_to")
    if isinstance(date_from, datetime):
        date_from = date_from.date()
    elif date_from and not isinstance(date_from, date):
        date_from = parse_date(str(date_from))
    if isinstance(date_to, datetime):
        date_to = date_to.date()
    elif date_to and not isinstance(date_to, date):
        date_to = parse_date(str(date_to))
    if not date_from or not date_to or date_to < date_from:
        return []

    first_month = date_from.replace(day=1)
    last_month = date_to.replace(day=1)
    templates = list(
        RecurringExpenseTemplate.objects.for_business(business)
        .filter(is_active=True, start_date__lte=date_to)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=first_month))
        .select_related("branch", "category")
        .order_by("id")
    )
    if not templates:
        return []

    generated = set(
        Expense.objects.for_business(business)
        .filter(
            recurring_template_id__in=[template.pk for template in templates],
            generated_for_month__gte=first_month,
            generated_for_month__lte=last_month,
        )
        .values_list("recurring_template_id", "generated_for_month")
    )
    legacy_branches = list(
        Branch.objects.for_business(business).filter(is_active=True).order_by("id")[:2]
    )
    sole_legacy_branch = legacy_branches[0] if len(legacy_branches) == 1 else None
    selected_branch_id = f.get("branch_id")
    allowed_branch_ids = f.get("allowed_branch_ids")
    if allowed_branch_ids is not None:
        allowed_branch_ids = set(allowed_branch_ids)

    occurrences = []
    for template in templates:
        branch = template.branch or sole_legacy_branch
        if branch is None:
            # Match generation safety: never assign an ambiguous legacy template
            # to an arbitrary branch.
            continue
        if selected_branch_id and branch.pk != selected_branch_id:
            continue
        if allowed_branch_ids is not None and branch.pk not in allowed_branch_ids:
            continue

        month = first_month
        while month <= last_month:
            month_end = month.replace(
                day=calendar.monthrange(month.year, month.month)[1]
            )
            due_date = month.replace(day=min(template.due_day, month_end.day))
            applicable = (
                template.start_date <= month_end
                and (template.end_date is None or template.end_date >= month)
            )
            if (
                applicable
                and date_from <= due_date <= date_to
                and (template.pk, month) not in generated
            ):
                occurrences.append({
                    "number": f"REC-{month:%Y%m}-{template.pk}",
                    "date": due_date,
                    "category": template.category.name,
                    "source": "Fixed",
                    "payee": template.name,
                    "branch": branch.name,
                    "amount": template.default_amount,
                    "status": Expense.Status.APPROVED.label,
                })
            month = (
                month.replace(year=month.year + 1, month=1)
                if month.month == 12
                else month.replace(month=month.month + 1)
            )
    return occurrences


def expenses_report(business, f):
    from apps.expenses.models import Expense

    qs = (
        Expense.objects.for_business(business)
        .exclude(status__in=["rejected", "cancelled"])
        .select_related("category", "branch", "supplier")
    )
    if f.get("date_from"):
        qs = qs.filter(expense_date__gte=f["date_from"])
    if f.get("date_to"):
        qs = qs.filter(expense_date__lte=f["date_to"])
    if f.get("branch_id"):
        qs = qs.filter(branch_id=f["branch_id"])
    allowed_branch_ids = f.get("allowed_branch_ids")
    if allowed_branch_ids is not None:
        qs = qs.filter(branch_id__in=allowed_branch_ids)
    rows = [[e.expense_number, str(e.expense_date), e.category.name,
             e.source_display,
             e.payee or (e.supplier.name if e.supplier else ""), e.branch.name,
             e.amount, e.get_status_display()] for e in qs[:2000]]
    rows.extend([
        [row["number"], str(row["date"]), row["category"], row["source"],
         row["payee"], row["branch"], row["amount"], row["status"]]
        for row in _fixed_expense_occurrences(business, f)
    ])
    rows.sort(key=lambda row: (row[1], row[0]), reverse=True)
    rows = rows[:2000]
    totals = ["TOTAL", "", "", "", "", "",
              sum((r[6] or ZERO) for r in rows), ""]
    return {"columns": ["Number", "Date", "Category", "Source", "Payee",
                        "Branch", "Amount", "Status"],
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
    allowed_branch_ids = f.get("allowed_branch_ids")
    if allowed_branch_ids is not None:
        exp_qs = exp_qs.filter(branch_id__in=allowed_branch_ids)
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
    allowed_branch_ids = f.get("allowed_branch_ids")
    if allowed_branch_ids is not None:
        qs = qs.filter(branch_id__in=allowed_branch_ids)
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
    allowed_branch_ids = f.get("allowed_branch_ids")
    if allowed_branch_ids is not None:
        exp_qs = exp_qs.filter(branch_id__in=allowed_branch_ids)
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

    def branch_scoped(qs, field):
        selected_branch_id = f.get("branch_id")
        if selected_branch_id:
            qs = qs.filter(**{field: selected_branch_id})
        allowed_branch_ids = f.get("allowed_branch_ids")
        if allowed_branch_ids is not None:
            qs = qs.filter(**{f"{field}__in": allowed_branch_ids})
        return qs

    sale_pay = (
        branch_scoped(
            ranged(SalePayment.objects.for_business(business), "created_at__date"),
            "sale__branch_id",
        )
        .exclude(method__kind__in=["customer_credit", "store_credit"])
        .values("method__name").annotate(t=Sum("amount")).order_by("-t")
    )
    collections = branch_scoped(
        ranged(
            CustomerPayment.objects.for_business(business).filter(kind="collection"),
            "created_at__date",
        ),
        "branch_id",
    ).aggregate(t=Sum("amount"))["t"] or ZERO
    supplier_immediate = branch_scoped(
        ranged(
            SupplierPayment.objects.for_business(business).exclude(
                method=SupplierPayment.Method.CHEQUE,
            ),
            "created_at__date",
        ),
        "purchase__branch_id",
    ).aggregate(t=Sum("amount"))["t"] or ZERO
    supplier_cleared_cheques = branch_scoped(
        ranged(
            SupplierPayment.objects.for_business(business).filter(
                method=SupplierPayment.Method.CHEQUE,
                cheque_status=SupplierPayment.ChequeStatus.CLEARED,
            ),
            "cleared_at__date",
        ),
        "purchase__branch_id",
    ).aggregate(t=Sum("amount"))["t"] or ZERO
    supplier_pay = supplier_immediate + supplier_cleared_cheques
    expenses = branch_scoped(
        ranged(
            Expense.objects.for_business(business).exclude(
                status__in=["rejected", "cancelled"]
            ),
            "expense_date",
        ),
        "branch_id",
    ).aggregate(t=Sum("amount"))["t"] or ZERO
    refunds = branch_scoped(
        ranged(
            SaleReturn.objects.for_business(business).filter(
                refund_method__in=["cash", "card", "bank"]
            ),
            "created_at__date",
        ),
        "branch_id",
    ).aggregate(t=Sum("refund_amount"))["t"] or ZERO

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
    allowed_branch_ids = f.get("allowed_branch_ids")
    if allowed_branch_ids is not None:
        qs = qs.filter(branch_id__in=allowed_branch_ids)
    data = list(qs.values("category__name").annotate(
        count=Count("id"), total=Sum("amount"), avg=Avg("amount")
    ))
    by_category = {
        row["category__name"]: {
            "count": row["count"],
            "total": row["total"] or ZERO,
        }
        for row in data
    }
    for occurrence in _fixed_expense_occurrences(business, f):
        category = by_category.setdefault(
            occurrence["category"], {"count": 0, "total": ZERO}
        )
        category["count"] += 1
        category["total"] += occurrence["amount"]
    data = sorted(
        (
            {
                "category__name": name,
                "count": values["count"],
                "total": values["total"],
                "avg": values["total"] / values["count"],
            }
            for name, values in by_category.items()
        ),
        key=lambda row: row["total"],
        reverse=True,
    )
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
    "fabric_history": ("Fabric History Report", fabric_history, "reports.view"),
    "purchases": ("Purchases", purchases_summary, "reports.view"),
    "supplier_balances": ("Outstanding supplier balances", supplier_balances, "reports.financial"),
    "supplier_payments_cheques": (
        "Supplier Payments & Cheques", supplier_payments_cheques, "reports.financial",
    ),
    "receivables": ("Outstanding receivables", customer_receivables, "reports.financial"),
    "top_customers": ("Top customers", top_customers, "reports.view"),
    "expenses": ("Expenses", expenses_report, "reports.financial"),
    "shifts": ("Shifts & cash differences", shifts_report, "reports.view"),
}

# Commercial-module requirements are kept beside the report registry so the
# screen, exports, and navigation all make the same authorization decision.
# Advanced reports explicitly list every source module they query; enabling
# Advanced Reports never grants access to those source modules.
REPORT_REQUIRED_MODULES = {
    "sales_summary": ("pos_core",),
    "sales_detailed": ("pos_core",),
    "product_sales": ("advanced_reports", "pos_core"),
    "category_sales": ("advanced_reports", "pos_core"),
    "cashier_sales": ("advanced_reports", "pos_core"),
    "payment_methods": ("pos_core",),
    "voids": ("pos_core",),
    "returns": ("pos_core",),
    "tax": ("pos_core",),
    "profit": ("advanced_reports", "pos_core", "expenses"),
    "profit_loss": ("advanced_reports", "pos_core", "expenses"),
    "cash_flow": (
        "advanced_reports",
        "pos_core",
        "customer_credit",
        "purchases",
        "expenses",
    ),
    "expense_analysis": ("advanced_reports", "expenses"),
    "customer_sales": ("advanced_reports", "pos_core", "customer_credit"),
    "current_stock": ("inventory",),
    "low_stock": ("inventory",),
    "stock_movements": ("inventory",),
    "fabric_history": ("tailoring", "inventory"),
    "purchases": ("purchases",),
    "supplier_balances": ("purchases",),
    "supplier_payments_cheques": ("purchases",),
    "receivables": ("customer_credit",),
    "top_customers": ("advanced_reports", "pos_core"),
    "expenses": ("expenses",),
    "shifts": ("pos_core",),
}

REPORT_GROUPS = [
    ("Sales", ["sales_summary", "sales_detailed", "product_sales", "category_sales",
               "customer_sales", "cashier_sales", "payment_methods", "voids",
               "returns", "tax"]),
    ("Inventory", ["current_stock", "low_stock", "stock_movements", "fabric_history"]),
    ("Purchasing", ["purchases", "supplier_balances", "supplier_payments_cheques"]),
    ("Customers", ["receivables", "top_customers"]),
    ("Financial", ["profit_loss", "cash_flow", "expense_analysis", "profit",
                   "expenses"]),
    ("Registers", ["shifts"]),
]

SALES_REPORT_KEYS = frozenset(REPORT_GROUPS[0][1])
