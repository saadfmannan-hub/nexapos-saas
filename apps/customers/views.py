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


@require_permission("customers.view")
def customer_statement(request, public_id):
    """Chronological statement with running balance."""
    from apps.sales.models import Sale, SaleReturn

    customer = get_tenant_object(Customer, request.business, public_id=public_id)
    entries = []
    if customer.opening_balance:
        entries.append({"date": customer.created_at, "type": "Opening balance",
                        "ref": "", "debit": customer.opening_balance, "credit": 0})
    for s in Sale.objects.for_business(request.business).filter(
        customer=customer
    ).exclude(status__in=[Sale.Status.DRAFT, Sale.Status.VOIDED]):
        credit_part = s.total - s.amount_paid
        if credit_part > 0:
            entries.append({"date": s.sale_date, "type": "Credit sale",
                            "ref": s.invoice_number, "debit": credit_part, "credit": 0})
    for p in CustomerPayment.objects.for_business(request.business).filter(
        customer=customer, kind=CustomerPayment.Kind.COLLECTION
    ):
        entries.append({"date": p.created_at, "type": "Payment received",
                        "ref": p.receipt_number, "debit": 0, "credit": p.amount})
    for r in SaleReturn.objects.for_business(request.business).filter(
        customer=customer, refund_method="customer_account"
    ):
        entries.append({"date": r.created_at, "type": "Return credited",
                        "ref": r.return_number, "debit": 0, "credit": r.refund_amount})
    entries.sort(key=lambda e: e["date"])
    balance = 0
    for e in entries:
        balance += e["debit"] - e["credit"]
        e["balance"] = balance
    return render(request, "customers/statement.html", {
        "customer": customer, "entries": entries, "active_nav": "customers",
        "closing_balance": balance,
    })
