"""Suppliers and supplier payments."""
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

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
    class Method(models.TextChoices):
        CASH = "cash", _("Cash")
        BANK = "bank", _("Bank Transfer")
        CARD = "card", _("Card")
        CHEQUE = "cheque", _("Cheque")

    class ChequeStatus(models.TextChoices):
        PENDING = "pending", _("Pending")
        CLEARED = "cleared", _("Cleared")
        BOUNCED = "bounced", _("Bounced")
        CANCELLED = "cancelled", _("Cancelled")

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
    method = models.CharField(max_length=20, choices=Method.choices, blank=True)
    cheque_number = models.CharField(max_length=100, blank=True)
    bank_name = models.CharField(max_length=120, blank=True)
    due_date = models.DateField(null=True, blank=True, db_index=True)
    cheque_status = models.CharField(
        max_length=12, choices=ChequeStatus.choices, blank=True,
    )
    cleared_at = models.DateTimeField(null=True, blank=True)
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
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="supplier_payment_amount_positive",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(method="cheque")
                    | models.Q(cheque_status="")
                ),
                name="supplier_payment_status_cheque_only",
            ),
        ]

    def __str__(self):
        return f"{self.payment_number} — {self.supplier} {self.amount}"

    @property
    def is_cheque(self):
        return self.method == self.Method.CHEQUE

    @property
    def method_label(self):
        if self.method:
            return self.get_method_display()
        if self.payment_method_id:
            return self.payment_method.name
        return _("Payment")
