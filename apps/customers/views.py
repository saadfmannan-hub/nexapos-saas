from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, Max, Q, Sum
from django.shortcuts import redirect, render

from apps.audit import services as audit
from apps.core.decorators import require_permission
from apps.core.mixins import get_tenant_object
from apps.core.money import money
from apps.registers import services as register_services
from apps.subscriptions import services as subscriptions

from . import services
from .forms import CustomerForm, CustomerPaymentForm
from .models import Customer, CustomerPayment


def _qs_without_page(request):
    params = request.GET.copy()
    params.pop("page", None)
    encoded = params.urlencode()
    return f"{encoded}&" if encoded else ""


@require_permission("customers.view")
def customer_list(request):
    qs = Customer.objects.for_business(request.business).select_related("group")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(full_name__icontains=q) | Q(mobile__icontains=q) |
                       Q(code__icontains=q) | Q(email__icontains=q))
    flt = request.GET.get("filter", "")
    if flt == "credit":
        qs = qs.filter(balance__gt=0)
    elif flt == "inactive":
        qs = qs.filter(is_active=False)
    receivables = (
        Customer.objects.for_business(request.business)
        .filter(balance__gt=0).aggregate(t=Sum("balance"))["t"] or 0
    )
    paginator = Paginator(qs.order_by("-is_walk_in", "full_name"), 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    c_cur, c_lim, _ = subscriptions.limit_state(request.business, "customers")
    return render(request, "customers/list.html", {
        "page_obj": page_obj, "q": q, "active_nav": "customers",
        "receivables": receivables, "customer_count": c_cur, "customer_limit": c_lim,
        "querystring": _qs_without_page(request),
    })


@require_permission("customers.manage")
def customer_form(request, public_id=None):
    instance = None
    if public_id:
        instance = get_tenant_object(Customer, request.business, public_id=public_id)
    else:
        from apps.subscriptions.helpers import guard_limit

        blocked = guard_limit(request, "customers")
        if blocked:
            return blocked
    form = CustomerForm(request.business, request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        customer = form.save(commit=False)
        customer.business = request.business
        if not customer.code:
            customer.code = services.next_customer_code(request.business)
        if instance is None and customer.opening_balance:
            customer.balance = customer.opening_balance
        customer.save()
        audit.log("customer.saved", request=request, module="customers", obj=customer,
                  description=f"Customer '{customer.full_name}' saved.")
        messages.success(request, "Customer saved.")
        return redirect("customers:detail", public_id=customer.public_id)
    return render(request, "customers/form.html",
                  {"form": form, "customer": instance, "active_nav": "customers"})


@require_permission("customers.view")
def customer_detail(request, public_id):
    from apps.sales.models import Sale, SaleReturn

    customer = get_tenant_object(Customer, request.business, public_id=public_id)
    sales = (
        Sale.objects.for_business(request.business)
        .filter(customer=customer)
        .exclude(status=Sale.Status.DRAFT)
        .select_related("branch")
        .order_by("-sale_date")
    )
    # Aggregate aliases must not shadow field names ("total" would break
    # Avg("total") with "Cannot compute Avg('total'): 'total' is an aggregate").
    stats = sales.exclude(status=Sale.Status.VOIDED).aggregate(
        sum_total=Sum("total"), paid=Sum("amount_paid"), count=Count("id"),
        avg=Avg("total"), last=Max("sale_date"),
    )
    stats["total"] = stats.pop("sum_total")
    payments = (
        CustomerPayment.objects.for_business(request.business)
        .filter(customer=customer).select_related("payment_method", "received_by")
        .order_by("-created_at")
    )
    returns = (
        SaleReturn.objects.for_business(request.business)
        .filter(customer=customer).select_related("sale")
        .order_by("-created_at")[:20]
    )
    payment_form = CustomerPaymentForm(request.business)
    return render(request, "customers/detail.html", {
        "customer": customer, "sales": sales[:25], "stats": stats,
        "payments": payments[:25], "returns": returns,
        "payment_form": payment_form, "active_nav": "customers",
        "can_collect": request.membership.has_perm("customers.payments"),
    })


@require_permission("customers.payments")
def customer_payment(request, public_id):
    customer = get_tenant_object(Customer, request.business, public_id=public_id)
    if request.method != "POST":
        return redirect("customers:detail", public_id=public_id)
    form = CustomerPaymentForm(request.business, request.POST)
    if form.is_valid():
        amount = money(form.cleaned_data["amount"])
        try:
            subscriptions.require_operational(request.business)
        except subscriptions.SubscriptionInactive as exc:
            messages.error(request, str(exc))
            return redirect("customers:detail", public_id=public_id)
        if amount > customer.balance:
            messages.error(request, "Payment exceeds the customer's outstanding "
                                    "balance. Use store credit for overpayments.")
            return redirect("customers:detail", public_id=public_id)
        with transaction.atomic():
            n = CustomerPayment.objects.for_business(request.business).count() + 1
            while CustomerPayment.objects.for_business(request.business).filter(
                receipt_number=f"RCV-{n:06d}"
            ).exists():
                n += 1
            shift = register_services.get_open_shift(request.business, request.user)
            payment = CustomerPayment.objects.create(
                business=request.business,
                receipt_number=f"RCV-{n:06d}",
                customer=customer,
                kind=CustomerPayment.Kind.COLLECTION,
                amount=amount,
                payment_method=form.cleaned_data["payment_method"],
                reference=form.cleaned_data["reference"],
                notes=form.cleaned_data["notes"],
                received_by=request.user,
                shift=shift,
            )
            services.apply_balance_change(customer.id, -amount)
            audit.log("customer.payment", request=request, module="customers",
                      obj=payment,
                      description=f"Collected {amount} from {customer.full_name} "
                                  f"({payment.receipt_number}).")
        messages.success(request, f"Payment {payment.receipt_number} recorded.")
    else:
        messages.error(request, "Invalid payment details.")
    return redirect("customers:detail", public_id=public_id)


def _statement_entries(business, customer, branch_id=None):
    """Full chronological ledger for one customer.

    Debits: opening balance, the unpaid (credit) portion of each sale.
    Credits: standalone collections, per-sale settlement payments,
    returns credited to the account.
    """
    from apps.sales.models import Sale, SalePayment, SaleReturn

    entries = []
    if customer.opening_balance:
        entries.append({"date": customer.created_at, "type": "Opening balance",
                        "ref": "", "debit": customer.opening_balance,
                        "credit": 0, "notes": ""})
    sales_qs = Sale.objects.for_business(business).filter(
        customer=customer
    ).exclude(status__in=[Sale.Status.DRAFT, Sale.Status.VOIDED])
    if branch_id:
        sales_qs = sales_qs.filter(branch_id=branch_id)
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
    for p in pay_qs:
        entries.append({"date": p.created_at, "type": "Payment received",
                        "ref": p.receipt_number, "debit": 0, "credit": p.amount,
                        "notes": p.notes[:60]})
    ret_qs = SaleReturn.objects.for_business(business).filter(
        customer=customer, refund_method="customer_account")
    if branch_id:
        ret_qs = ret_qs.filter(branch_id=branch_id)
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


@require_permission("customers.view")
def customer_statement(request, public_id):
    """Date-filterable statement with running balance and exports."""
    import datetime as _dt

    customer = get_tenant_object(Customer, request.business, public_id=public_id)
    branch_raw = request.GET.get("branch", "")
    branch_id = int(branch_raw) if branch_raw.isdigit() else None
    entries, closing_balance = _statement_entries(
        request.business, customer, branch_id=branch_id)

    # Period slice with brought-forward balance
    date_from, date_to = request.GET.get("from", ""), request.GET.get("to", "")
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

    export = request.GET.get("export", "")
    if export in ("csv", "pdf"):
        from apps.audit import services as audit
        from apps.reports import exports

        rows = []
        if brought_forward is not None:
            rows.append(["", "Balance brought forward", "", "", "", brought_forward, ""])
        rows += [[e["date"].strftime("%Y-%m-%d"), e["type"], e["ref"],
                  e["debit"] or "", e["credit"] or "", e["balance"],
                  e["notes"]] for e in entries]
        data = {
            "columns": ["Date", "Type", "Reference", "Debit", "Credit",
                        "Balance", "Notes"],
            "rows": rows,
            "totals": ["", "CLOSING BALANCE", "", "", "",
                       entries[-1]["balance"] if entries else (brought_forward or 0),
                       ""],
        }
        audit.log("customer.statement_exported", request=request,
                  module="customers", obj=customer,
                  description=f"Statement for {customer.full_name} exported "
                              f"as {export} ({date_from or 'start'} → "
                              f"{date_to or 'today'}).")
        title = f"statement-{customer.code}"
        if export == "csv":
            return exports.export_csv(title, data)
        return exports.export_pdf(
            f"Customer statement — {customer.full_name}", data,
            request.business, f"{date_from or 'start'} → {date_to or 'today'}")

    from apps.branches.models import Branch

    return render(request, "customers/statement.html", {
        "customer": customer, "entries": entries, "active_nav": "customers",
        "closing_balance": entries[-1]["balance"] if entries
                           else (brought_forward or 0),
        "brought_forward": brought_forward,
        "date_from": date_from, "date_to": date_to,
        "branches": Branch.objects.for_business(request.business).filter(
            is_active=True),
        "branch_id": branch_id,
    })
