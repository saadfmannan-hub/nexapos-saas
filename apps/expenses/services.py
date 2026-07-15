"""Expense defaults, numbering, and recurring monthly generation."""
import calendar
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.db.models import Q

from apps.audit import services as audit
from apps.branches.models import Branch
from apps.subscriptions import services as subscriptions

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


class RecurringExpenseGenerationError(Exception):
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


def _generation_branch(business):
    branches = Branch.objects.for_business(business).filter(is_active=True)
    return (
        branches.filter(is_head_office=True).order_by("id").first()
        or branches.order_by("id").first()
    )


@transaction.atomic
def ensure_recurring_expenses_for_month(business, target_date):
    """Create each applicable template's expense once for the target month."""
    if business is None or not business.is_active:
        raise RecurringExpenseGenerationError("An active business is required.")
    try:
        subscriptions.require_operational(business)
    except subscriptions.SubscriptionInactive as exc:
        raise RecurringExpenseGenerationError(str(exc)) from exc
    if not subscriptions.has_feature(business, "expenses"):
        raise RecurringExpenseGenerationError(
            "The business plan does not include expenses."
        )

    month_start, month_end = _month_bounds(target_date)
    branch = _generation_branch(business)
    if branch is None:
        raise RecurringExpenseGenerationError(
            "The business has no active branch for recurring expenses."
        )

    templates = (
        RecurringExpenseTemplate.objects.select_for_update()
        .for_business(business)
        .filter(is_active=True, start_date__lte=month_end)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=month_start))
        .select_related("category")
        .order_by("id")
    )
    month_last_day = month_end.day
    created_count = 0
    existing_count = 0

    for template in templates:
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
