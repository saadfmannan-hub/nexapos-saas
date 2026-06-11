"""Suppliers and supplier payments."""
from django.conf import settings
from django.db import models

from apps.core.models import TenantModel


class Supplier(TenantModel):
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=160)
    contact_person = models.CharField(max_length=120, blank=True)
    mobile = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.CharField(max_length=255, blank=True)
    tax_number = models.CharField(max_length=60, blank=True)
    opening_balance = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    balance = models.DecimalField(
        max_digits=14, decimal_places=3, default=0,
        help_text="Amount owed to the supplier (payable).",
    )
    payment_terms = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "code"], name="uniq_supplier_code_per_business"
            )
        ]

    def __str__(self):
        return self.name


class SupplierPayment(TenantModel):
    payment_number = models.CharField(max_length=30)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="payments")
    purchase = models.ForeignKey(
        "purchases.Purchase", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payments",
    )
    amount = models.DecimalField(max_digits=14, decimal_places=3)
    payment_method = models.ForeignKey(
        "sales.PaymentMethod", on_delete=models.PROTECT, null=True, blank=True
    )
    reference = models.CharField(max_length=120, blank=True)
    notes = models.CharField(max_length=300, blank=True)
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "payment_number"],
                name="uniq_supplier_payment_per_business",
            )
        ]

    def __str__(self):
        return f"{self.payment_number} — {self.supplier} {self.amount}"
