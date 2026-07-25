from django.shortcuts import redirect, render

from apps.core.date_ranges import business_localdate
from apps.subscriptions.access import AccessAction

from . import dashboard as dashboard_selectors
from . import selectors, services
from .access import wms_permission_required
from .forms import WmsLocationForm, WmsSettingsForm, WmsUserAccessForm
from .models import WmsLocation, WmsRole, WmsSettings, WmsUserAccess


@wms_permission_required("wms.dashboard.view", action=AccessAction.READ)
def dashboard(request):
    dashboard_data = dashboard_selectors.executive_dashboard(
        request.wms_user_access,
        today=business_localdate(request.business),
    )
    return render(
        request,
        "wms/dashboard/index.html",
        {
            "active_nav": "wms",
            "wms_active_nav": "dashboard",
            "dashboard": dashboard_data,
        },
    )


@wms_permission_required("wms.settings.manage")
def settings_view(request):
    settings_obj = WmsSettings.objects.for_business(request.business).get()
    form = WmsSettingsForm(
        request.business,
        request.POST or None,
        instance=settings_obj,
    )
    if request.method == "POST" and form.is_valid():
        services.save_settings(
            settings_obj,
            form.cleaned_data,
            request=request,
        )
        return redirect("wms:settings")
    return render(
        request,
        "wms/settings/index.html",
        {
            "form": form,
            "locations": WmsLocation.objects.for_business(request.business)
            .select_related("branch")
            .order_by("branch__name"),
            "active_nav": "wms",
            "wms_active_nav": "settings",
        },
    )


@wms_permission_required("wms.settings.manage")
def location_form(request, public_id=None):
    instance = (
        selectors.get_business_location(request.business, public_id)
        if public_id
        else WmsLocation(business=request.business)
    )
    form = WmsLocationForm(
        request.business,
        request.POST or None,
        instance=instance,
    )
    if request.method == "POST" and form.is_valid():
        services.save_location(
            business=request.business,
            branch=form.cleaned_data["branch"],
            location_type=form.cleaned_data["location_type"],
            is_active=form.cleaned_data["is_active"],
            instance=instance if instance.pk else None,
            request=request,
        )
        return redirect("wms:settings")
    return render(
        request,
        "wms/settings/location_form.html",
        {
            "form": form,
            "location": instance if instance.pk else None,
            "active_nav": "wms",
            "wms_active_nav": "settings",
        },
    )


@wms_permission_required("wms.users.manage", action=AccessAction.READ)
def user_list(request):
    accesses = (
        WmsUserAccess.objects.for_business(request.business)
        .select_related("membership__user", "role")
        .prefetch_related("allowed_locations__branch")
        .order_by("membership__user__full_name", "membership__user__email")
    )
    return render(
        request,
        "wms/users/index.html",
        {
            "accesses": accesses,
            "roles": WmsRole.objects.for_business(request.business).order_by("name"),
            "active_nav": "wms",
            "wms_active_nav": "users",
        },
    )


@wms_permission_required("wms.users.manage")
def user_access_form(request, public_id=None):
    instance = (
        selectors.get_business_user_access(request.business, public_id)
        if public_id
        else WmsUserAccess(business=request.business)
    )
    form = WmsUserAccessForm(
        request.business,
        request.POST or None,
        instance=instance,
    )
    if request.method == "POST" and form.is_valid():
        services.save_user_access(
            business=request.business,
            membership=form.cleaned_data["membership"],
            role=form.cleaned_data["role"],
            is_active=form.cleaned_data["is_active"],
            allowed_locations=form.cleaned_data["allowed_locations"],
            instance=instance if instance.pk else None,
            request=request,
        )
        return redirect("wms:user_list")
    return render(
        request,
        "wms/users/form.html",
        {
            "form": form,
            "access_record": instance if instance.pk else None,
            "active_nav": "wms",
            "wms_active_nav": "users",
        },
    )
