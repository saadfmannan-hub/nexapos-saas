"""Expense categories and expenses with an approval workflow."""
from django.conf import settings
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

    class Meta:
        ordering = ["-expense_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "expense_number"],
                name="uniq_expense_number_per_business",
            )
        ]

    def __str__(self):
        return f"{self.expense_number} — {self.amount}"
