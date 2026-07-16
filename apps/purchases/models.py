"""Purchases: orders, receiving, payments and purchase returns."""
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


class Purchase(TenantModel):
    class Status(models.TextChoices):
        ORDER = "order", _("Purchase Order")
        PARTIAL = "partially_received", _("Partially Received")
        RECEIVED = "received", _("Fully Received")
        CANCELLED = "cancelled", _("Cancelled")

    purchase_number = models.CharField(max_length=30)
    supplier = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.PROTECT, related_name="purchases"
    )
    branch = models.ForeignKey("branches.Branch", on_delete=models.PROTECT)
    warehouse = models.ForeignKey("branches.Warehouse", on_delete=models.PROTECT)
    supplier_invoice_number = models.CharField(max_length=60, blank=True)
    purchase_date = models.DateField(db_index=True)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ORDER)

    subtotal = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    tax_amount = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    shipping_cost = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    other_charges = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    amount_paid = models.DecimalField(max_digits=14, decimal_places=3, default=0)

    attachment = models.FileField(upload_to="purchases/%Y/%m/", blank=True, null=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="purchases_created",
    )

    class Meta:
        ordering = ["-purchase_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "purchase_number"],
                name="uniq_purchase_number_per_business",
            )
        ]

    def __str__(self):
        return self.purchase_number

    @property
    def outstanding(self):
        """Legacy settled-only balance retained for backward compatibility."""
        return self.total - self.amount_paid

    @property
    def cheques_pending(self):
        annotated = getattr(self, "_cheques_pending", None)
        if annotated is not None:
            return annotated
        return (
            self.payments.filter(
                business=self.business,
                method="cheque",
                cheque_status="pending",
            ).aggregate(total=models.Sum("amount"))["total"]
            or Decimal("0")
        )

    @property
    def remaining_balance(self):
        return max(
            Decimal("0"), self.total - self.amount_paid - self.cheques_pending,
        )

    @property
    def supplier_balance(self):
        return max(Decimal("0"), self.total - self.amount_paid)


class PurchaseItem(TenantModel):
    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(
        "catalog.Product", on_delete=models.PROTECT, related_name="purchase_items"
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.PROTECT, null=True, blank=True
    )
    product_name = models.CharField(max_length=240)
    quantity_ordered = models.DecimalField(max_digits=14, decimal_places=3)
    quantity_received = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=3)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    tax_amount = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    line_total = models.DecimalField(max_digits=14, decimal_places=3, default=0)

    def __str__(self):
        return f"{self.product_name} x {self.quantity_ordered}"

    @property
    def quantity_pending(self):
        return self.quantity_ordered - self.quantity_received


class PurchaseReturn(TenantModel):
    return_number = models.CharField(max_length=30)
    purchase = models.ForeignKey(
        Purchase, on_delete=models.PROTECT, related_name="purchase_returns"
    )
    supplier = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.PROTECT, related_name="purchase_returns"
    )
    warehouse = models.ForeignKey("branches.Warehouse", on_delete=models.PROTECT)
    reason = models.CharField(max_length=255, blank=True)
    total = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "return_number"],
                name="uniq_purchase_return_per_business",
            )
        ]

    def __str__(self):
        return self.return_number


class PurchaseReturnItem(TenantModel):
    purchase_return = models.ForeignKey(
        PurchaseReturn, on_delete=models.CASCADE, related_name="items"
    )
    purchase_item = models.ForeignKey(
        PurchaseItem, on_delete=models.PROTECT, related_name="return_items"
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    line_total = models.DecimalField(max_digits=14, decimal_places=3, default=0)

    def __str__(self):
        return f"Return {self.quantity} of {self.purchase_item.product_name}"
