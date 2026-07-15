"""Expense categories and expenses with an approval workflow."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


class ExpenseCategory(TenantModel):
    name = models.CharField(max_length=100)
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE, null=True, blank=True, related_name="children"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "expense categories"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "name", "parent"],
                name="uniq_expense_category_per_business",
            )
        ]

    def __str__(self):
        return self.name


class RecurringExpenseTemplate(TenantModel):
    name = models.CharField(max_length=160)
    category = models.ForeignKey(
        ExpenseCategory,
        on_delete=models.PROTECT,
        related_name="recurring_templates",
    )
    default_amount = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(0)],
    )
    due_day = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(31)]
    )
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(default_amount__gte=0),
                name="recurring_expense_amount_gte_0",
            ),
            models.CheckConstraint(
                condition=models.Q(due_day__gte=1, due_day__lte=31),
                name="recurring_expense_due_day_range",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(end_date__isnull=True)
                    | models.Q(end_date__gte=models.F("start_date"))
                ),
                name="recurring_expense_end_after_start",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.category_id and self.business_id:
            if self.category.business_id != self.business_id:
                errors["category"] = "Select a category from this business."
        if self.end_date and self.start_date and self.end_date < self.start_date:
            errors["end_date"] = "End date cannot be before start date."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.name


class Expense(TenantModel):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SUBMITTED = "submitted", _("Submitted")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")
        PAID = "paid", _("Paid")
        CANCELLED = "cancelled", _("Cancelled")

    expense_number = models.CharField(max_length=30)
    expense_date = models.DateField(db_index=True)
    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.PROTECT, related_name="expenses"
    )
    category = models.ForeignKey(
        ExpenseCategory, on_delete=models.PROTECT, related_name="expenses"
    )
    payee = models.CharField(max_length=160, blank=True)
    supplier = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="expenses",
    )
    amount = models.DecimalField(max_digits=14, decimal_places=3)
    tax_amount = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    payment_method = models.ForeignKey(
        "sales.PaymentMethod", on_delete=models.PROTECT, null=True, blank=True
    )
    reference = models.CharField(max_length=120, blank=True)
    description = models.TextField(blank=True)
    attachment = models.FileField(upload_to="expenses/%Y/%m/", blank=True, null=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.APPROVED)
    shift = models.ForeignKey(
        "registers.Shift", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="expenses",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="expenses_created",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="expenses_approved",
    )
    recurring_template = models.ForeignKey(
        RecurringExpenseTemplate,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="generated_expenses",
    )
    generated_for_month = models.DateField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-expense_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "expense_number"],
                name="uniq_expense_number_per_business",
            ),
            models.UniqueConstraint(
                fields=["recurring_template", "generated_for_month"],
                condition=(
                    models.Q(recurring_template__isnull=False)
                    & models.Q(generated_for_month__isnull=False)
                ),
                name="uniq_recurring_expense_per_month",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        recurring_template__isnull=True,
                        generated_for_month__isnull=True,
                    )
                    | models.Q(
                        recurring_template__isnull=False,
                        generated_for_month__isnull=False,
                    )
                ),
                name="expense_recurring_provenance_pair",
            ),
        ]

    @property
    def source(self):
        return "recurring" if self.recurring_template_id else "variable"

    @property
    def source_display(self):
        return "Fixed" if self.recurring_template_id else "Current"

    def clean(self):
        super().clean()
        errors = {}
        if self.business_id and self.branch_id:
            if self.branch.business_id != self.business_id:
                errors["branch"] = "Select a branch from this business."
        if self.business_id and self.category_id:
            if self.category.business_id != self.business_id:
                errors["category"] = "Select a category from this business."
        if self.recurring_template_id and self.business_id:
            if self.recurring_template.business_id != self.business_id:
                errors["recurring_template"] = (
                    "Select a recurring template from this business."
                )
        if bool(self.recurring_template_id) != bool(self.generated_for_month):
            errors["generated_for_month"] = (
                "Recurring template and generation month must be set together."
            )
        if self.generated_for_month and self.generated_for_month.day != 1:
            errors["generated_for_month"] = (
                "Generation month must use the first day of the month."
            )
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.expense_number} — {self.amount}"
