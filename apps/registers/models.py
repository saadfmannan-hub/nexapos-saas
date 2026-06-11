"""Cash registers and cashier shifts."""
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


class CashRegister(TenantModel):
    name = models.CharField(max_length=80)
    code = models.CharField(max_length=20)
    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.CASCADE, related_name="registers"
    )
    receipt_printer = models.CharField(
        max_length=20,
        choices=[("80mm", "80mm thermal"), ("58mm", "58mm thermal"), ("a4", "A4")],
        default="80mm",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "code"], name="uniq_register_code_per_business"
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.branch.name})"


class Shift(TenantModel):
    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        CLOSED = "closed", _("Closed")
        APPROVED = "approved", _("Approved")

    register = models.ForeignKey(CashRegister, on_delete=models.PROTECT, related_name="shifts")
    branch = models.ForeignKey("branches.Branch", on_delete=models.PROTECT, related_name="shifts")
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="shifts"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)

    opened_at = models.DateTimeField()
    opening_cash = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    opening_notes = models.CharField(max_length=300, blank=True)

    closed_at = models.DateTimeField(null=True, blank=True)
    expected_cash = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    actual_cash = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    difference = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    denominations = models.JSONField(null=True, blank=True)
    closing_notes = models.CharField(max_length=300, blank=True)

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="shifts_approved",
    )
    reopened_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-opened_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["register"],
                condition=models.Q(status="open"),
                name="uniq_open_shift_per_register",
            )
        ]

    def __str__(self):
        return f"Shift {self.pk} — {self.register} ({self.get_status_display()})"
