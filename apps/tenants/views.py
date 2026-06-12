from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.audit import services as audit
from apps.core.decorators import business_required, require_permission
from apps.core.middleware import SESSION_BUSINESS_KEY

from .forms import BusinessProfileForm, BusinessSettingsForm, RegistrationForm
from .services import provision_business


def register_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            owner = User.objects.create_user(
                email=form.cleaned_data["email"],
                password=form.cleaned_data["password"],
                full_name=form.cleaned_data["owner_name"],
                phone=form.cleaned_data["phone"],
            )
            business = provision_business(
                owner=owner,
                name=form.cleaned_data["business_name"],
                country=form.cleaned_data["country"],
                timezone_name=form.cleaned_data["timezone_name"],
                currency_code=form.currency_code,
                currency_precision=form.currency_precision,
                business_category=form.cleaned_data["business_category"],
                phone=form.cleaned_data["phone"],
                request=request,
            )
        auth_login(request, owner)
        request.session[SESSION_BUSINESS_KEY] = business.id
        messages.success(
            request,
            f"Welcome! Your business '{business.name}' is ready. "
            "Let's finish setting things up.",
        )
        return redirect("tenants:onboarding")
    return render(request, "auth/register.html", {"form": form})


def no_business_view(request):
    """Shown when an authenticated user has no active membership."""
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    if getattr(request, "business", None):
        return redirect("dashboard")
    return render(request, "tenants/no_business.html")


@require_POST
@business_required
def switch_business(request):
    business_id = request.POST.get("business_id")
    membership = request.user.memberships.filter(
        business_id=business_id, is_active=True, business__is_active=True
    ).first()
    if membership is None:
        messages.error(request, "You do not have access to that business.")
    else:
        request.session[SESSION_BUSINESS_KEY] = membership.business_id
        messages.success(request, f"Switched to {membership.business.name}.")
    return redirect("dashboard")


@business_required
def onboarding_view(request):
    """Setup checklist with live completion state — every step links to the
    real screen and can be skipped."""
    from apps.branches.models import Branch
    from apps.catalog.models import Category, Product, TaxRate
    from apps.registers.models import Shift

    business = request.business
    steps = [
        {
            "label": "Add your business logo & address",
            "done": bool(business.logo or business.address),
            "url": "tenants:profile", "icon": "bi-building",
        },
        {
            "label": "Review currency & tax settings",
            "done": TaxRate.objects.for_business(business).exists(),
            "url": "catalog:tax_list", "icon": "bi-percent",
        },
        {
            "label": "Create product categories",
            "done": Category.objects.for_business(business).exists(),
            "url": "catalog:category_list", "icon": "bi-tags",
        },
        {
            "label": "Add your first products",
            "done": Product.objects.for_business(business).exists(),
            "url": "catalog:product_create", "icon": "bi-box-seam",
        },
        {
            "label": "Add branches (optional)",
            "done": Branch.objects.for_business(business).count() > 1,
            "url": "branches:list", "icon": "bi-diagram-3",
        },
        {
            "label": "Add your cashiers and staff",
            "done": business.memberships.count() > 1,
            "url": "accounts:user_create", "icon": "bi-people",
        },
        {
            "label": "Open a cash register shift",
            "done": Shift.objects.for_business(business).exists(),
            "url": "registers:shift_list", "icon": "bi-cash-stack",
        },
        {
            "label": "Make your first sale",
            "done": business.sales_sale_set.exists(),
            "url": "sales:pos", "icon": "bi-cart3",
        },
    ]
    done_count = sum(1 for s in steps if s["done"])
    if request.method == "POST":  # "Finish onboarding"
        business.onboarding_completed = True
        business.save(update_fields=["onboarding_completed"])
        messages.success(request, "Onboarding completed. Welcome aboard!")
        return redirect("dashboard")
    return render(request, "tenants/onboarding.html", {
        "steps": steps, "done_count": done_count, "total": len(steps),
    })


@require_permission("settings.manage")
def profile_view(request):
    form = BusinessProfileForm(
        request.POST or None, request.FILES or None, instance=request.business
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        audit.log("business.profile_updated", request=request, module="tenants",
                  obj=request.business, description="Business profile updated.")
        messages.success(request, "Business profile saved.")
        return redirect("tenants:profile")
    return render(request, "tenants/profile.html",
                  {"form": form, "active_nav": "settings"})


@require_permission("settings.manage")
def settings_view(request):
    settings_obj = request.business.settings
    form = BusinessSettingsForm(request.POST or None, instance=settings_obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit.log("business.settings_updated", request=request, module="tenants",
                  obj=request.business, description="Business settings updated.")
        messages.success(request, "Settings saved.")
        return redirect("tenants:settings")
    return render(request, "tenants/settings.html",
                  {"form": form, "active_nav": "settings"})
