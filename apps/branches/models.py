"""Branches and warehouses."""
from django.conf import settings
from django.db import models

from apps.core.models import TenantModel


class Branch(TenantModel):
    class UsageType(models.TextChoices):
        SALES_BRANCH = "sales_branch", "Sales Branch"
        WORKSHOP_STOCK = "workshop_stock", "Workshop / Stock Location"

    name = models.CharField(max_length=120)
    code = models.CharField(max_length=20)
    usage_type = models.CharField(
        max_length=20,
        choices=UsageType.choices,
        default=UsageType.SALES_BRANCH,
    )
    address = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="managed_branches",
    )
    invoice_prefix = models.CharField(max_length=10, blank=True)
    receipt_footer = models.TextField(blank=True)
    is_head_office = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "branches"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "code"], name="uniq_branch_code_per_business"
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"


class Warehouse(TenantModel):
    name = models.CharField(max_length=120)
    code = models.CharField(max_length=20)
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name="warehouses",
        null=True, blank=True, help_text="Empty = central warehouse",
    )
    address = models.CharField(max_length=255, blank=True)
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="managed_warehouses",
    )
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "code"], name="uniq_warehouse_code_per_business"
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"
