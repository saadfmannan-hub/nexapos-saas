from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.core.date_ranges import business_localdate
from apps.subscriptions import services as subscriptions
from apps.subscriptions.access import AccessAction

from . import dashboard as dashboard_selectors
from . import selectors, services
from .access import wms_permission_required
from .forms import (
    WmsLocationForm,
    WmsRoleForm,
    WmsSettingsForm,
    WmsUserAccessForm,
    WmsUserForm,
)
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
        cleaned_data = dict(form.cleaned_data)
        business_timezone = cleaned_data.pop("business_timezone")
        services.save_settings(
            settings_obj,
            cleaned_data,
            request=request,
        )
        services.save_business_timezone(
            business=request.business,
            timezone_name=business_timezone,
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
def user_create(request):
    form = WmsUserForm(
        request.business,
        request.POST or None,
        acting_access=request.wms_user_access,
    )
    if request.method == "POST" and form.is_valid():
        try:
            services.create_wms_user(
                business=request.business,
                full_name=form.cleaned_data["full_name"],
                email=form.cleaned_data["email"],
                password=form.cleaned_data["password"],
                role=form.cleaned_data["role"],
                allowed_locations=form.cleaned_data["allowed_locations"],
                is_active=form.cleaned_data["is_active"],
                request=request,
            )
        except (
            subscriptions.LimitExceeded,
            subscriptions.SubscriptionInactive,
        ) as exc:
            from apps.subscriptions.helpers import limit_blocked_response

            return limit_blocked_response(request, exc, resource="users")
        messages.success(request, "WMS user added.")
        return redirect("wms:user_list")
    return render(
        request,
        "wms/users/user_form.html",
        {
            "form": form,
            "access_record": None,
            "active_nav": "wms",
            "wms_active_nav": "users",
        },
    )


@wms_permission_required("wms.users.manage")
def user_edit(request, public_id):
    access = selectors.get_business_user_access(request.business, public_id)
    initial = {
        "full_name": access.membership.user.full_name,
        "email": access.membership.user.email,
        "role": access.role,
        "allowed_locations": list(access.allowed_locations.all()),
        "is_active": access.is_active,
    }
    form = WmsUserForm(
        request.business,
        request.POST or None,
        initial=initial,
        access_record=access,
        acting_access=request.wms_user_access,
    )
    if request.method == "POST" and form.is_valid():
        if form.target_is_owner:
            # Disabled fields fall back to their initial values; only the
            # location scope may change for the owner account.
            services.save_user_access(
                business=request.business,
                membership=access.membership,
                role=access.role,
                is_active=access.is_active,
                allowed_locations=form.cleaned_data["allowed_locations"],
                instance=access,
                request=request,
            )
        else:
            services.update_wms_user(
                business=request.business,
                access=access,
                full_name=form.cleaned_data["full_name"],
                password=form.cleaned_data["password"] or None,
                role=form.cleaned_data["role"],
                allowed_locations=form.cleaned_data["allowed_locations"],
                is_active=form.cleaned_data["is_active"],
                request=request,
            )
        messages.success(request, "WMS user updated.")
        return redirect("wms:user_list")
    return render(
        request,
        "wms/users/user_form.html",
        {
            "form": form,
            "access_record": access,
            "active_nav": "wms",
            "wms_active_nav": "users",
        },
    )


@require_POST
@wms_permission_required("wms.users.manage")
def user_access_status(request, public_id, action):
    access = selectors.get_business_user_access(request.business, public_id)
    if action not in ("activate", "deactivate"):
        messages.error(request, "Unknown WMS access action.")
        return redirect("wms:user_list")
    is_active = action == "activate"
    if not is_active and access.pk == request.wms_user_access.pk:
        messages.error(request, "You cannot deactivate your own WMS access.")
        return redirect("wms:user_list")
    if not is_active and access.membership.user_id == request.business.owner_id:
        messages.error(
            request,
            "The business owner's WMS access cannot be deactivated.",
        )
        return redirect("wms:user_list")
    services.save_user_access(
        business=request.business,
        membership=access.membership,
        role=access.role,
        is_active=is_active,
        allowed_locations=list(access.allowed_locations.all()),
        instance=access,
        request=request,
    )
    messages.success(
        request,
        f"WMS access {'activated' if is_active else 'deactivated'} for "
        f"{access.membership.user.full_name}.",
    )
    return redirect("wms:user_list")


@wms_permission_required("wms.users.manage")
def role_form(request, public_id=None):
    instance = (
        selectors.get_business_role(request.business, public_id)
        if public_id
        else WmsRole(business=request.business)
    )
    if instance.pk and instance.is_system:
        messages.error(
            request,
            "System WMS roles are managed by the platform and cannot be edited.",
        )
        return redirect("wms:user_list")
    form = WmsRoleForm(
        request.business,
        request.POST or None,
        instance=instance,
        acting_access=request.wms_user_access,
    )
    if request.method == "POST" and form.is_valid():
        services.save_role(
            business=request.business,
            name=form.cleaned_data["name"],
            code=form.cleaned_data["code"],
            permissions=form.cleaned_data["permissions"],
            is_active=form.cleaned_data["is_active"],
            is_admin=form.cleaned_data["is_admin"],
            instance=instance if instance.pk else None,
            request=request,
        )
        messages.success(request, "WMS role saved.")
        return redirect("wms:user_list")
    return render(
        request,
        "wms/users/role_form.html",
        {
            "form": form,
            "role": instance if instance.pk else None,
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
