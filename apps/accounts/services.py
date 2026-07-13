"""Permission-aware destinations for authenticated users."""
from urllib.parse import urlsplit

from django.contrib.auth import logout as auth_logout
from django.db.models import Q
from django.shortcuts import redirect
from django.urls import Resolver404, resolve, reverse
from django.utils.http import url_has_allowed_host_and_scheme

from apps.core.middleware import SESSION_BUSINESS_KEY

SAFE_NEXT_ROUTES = {
    "dashboard": "dashboard.view",
    "sales:pos": "sales.create",
    # This route supports either shift operations or register administration.
    "registers:shift_list": None,
    "sales:list": "sales.view",
    "customers:list": "customers.view",
    "catalog:product_list": "products.view",
    "inventory:stock_list": "inventory.view",
    "purchases:list": "purchases.view",
    "suppliers:list": "suppliers.view",
    "expenses:list": "expenses.view",
    "reports:index": "reports.view",
    "tenants:settings": "settings.manage",
    "branches:list": "branches.manage",
    "accounts:user_list": "users.manage",
    "audit:list": "audit.view",
    "notifications:list": "notifications.view",
    "subscriptions:status": "settings.manage",
    "accounts:profile": None,
    "accounts:change_password": None,
}

MODULE_ROUTE_PRIORITY = (
    ("sales:list", "sales.view"),
    ("customers:list", "customers.view"),
    ("customers:create", "customers.manage"),
    ("catalog:product_list", "products.view"),
    ("catalog:product_create", "products.manage"),
    ("inventory:stock_list", "inventory.view"),
    ("inventory:import", "inventory.import"),
    ("inventory:transfer_list", "inventory.transfer"),
    ("inventory:adjustment_list", "inventory.adjust"),
    ("inventory:count_list", "inventory.count"),
    ("purchases:list", "purchases.view"),
    ("purchases:create", "purchases.manage"),
    ("suppliers:list", "suppliers.view"),
    ("suppliers:create", "suppliers.manage"),
    ("expenses:list", "expenses.view"),
    ("expenses:create", "expenses.manage"),
    ("reports:index", "reports.view"),
    ("tenants:settings", "settings.manage"),
    ("accounts:user_list", "users.manage"),
    ("branches:list", "branches.manage"),
    ("audit:list", "audit.view"),
    ("notifications:list", "notifications.view"),
)


def get_active_membership(user, preferred_business_id=None):
    """Return the active tenant membership used for this login session."""
    if not user.is_authenticated or not user.is_active:
        return None
    memberships = (
        user.memberships.select_related("business", "business__settings", "role")
        .prefetch_related("branches")
        .filter(is_active=True, business__is_active=True)
        .order_by("pk")
    )
    if preferred_business_id:
        preferred = memberships.filter(business_id=preferred_business_id).first()
        if preferred is not None:
            return preferred
    return memberships.first()


def _active_branches(membership):
    from apps.branches.models import Branch

    branches = Branch.objects.for_business(membership.business).filter(is_active=True)
    allowed = membership.allowed_branch_ids
    if allowed is not None:
        branches = branches.filter(id__in=allowed)
    return branches.order_by("name")


def _branch_has_warehouse(business, branch):
    from apps.branches.models import Warehouse

    return Warehouse.objects.for_business(business).filter(
        Q(branch=branch) | Q(branch__isnull=True), is_active=True
    ).exists()


def _shift_route_available(membership, branches):
    if membership.has_perm("registers.manage"):
        return True
    if not membership.has_perm("shifts.open") or not branches.exists():
        return False
    from apps.registers.models import CashRegister

    return CashRegister.objects.for_business(membership.business).filter(
        branch__in=branches, branch__is_active=True, is_active=True
    ).exists()


def _sales_destination(user, membership, excluded_routes):
    if not membership.has_perm("sales.create"):
        return None

    from apps.registers import services as register_services

    branches = _active_branches(membership)
    branch_ids = list(branches.values_list("id", flat=True))
    if not branch_ids:
        return None

    shift = register_services.get_open_shift(
        membership.business, user, membership=membership
    )
    if (
        shift is not None
        and "sales:pos" not in excluded_routes
        and _branch_has_warehouse(membership.business, shift.branch)
    ):
        return "sales:pos"

    settings_obj = membership.business.settings
    first_branch = branches.first()
    if (
        shift is None
        and settings_obj.allow_sale_without_shift
        and "sales:pos" not in excluded_routes
        and first_branch is not None
        and _branch_has_warehouse(membership.business, first_branch)
    ):
        return "sales:pos"

    if (
        "registers:shift_list" not in excluded_routes
        and _shift_route_available(membership, branches)
    ):
        return "registers:shift_list"
    return None


def resolve_user_home_route(user, membership=None, excluded_routes=None):
    """Return the first named route the user can actually open."""
    excluded_routes = frozenset(excluded_routes or ())
    if not user.is_authenticated or not user.is_active:
        return "accounts:login"
    if membership is None:
        membership = get_active_membership(user)
    if membership is None:
        if user.is_platform_staff:
            return "platformadmin:dashboard"
        return "tenants:no_business"

    if (
        "dashboard" not in excluded_routes
        and membership.has_perm("dashboard.view")
    ):
        return "dashboard"

    sales_destination = _sales_destination(user, membership, excluded_routes)
    if sales_destination:
        return sales_destination

    for route_name, permission in MODULE_ROUTE_PRIORITY:
        if route_name not in excluded_routes and membership.has_perm(permission):
            return route_name

    if "registers:shift_list" not in excluded_routes:
        branches = _active_branches(membership)
        if _shift_route_available(membership, branches):
            return "registers:shift_list"
    return "accounts:no_access"


def resolve_authorized_next_route(request, membership, next_url, excluded_routes=None):
    """Resolve a safe, exact landing route from an untrusted ``next`` value."""
    if not next_url or membership is None:
        return None
    if not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return None

    path = urlsplit(next_url).path
    try:
        match = resolve(path)
    except Resolver404:
        return None
    route_name = match.view_name
    if route_name not in SAFE_NEXT_ROUTES or route_name in set(excluded_routes or ()):
        return None
    if match.args or match.kwargs or reverse(route_name) != path:
        return None

    permission = SAFE_NEXT_ROUTES[route_name]
    if permission and not membership.has_perm(permission):
        return None
    if route_name == "sales:pos":
        return (
            route_name
            if _sales_destination(request.user, membership, frozenset()) == route_name
            else None
        )
    if route_name == "registers:shift_list":
        return (
            route_name
            if _shift_route_available(membership, _active_branches(membership))
            else None
        )
    return route_name


def post_login_redirect(request, *, next_url=None, membership=None, excluded_routes=None):
    """Build the safe redirect response shared by all authenticated entry points."""
    user = request.user
    if not user.is_authenticated:
        return redirect("accounts:login")
    if not user.is_active:
        auth_logout(request)
        return redirect("accounts:login")

    if membership is not None and (
        membership.user_id != user.id
        or not membership.is_active
        or not membership.business.is_active
    ):
        membership = None

    if membership is None:
        current = getattr(request, "membership", None)
        if (
            current is not None
            and current.user_id == user.id
            and current.is_active
            and current.business.is_active
        ):
            membership = current
        else:
            membership = get_active_membership(
                user, request.session.get(SESSION_BUSINESS_KEY)
            )

    if membership is not None:
        request.session[SESSION_BUSINESS_KEY] = membership.business_id
        route_name = resolve_authorized_next_route(
            request, membership, next_url, excluded_routes
        )
        if route_name:
            return redirect(route_name)

    return redirect(
        resolve_user_home_route(
            user, membership=membership, excluded_routes=excluded_routes
        )
    )
