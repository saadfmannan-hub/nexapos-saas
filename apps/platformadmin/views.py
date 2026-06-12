"""Platform Super Admin — separate from all tenant dashboards.

Access requires user.is_platform_admin. Tenant business data is NOT
shown here beyond operational metadata (name, owner, counts, status)
unless an active SupportAccessGrant exists.
"""
from datetime import timedelta
from functools import wraps

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.accounts.models import LoginHistory, Membership, User
from apps.audit import services as audit
from apps.audit.models import AuditLog
from apps.subscriptions.models import Coupon, Plan, Subscription, SubscriptionPayment
from apps.tenants.models import Business

from .models import Announcement, SupportAccessGrant


def platform_admin_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("accounts:login")
        if not request.user.is_platform_admin:
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return wrapper


@platform_admin_required
def dashboard(request):
    now = timezone.now()
    businesses = Business.objects.all()
    subs = Subscription.objects.select_related("plan", "business")
    status_counts = {}
    for sub in subs:
        status_counts[sub.effective_status] = status_counts.get(
            sub.effective_status, 0) + 1
    revenue = SubscriptionPayment.objects.aggregate(t=Sum("amount"))["t"] or 0
    recent_failed_logins = LoginHistory.objects.filter(
        success=False, created_at__gte=now - timedelta(days=1)
    ).count()
    recent_businesses = businesses.order_by("-created_at")[:8]
    expiring = [
        s for s in subs
        if s.effective_status in ("trial", "grace")
    ][:10]
    return render(request, "platformadmin/dashboard.html", {
        "total_businesses": businesses.count(),
        "active_businesses": businesses.filter(is_active=True).count(),
        "total_users": User.objects.count(),
        "status_counts": status_counts,
        "revenue": revenue,
        "recent_failed_logins": recent_failed_logins,
        "recent_businesses": recent_businesses,
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
        raise Http404
    sub = getattr(business, "subscription", None)
    members = Membership.objects.filter(business=business).select_related(
        "user", "role")
    payments = SubscriptionPayment.objects.filter(
        subscription__business=business)[:10]
    grants = SupportAccessGrant.objects.filter(business=business)[:10]
    plans = Plan.objects.filter(is_active=True)
    # Operational metadata only — counts, not data
    from apps.branches.models import Branch
    from apps.catalog.models import Product
    from apps.sales.models import Sale

    usage = {
        "branches": Branch.objects.for_business(business).count(),
        "products": Product.objects.for_business(business).count(),
        "invoices": Sale.objects.for_business(business).exclude(
            status="draft").count(),
        "users": members.count(),
    }
    return render(request, "platformadmin/business_detail.html", {
        "business": business, "sub": sub, "members": members,
        "payments": payments, "grants": grants, "plans": plans, "usage": usage,
        "now": timezone.now(),
    })


@platform_admin_required
def business_action(request, public_id, action):
    if request.method != "POST":
        return redirect("platformadmin:business_list")
    try:
        business = Business.objects.get(public_id=public_id)
    except Business.DoesNotExist:
        from django.http import Http404
        raise Http404
    sub = getattr(business, "subscription", None)

    if action == "suspend":
        reason = request.POST.get("reason", "")[:255]
        business.is_active = False
        business.suspended_at = timezone.now()
        business.suspension_reason = reason
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
        business.save()
        if sub and sub.status == Subscription.Status.SUSPENDED:
            sub.status = Subscription.Status.ACTIVE
            if not sub.current_period_end or sub.current_period_end < timezone.now():
                sub.current_period_start = timezone.now()
                sub.current_period_end = timezone.now() + timedelta(days=30)
            sub.save()
        audit.log("platform.business_activated", business=business,
                  user=request.user, request=request, module="platformadmin",
                  obj=business, description="Business reactivated.")
        messages.success(request, f"{business.name} activated.")
    elif action == "extend" and sub:
        days = int(request.POST.get("days", 30))
        plan_id = request.POST.get("plan_id")
        if plan_id:
            sub.plan = Plan.objects.get(pk=plan_id)
        base = sub.current_period_end or timezone.now()
        if base < timezone.now():
            base = timezone.now()
        sub.current_period_start = timezone.now()
        sub.current_period_end = base + timedelta(days=days)
        sub.status = Subscription.Status.ACTIVE
        sub.save()
        amount = request.POST.get("amount", "")
        if amount:
            SubscriptionPayment.objects.create(
                subscription=sub, amount=amount,
                currency_code=sub.plan.currency_code,
                method=request.POST.get("method", "manual"),
                reference=request.POST.get("reference", "")[:120],
                period_start=sub.current_period_start,
                period_end=sub.current_period_end,
                recorded_by=request.user,
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


@platform_admin_required
def support_access(request, public_id):
    try:
        business = Business.objects.get(public_id=public_id)
    except Business.DoesNotExist:
        from django.http import Http404
        raise Http404
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
# Plans / coupons / announcements
# ---------------------------------------------------------------------------
class PlanForm(forms.ModelForm):
    class Meta:
        model = Plan
        exclude = ["public_id", "created_at", "updated_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in self.fields.values():
            if isinstance(f.widget, forms.CheckboxInput):
                f.widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(f.widget, forms.Textarea):
                f.widget.attrs.setdefault("class", "form-control")
                f.widget.attrs.setdefault("rows", 2)
            else:
                f.widget.attrs.setdefault("class", "form-control")


@platform_admin_required
def plan_list(request):
    plans = Plan.objects.annotate(sub_count=Count("subscriptions"))
    return render(request, "platformadmin/plan_list.html", {"plans": plans})


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
                  {"form": form, "plan": instance})


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
            Announcement.objects.create(
                title=title[:160], body=request.POST.get("body", ""),
            )
            messages.success(request, "Announcement published.")
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
