"""Register/shift services: defaults, open/close, expected cash."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.audit import services as audit

from .models import CashRegister, Shift


def create_default_register(business, branch):
    register, _ = CashRegister.objects.get_or_create(
        business=business, code="REG1",
        defaults={"name": "Main Register", "branch": branch},
    )
    return register


def get_open_shift(business, user, register=None, membership=None):
    qs = Shift.objects.for_business(business).filter(status=Shift.Status.OPEN)
    if register is not None:
        qs = qs.filter(register=register)
    else:
        qs = qs.filter(cashier=user)
    if membership is not None:
        if membership.business_id != business.id or membership.user_id != user.id:
            return None
        qs = qs.filter(branch__is_active=True, register__is_active=True)
        allowed = membership.allowed_branch_ids
        if allowed is not None:
            qs = qs.filter(branch_id__in=allowed)
    return qs.select_related("register", "branch").first()


class ShiftError(Exception):
    pass


@transaction.atomic
def open_shift(*, business, register, cashier, opening_cash, notes="", request=None):
    if Shift.objects.for_business(business).filter(
        register=register, status=Shift.Status.OPEN
    ).exists():
        raise ShiftError("This register already has an open shift.")
    if Shift.objects.for_business(business).filter(
        cashier=cashier, status=Shift.Status.OPEN
    ).exists():
        raise ShiftError("You already have an open shift on another register.")
    shift = Shift.objects.create(
        business=business,
        register=register,
        branch=register.branch,
        cashier=cashier,
        opened_at=timezone.now(),
        opening_cash=opening_cash,
        opening_notes=notes,
    )
    audit.log("shift.opened", business=business, user=cashier, request=request,
              module="registers", obj=shift,
              description=f"Shift opened on {register} with {opening_cash} opening cash.")
    return shift


def shift_totals(shift):
    """Aggregate payment/refund/expense figures for X/Z reports."""
    from apps.customers.models import CustomerPayment
    from apps.expenses.models import Expense
    from apps.sales.models import SalePayment, SaleReturn

    zero = Decimal("0")
    payments = (
        SalePayment.objects.for_business(shift.business)
        .filter(shift=shift)
        .values("method__kind")
        .annotate(total=Sum("amount"))
    )
    by_kind = {row["method__kind"]: row["total"] or zero for row in payments}

    collections = (
        CustomerPayment.objects.for_business(shift.business)
        .filter(shift=shift, kind=CustomerPayment.Kind.COLLECTION)
        .values("payment_method__kind")
        .annotate(total=Sum("amount"))
    )
    collections_by_kind = {
        row["payment_method__kind"]: row["total"] or zero for row in collections
    }

    cash_refunds = (
        SaleReturn.objects.for_business(shift.business)
        .filter(shift=shift, refund_method=SaleReturn.RefundMethod.CASH)
        .aggregate(t=Sum("refund_amount"))["t"] or zero
    )
    cash_expenses = (
        Expense.objects.for_business(shift.business)
        .filter(shift=shift, payment_method__kind="cash")
        .exclude(status__in=["rejected", "cancelled"])
        .aggregate(t=Sum("amount"))["t"] or zero
    )

    cash_sales = by_kind.get("cash", zero)
    cash_collected = collections_by_kind.get("cash", zero)
    # Change is recorded on the sale; cash payments are stored net of change.
    expected_cash = (
        shift.opening_cash + cash_sales + cash_collected - cash_refunds - cash_expenses
    )
    return {
        "cash_sales": cash_sales,
        "card_sales": by_kind.get("card", zero),
        "bank_sales": by_kind.get("bank", zero),
        "credit_sales": by_kind.get("customer_credit", zero),
        "store_credit_used": by_kind.get("store_credit", zero),
        "other_sales": by_kind.get("other", zero) + by_kind.get("online", zero),
        "customer_collections_cash": cash_collected,
        "cash_refunds": cash_refunds,
        "cash_expenses": cash_expenses,
        "expected_cash": expected_cash,
    }


@transaction.atomic
def close_shift(*, shift, actual_cash, notes="", denominations=None, user=None, request=None):
    if shift.status != Shift.Status.OPEN:
        raise ShiftError("This shift is not open.")
    totals = shift_totals(shift)
    shift.expected_cash = totals["expected_cash"]
    shift.actual_cash = actual_cash
    shift.difference = actual_cash - shift.expected_cash
    shift.closing_notes = notes
    shift.denominations = denominations
    shift.closed_at = timezone.now()
    shift.status = Shift.Status.CLOSED
    shift.save()
    audit.log("shift.closed", business=shift.business, user=user or shift.cashier,
              request=request, module="registers", obj=shift,
              description=(f"Shift closed. Expected {shift.expected_cash}, "
                           f"actual {actual_cash}, difference {shift.difference}."))
    if shift.difference != 0:
        from apps.notifications.services import notify_role

        notify_role(
            shift.business, "shifts.approve",
            f"Cash difference of {shift.difference} on {shift.register.name}",
            body=f"Shift {shift.pk} closed by {shift.cashier.full_name}.",
            severity="warning", category="cash_difference",
        )
    return shift


@transaction.atomic
def reopen_shift(*, shift, user, request=None):
    if shift.status not in (Shift.Status.CLOSED, Shift.Status.APPROVED):
        raise ShiftError("Only closed shifts can be reopened.")
    if Shift.objects.for_business(shift.business).filter(
        register=shift.register, status=Shift.Status.OPEN
    ).exists():
        raise ShiftError("The register already has another open shift.")
    shift.status = Shift.Status.OPEN
    shift.closed_at = None
    shift.reopened_count += 1
    shift.save()
    audit.log("shift.reopened", business=shift.business, user=user, request=request,
              module="registers", obj=shift,
              description=f"Shift {shift.pk} reopened (count {shift.reopened_count}).")
    return shift
