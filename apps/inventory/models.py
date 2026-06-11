"""Inventory: ledger-based stock with transfers, adjustments and counts.

Stock is NEVER edited as a bare number. Every change flows through
apps.inventory.services which writes a StockMovement row and updates
the cached StockLevel inside one database transaction.
"""
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


class StockLevel(TenantModel):
    """Cached current quantity per (warehouse, product[, variant])."""

    warehouse = models.ForeignKey(
        "branches.Warehouse", on_delete=models.CASCADE, related_name="stock_levels"
    )
    product = models.ForeignKey(
        "catalog.Product", on_delete=models.CASCADE, related_name="stock_levels"
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.CASCADE,
        related_name="stock_levels", null=True, blank=True,
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["warehouse", "product", "variant"],
                condition=models.Q(variant__isnull=False),
                name="uniq_stocklevel_variant",
            ),
            models.UniqueConstraint(
                fields=["warehouse", "product"],
                condition=models.Q(variant__isnull=True),
                name="uniq_stocklevel_product",
            ),
        ]
        indexes = [models.Index(fields=["business", "product"])]

    def __str__(self):
        item = self.variant or self.product
        return f"{item} @ {self.warehouse}: {self.quantity}"


class StockMovement(TenantModel):
    """Immutable stock ledger entry. Positive quantity = stock in."""

    class Type(models.TextChoices):
        OPENING = "opening", _("Opening stock")
        PURCHASE = "purchase", _("Purchase receipt")
        SALE = "sale", _("Sale")
        SALE_RETURN = "sale_return", _("Sale return")
        PURCHASE_RETURN = "purchase_return", _("Purchase return")
        TRANSFER_OUT = "transfer_out", _("Transfer out")
        TRANSFER_IN = "transfer_in", _("Transfer in")
        ADJUST_IN = "adjust_in", _("Adjustment increase")
        ADJUST_OUT = "adjust_out", _("Adjustment decrease")
        DAMAGE = "damage", _("Damaged stock")
        WASTAGE = "wastage", _("Wastage")
        INTERNAL = "internal", _("Internal use")
        COUNT = "count", _("Stock count correction")

    warehouse = models.ForeignKey(
        "branches.Warehouse", on_delete=models.PROTECT, related_name="stock_movements"
    )
    product = models.ForeignKey(
        "catalog.Product", on_delete=models.PROTECT, related_name="stock_movements"
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.PROTECT,
        related_name="stock_movements", null=True, blank=True,
    )
    movement_type = models.CharField(max_length=20, choices=Type.choices, db_index=True)
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    balance_after = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    reference_type = models.CharField(max_length=40, blank=True)
    reference_id = models.CharField(max_length=60, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["business", "product", "-created_at"]),
            models.Index(fields=["business", "warehouse", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.get_movement_type_display()} {self.quantity} of {self.product}"


class StockTransfer(TenantModel):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        APPROVED = "approved", _("Approved")
        DISPATCHED = "dispatched", _("Dispatched")
        RECEIVED = "received", _("Received")
        REJECTED = "rejected", _("Rejected")
        CANCELLED = "cancelled", _("Cancelled")

    transfer_number = models.CharField(max_length=30)
    from_warehouse = models.ForeignKey(
        "branches.Warehouse", on_delete=models.PROTECT, related_name="transfers_out"
    )
    to_warehouse = models.ForeignKey(
        "branches.Warehouse", on_delete=models.PROTECT, related_name="transfers_in"
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="transfers_requested",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="transfers_approved",
    )
    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="transfers_dispatched",
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="transfers_received",
    )
    dispatched_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "transfer_number"],
                name="uniq_transfer_number_per_business",
            )
        ]

    def __str__(self):
        return self.transfer_number


class StockTransferItem(TenantModel):
    transfer = models.ForeignKey(StockTransfer, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT)
    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.PROTECT, null=True, blank=True
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=3, default=0)

    def __str__(self):
        return f"{self.product} x {self.quantity}"


class StockAdjustment(TenantModel):
    class Reason(models.TextChoices):
        DAMAGE = "damage", _("Damage")
        EXPIRY = "expiry", _("Expiry")
        LOSS = "loss", _("Loss")
        THEFT = "theft", _("Theft")
        WASTAGE = "wastage", _("Wastage")
        INTERNAL = "internal", _("Internal use")
        SAMPLE = "sample", _("Sample")
        COUNT = "count", _("Count correction")
        DATA = "data", _("Data correction")
        OTHER = "other", _("Other")

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending approval")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")

    adjustment_number = models.CharField(max_length=30)
    warehouse = models.ForeignKey(
        "branches.Warehouse", on_delete=models.PROTECT, related_name="adjustments"
    )
    reason = models.CharField(max_length=12, choices=Reason.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.APPROVED)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="adjustments_created",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="adjustments_approved",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "adjustment_number"],
                name="uniq_adjustment_number_per_business",
            )
        ]

    def __str__(self):
        return self.adjustment_number


class StockAdjustmentItem(TenantModel):
    adjustment = models.ForeignKey(
        StockAdjustment, on_delete=models.CASCADE, related_name="items"
    )
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT)
    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.PROTECT, null=True, blank=True
    )
    previous_quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    change = models.DecimalField(
        max_digits=14, decimal_places=3,
        help_text="Positive = increase, negative = decrease",
    )

    def __str__(self):
        return f"{self.product} {self.change:+}"


class StockCount(TenantModel):
    class Status(models.TextChoices):
        OPEN = "open", _("Counting")
        REVIEW = "review", _("Review")
        APPROVED = "approved", _("Approved")
        CANCELLED = "cancelled", _("Cancelled")

    count_number = models.CharField(max_length=30)
    warehouse = models.ForeignKey(
        "branches.Warehouse", on_delete=models.PROTECT, related_name="stock_counts"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="counts_created",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="counts_approved",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "count_number"],
                name="uniq_count_number_per_business",
            )
        ]

    def __str__(self):
        return self.count_number


class StockCountItem(TenantModel):
    count = models.ForeignKey(StockCount, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT)
    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.PROTECT, null=True, blank=True
    )
    expected_quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    counted_quantity = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)

    @property
    def variance(self):
        if self.counted_quantity is None:
            return None
        return self.counted_quantity - self.expected_quantity

    def __str__(self):
        return f"{self.product}: expected {self.expected_quantity}"
