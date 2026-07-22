"""Subscription enforcement and payment-ledger services.

All creation paths for limited resources call `check_limit` (raising
LimitExceeded) or `limit_state` (for UI badges). Limits live on the
Plan; 0 means unlimited. Existing data is never deleted when a limit
is exceeded — creation of new records is simply blocked.
"""
from datetime import datetime

from django.db import transaction
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


SUBSCRIPTION_STATE_FIELDS = (
    "plan_id",
    "status",
    "billing_cycle",
    "trial_ends_at",
    "current_period_start",
    "current_period_end",
    "cancelled_at",
    "notes",
)
SUBSCRIPTION_PERIOD_FIELDS = (
    "current_period_start",
    "current_period_end",
)
SUBSCRIPTION_DATETIME_FIELDS = {
    "trial_ends_at",
    "current_period_start",
    "current_period_end",
    "cancelled_at",
}


class PaymentAlreadyReversed(Exception):
    pass


class PaymentReversalReasonRequired(Exception):
    pass


def _serialize_state_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def capture_subscription_state(subscription):
    """Return a JSON-safe snapshot sufficient to reverse a period change."""
    return {
        field: _serialize_state_value(getattr(subscription, field))
        for field in SUBSCRIPTION_STATE_FIELDS
    }


def _deserialize_state_value(field, value):
    if field in SUBSCRIPTION_DATETIME_FIELDS and value:
        return datetime.fromisoformat(value)
    return value


def _state_matches(subscription, expected):
    if not expected:
        return False
    current = capture_subscription_state(subscription)
    return all(current.get(field) == expected.get(field)
               for field in SUBSCRIPTION_STATE_FIELDS)


def _restore_subscription_state(subscription, state, *, period_only=False):
    fields = SUBSCRIPTION_PERIOD_FIELDS if period_only else SUBSCRIPTION_STATE_FIELDS
    for field in fields:
        if field in state:
            setattr(
                subscription,
                field,
                _deserialize_state_value(field, state[field]),
            )
    subscription.save(update_fields=[*fields, "updated_at"])


def payment_audit_values(payment):
    return {
        "amount": str(payment.amount),
        "method": payment.method,
        "reference": payment.reference,
        "payment_date": str(payment.payment_date),
        "notes": payment.notes,
        "reversed_at": (
            payment.reversed_at.isoformat() if payment.reversed_at else None
        ),
        "reversed_by": payment.reversed_by_id,
        "reversal_reason": payment.reversal_reason,
    }


@transaction.atomic
def reverse_subscription_payment(*, payment_id, reversed_by, reason):
    """Soft-reverse a payment and safely restore its subscription effect.

    New payment-linked period changes carry exact before/after snapshots. Legacy
    rows fall back to the latest remaining period-bearing payment, or to the
    reversed row's period start. The fallback is deliberately conservative and
    never leaves the reversed period end in force.
    """
    from apps.subscriptions.models import Subscription, SubscriptionPayment

    reason = (reason or "").strip()
    if not reason:
        raise PaymentReversalReasonRequired

    payment = (
        SubscriptionPayment.objects.select_for_update()
        .select_related("subscription")
        .get(pk=payment_id)
    )
    if payment.reversed_at:
        raise PaymentAlreadyReversed

    subscription = Subscription.objects.select_for_update().get(
        pk=payment.subscription_id
    )
    payment_before = payment_audit_values(payment)
    subscription_before = capture_subscription_state(subscription)

    payment.reversed_at = timezone.now()
    payment.reversed_by = reversed_by
    payment.reversal_reason = reason[:400]
    payment.save(update_fields=[
        "reversed_at", "reversed_by", "reversal_reason", "updated_at",
    ])

    if payment.period_end and subscription.current_period_end == payment.period_end:
        if payment.subscription_state_before:
            restore_full_state = _state_matches(
                subscription, payment.subscription_state_after
            )
            _restore_subscription_state(
                subscription,
                payment.subscription_state_before,
                period_only=not restore_full_state,
            )
        else:
            latest_period_payment = (
                SubscriptionPayment.objects.active()
                .filter(
                    subscription=subscription,
                    period_end__isnull=False,
                )
                .exclude(pk=payment.pk)
                .order_by("-period_end", "-created_at")
                .first()
            )
            subscription.current_period_start = (
                latest_period_payment.period_start
                if latest_period_payment else None
            )
            subscription.current_period_end = (
                latest_period_payment.period_end
                if latest_period_payment else payment.period_start
            )
            subscription.save(update_fields=[
                "current_period_start", "current_period_end", "updated_at",
            ])

    return (
        payment,
        payment_before,
        subscription_before,
        capture_subscription_state(subscription),
    )
