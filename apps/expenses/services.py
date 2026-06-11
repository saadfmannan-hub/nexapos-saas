"""Expense defaults and numbering."""
from .models import Expense, ExpenseCategory

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
