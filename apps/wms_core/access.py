"""Central WMS entitlement, role, permission, and location authorization."""

from dataclasses import dataclass
from enum import Enum
from functools import wraps

from django.core.exceptions import PermissionDenied

from apps.core.decorators import business_required
from apps.subscriptions.access import (
    AccessAction,
    evaluate_access,
    evaluate_actor_access,
)

from .models import WmsUserAccess
from .permissions import WMS_PERMISSIONS

_CACHE_ATTRIBUTE = "_nexa_wms_user_access"


class WmsDenialCode(str, Enum):
    ENTITLEMENT_DENIED = "wms_entitlement_denied"
    USER_ACCESS_REQUIRED = "wms_user_access_required"
    USER_ACCESS_INACTIVE = "wms_user_access_inactive"
    ROLE_INACTIVE = "wms_role_inactive"
    PERMISSION_DENIED = "wms_permission_denied"
    LOCATION_DENIED = "wms_location_denied"


@dataclass(frozen=True, slots=True)
class WmsAccessDecision:
    allowed: bool
    user_access: WmsUserAccess | None
    code: str = ""
    message: str = ""


class WmsAccessDenied(PermissionDenied):
    def __init__(self, decision):
        self.code = decision.code
        super().__init__(decision.message)


def _load_user_access(request, business, membership):
    cache = getattr(request, _CACHE_ATTRIBUTE, None)
    key = (getattr(business, "pk", None), getattr(membership, "pk", None))
    if cache is None:
        cache = {}
        setattr(request, _CACHE_ATTRIBUTE, cache)
    if key not in cache:
        cache[key] = (
            WmsUserAccess.objects.for_business(business)
            .select_related("membership__user", "role")
            .prefetch_related("allowed_locations")
            .filter(membership=membership)
            .first()
        )
    return cache[key]


def _evaluate_explicit_access(user_access, *, permission_code=None, location=None):
    if user_access is None:
        return WmsAccessDecision(
            False,
            None,
            WmsDenialCode.USER_ACCESS_REQUIRED.value,
            "Explicit WMS user access is required.",
        )
    if not user_access.is_active:
        return WmsAccessDecision(
            False,
            user_access,
            WmsDenialCode.USER_ACCESS_INACTIVE.value,
            "WMS user access is inactive.",
        )
    if not user_access.role.is_active:
        return WmsAccessDecision(
            False,
            user_access,
            WmsDenialCode.ROLE_INACTIVE.value,
            "The assigned WMS role is inactive.",
        )
    if permission_code is not None:
        if permission_code not in WMS_PERMISSIONS or not user_access.has_perm(
            permission_code
        ):
            return WmsAccessDecision(
                False,
                user_access,
                WmsDenialCode.PERMISSION_DENIED.value,
                f"WMS permission '{permission_code}' is required.",
            )
    if location is not None and not user_access.can_access_location(location):
        return WmsAccessDecision(
            False,
            user_access,
            WmsDenialCode.LOCATION_DENIED.value,
            "The WMS location is outside the allowed scope.",
        )
    return WmsAccessDecision(True, user_access)


def evaluate_wms_access(
    request,
    *,
    permission_code=None,
    location=None,
    action=None,
):
    shared = evaluate_access(
        request,
        "wms",
        action=action,
    )
    if not shared.allowed:
        return WmsAccessDecision(
            False,
            None,
            WmsDenialCode.ENTITLEMENT_DENIED.value,
            shared.denial.message,
        )
    user_access = _load_user_access(
        request,
        shared.context.business,
        shared.context.membership,
    )
    return _evaluate_explicit_access(
        user_access,
        permission_code=permission_code,
        location=location,
    )


def evaluate_wms_actor_access(
    user,
    business,
    membership,
    *,
    permission_code=None,
    location=None,
    action=AccessAction.READ,
    request=None,
):
    shared = evaluate_actor_access(
        user,
        business,
        "wms",
        action=action,
        membership=membership,
        request=request,
    )
    if not shared.allowed:
        return WmsAccessDecision(
            False,
            None,
            WmsDenialCode.ENTITLEMENT_DENIED.value,
            shared.denial.message,
        )
    user_access = (
        WmsUserAccess.objects.for_business(business)
        .select_related("membership__user", "role")
        .prefetch_related("allowed_locations")
        .filter(membership=membership)
        .first()
    )
    return _evaluate_explicit_access(
        user_access,
        permission_code=permission_code,
        location=location,
    )


def require_wms_access(request, **kwargs):
    decision = evaluate_wms_access(request, **kwargs)
    if not decision.allowed:
        raise WmsAccessDenied(decision)
    return decision.user_access


def first_permitted_wms_route(user_access):
    if user_access is None:
        return None
    priorities = (
        ("wms.dashboard.view", "wms:dashboard"),
        ("wms.orders.view", "wms:order_list"),
        ("wms.alterations.view", "wms:alteration_list"),
        ("wms.attendance.view", "wms:attendance_list"),
        ("wms.production.view", "wms:production_entry_list"),
        ("wms.employees.view", "wms:employee_list"),
        ("wms.categories.view", "wms:category_list"),
        ("wms.users.manage", "wms:user_list"),
        ("wms.settings.manage", "wms:settings"),
    )
    for permission_code, route_name in priorities:
        if user_access.has_perm(permission_code):
            return route_name
    return None


def resolve_wms_home_route(user, business, membership, *, request=None):
    decision = evaluate_wms_actor_access(
        user,
        business,
        membership,
        action=AccessAction.READ,
        request=request,
    )
    if not decision.allowed:
        return None
    return first_permitted_wms_route(decision.user_access)


def wms_permission_required(permission_code, action=None):
    if permission_code not in WMS_PERMISSIONS:
        raise ValueError(f"Unknown WMS permission: {permission_code}")

    def decorator(view_func):
        @wraps(view_func)
        @business_required
        def wrapper(request, *args, **kwargs):
            request.wms_user_access = require_wms_access(
                request,
                permission_code=permission_code,
                action=action,
            )
            return view_func(request, *args, **kwargs)

        wrapper._subscription_module_guarded = True
        return wrapper

    return decorator
