"""Platform Super Admin — separate from all tenant dashboards.

Access requires user.is_platform_admin. Tenant business data is NOT
shown here beyond operational metadata (name, owner, counts, status)
unless an active SupportAccessGrant exists.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal
from functools import wraps

from django import forms
from django.contrib import messages
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.views.decorators.http import require_POST

from apps.accounts.models import LoginHistory, Membership, User
from apps.audit import services as audit
from apps.audit.models import AuditLog
from apps.core.currencies import currency_choices, precision_for
from apps.core.date_ranges import business_localtime, business_timezone
from apps.subscriptions.models import Coupon, Plan, Subscription, SubscriptionPayment
from apps.subscriptions.services import (
    PaymentAlreadyReversed,
    PaymentReversalReasonRequired,
    capture_subscription_state,
    payment_audit_values,
    reverse_subscription_payment,
)
from apps.tenants.models import Business

from .models import Announcement, SupportAccessGrant


def platform_admin_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("accounts:login")
        if not request.user.is_platform_staff:
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return wrapper


def local_date_start(value, business=None):
    return timezone.make_aware(
        datetime.combine(value, time.min),
        business_timezone(business),
    )


def default_renewal_start(subscription):
    end = subscription.current_period_end if subscription else None
    if end and end > timezone.now():
        return business_localtime(
            subscription.business, value=end
        ).date()
    return timezone.localdate()


def build_subscription_history(business, subscription):
    history = []
    if business.created_at:
        history.append({
            "date": business.created_at,
            "label": "Created",
            "detail": f"Business created for {business.owner.email}",
        })
    if subscription and subscription.trial_ends_at:
        history.append({
            "date": subscription.created_at,
            "label": "Trial started",
            "detail": f"Trial on {subscription.plan.name}",
        })
    for payment in SubscriptionPayment.objects.filter(business=business)[:25]:
        reversed_suffix = (
            f" (reversed: {payment.reversal_reason})"
            if payment.is_reversed else ""
        )
        history.append({
            "date": payment.created_at,
            "label": "Payment reversed" if payment.is_reversed else "Payment recorded",
            "detail": f"{payment.amount} {payment.currency_code} via "
                      f"{payment.get_method_display()}{reversed_suffix}",
        })
    for row in AuditLog.objects.filter(
        business=business,
        action__in=[
            "platform.business_created", "platform.business_suspended",
            "platform.business_reactivated", "platform.subscription_renewed",
            "platform.subscription_plan_changed", "platform.subscription_payment_recorded",
            "platform.subscription_payment_edited",
            "platform.subscription_payment_reversed",
            "platform.subscription_extended", "platform.trial_extended",
        ],
    )[:50]:
        label = row.action.replace("platform.", "").replace("_", " ").title()
        history.append({"date": row.created_at, "label": label, "detail": row.description})
    return sorted(history, key=lambda item: item["date"], reverse=True)[:25]


@platform_admin_required
def dashboard(request):
    from decimal import Decimal

    now = timezone.now()
    today = timezone.localdate()
    month_start = today.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)

    businesses = Business.objects.all()
    subs = list(Subscription.objects.select_related("plan", "business"))

    # ---- business metrics by display status -------------------------------
    status_counts = {}
    mrr = Decimal("0")
    for sub in subs:
        st = sub.display_status
        status_counts[st] = status_counts.get(st, 0) + 1
        # MRR: monthly-equivalent price of currently operational paid subs
        if sub.is_operational and sub.effective_status != Subscription.Status.TRIAL:
            price = sub.plan.monthly_price or Decimal("0")
            if sub.billing_cycle == "annual" and sub.plan.annual_price:
                price = (sub.plan.annual_price / Decimal("12"))
            mrr += price

    biz_metrics = {
        "total": businesses.count(),
        "active": businesses.filter(is_active=True).count(),
        "suspended": businesses.filter(is_active=False).count(),
        "trial": status_counts.get("trial", 0),
        "expiring_soon": status_counts.get("expiring_soon", 0),
        "expired": status_counts.get("expired", 0),
        "paid_active": status_counts.get("active", 0),
    }

    # ---- revenue metrics ---------------------------------------------------
    payments = SubscriptionPayment.objects.active()
    revenue_total = payments.aggregate(t=Sum("amount"))["t"] or Decimal("0")
    revenue_this_month = payments.filter(
        created_at__date__gte=month_start).aggregate(t=Sum("amount"))["t"] or Decimal("0")
    revenue_last_month = payments.filter(
        created_at__date__gte=last_month_start,
        created_at__date__lte=last_month_end).aggregate(
        t=Sum("amount"))["t"] or Decimal("0")

    # ---- user metrics ------------------------------------------------------
    active_user_ids = LoginHistory.objects.filter(
        success=True, created_at__gte=now - timedelta(days=30)
    ).values_list("user_id", flat=True).distinct()
    user_metrics = {
        "total": User.objects.count(),
        "active": User.objects.filter(pk__in=list(active_user_ids)).count(),
    }

    # ---- plan distribution (chart) ----------------------------------------
    plan_dist = (
        Subscription.objects.values("plan__name")
        .annotate(c=Count("id")).order_by("-c")
    )
    chart_plans = {
        "labels": [r["plan__name"] for r in plan_dist],
        "data": [r["c"] for r in plan_dist],
    }
    chart_status = {
        "labels": list(status_counts.keys()),
        "data": list(status_counts.values()),
    }

    expiring = [s for s in subs if s.is_expiring_soon][:10]
    recent_failed_logins = LoginHistory.objects.filter(
        success=False, created_at__gte=now - timedelta(days=1)).count()

    return render(request, "platformadmin/dashboard.html", {
        "biz": biz_metrics,
        "mrr": mrr,
        "revenue_total": revenue_total,
        "revenue_this_month": revenue_this_month,
        "revenue_last_month": revenue_last_month,
        "user_metrics": user_metrics,
        "status_counts": status_counts,
        "chart_plans": chart_plans,
        "chart_status": chart_status,
        "recent_failed_logins": recent_failed_logins,
        "recent_businesses": businesses.select_related(
            "owner", "subscription__plan").order_by("-created_at")[:8],
        "expiring": expiring,
        "active_grants": SupportAccessGrant.objects.filter(
            revoked_at__isnull=True, expires_at__gt=now).count(),
    })


@platform_admin_required
def business_list(request):
    qs = Business.objects.select_related("owner", "subscription__plan").annotate(
        member_count=Count("memberships", distinct=True),
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(owner__email__icontains=q))
    status = request.GET.get("status", "")
    if status == "suspended":
        qs = qs.filter(is_active=False)
    elif status == "active":
        qs = qs.filter(is_active=True)
    paginator = Paginator(qs.order_by("-created_at"), 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "platformadmin/business_list.html",
                  {"page_obj": page_obj, "q": q, "querystring": ""})


@platform_admin_required
def business_detail(request, public_id):
    try:
        business = Business.objects.select_related(
            "owner", "subscription__plan").get(public_id=public_id)
    except Business.DoesNotExist:
        from django.http import Http404
        raise Http404 from None
    sub = getattr(business, "subscription", None)
    members = Membership.objects.filter(business=business).select_related(
        "user", "role")
    payment_queryset = SubscriptionPayment.objects.filter(
        subscription__business=business,
    ).select_related("recorded_by", "reversed_by")
    payments = payment_queryset[:10]
    last_active_payment = payment_queryset.active().first()
    grants = SupportAccessGrant.objects.filter(business=business)[:10]
    plans = Plan.objects.filter(is_active=True)
    # Operational metadata only — counts, not data
    from apps.branches.models import Branch, Warehouse
    from apps.catalog.models import Product
    from apps.customers.models import Customer
    from apps.sales.models import Sale

    month_start = timezone.localdate().replace(day=1)
    usage = {
        "branches": Branch.objects.for_business(business).count(),
        "warehouses": Warehouse.objects.for_business(business).count(),
        "products": Product.objects.for_business(business).count(),
        "customers": Customer.objects.for_business(business).count(),
        "invoices": Sale.objects.for_business(business).exclude(
            status="draft").count(),
        "monthly_invoices": Sale.objects.for_business(business).filter(
            created_at__date__gte=month_start,
        ).exclude(status__in=["draft", "held"]).count(),
        "users": members.count(),
        "storage_mb": None,
    }
    payment_summary = SubscriptionPayment.objects.active().filter(
        business=business,
    ).aggregate(total=Sum("amount"), count=Count("id"))
    renewal_initial = {"plan": sub.plan_id if sub else None}
    if sub:
        renewal_initial.update({
            "start_date": default_renewal_start(sub),
            "payment_amount": sub.plan.monthly_price or Decimal("0"),
            "payment_method": "manual",
        })
    renewal_form = SubscriptionRenewalForm(initial=renewal_initial, prefix="renew")
    plan_change_form = PlanChangeForm(
        initial={
            "new_plan": sub.plan_id if sub else None,
            "effective_date": timezone.localdate(),
        },
        prefix="plan",
    )
    payment_form = SubscriptionPaymentForm(
        initial={"payment_method": "manual", "payment_date": timezone.localdate()},
        prefix="payment",
    )
    return render(request, "platformadmin/business_detail.html", {
        "business": business, "sub": sub, "members": members,
        "payments": payments, "grants": grants, "plans": plans, "usage": usage,
        "payment_summary": payment_summary, "renewal_form": renewal_form,
        "plan_change_form": plan_change_form, "payment_form": payment_form,
        "last_active_payment": last_active_payment,
        "history": build_subscription_history(business, sub),
        "now": timezone.now(), "pa_nav": "businesses",
    })


def record_subscription_payment(
    *,
    business,
    subscription,
    amount,
    method,
    reference="",
    payment_date=None,
    period_start=None,
    period_end=None,
    notes="",
    user=None,
    subscription_state_before=None,
    subscription_state_after=None,
):
    return SubscriptionPayment.objects.create(
        business=business,
        subscription=subscription,
        amount=amount,
        currency_code=subscription.plan.currency_code,
        method=method,
        reference=reference[:120],
        payment_date=payment_date or timezone.localdate(),
        period_start=period_start,
        period_end=period_end,
        recorded_by=user,
        notes=notes,
        subscription_state_before=subscription_state_before or {},
        subscription_state_after=subscription_state_after or {},
    )


@platform_admin_required
@transaction.atomic
def subscription_payment_edit(request, business_public_id, payment_public_id):
    business = get_object_or_404(Business, public_id=business_public_id)
    payment = get_object_or_404(
        SubscriptionPayment.objects.select_for_update().select_related(
            "subscription",
        ),
        public_id=payment_public_id,
        business=business,
        subscription__business=business,
    )
    if payment.is_reversed:
        return HttpResponseBadRequest("Reversed payments cannot be edited.")

    old_values = payment_audit_values(payment)
    form = SubscriptionPaymentEditForm(request.POST or None, instance=payment)
    if request.method == "POST" and form.is_valid():
        payment = form.save(commit=False)
        payment.save(update_fields=[
            "payment_date", "method", "reference", "amount", "notes",
            "updated_at",
        ])
        audit.log(
            "platform.subscription_payment_edited",
            business=business,
            user=request.user,
            request=request,
            module="platformadmin",
            obj=payment,
            old_values=old_values,
            new_values=payment_audit_values(payment),
            description=(
                f"Subscription payment edited: {payment.amount} "
                f"{payment.currency_code}."
            ),
        )
        messages.success(request, "Payment updated.")
        return redirect(
            "platformadmin:business_detail",
            public_id=business.public_id,
        )

    return render(request, "platformadmin/payment_edit.html", {
        "business": business,
        "payment": payment,
        "form": form,
        "pa_nav": "businesses",
    })


@platform_admin_required
@require_POST
@transaction.atomic
def subscription_payment_reverse(request, business_public_id, payment_public_id):
    business = get_object_or_404(Business, public_id=business_public_id)
    payment = get_object_or_404(
        SubscriptionPayment,
        public_id=payment_public_id,
        business=business,
        subscription__business=business,
    )
    form = SubscriptionPaymentReversalForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest("A reversal reason is required.")

    try:
        (
            payment,
            payment_before,
            subscription_before,
            subscription_after,
        ) = reverse_subscription_payment(
            payment_id=payment.pk,
            reversed_by=request.user,
            reason=form.cleaned_data["reversal_reason"],
        )
    except PaymentAlreadyReversed:
        return HttpResponseBadRequest("This payment has already been reversed.")
    except PaymentReversalReasonRequired:
        return HttpResponseBadRequest("A reversal reason is required.")

    audit.log(
        "platform.subscription_payment_reversed",
        business=business,
        user=request.user,
        request=request,
        module="platformadmin",
        obj=payment,
        old_values={
            "payment": payment_before,
            "subscription": subscription_before,
        },
        new_values={
            "payment": payment_audit_values(payment),
            "subscription": subscription_after,
        },
        description=(
            f"Subscription payment reversed: {payment.amount} "
            f"{payment.currency_code}. Reason: {payment.reversal_reason}"
        ),
    )
    messages.success(request, "Payment reversed and subscription totals updated.")
    return redirect(
        "platformadmin:business_detail",
        public_id=business.public_id,
    )


@platform_admin_required
@transaction.atomic
def business_action(request, public_id, action):
    if request.method != "POST":
        return redirect("platformadmin:business_list")
    try:
        business = Business.objects.select_for_update().get(public_id=public_id)
    except Business.DoesNotExist:
        from django.http import Http404
        raise Http404 from None
    sub = getattr(business, "subscription", None)

    if action == "suspend":
        reason = request.POST.get("reason", "")[:255]
        business.is_active = False
        business.suspended_at = timezone.now()
        business.suspended_by = request.user
        business.suspension_reason = reason
        business.reactivated_at = None
        business.reactivated_by = None
        business.save()
        if sub:
            sub.status = Subscription.Status.SUSPENDED
            sub.save(update_fields=["status", "updated_at"])
        audit.log("platform.business_suspended", business=business,
                  user=request.user, request=request, module="platformadmin",
                  obj=business, description=f"Suspended: {reason}")
        messages.success(request, f"{business.name} suspended. No data was deleted.")
    elif action == "activate":
        business.is_active = True
        business.suspended_at = None
        business.suspension_reason = ""
        business.reactivated_at = timezone.now()
        business.reactivated_by = request.user
        business.save()
        if sub and sub.status == Subscription.Status.SUSPENDED:
            sub.status = Subscription.Status.ACTIVE
            if not sub.current_period_end or sub.current_period_end < timezone.now():
                sub.current_period_start = timezone.now()
                sub.current_period_end = timezone.now() + timedelta(days=30)
            sub.save()
        audit.log("platform.business_reactivated", business=business,
                  user=request.user, request=request, module="platformadmin",
                  obj=business, description="Business reactivated — access restored.")
        messages.success(request, f"{business.name} reactivated. Access restored immediately.")
    elif action == "renew" and sub:
        form = SubscriptionRenewalForm(request.POST, prefix="renew")
        if form.is_valid():
            cd = form.cleaned_data
            plan = cd["plan"]
            renewal_type = cd["renewal_type"]
            start_date = cd.get("start_date") or default_renewal_start(sub)
            explicit_end_date = cd.get("end_date")
            if explicit_end_date:
                end_date = explicit_end_date
            elif renewal_type == "monthly":
                end_date = start_date + timedelta(days=30)
            elif renewal_type == "annual":
                end_date = start_date + timedelta(days=365)
            else:
                # The form requires this value for a custom renewal.
                end_date = cd["end_date"]
            if renewal_type == "monthly":
                sub.billing_cycle = "monthly"
            elif renewal_type == "annual":
                sub.billing_cycle = "annual"
            period_start = local_date_start(start_date, business)
            period_end = local_date_start(end_date, business)
            old_values = {
                "plan": sub.plan.name,
                "status": sub.status,
                "current_period_end": (
                    sub.current_period_end.isoformat()
                    if sub.current_period_end else None
                ),
            }
            subscription_state_before = capture_subscription_state(sub)
            sub.plan = plan
            sub.status = Subscription.Status.ACTIVE
            sub.trial_ends_at = None
            sub.current_period_start = period_start
            sub.current_period_end = period_end
            sub.notes = cd.get("notes", "")
            sub.save()
            subscription_state_after = capture_subscription_state(sub)
            if not business.is_active:
                business.is_active = True
                business.suspended_at = None
                business.suspension_reason = ""
                business.reactivated_at = timezone.now()
                business.reactivated_by = request.user
                business.save(update_fields=[
                    "is_active", "suspended_at", "suspension_reason",
                    "reactivated_at", "reactivated_by", "updated_at",
                ])
            payment = record_subscription_payment(
                business=business,
                subscription=sub,
                amount=cd["payment_amount"],
                method=cd["payment_method"],
                reference=cd.get("payment_reference", ""),
                period_start=period_start,
                period_end=period_end,
                notes=cd.get("notes", ""),
                user=request.user,
                subscription_state_before=subscription_state_before,
                subscription_state_after=subscription_state_after,
            )
            audit.log(
                "platform.subscription_renewed", business=business,
                user=request.user, request=request, module="platformadmin",
                obj=sub, old_values=old_values,
                new_values={
                    "plan": plan.name,
                    "renewal_type": renewal_type,
                    "current_period_start": period_start.isoformat(),
                    "current_period_end": period_end.isoformat(),
                    "payment": str(payment.amount),
                },
                description=f"Subscription renewed on {plan.name} until "
                            f"{period_end:%Y-%m-%d}.")
            messages.success(request, "Subscription renewed.")
        else:
            messages.error(request, "Renewal could not be saved. Check the form values.")
    elif action == "change_plan" and sub:
        form = PlanChangeForm(request.POST, prefix="plan")
        if form.is_valid():
            cd = form.cleaned_data
            old_plan = sub.plan
            new_plan = cd["new_plan"]
            effective_date = cd["effective_date"]
            sub.plan = new_plan
            sub.notes = cd.get("notes", "")
            sub.save(update_fields=["plan", "notes", "updated_at"])
            payment_amount = cd.get("payment_amount")
            payment = None
            if payment_amount is not None:
                payment = record_subscription_payment(
                    business=business,
                    subscription=sub,
                    amount=payment_amount,
                    method=cd["payment_method"],
                    reference=cd.get("payment_reference", ""),
                    payment_date=effective_date,
                    period_start=local_date_start(effective_date),
                    notes=cd.get("notes", ""),
                    user=request.user,
                )
            audit.log(
                "platform.subscription_plan_changed", business=business,
                user=request.user, request=request, module="platformadmin",
                obj=sub,
                old_values={"plan": old_plan.name},
                new_values={
                    "plan": new_plan.name,
                    "effective_date": str(effective_date),
                    "payment": str(payment.amount) if payment else "",
                },
                description=f"Plan changed from {old_plan.name} to "
                            f"{new_plan.name} effective {effective_date}.")
            messages.success(request, "Plan changed.")
        else:
            messages.error(request, "Plan change could not be saved. Check the form values.")
    elif action == "record_payment" and sub:
        form = SubscriptionPaymentForm(request.POST, prefix="payment")
        if form.is_valid():
            cd = form.cleaned_data
            payment = record_subscription_payment(
                business=business,
                subscription=sub,
                amount=cd["amount"],
                method=cd["payment_method"],
                reference=cd.get("payment_reference", ""),
                payment_date=cd["payment_date"],
                notes=cd.get("notes", ""),
                user=request.user,
            )
            audit.log(
                "platform.subscription_payment_recorded", business=business,
                user=request.user, request=request, module="platformadmin",
                obj=payment,
                new_values={
                    "amount": str(payment.amount),
                    "method": payment.method,
                    "reference": payment.reference,
                    "payment_date": str(payment.payment_date),
                },
                description=f"Payment recorded: {payment.amount} "
                            f"{payment.currency_code}.")
            messages.success(request, "Payment recorded.")
        else:
            messages.error(request, "Payment could not be recorded. Check the form values.")
    elif action == "extend" and sub:
        days = int(request.POST.get("days", 30))
        plan_id = request.POST.get("plan_id")
        subscription_state_before = capture_subscription_state(sub)
        if plan_id:
            sub.plan = Plan.objects.get(pk=plan_id)
        base = sub.current_period_end or timezone.now()
        if base < timezone.now():
            base = timezone.now()
        sub.current_period_start = timezone.now()
        sub.current_period_end = base + timedelta(days=days)
        sub.status = Subscription.Status.ACTIVE
        sub.save()
        subscription_state_after = capture_subscription_state(sub)
        amount = request.POST.get("amount", "")
        if amount:
            record_subscription_payment(
                business=business,
                subscription=sub,
                amount=amount,
                method=request.POST.get("method", "manual"),
                reference=request.POST.get("reference", "")[:120],
                period_start=sub.current_period_start,
                period_end=sub.current_period_end,
                user=request.user,
                subscription_state_before=subscription_state_before,
                subscription_state_after=subscription_state_after,
            )
        audit.log("platform.subscription_extended", business=business,
                  user=request.user, request=request, module="platformadmin",
                  obj=sub,
                  description=f"Subscription extended {days} days "
                              f"on plan {sub.plan.name}.")
        messages.success(request, f"Subscription extended to "
                                  f"{sub.current_period_end:%Y-%m-%d}.")
    elif action == "extend_trial" and sub:
        days = int(request.POST.get("days", 7))
        base = sub.trial_ends_at or timezone.now()
        if base < timezone.now():
            base = timezone.now()
        sub.trial_ends_at = base + timedelta(days=days)
        sub.status = Subscription.Status.TRIAL
        sub.save()
        audit.log("platform.trial_extended", business=business, user=request.user,
                  request=request, module="platformadmin", obj=sub,
                  description=f"Trial extended {days} days.")
        messages.success(request, "Trial extended.")
    return redirect("platformadmin:business_detail", public_id=public_id)


# ---------------------------------------------------------------------------
# Create Business — platform admin provisions a new tenant + owner account
# ---------------------------------------------------------------------------
class BusinessCreateForm(forms.Form):
    """Platform admin: create a new business with an owner account and a
    subscription. Reuses the same validation rules as public registration."""

    INPUT = {"class": "form-control"}
    SELECT = {"class": "form-select"}

    business_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={**INPUT, "placeholder": "e.g. Sunrise Trading"}),
    )
    country = forms.CharField(
        max_length=100, required=False, widget=forms.TextInput(attrs=INPUT))
    currency = forms.ChoiceField(
        choices=currency_choices(), initial="USD",
        widget=forms.Select(attrs=SELECT))
    business_category = forms.ChoiceField(
        required=False,
        choices=[("", "—")] + [
            (c, c) for c in [
                "Clothing", "Perfumes", "Mobile & Accessories", "Electronics",
                "Grocery", "General Trading", "Gifts", "Hardware", "Tailoring",
                "Services", "Wholesale", "Other",
            ]
        ],
        widget=forms.Select(attrs=SELECT))

    owner_name = forms.CharField(
        max_length=150, label="Owner full name",
        widget=forms.TextInput(attrs=INPUT))
    owner_email = forms.EmailField(
        label="Owner email", widget=forms.EmailInput(attrs=INPUT))
    phone = forms.CharField(
        max_length=30, required=False, label="Owner phone",
        widget=forms.TextInput(attrs=INPUT))
    password = forms.CharField(
        required=False, label="Password (leave blank to auto-generate)",
        widget=forms.PasswordInput(attrs={**INPUT, "autocomplete": "new-password"}))

    plan = forms.ModelChoiceField(
        queryset=Plan.objects.filter(is_active=True),
        widget=forms.Select(attrs=SELECT))
    subscription_mode = forms.ChoiceField(
        choices=[("trial", "Trial"), ("active", "Active (paid)")],
        initial="trial", widget=forms.Select(attrs=SELECT))
    days = forms.IntegerField(
        required=False, min_value=1, label="Days (trial / paid period)",
        help_text="Leave blank to use the plan's trial length.",
        widget=forms.NumberInput(attrs=INPUT))
    amount = forms.DecimalField(
        required=False, min_value=0, label="Payment amount (optional)",
        widget=forms.NumberInput(attrs=INPUT))
    reference = forms.CharField(
        max_length=120, required=False, label="Payment reference (optional)",
        widget=forms.TextInput(attrs=INPUT))

    def clean_owner_email(self):
        email = self.cleaned_data["owner_email"].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(
                "An account with this email already exists.")
        return email

    def clean(self):
        data = super().clean()
        if data.get("password"):
            validate_password(data["password"])
        plan = data.get("plan")
        if (
            plan
            and data.get("subscription_mode") == "trial"
            and not plan.allow_trial
        ):
            self.add_error(
                "subscription_mode",
                "This plan does not allow trial subscriptions.",
            )
        return data


class SubscriptionRenewalForm(forms.Form):
    INPUT = {"class": "form-control form-control-sm"}
    SELECT = {"class": "form-select form-select-sm"}

    plan = forms.ModelChoiceField(
        queryset=Plan.objects.filter(is_active=True),
        widget=forms.Select(attrs=SELECT),
    )
    renewal_type = forms.ChoiceField(
        choices=[("monthly", "Monthly"), ("annual", "Annual"), ("custom", "Custom")],
        initial="monthly",
        widget=forms.Select(attrs=SELECT),
    )
    start_date = forms.DateField(required=False, widget=forms.DateInput(
        attrs={**INPUT, "type": "date"}))
    end_date = forms.DateField(required=False, widget=forms.DateInput(
        attrs={**INPUT, "type": "date"}))
    payment_amount = forms.DecimalField(
        min_value=0, decimal_places=3, max_digits=14,
        widget=forms.NumberInput(attrs=INPUT),
    )
    payment_reference = forms.CharField(
        max_length=120, required=False, widget=forms.TextInput(attrs=INPUT))
    payment_method = forms.ChoiceField(
        choices=SubscriptionPayment._meta.get_field("method").choices,
        widget=forms.Select(attrs=SELECT),
    )
    notes = forms.CharField(required=False, widget=forms.Textarea(
        attrs={**INPUT, "rows": 2}))

    def clean(self):
        data = super().clean()
        renewal_type = data.get("renewal_type")
        start = data.get("start_date")
        end = data.get("end_date")
        if renewal_type == "custom" and not end:
            self.add_error("end_date", "End date is required for a custom renewal.")
        if start and end and end <= start:
            self.add_error("end_date", "End date must be after the start date.")
        return data


class PlanChangeForm(forms.Form):
    INPUT = {"class": "form-control form-control-sm"}
    SELECT = {"class": "form-select form-select-sm"}

    new_plan = forms.ModelChoiceField(
        queryset=Plan.objects.filter(is_active=True),
        widget=forms.Select(attrs=SELECT),
    )
    effective_date = forms.DateField(
        initial=timezone.localdate,
        widget=forms.DateInput(attrs={**INPUT, "type": "date"}),
    )
    notes = forms.CharField(required=False, widget=forms.Textarea(
        attrs={**INPUT, "rows": 2}))
    payment_amount = forms.DecimalField(
        required=False, min_value=0, decimal_places=3, max_digits=14,
        widget=forms.NumberInput(attrs=INPUT),
    )
    payment_reference = forms.CharField(
        max_length=120, required=False, widget=forms.TextInput(attrs=INPUT))
    payment_method = forms.ChoiceField(
        choices=SubscriptionPayment._meta.get_field("method").choices,
        initial="manual",
        widget=forms.Select(attrs=SELECT),
    )


class SubscriptionPaymentForm(forms.Form):
    INPUT = {"class": "form-control form-control-sm"}
    SELECT = {"class": "form-select form-select-sm"}

    amount = forms.DecimalField(
        min_value=0, decimal_places=3, max_digits=14,
        widget=forms.NumberInput(attrs=INPUT),
    )
    payment_reference = forms.CharField(
        max_length=120, required=False, widget=forms.TextInput(attrs=INPUT))
    payment_method = forms.ChoiceField(
        choices=SubscriptionPayment._meta.get_field("method").choices,
        widget=forms.Select(attrs=SELECT),
    )
    payment_date = forms.DateField(
        initial=timezone.localdate,
        widget=forms.DateInput(attrs={**INPUT, "type": "date"}),
    )
    notes = forms.CharField(required=False, widget=forms.Textarea(
        attrs={**INPUT, "rows": 2}))


class SubscriptionPaymentEditForm(forms.ModelForm):
    class Meta:
        model = SubscriptionPayment
        fields = ("payment_date", "method", "reference", "amount", "notes")
        widgets = {
            "payment_date": forms.DateInput(attrs={
                "class": "form-control form-control-sm",
                "type": "date",
            }),
            "method": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "reference": forms.TextInput(attrs={
                "class": "form-control form-control-sm",
            }),
            "amount": forms.NumberInput(attrs={
                "class": "form-control form-control-sm",
                "min": "0.001",
                "step": "0.001",
            }),
            "notes": forms.Textarea(attrs={
                "class": "form-control form-control-sm",
                "rows": 3,
            }),
        }

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("Amount must be greater than zero.")
        return amount


class SubscriptionPaymentReversalForm(forms.Form):
    reversal_reason = forms.CharField(
        max_length=400,
        strip=True,
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 3,
            "required": True,
        }),
    )


@platform_admin_required
def business_create(request):
    form = BusinessCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        from apps.tenants.services import provision_business

        cd = form.cleaned_data
        currency_code = cd["currency"]
        generated_password = ""
        password = cd.get("password")
        if not password:
            password = get_random_string(12)
            generated_password = password

        with transaction.atomic():
            owner = User.objects.create_user(
                email=cd["owner_email"],
                password=password,
                full_name=cd["owner_name"],
                phone=cd.get("phone", ""),
            )
            business = provision_business(
                owner=owner,
                name=cd["business_name"],
                country=cd.get("country", ""),
                currency_code=currency_code,
                currency_precision=precision_for(currency_code, default=2),
                business_category=cd.get("business_category", ""),
                phone=cd.get("phone", ""),
                plan=cd["plan"],
                request=request,
            )

            sub = business.subscription
            now = timezone.now()
            days = cd.get("days")
            if cd["subscription_mode"] == "active":
                # Mirror the existing "extend" action: active paid period.
                period_days = days or 30
                subscription_state_before = capture_subscription_state(sub)
                sub.status = Subscription.Status.ACTIVE
                sub.current_period_start = now
                sub.current_period_end = now + timedelta(days=period_days)
                sub.save()
                subscription_state_after = capture_subscription_state(sub)
                amount = cd.get("amount")
                if amount:
                    record_subscription_payment(
                        business=business,
                        subscription=sub,
                        amount=amount,
                        method="manual",
                        reference=cd.get("reference", "")[:120],
                        period_start=sub.current_period_start,
                        period_end=sub.current_period_end,
                        user=request.user,
                        subscription_state_before=subscription_state_before,
                        subscription_state_after=subscription_state_after,
                    )
            elif days:
                # Trial with an explicit length overriding the plan default.
                sub.status = Subscription.Status.TRIAL
                sub.trial_ends_at = now + timedelta(days=days)
                sub.save()

            audit.log(
                "platform.business_created", business=business,
                user=request.user, request=request, module="platformadmin",
                obj=business,
                description=f"Business '{business.name}' created with owner "
                            f"{owner.email} on plan {sub.plan.name}.")

        if generated_password:
            messages.success(
                request,
                f"Business '{business.name}' created. Owner login — "
                f"email: {owner.email} · password: {generated_password} "
                "(shown once; copy it now).")
        else:
            messages.success(
                request,
                f"Business '{business.name}' created. Owner: {owner.email}.")
        return redirect("platformadmin:business_detail",
                        public_id=business.public_id)

    return render(request, "platformadmin/business_create.html",
                  {"form": form, "pa_nav": "businesses"})


@platform_admin_required
def support_access(request, public_id):
    try:
        business = Business.objects.get(public_id=public_id)
    except Business.DoesNotExist:
        from django.http import Http404
        raise Http404 from None
    if request.method == "POST":
        if request.POST.get("revoke_id"):
            grant = SupportAccessGrant.objects.filter(
                pk=request.POST["revoke_id"], business=business).first()
            if grant:
                grant.revoked_at = timezone.now()
                grant.revoked_by = request.user
                grant.save()
                audit.log("platform.support_access_revoked", business=business,
                          user=request.user, request=request,
                          module="platformadmin", obj=grant,
                          description="Support access revoked.")
                messages.success(request, "Support access revoked.")
        else:
            reason = request.POST.get("reason", "").strip()
            hours = int(request.POST.get("hours", 4))
            if not reason:
                messages.error(request, "A reason is required for support access.")
            else:
                grant = SupportAccessGrant.objects.create(
                    business=business, granted_to=request.user,
                    reason=reason[:300],
                    expires_at=timezone.now() + timedelta(hours=min(hours, 72)),
                )
                audit.log("platform.support_access_granted", business=business,
                          user=request.user, request=request,
                          module="platformadmin", obj=grant,
                          description=f"Support access granted: {reason}")
                if business.settings.notify_support_access:
                    from apps.notifications.services import notify

                    notify(business, business.owner,
                           "Platform support accessed your account",
                           body=f"Reason: {reason}. Access expires in {hours}h.",
                           severity="high", category="support_access")
                messages.success(request, "Support access granted and audited.")
    return redirect("platformadmin:business_detail", public_id=public_id)


# ---------------------------------------------------------------------------
# Login As Owner — support-mode impersonation
# ---------------------------------------------------------------------------
@platform_admin_required
def support_login_as(request, public_id):
    """Start a support session impersonating the business owner."""
    from apps.platformadmin.middleware import SESSION_KEY

    try:
        business = Business.objects.select_related("owner").get(public_id=public_id)
    except Business.DoesNotExist:
        from django.http import Http404
        raise Http404 from None
    if request.method != "POST":
        return redirect("platformadmin:business_detail", public_id=public_id)
    reason = request.POST.get("reason", "").strip()
    if not reason:
        messages.error(request, "A reason is required to start a support session.")
        return redirect("platformadmin:business_detail", public_id=public_id)
    if not business.is_active:
        messages.error(request, "Reactivate the business before opening a support session.")
        return redirect("platformadmin:business_detail", public_id=public_id)

    grant = SupportAccessGrant.objects.create(
        business=business, granted_to=request.user, reason=reason[:300],
        expires_at=timezone.now() + timedelta(hours=2),
    )
    request.session[SESSION_KEY] = {
        "admin_id": request.user.pk,
        "owner_id": business.owner_id,
        "business_id": business.id,
        "business_name": business.name,
        "grant_id": grant.pk,
        "reason": reason[:300],
        "started": timezone.now().isoformat(),
    }
    # Pin the active business to the impersonated tenant
    from apps.core.middleware import SESSION_BUSINESS_KEY

    request.session[SESSION_BUSINESS_KEY] = business.id
    audit.log("platform.login_as_owner", business=business, user=request.user,
              request=request, module="platformadmin", obj=business,
              description=f"Support session started as owner "
                          f"{business.owner.email}: {reason}")
    if business.settings.notify_support_access:
        from apps.notifications.services import notify

        notify(business, business.owner,
               "A support session was started on your account",
               body=f"Reason: {reason}", severity="high",
               category="support_access")
    messages.warning(request, f"Support session active — you are now viewing "
                              f"{business.name} as its owner.")
    return redirect("dashboard")


def support_exit(request):
    """End the active support session and return to the platform panel.

    Available to the impersonated request (request.user is the owner here),
    so this is gated on the session context, not platform_admin_required.
    """
    from apps.platformadmin.middleware import SESSION_KEY

    data = request.session.get(SESSION_KEY)
    if not data:
        return redirect("platformadmin:dashboard")
    started = data.get("started")
    duration = ""
    if started:
        from django.utils.dateparse import parse_datetime

        start_dt = parse_datetime(started)
        if start_dt:
            secs = int((timezone.now() - start_dt).total_seconds())
            duration = f"{secs // 60}m {secs % 60}s"
    business = Business.objects.filter(pk=data.get("business_id")).first()
    audit.log("platform.support_session_ended", business=business,
              user=getattr(request, "support_admin", None),
              request=request, module="platformadmin",
              description=f"Support session on "
                          f"{data.get('business_name')} ended (duration {duration}).")
    request.session.pop(SESSION_KEY, None)
    from apps.core.middleware import SESSION_BUSINESS_KEY

    request.session.pop(SESSION_BUSINESS_KEY, None)
    messages.success(request, "Support session ended.")
    return redirect("platformadmin:dashboard")


# ---------------------------------------------------------------------------
# Platform settings
# ---------------------------------------------------------------------------
@platform_admin_required
def platform_settings(request):
    from .models import PlatformConfig

    config = PlatformConfig.get_solo()
    if request.method == "POST":
        mode = request.POST.get("expiry_mode", "")
        if mode in PlatformConfig.ExpiryMode.values:
            old = config.expiry_mode
            config.expiry_mode = mode
            config.save(update_fields=["expiry_mode", "updated_at"])
            audit.log("platform.settings_changed", user=request.user,
                      request=request, module="platformadmin", obj=config,
                      old_values={"expiry_mode": old},
                      new_values={"expiry_mode": mode},
                      description=f"Expiry mode changed {old} → {mode}.")
            messages.success(request, "Platform settings saved.")
        return redirect("platformadmin:settings")
    return render(request, "platformadmin/settings.html",
                  {"config": config, "modes": PlatformConfig.ExpiryMode.choices})


# ---------------------------------------------------------------------------
# Plans / coupons / announcements
# ---------------------------------------------------------------------------
PLAN_BASIC_FIELDS = [
    "name", "support_level", "sort_order", "is_active", "description",
]
PLAN_PRICING_FIELDS = [
    "monthly_price", "annual_price", "setup_fee", "currency_code",
    "trial_days", "allow_trial",
]
PLAN_LIMIT_FIELDS = [
    "max_branches", "max_users", "max_warehouses", "max_products",
    "max_customers", "max_monthly_invoices", "storage_limit_mb",
    "max_employees", "max_suppliers", "max_active_orders",
    "max_api_calls", "max_branch_managers", "max_cashiers",
    "max_logged_in_devices", "max_pos_terminals",
]
PLAN_MODULE_FIELDS = [
    "feature_sales", "feature_inventory", "feature_suppliers",
    "feature_purchases", "feature_expenses", "feature_transfers",
    "feature_tailoring_module", "feature_customer_credit",
    "feature_advanced_reports", "feature_audit_logs",
    "feature_barcode_printing", "feature_custom_roles",
    "feature_api_access",
]
PLAN_MODULE_LABELS = {
    "feature_sales": "POS Core",
    "feature_inventory": "Inventory Management",
    "feature_suppliers": "Suppliers",
    "feature_purchases": "Purchasing",
    "feature_expenses": "Expenses",
    "feature_transfers": "Stock Transfers",
    "feature_tailoring_module": "Tailoring Operations",
    "feature_customer_credit": "Customer Credit",
    "feature_advanced_reports": "Advanced Reports",
    "feature_audit_logs": "Audit Logs",
    "feature_barcode_printing": "Barcode Printing",
    "feature_custom_roles": "Custom Roles",
    "feature_api_access": "API Access",
}
PLAN_MODULE_HELP_TEXT = {
    "feature_purchases": (
        "Purchasing requires POS Core, Inventory Management, and Suppliers."
    ),
    "feature_transfers": "Stock Transfers require Inventory Management.",
    "feature_tailoring_module": (
        "Tailoring Operations require POS Core and Inventory Management."
    ),
    "feature_customer_credit": "Customer Credit requires POS Core.",
    "feature_advanced_reports": (
        "Advanced Reports require POS Core, Inventory Management, Suppliers, "
        "Purchasing, Expenses, and Customer Credit."
    ),
    "feature_barcode_printing": "Barcode Printing requires POS Core.",
    "feature_custom_roles": "Custom Roles require POS Core Users & Staff.",
    "feature_api_access": (
        "API Access does not automatically enable any business module."
    ),
}
PLAN_MODULE_DEPENDENCIES = {
    "feature_purchases": (
        "feature_sales",
        "feature_inventory",
        "feature_suppliers",
    ),
    "feature_tailoring_module": (
        "feature_sales",
        "feature_inventory",
    ),
    "feature_customer_credit": ("feature_sales",),
    "feature_advanced_reports": (
        "feature_sales",
        "feature_inventory",
        "feature_suppliers",
        "feature_purchases",
        "feature_expenses",
        "feature_customer_credit",
    ),
}
PLAN_FORM_FIELDS = (
    PLAN_BASIC_FIELDS + PLAN_PRICING_FIELDS + PLAN_LIMIT_FIELDS
    + PLAN_MODULE_FIELDS
)


class PlanForm(forms.ModelForm):
    BASIC_FIELDS = PLAN_BASIC_FIELDS
    PRICING_FIELDS = PLAN_PRICING_FIELDS
    LIMIT_FIELDS = PLAN_LIMIT_FIELDS
    MODULE_FIELDS = PLAN_MODULE_FIELDS

    class Meta:
        model = Plan
        fields = PLAN_FORM_FIELDS
        labels = PLAN_MODULE_LABELS
        help_texts = PLAN_MODULE_HELP_TEXT

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in self.fields.values():
            if isinstance(f.widget, forms.CheckboxInput):
                f.widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(f.widget, forms.Select):
                f.widget.attrs.setdefault("class", "form-select")
            elif isinstance(f.widget, forms.Textarea):
                f.widget.attrs.setdefault("class", "form-control")
                f.widget.attrs["rows"] = 2
            else:
                f.widget.attrs.setdefault("class", "form-control")
        self.basic_fields = [self[name] for name in self.BASIC_FIELDS]
        self.pricing_fields = [self[name] for name in self.PRICING_FIELDS]
        self.limit_fields = [self[name] for name in self.LIMIT_FIELDS]
        self.module_fields = [self[name] for name in self.MODULE_FIELDS]

    def clean(self):
        cleaned_data = super().clean()
        for module_field, dependency_fields in PLAN_MODULE_DEPENDENCIES.items():
            if not cleaned_data.get(module_field):
                continue
            missing_labels = [
                PLAN_MODULE_LABELS[field]
                for field in dependency_fields
                if not cleaned_data.get(field)
            ]
            if missing_labels:
                self.add_error(
                    module_field,
                    f"{PLAN_MODULE_LABELS[module_field]} requires enabled modules: "
                    f"{', '.join(missing_labels)}.",
                )
        return cleaned_data


@platform_admin_required
def plan_list(request):
    plans = Plan.objects.annotate(sub_count=Count("subscriptions"))
    return render(request, "platformadmin/plan_list.html",
                  {"plans": plans, "pa_nav": "plans"})


@platform_admin_required
def plan_form(request, pk=None):
    instance = Plan.objects.filter(pk=pk).first() if pk else None
    form = PlanForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        plan = form.save()
        audit.log("platform.plan_saved", user=request.user, request=request,
                  module="platformadmin", obj=plan,
                  description=f"Plan '{plan.name}' saved.")
        messages.success(request, "Plan saved.")
        return redirect("platformadmin:plan_list")
    return render(request, "platformadmin/plan_form.html",
                  {"form": form, "plan": instance, "pa_nav": "plans"})


class CouponForm(forms.ModelForm):
    class Meta:
        model = Coupon
        fields = ["code", "description", "percent_off", "amount_off",
                  "extra_trial_days", "max_redemptions", "valid_until", "is_active"]
        widgets = {"valid_until": forms.DateTimeInput(attrs={"type": "datetime-local"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in self.fields.values():
            if isinstance(f.widget, forms.CheckboxInput):
                f.widget.attrs.setdefault("class", "form-check-input")
            else:
                f.widget.attrs.setdefault("class", "form-control")


@platform_admin_required
def coupon_list(request):
    instance = None
    if request.GET.get("edit"):
        instance = Coupon.objects.filter(public_id=request.GET["edit"]).first()
    form = CouponForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Coupon saved.")
        return redirect("platformadmin:coupon_list")
    coupons = Coupon.objects.all()
    return render(request, "platformadmin/coupon_list.html",
                  {"coupons": coupons, "form": form, "editing": instance})


@platform_admin_required
def announcement_list(request):
    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        if title:
            from django.urls import reverse

            from apps.notifications import services as notifications

            announcement = Announcement.objects.create(
                title=title[:160], body=request.POST.get("body", ""),
            )
            sent = notifications.broadcast_announcement(
                announcement, link=reverse("notifications:list"))
            messages.success(
                request,
                f"Announcement published and delivered to {sent} "
                f"recipient{'' if sent == 1 else 's'} across active businesses.")
        return redirect("platformadmin:announcements")
    announcements = Announcement.objects.all()[:50]
    return render(request, "platformadmin/announcements.html",
                  {"announcements": announcements})


@platform_admin_required
def platform_audit(request):
    qs = AuditLog.objects.select_related("user", "business").filter(
        module="platformadmin"
    )
    paginator = Paginator(qs, 40)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "platformadmin/audit.html",
                  {"page_obj": page_obj, "querystring": ""})
