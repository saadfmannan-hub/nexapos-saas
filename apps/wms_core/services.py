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
        if not created:
            missing_permissions = [
                permission
                for permission in template["permissions"]
                if permission not in set(role.permissions or [])
            ]
            if missing_permissions:
                role.permissions = validate_wms_permissions(
                    [*(role.permissions or []), *missing_permissions]
                )
                role.save(update_fields=["permissions", "updated_at"])
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
def save_business_timezone(*, business, timezone_name, user=None, request=None):
    """Update the shared Business timezone from the WMS settings screen.

    Reuses the existing Business.timezone field; historical data is never
    converted or rewritten.
    """

    timezone_name = (timezone_name or "").strip()
    if not timezone_name or timezone_name == business.timezone:
        return business
    previous = business.timezone
    business.timezone = timezone_name
    business.save(update_fields=["timezone", "updated_at"])
    audit.log(
        "wms.business_timezone_changed",
        business=business,
        user=_actor(user, request),
        request=request,
        module="wms",
        obj=business,
        description=(
            f"Business time zone changed from '{previous}' to '{timezone_name}'."
        ),
    )
    return business


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


# Platform role assigned to memberships created from the WMS staff screen.
# It intentionally carries no POS permissions; WMS authorization is granted
# separately through WmsUserAccess.
WMS_STAFF_PLATFORM_ROLE_NAME = "WMS Staff"


def _wms_staff_platform_role(business):
    from apps.accounts.models import Role

    role, _created = Role.objects.get_or_create(
        business=business,
        name=WMS_STAFF_PLATFORM_ROLE_NAME,
        defaults={"is_system": True, "permissions": []},
    )
    return role


def _lock_and_check_user_seat(business):
    """Serialize active-seat allocation exactly like the POS user screens."""

    from apps.subscriptions import services as subscriptions

    Business.objects.select_for_update().only("pk").get(pk=business.pk)
    subscriptions.check_limit(business, "users")


@transaction.atomic
def create_wms_user(
    *,
    business,
    full_name,
    email,
    password,
    role,
    allowed_locations=(),
    is_active=True,
    user=None,
    request=None,
):
    """Create a login account, membership, and WMS access in one step.

    Reuses the existing account uniqueness rules, seat limits, secure
    password hashing, and the established WMS access service.
    """

    from apps.accounts.models import Membership, User

    _lock_and_check_user_seat(business)
    email = User.objects.normalize_email(email)
    account = User.objects.filter(email__iexact=email).first()
    account_created = account is None
    if account_created:
        account = User.objects.create_user(
            email=email,
            password=password,
            full_name=full_name,
        )
    if Membership.objects.filter(business=business, user=account).exists():
        raise ValidationError(
            "This email already belongs to a member of this business."
        )
    membership = Membership.objects.create(
        business=business,
        user=account,
        role=_wms_staff_platform_role(business),
        is_active=True,
    )
    access = save_user_access(
        business=business,
        membership=membership,
        role=role,
        is_active=is_active,
        allowed_locations=allowed_locations,
        user=user,
        request=request,
    )
    audit.log(
        "wms.user_created",
        business=business,
        user=_actor(user, request),
        request=request,
        module="wms",
        obj=account,
        description=(
            f"WMS staff user {account.email} "
            f"{'created' if account_created else 'attached'} with WMS role "
            f"'{role.name}'."
        ),
    )
    return access


@transaction.atomic
def update_wms_user(
    *,
    business,
    access,
    full_name=None,
    password=None,
    role,
    allowed_locations=(),
    is_active=True,
    user=None,
    request=None,
):
    """Update a WMS staff member's identity, password, role, and scope."""

    account = access.membership.user
    identity_changes = []
    if full_name is not None and full_name != account.full_name:
        account.full_name = full_name
        identity_changes.append("full name")
    if password:
        account.set_password(password)
        identity_changes.append("password")
    if identity_changes:
        account.save()
        audit.log(
            "wms.user_updated",
            business=business,
            user=_actor(user, request),
            request=request,
            module="wms",
            obj=account,
            description=(
                f"WMS staff user {account.email} updated "
                f"({', '.join(identity_changes)})."
            ),
        )
    return save_user_access(
        business=business,
        membership=access.membership,
        role=role,
        is_active=is_active,
        allowed_locations=allowed_locations,
        instance=access,
        user=user,
        request=request,
    )


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
