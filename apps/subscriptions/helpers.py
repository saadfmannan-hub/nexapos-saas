"""View helpers for subscription limit enforcement.

Every "create" view funnels LimitExceeded / SubscriptionInactive into
`limit_blocked_response`, which renders a dedicated upgrade page that
names the exceeded limit, shows current usage, and links to the plans.
"""
from django.shortcuts import render

from . import services

RESOURCE_LABELS = {
    "branches": "Branches",
    "warehouses": "Warehouses",
    "users": "Users",
    "products": "Products",
    "customers": "Customers",
    "monthly_invoices": "Invoices this month",
    "employees": "Employees",
    "suppliers": "Suppliers",
    "active_orders": "Active orders",
    "api_calls": "API calls",
    "branch_managers": "Branch managers",
    "cashiers": "Cashiers",
    "logged_in_devices": "Logged-in devices",
    "pos_terminals": "POS terminals",
}


def limit_blocked_response(request, exc, resource=None):
    """Render the upgrade page for a blocked creation attempt."""
    context = {
        "active_nav": "",
        "error_message": str(exc),
        "resource": resource,
        "resource_label": RESOURCE_LABELS.get(resource, resource or ""),
    }
    if isinstance(exc, services.LimitExceeded) and resource:
        current, limit, _allowed = services.limit_state(request.business, resource)
        context.update({"current": current, "limit": limit})
        # Full usage table so the owner sees exactly where they stand
        usage = {}
        for key in RESOURCE_LABELS:
            current, limit, allowed = services.limit_state(request.business, key)
            usage[key] = {"label": RESOURCE_LABELS[key], "current": current,
                          "limit": limit, "exceeded": not allowed and limit > 0}
        context["usage"] = usage
        context["plan"] = request.subscription.plan if request.subscription else None
    return render(request, "subscriptions/limit_blocked.html", context, status=200)


def guard_limit(request, resource):
    """Returns an upgrade-page response if creating `resource` is blocked,
    else None. Usage:  blocked = guard_limit(request, "branches");
    if blocked: return blocked"""
    try:
        services.check_limit(request.business, resource)
    except (services.LimitExceeded, services.SubscriptionInactive) as exc:
        return limit_blocked_response(request, exc, resource=resource)
    return None
