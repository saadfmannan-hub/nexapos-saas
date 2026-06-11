"""Subscription limit enforcement.

All creation paths for limited resources call `check_limit` (raising
LimitExceeded) or `limit_state` (for UI badges). Limits live on the
Plan; 0 means unlimited. Existing data is never deleted when a limit
is exceeded — creation of new records is simply blocked.
"""
from django.utils import timezone


class LimitExceeded(Exception):
    def __init__(self, resource, limit):
        self.resource = resource
        self.limit = limit
        super().__init__(
            f"Your current plan allows at most {limit} {resource}. "
            "Upgrade your plan to add more."
        )


class SubscriptionInactive(Exception):
    pass


def get_subscription(business):
    return getattr(business, "subscription", None)


def require_operational(business):
    sub = get_subscription(business)
    if sub is None or not sub.is_operational:
        raise SubscriptionInactive(
            "The subscription for this business is not active."
        )
    return sub


def _count_for(business, resource):
    if resource == "branches":
        from apps.branches.models import Branch

        return Branch.objects.for_business(business).count()
    if resource == "warehouses":
        from apps.branches.models import Warehouse

        return Warehouse.objects.for_business(business).count()
    if resource == "users":
        from apps.accounts.models import Membership

        return Membership.objects.for_business(business).filter(is_active=True).count()
    if resource == "products":
        from apps.catalog.models import Product

        return Product.objects.for_business(business).count()
    if resource == "customers":
        from apps.customers.models import Customer

        return Customer.objects.for_business(business).count()
    if resource == "monthly_invoices":
        from apps.sales.models import Sale

        now = timezone.now()
        return Sale.objects.for_business(business).filter(
            created_at__year=now.year, created_at__month=now.month
        ).exclude(status__in=["draft", "held"]).count()
    raise ValueError(f"Unknown limited resource: {resource}")


_LIMIT_FIELDS = {
    "branches": "max_branches",
    "warehouses": "max_warehouses",
    "users": "max_users",
    "products": "max_products",
    "customers": "max_customers",
    "monthly_invoices": "max_monthly_invoices",
}


def limit_state(business, resource):
    """Returns (current, limit, allowed). limit 0 = unlimited."""
    sub = get_subscription(business)
    if sub is None:
        return (0, 0, False)
    limit = getattr(sub.plan, _LIMIT_FIELDS[resource], 0)
    current = _count_for(business, resource)
    allowed = limit == 0 or current < limit
    return (current, limit, allowed)


def check_limit(business, resource):
    """Raise LimitExceeded if creating one more `resource` is not allowed."""
    require_operational(business)
    current, limit, allowed = limit_state(business, resource)
    if not allowed:
        raise LimitExceeded(resource.replace("_", " "), limit)


def has_feature(business, feature: str) -> bool:
    sub = get_subscription(business)
    if sub is None:
        return False
    return bool(getattr(sub.plan, f"feature_{feature}", False))
