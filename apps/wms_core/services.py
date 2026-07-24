"""Transactional mutation and provisioning services for the WMS foundation."""

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.accounts.models import Membership
from apps.audit import services as audit
from apps.tenants.models import Business

from .models import WmsLocation, WmsRole, WmsSettings, WmsUserAccess
from .permissions import WMS_SYSTEM_ROLE_TEMPLATES, validate_wms_permissions


def _actor(user=None, request=None):
    return user or getattr(request, "user", None)


@transaction.atomic
def provision_wms_foundation(
    business,
    *,
    owner_membership=None,
    location_specs=(),
    reactivate_owner=False,
    user=None,
    request=None,
):
    """Idempotently create WMS settings, system roles, and explicit owner access."""

    business = Business.objects.select_for_update().get(pk=business.pk)
    settings_obj, _created = WmsSettings.objects.get_or_create(business=business)
    roles = {}
    for code, template in WMS_SYSTEM_ROLE_TEMPLATES.items():
        role, created = WmsRole.objects.get_or_create(
            business=business,
            code=code,
            defaults={
                "name": template["name"],
                "permissions": list(template["permissions"]),
                "is_system": True,
                "is_admin": template["is_admin"],
                "is_active": True,
            },
        )
        if not created and role.name.casefold() != template["name"].casefold():
            raise ValidationError(
                f"WMS role code '{code}' already belongs to '{role.name}'."
            )
        roles[code] = role

    for branch, location_type in location_specs:
        existing_location = WmsLocation.objects.for_business(business).filter(
            branch=branch
        ).first()
        if existing_location is None:
            save_location(
                business=business,
                branch=branch,
                location_type=location_type,
                is_active=True,
                user=user,
                request=request,
            )
        elif existing_location.location_type != location_type:
            raise ValidationError(
                f"Branch '{branch.name}' already has a different WMS location type."
            )

    owner_access = None
    if owner_membership is not None:
        if owner_membership.business_id != business.pk:
            raise ValidationError(
                "The WMS owner membership must belong to the same business."
            )
        owner_access, created = WmsUserAccess.objects.get_or_create(
            business=business,
            membership=owner_membership,
            defaults={
                "role": roles["owner_admin"],
                "is_active": True,
            },
        )
        if created:
            audit.log(
                "wms.user_access_created",
                business=business,
                user=_actor(user, request),
                request=request,
                module="wms",
                obj=owner_access,
                description=f"WMS owner access created for {owner_membership.user.email}.",
            )
        elif reactivate_owner and not owner_access.is_active:
            owner_access.is_active = True
            owner_access.save(update_fields=["is_active", "updated_at"])
            audit.log(
                "wms.user_access_reactivated",
                business=business,
                user=_actor(user, request),
                request=request,
                module="wms",
                obj=owner_access,
                description=(
                    f"WMS owner access reactivated for "
                    f"{owner_membership.user.email}."
                ),
            )
    return {
        "settings": settings_obj,
        "roles": roles,
        "owner_access": owner_access,
    }


@transaction.atomic
def sync_wms_entitlement(
    business,
    *,
    was_enabled,
    is_enabled,
    user=None,
    request=None,
):
    """Provision on enable; retain all WMS data on disable."""

    if was_enabled == is_enabled:
        return None
    if is_enabled:
        owner_membership = (
            Membership.objects.for_business(business)
            .select_related("user")
            .filter(user=business.owner, is_active=True)
            .first()
        )
        result = provision_wms_foundation(
            business,
            owner_membership=owner_membership,
            reactivate_owner=True,
            user=user,
            request=request,
        )
        action = "wms.enabled"
        description = "WMS entitlement enabled and owner foundation provisioned."
    else:
        result = None
        action = "wms.disabled"
        description = "WMS entitlement disabled; existing WMS data was retained."
    audit.log(
        action,
        business=business,
        user=_actor(user, request),
        request=request,
        module="wms",
        obj=business,
        description=description,
    )
    return result


@transaction.atomic
def save_location(
    *,
    business,
    branch,
    location_type,
    is_active=True,
    instance=None,
    user=None,
    request=None,
):
    before_active = instance.is_active if instance is not None else None
    location = instance or WmsLocation(business=business)
    if location.business_id != business.pk:
        raise ValidationError("The WMS location belongs to another business.")
    location.branch = branch
    location.location_type = location_type
    location.is_active = is_active
    location.save()
    if before_active is None:
        action = "wms.location_created"
    elif before_active != is_active:
        action = (
            "wms.location_activated"
            if is_active
            else "wms.location_deactivated"
        )
    else:
        action = "wms.location_updated"
    audit.log(
        action,
        business=business,
        user=_actor(user, request),
        request=request,
        module="wms",
        obj=location,
        description=f"WMS location '{location.branch.name}' saved.",
    )
    return location


@transaction.atomic
def save_settings(settings_obj, cleaned_data, *, user=None, request=None):
    for field, value in cleaned_data.items():
        setattr(settings_obj, field, value)
    settings_obj.save()
    audit.log(
        "wms.settings_changed",
        business=settings_obj.business,
        user=_actor(user, request),
        request=request,
        module="wms",
        obj=settings_obj,
        description="WMS settings updated.",
    )
    return settings_obj


@transaction.atomic
def save_role(
    *,
    business,
    name,
    code,
    permissions,
    is_active=True,
    is_admin=False,
    instance=None,
    user=None,
    request=None,
):
    role = instance or WmsRole(business=business)
    if role.business_id != business.pk:
        raise ValidationError("The WMS role belongs to another business.")
    role.name = name
    role.code = code
    role.permissions = validate_wms_permissions(permissions)
    role.is_active = is_active
    role.is_admin = is_admin
    role.save()
    audit.log(
        "wms.role_changed",
        business=business,
        user=_actor(user, request),
        request=request,
        module="wms",
        obj=role,
        description=f"WMS role '{role.name}' saved.",
    )
    return role


@transaction.atomic
def save_user_access(
    *,
    business,
    membership,
    role,
    is_active=True,
    allowed_locations=(),
    instance=None,
    user=None,
    request=None,
):
    previous_active = instance.is_active if instance is not None else None
    access = instance or WmsUserAccess(business=business)
    if access.business_id != business.pk:
        raise ValidationError("The WMS access record belongs to another business.")
    access.membership = membership
    access.role = role
    access.is_active = is_active
    access.save()
    locations = list(allowed_locations)
    invalid = [location for location in locations if location.business_id != business.pk]
    if invalid:
        raise ValidationError(
            "Every allowed WMS location must belong to the same business."
        )
    access.allowed_locations.set(locations)
    if previous_active is None:
        action = "wms.user_access_created"
    elif previous_active != is_active:
        action = (
            "wms.user_access_reactivated"
            if is_active
            else "wms.user_access_deactivated"
        )
    else:
        action = "wms.user_access_updated"
    audit.log(
        action,
        business=business,
        user=_actor(user, request),
        request=request,
        module="wms",
        obj=access,
        description=f"WMS access saved for {membership.user.email}.",
    )
    return access
