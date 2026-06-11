"""Customer helpers and balance maintenance."""
from decimal import Decimal

from django.db import transaction
from django.db.models import F

from .models import Customer


def ensure_walk_in_customer(business):
    customer, _ = Customer.objects.get_or_create(
        business=business,
        is_walk_in=True,
        defaults={"code": "WALK-IN", "full_name": "Walk-In Customer"},
    )
    return customer


def next_customer_code(business):
    count = Customer.objects.for_business(business).count()
    n = count + 1
    while Customer.objects.for_business(business).filter(code=f"CUST-{n:05d}").exists():
        n += 1
    return f"CUST-{n:05d}"


@transaction.atomic
def apply_balance_change(customer_id, delta: Decimal):
    """Atomically shift a customer's receivable balance."""
    Customer.objects.filter(pk=customer_id).update(balance=F("balance") + delta)


@transaction.atomic
def apply_store_credit_change(customer_id, delta: Decimal):
    Customer.objects.filter(pk=customer_id).update(store_credit=F("store_credit") + delta)
