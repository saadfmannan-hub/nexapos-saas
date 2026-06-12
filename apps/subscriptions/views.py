from django.shortcuts import render

from apps.core.decorators import business_required

from . import services
from .models import Plan


@business_required
def status_view(request):
    sub = request.subscription
    plans = Plan.objects.filter(is_active=True).order_by("sort_order", "monthly_price")
    usage = {}
    if sub:
        for resource in ("branches", "users", "warehouses", "products",
                         "customers", "monthly_invoices"):
            current, limit, allowed = services.limit_state(request.business, resource)
            usage[resource] = {"current": current, "limit": limit, "allowed": allowed}
    return render(request, "subscriptions/status.html", {
        "subscription": sub, "plans": plans, "usage": usage,
        "active_nav": "subscription",
    })
