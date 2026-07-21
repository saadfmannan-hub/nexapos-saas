from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Avg, Count, Max, Q, Sum
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.audit import services as audit
from apps.core.date_ranges import date_range_querystring, resolve_date_range
from apps.core.mixins import get_tenant_object
from apps.registers import services as register_services
from apps.reports.pdf import render_pdf
from apps.subscriptions import services as subscriptions
from apps.subscriptions.access import AccessAction, evaluate_access, require_access
from apps.subscriptions.decorators import module_permission_required

from . import services
from .forms import CustomerForm, CustomerPaymentForm
from .models import CustomerPayment


def _qs_without_page(request):
    params = request.GET.copy()
    params.pop("page", None)
    encoded = params.urlencode()
    return f"{encoded}&" if encoded else ""


def _credit_decision(request, *, permission_code=None, action=AccessAction.READ):
    return evaluate_access(
        request,
        "customer_credit",
        permission_code=permission_code,
        action=action,
    )


def _scope_to_membership_branches(request, queryset, field_name="branch_id"):
    allowed_branch_ids = request.membership.allowed_branch_ids
    if allowed_branch_ids is not None:
        queryset = queryset.filter(
            **{f"{field_name}__in": allowed_branch_ids}
        )
    return queryset


def _customer_queryset(request):
    return services.customer_queryset_for_membership(
        request.business,
        request.membership,
    )


def _selected_branch(request, *, required=False, post_field="branch"):
    raw_branch = (
        request.POST.get(post_field)
        if request.method == "POST"
        else request.GET.get("branch")
    )
    return services.resolve_branch_context(
        request.business,
        request.membership,
        raw_branch,
        required=required,
    )


def _branch_choices(request):
    from apps.branches.models import Branch

    branches = Branch.objects.for_business(request.business).filter(
        is_active=True,
        usage_type=Branch.UsageType.SALES_BRANCH,
    )
    allowed = request.membership.allowed_branch_ids
    if allowed is not None:
        branches = branches.filter(pk__in=allowed)
    return branches.order_by("name")


@module_permission_required("pos_core", "customers.view")
def customer_list(request):
    selected_branch = _selected_branch(request)
    qs = _customer_queryset(request).select_related("group", "home_branch")
    if selected_branch is not None:
        qs = qs.filter(home_branch=selected_branch)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(full_name__icontains=q) | Q(mobile__icontains=q) |
                       Q(code__icontains=q) | Q(email__icontains=q))
    flt = request.GET.get("filter", "")
    credit_access = _credit_decision(
        request, permission_code="customers.view"
    ).allowed
    credit_balance_access = credit_access
    if flt == "credit":
        if not credit_balance_access:
            require_access(
                request,
                "customer_credit",
                permission_code="customers.view",
                action=AccessAction.READ,
                scope_allowed=False,
            )
        qs = qs.filter(balance__gt=0)
    elif flt == "inactive":
        qs = qs.filter(is_active=False)
    receivables = None
    if credit_balance_access:
        receivables = (
            _customer_queryset(request)
            .filter(home_branch=selected_branch) if selected_branch else _customer_queryset(request)
        )
        receivables = (
            receivables
            .filter(balance__gt=0).aggregate(t=Sum("balance"))["t"] or 0
        )
    paginator = Paginator(qs.order_by("-is_walk_in", "full_name"), 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    _c_cur, c_lim, _ = subscriptions.limit_state(request.business, "customers")
    return render(request, "customers/list.html", {
        "page_obj": page_obj, "q": q, "active_nav": "customers",
        "receivables": receivables, "customer_count": qs.count(), "customer_limit": c_lim,
        "credit_access": credit_balance_access,
        "querystring": _qs_without_page(request),
        "branches": _branch_choices(request),
        "selected_branch": selected_branch,
        "branch_locked": request.membership.allowed_branch_ids is not None,
        "branch_actions_enabled": selected_branch is not None,
    })


@module_permission_required("pos_core", "customers.manage")
def customer_form(request, public_id=None):
    instance = None
    if public_id:
        instance = get_tenant_object(
            _customer_queryset(request), request.business, public_id=public_id
        )
    else:
        from apps.subscriptions.helpers import guard_limit

        blocked = guard_limit(request, "customers")
        if blocked:
            return blocked
    credit_write = _credit_decision(
        request,
        permission_code="customers.manage",
        action=AccessAction.WRITE,
    ).allowed
    raw_branch = (
        request.POST.get("home_branch")
        if request.method == "POST"
        else request.GET.get("branch")
    )
    selected_branch = services.resolve_branch_context(
        request.business,
        request.membership,
        raw_branch or (instance.home_branch_id if instance else None),
        required=request.membership.allowed_branch_ids is not None,
    )
    form = CustomerForm(
        request.business,
        request.POST or None,
        instance=instance,
        include_credit=credit_write,
        membership=request.membership,
        selected_branch=selected_branch,
    )
    if request.method == "POST" and form.is_valid():
        customer = form.save(commit=False)
        if not customer.code:
            customer.code = services.next_customer_code(
                request.business,
                customer.home_branch,
            )
        if instance is None and customer.opening_balance:
            customer.balance = customer.opening_balance
        customer = services.save_customer(
            customer=customer, business=request.business, user=request.user,
            membership=request.membership, request=request,
        )
        audit.log("customer.saved", request=request, module="customers", obj=customer,
                  description=f"Customer '{customer.full_name}' saved.")
        messages.success(request, "Customer saved.")
        return redirect("customers:detail", public_id=customer.public_id)
    return render(request, "customers/form.html",
                  {"form": form, "customer": instance, "active_nav": "customers",
                   "credit_write": credit_write,
                   "selected_branch": selected_branch})


@module_permission_required("pos_core", "customers.view")
def customer_detail(request, public_id):
    from apps.sales.models import Sale, SaleReturn

    customer = get_tenant_object(
        _customer_queryset(request), request.business, public_id=public_id
    )
    sales = _scope_to_membership_branches(
        request,
        Sale.objects.for_business(request.business)
        .filter(customer=customer)
        .exclude(status=Sale.Status.DRAFT)
        .select_related("branch")
        .order_by("-sale_date"),
    )
    # Aggregate aliases must not shadow field names ("total" would break
    # Avg("total") with "Cannot compute Avg('total'): 'total' is an aggregate").
    stats = sales.exclude(status=Sale.Status.VOIDED).aggregate(
        sum_total=Sum("total"), paid=Sum("amount_paid"), count=Count("id"),
        avg=Avg("total"), last=Max("sale_date"),
    )
    stats["total"] = stats.pop("sum_total")
    credit_access = _credit_decision(
        request, permission_code="customers.view"
    ).allowed
    credit_balance_access = credit_access
    payments = CustomerPayment.objects.none()
    if credit_access:
        payments = _scope_to_membership_branches(
            request,
            CustomerPayment.objects.for_business(request.business)
            .filter(customer=customer)
            .select_related("payment_method", "received_by")
            .order_by("-created_at"),
        )
    returns = _scope_to_membership_branches(
        request,
        SaleReturn.objects.for_business(request.business)
        .filter(customer=customer).select_related("sale")
        .order_by("-created_at"),
    )
    if not credit_access:
        returns = returns.exclude(
            refund_method__in=["customer_account", "store_credit"]
        )
    can_collect = _credit_decision(
        request,
        permission_code="customers.payments",
        action=AccessAction.WRITE,
    ).allowed
    payment_form = CustomerPaymentForm(request.business) if can_collect else None
    return render(request, "customers/detail.html", {
        "customer": customer, "sales": sales[:25], "stats": stats,
        "payments": payments[:25], "returns": returns[:20],
        "payment_form": payment_form, "active_nav": "customers",
        "can_collect": can_collect,
        "credit_access": credit_access,
        "credit_balance_access": credit_balance_access,
        "more_options": services.more_option_values(request.business, customer),
    })


@require_POST
@module_permission_required(
    "customer_credit", "customers.payments", action=AccessAction.WRITE
)
def customer_payment(request, public_id):
    customer = get_tenant_object(
        _customer_queryset(request), request.business, public_id=public_id
    )
    form = CustomerPaymentForm(request.business, request.POST)
    if form.is_valid():
        try:
            shift = register_services.get_open_shift(request.business, request.user)
            payment = services.record_customer_payment(
                business=request.business,
                customer=customer,
                amount=form.cleaned_data["amount"],
                payment_method=form.cleaned_data["payment_method"],
                reference=form.cleaned_data["reference"],
                notes=form.cleaned_data["notes"],
                user=request.user,
                shift=shift,
                membership=request.membership,
                request=request,
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(request, f"Payment {payment.receipt_number} recorded.")
    else:
        messages.error(request, "Invalid payment details.")
    return redirect("customers:detail", public_id=public_id)


def _statement_entries(
    business, customer, branch_id=None, *, allowed_branch_ids=None
):
    """Full chronological ledger for one customer.

    Debits: opening balance, the unpaid (credit) portion of each sale.
    Credits: standalone collections, per-sale settlement payments,
    returns credited to the account.
    """
    from apps.sales.models import Sale, SaleReturn

    entries = []
    include_opening_balance = (
        customer.opening_balance
        and (branch_id is None or customer.home_branch_id == branch_id)
        and (
            allowed_branch_ids is None
            or customer.home_branch_id in allowed_branch_ids
        )
    )
    if include_opening_balance:
        entries.append({"date": customer.created_at, "type": "Opening balance",
                        "ref": "", "debit": customer.opening_balance,
                        "credit": 0, "notes": ""})
    sales_qs = Sale.objects.for_business(business).filter(
        customer=customer
    ).exclude(status__in=[Sale.Status.DRAFT, Sale.Status.VOIDED])
    if branch_id:
        sales_qs = sales_qs.filter(branch_id=branch_id)
    elif allowed_branch_ids is not None:
        sales_qs = sales_qs.filter(branch_id__in=allowed_branch_ids)
    NON_CASH_KINDS = ("customer_credit", "store_credit")
    for s in sales_qs.prefetch_related("payments__method"):
        # Debit = the credit extended at sale time: total minus real money
        # paid at the counter (credit/store-credit tenders are not money in)
        real_payments = [p for p in s.payments.all()
                         if p.method.kind not in NON_CASH_KINDS]
        first_day_paid = sum(
            (p.amount for p in real_payments
             if p.payment_date == s.sale_date.date()), start=0
        )
        credit_part = s.total - first_day_paid
        if credit_part > 0:
            entries.append({"date": s.sale_date, "type": "Credit sale",
                            "ref": s.invoice_number, "debit": credit_part,
                            "credit": 0, "notes": s.notes[:60]})
        # Later settlement payments against this sale = credits on their dates
        import datetime as _dt

        from django.utils import timezone as _tz

        for p in real_payments:
            if p.payment_date and p.payment_date != s.sale_date.date():
                when = _tz.make_aware(
                    _dt.datetime.combine(p.payment_date, _dt.time(12, 0)))
                entries.append({"date": when, "type": "Invoice payment",
                                "ref": s.invoice_number, "debit": 0,
                                "credit": p.amount, "notes": p.notes[:60]})
    pay_qs = CustomerPayment.objects.for_business(business).filter(
        customer=customer, kind=CustomerPayment.Kind.COLLECTION)
    if branch_id:
        pay_qs = pay_qs.filter(branch_id=branch_id)
    elif allowed_branch_ids is not None:
        pay_qs = pay_qs.filter(branch_id__in=allowed_branch_ids)
    for p in pay_qs:
        entries.append({"date": p.created_at, "type": "Payment received",
                        "ref": p.receipt_number, "debit": 0, "credit": p.amount,
                        "notes": p.notes[:60]})
    ret_qs = SaleReturn.objects.for_business(business).filter(
        customer=customer, refund_method="customer_account")
    if branch_id:
        ret_qs = ret_qs.filter(branch_id=branch_id)
    elif allowed_branch_ids is not None:
        ret_qs = ret_qs.filter(branch_id__in=allowed_branch_ids)
    for r in ret_qs:
        entries.append({"date": r.created_at, "type": "Return credited",
                        "ref": r.return_number, "debit": 0,
                        "credit": r.refund_amount, "notes": r.reason[:60]})
    entries.sort(key=lambda e: e["date"])
    balance = 0
    for e in entries:
        balance += e["debit"] - e["credit"]
        e["balance"] = balance
    return entries, balance


@module_permission_required("pos_core", "customers.export")
def customer_export(request):
    """Export the filtered customer list as CSV or Excel."""
    from apps.reports import exports

    selected_branch = _selected_branch(request, required=True)
    qs = _customer_queryset(request).filter(home_branch=selected_branch)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(full_name__icontains=q) | Q(mobile__icontains=q) |
                       Q(code__icontains=q) | Q(email__icontains=q))
    flt = request.GET.get("filter", "")
    credit_access = _credit_decision(
        request, permission_code="customers.export"
    ).allowed
    credit_balance_access = credit_access
    if flt == "credit":
        if not credit_balance_access:
            require_access(
                request,
                "customer_credit",
                permission_code="customers.export",
                action=AccessAction.READ,
                scope_allowed=False,
            )
        qs = qs.filter(balance__gt=0)
    elif flt == "inactive":
        qs = qs.filter(is_active=False)
    data = services.export_dataset(
        request.business,
        qs.order_by("full_name"),
        include_credit=credit_balance_access,
    )
    if request.GET.get("format") == "xlsx":
        return exports.export_xlsx("customers", data)
    return exports.export_csv("customers", data)


@module_permission_required("pos_core", "customers.import")
def customer_import_template(request):
    from apps.reports import exports

    selected_branch = _selected_branch(request, required=True)
    credit_write = _credit_decision(
        request,
        permission_code="customers.import",
        action=AccessAction.WRITE,
    ).allowed
    columns = [
        c.title()
        for c in services.import_columns(
            request.business, include_credit=credit_write
        )
    ]
    sample = [selected_branch.code, selected_branch.name,
              "CUST-00001", "Sample Customer", "99000000", "99000000",
              "sample@example.com", "123 Market St", "Muscat", "Oman",
              "Retail"]
    if credit_write:
        sample += ["100.000", "0.000"]
    sample += ["VIP notes", "Active"]
    sample += ["Sample value" for _ in request.business.settings.more_option_labels]
    data = {
        "columns": columns,
        "rows": [sample],
        "totals": None,
    }
    if request.GET.get("format") == "xlsx":
        return exports.export_xlsx("customer_import_template", data)
    return exports.export_csv("customer_import_template", data)


@module_permission_required("pos_core", "customers.import")
def customer_import(request):
    from apps.core.imports import error_report_response, parse_tabular_file

    selected_branch = _selected_branch(
        request,
        required=True,
        post_field="branch",
    )

    # Stash the last error report in session for download
    if request.GET.get("errors") == "1":
        errors = request.session.get("customer_import_errors", [])
        return error_report_response("customer_import_errors.csv", errors)

    results = None
    import_error = None
    if request.method == "POST":
        try:
            subscriptions.require_operational(request.business)
        except subscriptions.SubscriptionInactive as exc:
            messages.error(request, str(exc))
            return redirect("customers:list")
        upload = request.FILES.get("file")
        mode = request.POST.get("mode", "skip")
        if not upload:
            import_error = "Choose a file to import."
            messages.error(request, "Choose a file to import.")
        else:
            rows, parse_error = parse_tabular_file(upload)
            if parse_error:
                import_error = parse_error
                messages.error(request, parse_error)
            else:
                summary, errors = services.import_customers(
                    business=request.business, branch=selected_branch,
                    rows=rows, mode=mode,
                    user=request.user, membership=request.membership,
                    request=request)
                request.session["customer_import_errors"] = errors
                results = {"summary": summary, "errors": errors,
                           "total": len(rows)}
                audit.log("customer.imported", request=request,
                          module="customers",
                          description=(f"Customer import ({mode}): "
                                       f"{summary['imported']} new, "
                                       f"{summary['updated']} updated, "
                                       f"{summary['skipped']} skipped, "
                                       f"{summary['failed']} failed."))
    credit_write = _credit_decision(
        request,
        permission_code="customers.import",
        action=AccessAction.WRITE,
    ).allowed
    return render(request, "customers/import.html", {
        "results": results, "import_error": import_error,
        "active_nav": "customers",
        "selected_branch": selected_branch,
        "columns": [
            c.title()
            for c in services.import_columns(
                request.business, include_credit=credit_write
            )
        ],
    })


@module_permission_required(
    "customer_credit", "customers.view", action=AccessAction.READ
)
def customer_statement(request, public_id):
    """Date-filterable statement with running balance and exports."""
    import datetime as _dt

    customer = get_tenant_object(
        _customer_queryset(request), request.business, public_id=public_id
    )
    from apps.branches.models import Branch

    branches = Branch.objects.for_business(request.business).filter(
        is_active=True,
        usage_type=Branch.UsageType.SALES_BRANCH,
    )
    allowed_branch_ids = request.membership.allowed_branch_ids
    if allowed_branch_ids is not None:
        branches = branches.filter(id__in=allowed_branch_ids)
    branch_raw = request.GET.get("branch", "")
    if branch_raw and not branch_raw.isdigit():
        raise Http404
    branch_id = int(branch_raw) if branch_raw.isdigit() else None
    branch = None
    if branch_id is not None:
        branch = branches.filter(id=branch_id).first()
        if branch is None:
            raise Http404
    entries, closing_balance = _statement_entries(
        request.business,
        customer,
        branch_id=branch_id,
        allowed_branch_ids=allowed_branch_ids,
    )

    # Period slice with brought-forward balance
    date_from, date_to = resolve_date_range(request.GET, request.business)
    brought_forward = None
    if date_from:
        cutoff = _dt.date.fromisoformat(date_from)
        before = [e for e in entries if e["date"].date() < cutoff]
        bf = before[-1]["balance"] if before else 0
        entries = [e for e in entries if e["date"].date() >= cutoff]
        brought_forward = bf
    if date_to:
        cutoff = _dt.date.fromisoformat(date_to)
        entries = [e for e in entries if e["date"].date() <= cutoff]

    opening_balance = brought_forward if brought_forward is not None else 0
    closing = entries[-1]["balance"] if entries else opening_balance
    total_debits = sum((e["debit"] for e in entries), start=0)
    total_credits = sum((e["credit"] for e in entries), start=0)

    export = request.GET.get("export", "")
    if export in ("csv", "pdf"):
        from apps.audit import services as audit
        from apps.reports import exports

        audit.log("customer.statement_exported", request=request,
                  module="customers", obj=customer,
                  description=f"Statement for {customer.full_name} exported "
                              f"as {export} ({date_from or 'start'} → "
                              f"{date_to or 'today'}).")
        title = f"statement-{customer.code}"
        if export == "csv":
            rows = []
            if brought_forward is not None:
                rows.append(["", "Balance brought forward", "", "", "",
                             brought_forward, ""])
            rows += [[e["date"].strftime("%Y-%m-%d"), e["type"], e["ref"],
                      e["debit"] or "", e["credit"] or "", e["balance"],
                      e["notes"]] for e in entries]
            data = {
                "columns": ["Date", "Type", "Reference", "Debit", "Credit",
                            "Balance", "Notes"],
                "rows": rows,
                "totals": ["", "CLOSING BALANCE", "", str(total_debits),
                           str(total_credits), closing, ""],
            }
            return exports.export_csv(title, data)

        # Dedicated professional landscape statement PDF
        pdf = render_pdf("invoices/customer_statement_pdf.html", {
            "business": request.business, "customer": customer, "branch": branch,
            "entries": entries, "opening_balance": opening_balance,
            "brought_forward": brought_forward, "closing_balance": closing,
            "total_debits": total_debits, "total_credits": total_credits,
            "date_from": date_from, "date_to": date_to,
            "generated_at": timezone.localtime(),
            "precision": request.business.currency_precision,
            "PRODUCT_NAME": settings.PRODUCT_NAME,
        })
        from django.http import HttpResponse

        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{title}.pdf"'
        return response

    filter_querystring = date_range_querystring(
        request.GET,
        date_from,
        date_to,
    )
    return render(request, "customers/statement.html", {
        "customer": customer, "entries": entries, "active_nav": "customers",
        "closing_balance": entries[-1]["balance"] if entries
                           else (brought_forward or 0),
        "brought_forward": brought_forward,
        "date_from": date_from, "date_to": date_to,
        "branches": branches,
        "branch_id": branch_id,
        "filter_querystring": filter_querystring,
    })
