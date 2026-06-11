"""Tenant (Business) model and per-business settings."""
import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


class Business(TimeStampedModel):
    """One registered business — the tenant root of all data isolation."""

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    name = models.CharField(max_length=150)
    legal_name = models.CharField(max_length=200, blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_businesses"
    )
    logo = models.ImageField(upload_to="business_logos/", blank=True, null=True)

    # Contact / locale
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    whatsapp = models.CharField(max_length=30, blank=True)
    website = models.URLField(blank=True)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=100, blank=True)
    timezone = models.CharField(max_length=60, default="UTC")

    # Registration / tax identifiers
    commercial_registration = models.CharField(max_length=60, blank=True)
    tax_registration_number = models.CharField(max_length=60, blank=True)

    # Money / formats
    currency_code = models.CharField(max_length=10, default="USD")
    currency_symbol = models.CharField(max_length=10, blank=True)
    currency_precision = models.PositiveSmallIntegerField(default=2)
    date_format = models.CharField(max_length=20, default="Y-m-d")

    business_category = models.CharField(max_length=80, blank=True)
    default_language = models.CharField(max_length=10, default="en")
    financial_year_start_month = models.PositiveSmallIntegerField(default=1)

    is_active = models.BooleanField(default=True)
    suspended_at = models.DateTimeField(null=True, blank=True)
    suspension_reason = models.CharField(max_length=255, blank=True)
    onboarding_completed = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = "businesses"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def currency_display(self):
        return self.currency_symbol or self.currency_code


class BusinessSettings(TimeStampedModel):
    """Operational settings, one row per business."""

    class NegativeStock(models.TextChoices):
        BLOCK = "block", _("Block sales when stock is insufficient")
        WARN = "warn", _("Warn but allow")
        ALLOW = "allow", _("Allow silently")

    business = models.OneToOneField(
        Business, on_delete=models.CASCADE, related_name="settings"
    )

    # Tax
    prices_include_tax = models.BooleanField(default=False)
    show_tax_on_receipt = models.BooleanField(default=True)

    # Invoice / receipt
    invoice_prefix = models.CharField(max_length=10, default="INV")
    invoice_footer = models.TextField(blank=True)
    receipt_footer = models.TextField(blank=True, default="Thank you for your business!")
    terms_and_conditions = models.TextField(blank=True)
    show_logo_on_invoice = models.BooleanField(default=True)

    # Stock / sales policies
    negative_stock_policy = models.CharField(
        max_length=10, choices=NegativeStock.choices, default=NegativeStock.BLOCK
    )
    allow_sale_without_shift = models.BooleanField(default=False)
    max_discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=100)
    require_customer_for_credit = models.BooleanField(default=True)
    return_window_days = models.PositiveIntegerField(default=0, help_text="0 = unlimited")

    # Approvals
    expense_approval_threshold = models.DecimalField(
        max_digits=14, decimal_places=3, default=0,
        help_text="Expenses above this amount require approval. 0 disables.",
    )
    adjustment_requires_approval = models.BooleanField(default=False)

    # Rounding
    price_rounding = models.CharField(
        max_length=10,
        choices=[("none", _("No rounding")), ("nearest", _("Round total to precision"))],
        default="nearest",
    )

    # Notifications
    notify_low_stock = models.BooleanField(default=True)
    notify_credit_overdue = models.BooleanField(default=True)
    notify_support_access = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "business settings"

    def __str__(self):
        return f"Settings for {self.business}"
