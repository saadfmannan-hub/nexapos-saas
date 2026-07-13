"""Register lifecycle and shift services."""
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import ProtectedError, Q, Sum
from django.utils import timezone

from apps.audit import services as audit
from apps.subscriptions import services as subscriptions

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


class RegisterLifecycleError(Exception):
    pass


REGISTER_HISTORY_DELETE_ERROR = (
    "This register has transaction or shift history and cannot be permanently "
    "deleted. Archive it instead."
)


@dataclass(frozen=True)
class RegisterDeletionAssessment:
    blockers: tuple[str, ...]
    audit_logs_preserved: bool

    @property
    def can_delete(self):
        return not self.blockers


def assess_register_deletion(register):
    """Find every operational or financial dependency before hard deletion.

    Audit rows are intentionally not blockers: they store a detached object id
    and remain immutable after a safe register deletion.
    """
    from apps.audit.models import AuditLog
    from apps.customers.models import CustomerPayment
    from apps.expenses.models import Expense
    from apps.sales.models import Sale, SalePayment, SaleReturn

    business = register.business
    blockers = []
    checks = (
        (
            "shift, session, or reconciliation history",
            Shift.objects.for_business(business).filter(register=register),
        ),
        (
            "sale or invoice history",
            Sale.objects.for_business(business).filter(register=register),
        ),
        (
            "sale payment history",
            SalePayment.objects.for_business(business).filter(
                Q(sale__register=register) | Q(shift__register=register)
            ),
        ),
        (
            "return or refund history",
            SaleReturn.objects.for_business(business).filter(
                Q(sale__register=register) | Q(shift__register=register)
            ),
        ),
        (
            "customer payment history",
            CustomerPayment.objects.for_business(business).filter(
                shift__register=register
            ),
        ),
        (
            "expense or cash movement history",
            Expense.objects.for_business(business).filter(shift__register=register),
        ),
    )
    for label, queryset in checks:
        if queryset.exists():
            blockers.append(label)

    has_audit_logs = AuditLog.objects.filter(
        business=business,
        object_type="CashRegister",
        object_id=str(register.public_id),
    ).exists()
    return RegisterDeletionAssessment(tuple(blockers), has_audit_logs)


@transaction.atomic
def archive_register(*, register, user, request=None):
    register = (
        CashRegister.objects.select_for_update()
        .select_related("branch", "business")
        .get(pk=register.pk)
    )
    if not register.is_active:
        raise RegisterLifecycleError("This register is already archived.")
    if register.shifts.filter(status=Shift.Status.OPEN).exists():
        raise RegisterLifecycleError(
            "Close the register's open shift before archiving it."
        )
    register.is_active = False
    register.save(update_fields=["is_active", "updated_at"])
    audit.log(
        "register.archived",
        business=register.business,
        user=user,
        request=request,
        module="registers",
        obj=register,
        description=f"Register {register.code} archived.",
        old_values={"is_active": True},
        new_values={"is_active": False},
    )
    return register


@transaction.atomic
def reactivate_register(*, register, user, request=None):
    register = (
        CashRegister.objects.select_for_update()
        .select_related("branch", "business")
        .get(pk=register.pk)
    )
    if register.is_active:
        raise RegisterLifecycleError("This register is already active.")
    if not register.branch.is_active:
        raise RegisterLifecycleError(
            "Move this register to an active branch before reactivating it."
        )
    if CashRegister.objects.for_business(register.business).filter(
        code__iexact=register.code,
        is_active=True,
    ).exclude(pk=register.pk).exists():
        raise RegisterLifecycleError(
            "An active register already uses this code. Edit the code first."
        )
    subscriptions.check_limit(register.business, "pos_terminals")
    register.is_active = True
    register.save(update_fields=["is_active", "updated_at"])
    audit.log(
        "register.reactivated",
        business=register.business,
        user=user,
        request=request,
        module="registers",
        obj=register,
        description=f"Register {register.code} reactivated.",
        old_values={"is_active": False},
        new_values={"is_active": True},
    )
    return register


@transaction.atomic
def delete_register_if_safe(*, register, user, request=None):
    register = (
        CashRegister.objects.select_for_update()
        .select_related("branch", "business")
        .get(pk=register.pk)
    )
    assessment = assess_register_deletion(register)
    if not assessment.can_delete:
        raise RegisterLifecycleError(REGISTER_HISTORY_DELETE_ERROR)

    code = register.code
    name = register.name
    business = register.business
    try:
        with transaction.atomic():
            register.delete()
    except ProtectedError as exc:
        raise RegisterLifecycleError(REGISTER_HISTORY_DELETE_ERROR) from exc

    audit.log(
        "register.deleted",
        business=business,
        user=user,
        request=request,
        module="registers",
        obj=register,
        description=f"Unused register {code} ({name}) permanently deleted.",
        old_values={"name": name, "code": code},
    )
    return assessment


@transaction.atomic
def open_shift(*, business, register, cashier, opening_cash, notes="", request=None):
    try:
        register = (
            CashRegister.objects.select_for_update()
            .select_related("branch")
            .get(pk=register.pk, business=business)
        )
    except CashRegister.DoesNotExist as exc:
        raise ShiftError("This register is not available.") from exc
    if not register.is_active or not register.branch.is_active:
        raise ShiftError("This register is archived or its branch is inactive.")
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
    shift = (
        Shift.objects.select_for_update()
        .select_related("register__branch")
        .get(pk=shift.pk)
    )
    if shift.status not in (Shift.Status.CLOSED, Shift.Status.APPROVED):
        raise ShiftError("Only closed shifts can be reopened.")
    if not shift.register.is_active or not shift.register.branch.is_active:
        raise ShiftError("Archived registers cannot be reopened for operations.")
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
