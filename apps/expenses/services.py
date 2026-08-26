"""Expense defaults, numbering, and recurring monthly generation."""
import calendar
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from apps.audit import services as audit
from apps.branches.models import Branch
from apps.core.date_ranges import business_localdate
from apps.subscriptions.access import (
    AccessAction,
    evaluate_public_access,
    require_actor_access,
)
from apps.subscriptions.exceptions import DenialCode

from .models import Expense, ExpenseCategory, RecurringExpenseTemplate

DEFAULT_EXPENSE_CATEGORIES = [
    "Rent", "Salaries", "Utilities", "Transport",
    "Maintenance", "Marketing", "Office Supplies", "Other",
]


def create_default_expense_categories(business):
    for name in DEFAULT_EXPENSE_CATEGORIES:
        ExpenseCategory.objects.get_or_create(business=business, name=name, parent=None)


def next_expense_number(business):
    n = Expense.objects.for_business(business).count() + 1
    while Expense.objects.for_business(business).filter(
        expense_number=f"EXP-{n:06d}"
    ).exists():
        n += 1
    return f"EXP-{n:06d}"


def matching_open_drawer_shift(*, business, user, branch, membership=None):
    """Return the actor's one unambiguous open drawer for this branch."""
    if branch is None or branch.business_id != business.id:
        return None
    from apps.registers.models import Shift

    if membership is not None and (
        membership.business_id != business.id or membership.user_id != user.id
    ):
        return None
    shifts = Shift.objects.for_business(business).filter(
        status=Shift.Status.OPEN,
        cashier=user,
        branch=branch,
        branch__usage_type=Branch.UsageType.SALES_BRANCH,
    )
    if membership is not None:
        shifts = shifts.filter(branch__is_active=True, register__is_active=True)
        allowed = membership.allowed_branch_ids
        if allowed is not None:
            shifts = shifts.filter(branch_id__in=allowed)
    matches = list(
        shifts.select_related("register", "branch", "cashier")
        .order_by("-opened_at", "-pk")[:2]
    )
    if len(matches) != 1:
        return None
    return matches[0]


def filter_by_payment_medium(queryset, medium):
    """Filter expenses by the derived payment-medium classification."""
    if medium == "cash_register":
        return queryset.filter(payment_method__kind="cash", shift__isnull=False)
    if medium == "cash_non_register":
        return queryset.filter(payment_method__kind="cash", shift__isnull=True)
    if medium in {"card", "bank", "online"}:
        return queryset.filter(payment_method__kind=medium)
    if medium == "other":
        return queryset.filter(
            payment_method__kind__in=(
                "other", "customer_credit", "store_credit",
            )
        )
    if medium == "unspecified":
        return queryset.filter(payment_method__isnull=True)
    return queryset


def _refresh_closed_shift_snapshots(shifts):
    """Keep stored close totals aligned after an audited expense correction."""
    from apps.registers import services as register_services
    from apps.registers.models import Shift

    changes = []
    for shift in shifts:
        if shift.status not in (Shift.Status.CLOSED, Shift.Status.APPROVED):
            continue
        totals = register_services.shift_totals(shift)
        old_expected = shift.expected_cash
        old_difference = shift.difference
        shift.expected_cash = totals["expected_cash"]
        if shift.actual_cash is not None:
            shift.difference = shift.actual_cash - shift.expected_cash
        shift.save(update_fields=["expected_cash", "difference", "updated_at"])
        changes.append({
            "shift_id": shift.pk,
            "old_expected_cash": str(old_expected),
            "new_expected_cash": str(shift.expected_cash),
            "old_difference": str(old_difference),
            "new_difference": str(shift.difference),
        })
    return changes


def _drawer_cash_contribution(expense, *, shift_id, status):
    """Return this expense's contribution to one exact drawer shift."""
    if (
        not shift_id
        or not expense.payment_method_id
        or expense.payment_method.kind != "cash"
        or status in (Expense.Status.REJECTED, Expense.Status.CANCELLED)
    ):
        return Decimal("0")
    return expense.amount or Decimal("0")


def _require_closed_shift_reconciliation_approval(
    *,
    old_expense,
    new_expense,
    old_shift_id,
    new_shift_id,
    old_status,
    new_status,
    locked_shifts,
    user,
    business,
    membership,
    request=None,
):
    """Require shift approval only when a closed Z total would change."""
    from apps.registers.models import Shift

    old_contribution = _drawer_cash_contribution(
        old_expense,
        shift_id=old_shift_id,
        status=old_status,
    )
    new_contribution = _drawer_cash_contribution(
        new_expense,
        shift_id=new_shift_id,
        status=new_status,
    )
    for shift_id in {old_shift_id, new_shift_id} - {None}:
        shift = locked_shifts.get(shift_id)
        if shift is None or shift.status not in (
            Shift.Status.CLOSED,
            Shift.Status.APPROVED,
        ):
            continue
        old_value = old_contribution if shift_id == old_shift_id else Decimal("0")
        new_value = new_contribution if shift_id == new_shift_id else Decimal("0")
        if old_value == new_value:
            continue
        require_actor_access(
            user,
            business,
            "pos_core",
            permission_code="shifts.approve",
            action=AccessAction.WRITE,
            membership=membership,
            request=request,
        )
        return


@transaction.atomic
def save_manual_expense(
    *,
    expense,
    business,
    user,
    membership=None,
    requested_shift=None,
    drawer_requested=False,
    historical_correction=False,
    request=None,
):
    """Validate and persist one manual expense and its exact drawer link.

    A cash payment is a register movement only when ``requested_shift`` is
    explicitly supplied. Non-cash methods always clear the shift. Historical
    changes to closed/approved shifts require both expense and shift approval
    permissions and refresh the stored Z-report snapshot atomically.
    """
    context = require_actor_access(
        user,
        business,
        "expenses",
        permission_code="expenses.manage",
        action=AccessAction.WRITE,
        membership=membership,
        request=request,
    )
    membership = context.membership
    if historical_correction:
        require_actor_access(
            user,
            business,
            "expenses",
            permission_code="expenses.approve",
            action=AccessAction.WRITE,
            membership=membership,
            request=request,
        )
        require_actor_access(
            user,
            business,
            "pos_core",
            permission_code="shifts.approve",
            action=AccessAction.WRITE,
            membership=membership,
            request=request,
        )

    persisted = None
    if expense.pk:
        persisted = (
            Expense.objects.select_for_update()
            .for_business(business)
            .filter(pk=expense.pk)
            .select_related("payment_method")
            .first()
        )
        if persisted is None:
            raise ValidationError("This expense is no longer available.")
    elif expense.created_by_id is None:
        expense.created_by = user

    if expense.business_id and expense.business_id != business.id:
        raise ValidationError("The expense belongs to another business.")
    expense.business = business
    if expense.branch_id is None or expense.branch.business_id != business.id:
        raise ValidationError({"branch": "Select a branch from this business."})
    if not membership.can_access_branch(expense.branch):
        raise ValidationError({"branch": "You cannot use this expense branch."})
    if (
        expense.payment_method_id
        and expense.payment_method.business_id != business.id
    ):
        raise ValidationError({
            "payment_method": "Select a payment medium from this business."
        })
    if persisted is None and expense.payment_method_id is None:
        raise ValidationError({
            "payment_method": "Payment Medium / Paid Via is required."
        })

    is_cash = bool(
        expense.payment_method_id and expense.payment_method.kind == "cash"
    )
    shift_id = getattr(requested_shift, "pk", requested_shift)
    if not is_cash:
        requested_shift = None
        shift_id = None
        drawer_requested = False
    elif drawer_requested and shift_id is None:
        raise ValidationError({
            "paid_from_drawer": (
                "No matching open cash drawer is available for this branch."
            )
        })
    elif not drawer_requested:
        requested_shift = None
        shift_id = None

    old_shift_id = persisted.shift_id if persisted else None
    locked_shift_ids = sorted({pk for pk in (old_shift_id, shift_id) if pk})
    from apps.registers.models import Shift

    locked_shifts = {
        shift.pk: shift
        for shift in (
            Shift.objects.select_for_update()
            .for_business(business)
            .filter(pk__in=locked_shift_ids)
            .select_related("register", "branch", "cashier")
            .order_by("pk")
        )
    }
    if shift_id:
        requested_shift = locked_shifts.get(shift_id)
        if requested_shift is None:
            raise ValidationError({
                "shift": "Select a register shift from this business."
            })
        if requested_shift.branch_id != expense.branch_id:
            raise ValidationError({
                "shift": "The register shift must match the expense branch."
            })
        if historical_correction:
            if requested_shift.status not in (
                Shift.Status.OPEN,
                Shift.Status.CLOSED,
                Shift.Status.APPROVED,
            ):
                raise ValidationError({"shift": "Select a valid register shift."})
        elif requested_shift.pk != getattr(
            matching_open_drawer_shift(
                business=business,
                user=user,
                branch=expense.branch,
                membership=membership,
            ),
            "pk",
            None,
        ) and not (
            persisted
            and requested_shift.pk == persisted.shift_id
            and requested_shift.status in (
                Shift.Status.CLOSED,
                Shift.Status.APPROVED,
            )
        ):
            raise ValidationError({
                "paid_from_drawer": "Only your current open drawer can be used."
            })

    _require_closed_shift_reconciliation_approval(
        old_expense=persisted or expense,
        new_expense=expense,
        old_shift_id=old_shift_id,
        new_shift_id=shift_id,
        old_status=persisted.status if persisted else expense.status,
        new_status=expense.status,
        locked_shifts=locked_shifts,
        user=user,
        business=business,
        membership=membership,
        request=request,
    )
    expense.shift = requested_shift
    # ``created_by`` is nullable because the originating user may later be
    # deleted, and historical rows can legitimately contain NULL.  The
    # service assigns the current actor on new rows while preserving that
    # valid legacy state when an existing expense is edited.
    expense.full_clean(exclude={"created_by"})
    expense.save()

    snapshot_changes = _refresh_closed_shift_snapshots(locked_shifts.values())
    if historical_correction or snapshot_changes:
        audit.log(
            "expense.drawer_corrected",
            business=business,
            user=user,
            request=request,
            module="expenses",
            obj=expense,
            description=(
                f"Expense {expense.expense_number} payment/drawer link corrected."
            ),
            old_values={
                "payment_method_id": persisted.payment_method_id if persisted else None,
                "shift_id": old_shift_id,
            },
            new_values={
                "payment_method_id": expense.payment_method_id,
                "shift_id": expense.shift_id,
                "shift_snapshots": snapshot_changes,
            },
        )
    return expense


@transaction.atomic
def set_expense_status(
    *, expense, status, user, membership=None, request=None, approved_by=None
):
    """Change an expense status and refresh any closed drawer snapshot."""
    business = expense.business
    require_actor_access(
        user,
        business,
        "expenses",
        permission_code="expenses.approve",
        action=AccessAction.WRITE,
        membership=membership,
        request=request,
    )
    locked = (
        Expense.objects.select_for_update()
        .for_business(business)
        .select_related("payment_method")
        .get(pk=expense.pk)
    )
    old_status = locked.status
    snapshot_changes = []
    locked_shifts = {}
    if locked.shift_id:
        from apps.registers.models import Shift

        shift = (
            Shift.objects.select_for_update()
            .for_business(business)
            .filter(pk=locked.shift_id)
            .first()
        )
        if shift is not None:
            locked_shifts[shift.pk] = shift

    _require_closed_shift_reconciliation_approval(
        old_expense=locked,
        new_expense=locked,
        old_shift_id=locked.shift_id,
        new_shift_id=locked.shift_id,
        old_status=old_status,
        new_status=status,
        locked_shifts=locked_shifts,
        user=user,
        business=business,
        membership=membership,
        request=request,
    )
    locked.status = status
    if approved_by is not None:
        locked.approved_by = approved_by
        update_fields = ["status", "approved_by", "updated_at"]
    else:
        update_fields = ["status", "updated_at"]
    locked.save(update_fields=update_fields)

    if locked_shifts:
        snapshot_changes = _refresh_closed_shift_snapshots(
            locked_shifts.values()
        )
    audit.log(
        "expense.status_changed",
        business=business,
        user=user,
        request=request,
        module="expenses",
        obj=locked,
        description=(
            f"Expense {locked.expense_number} status changed from "
            f"{old_status} to {status}."
        ),
        old_values={"status": old_status},
        new_values={"status": status, "shift_snapshots": snapshot_changes},
    )
    return locked


class RecurringExpenseGenerationError(Exception):
    pass


class RecurringExpenseRangeError(RecurringExpenseGenerationError):
    pass


@dataclass(frozen=True)
class RecurringGenerationResult:
    created: int
    existing: int


def _month_bounds(target_date):
    if isinstance(target_date, datetime):
        target_date = target_date.date()
    month_start = target_date.replace(day=1)
    last_day = calendar.monthrange(target_date.year, target_date.month)[1]
    return month_start, target_date.replace(day=last_day)


def _as_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise RecurringExpenseRangeError(
            "Choose a valid expense report date range."
        ) from exc


def _generation_branch(business, template):
    """Resolve a fixed expense branch without choosing an arbitrary branch."""
    if template.branch_id:
        if template.branch.business_id != business.id:
            raise RecurringExpenseGenerationError(
                f"Fixed expense '{template.name}' has an invalid branch."
            )
        return template.branch

    # A sole active branch is reliable evidence for a legacy nullable template.
    branches = list(
        Branch.objects.for_business(business).filter(is_active=True).order_by("id")[:2]
    )
    if len(branches) == 1:
        return branches[0]
    if not branches:
        raise RecurringExpenseGenerationError(
            "The business has no active branch for recurring expenses."
        )
    raise RecurringExpenseGenerationError(
        f"Choose a branch for legacy fixed expense '{template.name}' before "
        "generating monthly expenses."
    )


@transaction.atomic
def ensure_recurring_expenses_for_month(business, target_date):
    """Create each applicable template's expense once for the target month."""
    decision = evaluate_public_access(
        business, "expenses", action=AccessAction.WRITE
    )
    if not decision.allowed:
        if decision.denial.code == DenialCode.BUSINESS_INACTIVE:
            message = "Recurring expense generation requires an active business."
        elif decision.denial.code == DenialCode.MODULE_DISABLED:
            message = "The current plan does not include expenses."
        else:
            message = decision.denial.message
        raise RecurringExpenseGenerationError(message)

    month_start, month_end = _month_bounds(target_date)
    templates = (
        RecurringExpenseTemplate.objects.select_for_update()
        .for_business(business)
        .filter(is_active=True, start_date__lte=month_end)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=month_start))
        .select_related("category", "branch")
        .order_by("id")
    )
    month_last_day = month_end.day
    created_count = 0
    existing_count = 0

    for template in templates:
        branch = _generation_branch(business, template)
        due_date = month_start.replace(day=min(template.due_day, month_last_day))
        expense, created = Expense.objects.get_or_create(
            business=business,
            recurring_template=template,
            generated_for_month=month_start,
            defaults={
                "expense_number": (
                    f"REC-{month_start:%Y%m}-{template.pk}"
                ),
                "expense_date": due_date,
                "branch": branch,
                "category": template.category,
                "payee": template.name,
                "amount": template.default_amount,
                "description": template.notes,
                "status": Expense.Status.APPROVED,
            },
        )
        if created:
            created_count += 1
            audit.log(
                "recurring_expense.generated",
                business=business,
                module="expenses",
                obj=expense,
                description=(
                    f"Generated {expense.expense_number} from recurring "
                    f"template '{template.name}'."
                ),
            )
        else:
            existing_count += 1

    return RecurringGenerationResult(
        created=created_count,
        existing=existing_count,
    )


def ensure_recurring_expenses_for_range(
    business,
    date_from=None,
    date_to=None,
    *,
    max_months=120,
):
    """Ensure fixed expenses for each requested month, with a hard range cap."""
    today = business_localdate(business)
    start = _as_date(date_from) or today.replace(day=1)
    end = _as_date(date_to)
    if end is None:
        end = today if start <= today else start
    if end < start:
        return RecurringGenerationResult(created=0, existing=0)

    month_count = (end.year - start.year) * 12 + end.month - start.month + 1
    if month_count > max_months:
        raise RecurringExpenseRangeError(
            "Choose an expense report date range of 10 years or less."
        )

    month_start = start.replace(day=1)
    _, final_month_end = _month_bounds(end)
    if not (
        RecurringExpenseTemplate.objects.for_business(business)
        .filter(is_active=True, start_date__lte=final_month_end)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=month_start))
        .exists()
    ):
        return RecurringGenerationResult(created=0, existing=0)

    created_count = 0
    existing_count = 0
    current = month_start
    for _ in range(month_count):
        result = ensure_recurring_expenses_for_month(business, current)
        created_count += result.created
        existing_count += result.existing
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)

    return RecurringGenerationResult(
        created=created_count,
        existing=existing_count,
    )
