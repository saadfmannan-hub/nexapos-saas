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
    if resource == "employees":
        from apps.accounts.models import Membership

        return Membership.objects.for_business(business).filter(
            is_active=True,
        ).exclude(role__is_owner=True).count()
    if resource == "suppliers":
        from apps.suppliers.models import Supplier

        return Supplier.objects.for_business(business).filter(is_active=True).count()
    if resource == "active_orders":
        from apps.sales.models import HeldSale, Sale

        open_deliveries = Sale.objects.for_business(business).exclude(
            delivery_status__in=[
                "",
                Sale.DeliveryStatus.DELIVERED,
                Sale.DeliveryStatus.CANCELLED,
            ]
        ).count()
        held_carts = HeldSale.objects.for_business(business).count()
        return open_deliveries + held_carts
    if resource == "branch_managers":
        from apps.accounts.models import Membership

        return Membership.objects.for_business(business).filter(
            is_active=True,
            role__name__iexact="Branch Manager",
        ).count()
    if resource == "cashiers":
        from apps.accounts.models import Membership

        return Membership.objects.for_business(business).filter(
            is_active=True,
            role__name__iexact="Cashier",
        ).count()
    if resource == "pos_terminals":
        from apps.registers.models import CashRegister

        return CashRegister.objects.for_business(business).filter(is_active=True).count()
    if resource in {"api_calls", "logged_in_devices"}:
        # No usage ledger exists yet; keep these commercial limits inert until
        # the related modules record API/device activity.
        return 0
    raise ValueError(f"Unknown limited resource: {resource}")


_LIMIT_FIELDS = {
    "branches": "max_branches",
    "warehouses": "max_warehouses",
    "users": "max_users",
    "products": "max_products",
    "customers": "max_customers",
    "monthly_invoices": "max_monthly_invoices",
    "employees": "max_employees",
    "suppliers": "max_suppliers",
    "active_orders": "max_active_orders",
    "api_calls": "max_api_calls",
    "branch_managers": "max_branch_managers",
    "cashiers": "max_cashiers",
    "logged_in_devices": "max_logged_in_devices",
    "pos_terminals": "max_pos_terminals",
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
    return sub.plan.has_feature(feature)


def has_tailoring_module(business) -> bool:
    return has_feature(business, "tailoring_module")


def has_executive_dashboard(business) -> bool:
    return has_feature(business, "executive_dashboard")
