"""Sales, payments, invoice sequences, held sales and returns.

Financial records are immutable snapshots: item prices, costs and tax
are frozen at completion time. Completed sales are never edited —
corrections happen through void, return or credit note.
"""
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


class PaymentMethod(TenantModel):
    class Kind(models.TextChoices):
        CASH = "cash", _("Cash")
        CARD = "card", _("Card")
        BANK = "bank", _("Bank Transfer")
        ONLINE = "online", _("Online Payment")
        CUSTOMER_CREDIT = "customer_credit", _("Customer Credit (pay later)")
        STORE_CREDIT = "store_credit", _("Store Credit")
        OTHER = "other", _("Other")

    name = models.CharField(max_length=60)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.CASH)
    is_active = models.BooleanField(default=True)
    is_system = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "name"], name="uniq_payment_method_per_business"
            )
        ]

    def __str__(self):
        return self.name


class InvoiceSequence(TenantModel):
    """Concurrency-safe invoice numbering per branch and year."""

    branch = models.ForeignKey("branches.Branch", on_delete=models.CASCADE)
    year = models.PositiveIntegerField()
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["business", "branch", "year"],
                name="uniq_invoice_sequence",
            )
        ]


class Sale(TenantModel):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        COMPLETED = "completed", _("Completed")
        PARTIAL = "partially_paid", _("Partially Paid")
        CREDIT = "credit", _("Credit")
        VOIDED = "voided", _("Voided")
        PART_RETURNED = "partially_returned", _("Partially Returned")
        RETURNED = "fully_returned", _("Fully Returned")

    branch = models.ForeignKey("branches.Branch", on_delete=models.PROTECT, related_name="sales")
    warehouse = models.ForeignKey(
        "branches.Warehouse", on_delete=models.PROTECT, related_name="sales"
    )
    register = models.ForeignKey(
        "registers.CashRegister", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sales",
    )
    shift = models.ForeignKey(
        "registers.Shift", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sales",
    )
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sales_as_cashier"
    )
    salesperson = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sales_as_salesperson",
    )
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="sales"
    )

    invoice_number = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    sale_date = models.DateTimeField(db_index=True)

    # Delivery / order fulfilment (optional — used by made-to-order and
    # delivery businesses; empty for ordinary counter sales)
    class DeliveryStatus(models.TextChoices):
        PENDING = "pending", _("Pending")
        IN_PRODUCTION = "in_production", _("In Production")
        READY = "ready", _("Ready")
        DELIVERED = "delivered", _("Delivered")
        CANCELLED = "cancelled", _("Cancelled")

    delivery_date = models.DateField(null=True, blank=True, db_index=True)
    delivery_status = models.CharField(
        max_length=15, choices=DeliveryStatus.choices, blank=True, default=""
    )

    # Money snapshot (tax-exclusive subtotal)
    subtotal = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    tax_amount = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    rounding = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    amount_paid = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    change_due = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    total_cost = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    gross_profit = models.DecimalField(max_digits=14, decimal_places=3, default=0)

    notes = models.TextField(blank=True)
    reprint_count = models.PositiveIntegerField(default=0)

    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sales_voided",
    )
    void_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-sale_date"]
        indexes = [
            models.Index(fields=["business", "-sale_date"]),
            models.Index(fields=["business", "invoice_number"]),
            models.Index(fields=["business", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "invoice_number"],
                condition=~models.Q(invoice_number=""),
                name="uniq_invoice_number_per_business",
            )
        ]

    def __str__(self):
        return self.invoice_number or f"Sale #{self.pk}"

    @property
    def balance(self):
        return self.total - self.amount_paid

    @property
    def is_delivery_overdue(self):
        from django.utils import timezone

        return bool(
            self.delivery_date
            and self.delivery_date < timezone.localdate()
            and self.delivery_status not in (
                self.DeliveryStatus.DELIVERED, self.DeliveryStatus.CANCELLED,
            )
        )

    @property
    def payment_state(self):
        """Unpaid / Partially Paid / Paid / Overpaid — derived, never stored."""
        if self.amount_paid <= 0 and self.total > 0:
            return "Unpaid"
        if self.amount_paid > self.total:
            return "Overpaid"
        if self.amount_paid < self.total:
            return "Partially Paid"
        return "Paid"

    @property
    def is_finalized(self):
        return self.status not in (self.Status.DRAFT,)


class SaleItem(TenantModel):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(
        "catalog.Product", on_delete=models.PROTECT, related_name="sale_items"
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.PROTECT, null=True, blank=True,
        related_name="sale_items",
    )
    # Immutable snapshots
    product_name = models.CharField(max_length=240)
    sku = models.CharField(max_length=60, blank=True)
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_price = models.DecimalField(max_digits=14, decimal_places=3)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    tax_rate = models.DecimalField(max_digits=6, decimal_places=3, default=0)
    tax_amount = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    line_total = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    gross_profit = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    returned_quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0)

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"

    @property
    def returnable_quantity(self):
        return self.quantity - self.returned_quantity


class SalePayment(TenantModel):
    """One payment against a sale. A sale may collect several payments on
    different dates (multi-payment ledger); each keeps its own date,
    method, reference and receiver."""

    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="payments")
    method = models.ForeignKey(PaymentMethod, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=14, decimal_places=3)
    payment_date = models.DateField(null=True, blank=True, db_index=True)
    reference = models.CharField(max_length=120, blank=True)
    notes = models.CharField(max_length=300, blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sale_payments_received",
    )
    shift = models.ForeignKey(
        "registers.Shift", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sale_payments",
    )

    class Meta:
        ordering = ["payment_date", "created_at"]

    def __str__(self):
        return f"{self.method} {self.amount}"


class HeldSale(TenantModel):
    """A parked cart, resumable from the POS screen."""

    branch = models.ForeignKey("branches.Branch", on_delete=models.CASCADE)
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    label = models.CharField(max_length=80, blank=True)
    cart = models.JSONField(default=dict)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.label or f"Held #{self.pk}"


class SaleReturn(TenantModel):
    class RefundMethod(models.TextChoices):
        CASH = "cash", _("Cash")
        CARD = "card", _("Card")
        BANK = "bank", _("Bank transfer")
        STORE_CREDIT = "store_credit", _("Store credit")
        CUSTOMER_ACCOUNT = "customer_account", _("Reduce customer balance")

    return_number = models.CharField(max_length=40)
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="returns")
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="returns"
    )
    branch = models.ForeignKey("branches.Branch", on_delete=models.PROTECT)
    warehouse = models.ForeignKey("branches.Warehouse", on_delete=models.PROTECT)
    reason = models.CharField(max_length=255, blank=True)
    refund_method = models.CharField(max_length=20, choices=RefundMethod.choices)
    refund_amount = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    restock = models.BooleanField(default=True)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True
    )
    shift = models.ForeignKey(
        "registers.Shift", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="returns",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "return_number"],
                name="uniq_return_number_per_business",
            )
        ]

    def __str__(self):
        return self.return_number


class SaleReturnItem(TenantModel):
    sale_return = models.ForeignKey(SaleReturn, on_delete=models.CASCADE, related_name="items")
    sale_item = models.ForeignKey(SaleItem, on_delete=models.PROTECT, related_name="return_items")
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    refund_per_unit = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    line_refund = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    restocked = models.BooleanField(default=True)

    def __str__(self):
        return f"Return {self.quantity} of {self.sale_item.product_name}"
