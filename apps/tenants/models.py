"""Tenant (Business) model and per-business settings."""
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
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
    suspended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="businesses_suspended",
    )
    suspension_reason = models.CharField(max_length=255, blank=True)
    reactivated_at = models.DateTimeField(null=True, blank=True)
    reactivated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="businesses_reactivated",
    )
    onboarding_completed = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = "businesses"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def currency_display(self):
        """Explicit symbol > registry symbol > raw code."""
        if self.currency_symbol:
            return self.currency_symbol
        from apps.core.currencies import symbol_for

        return symbol_for(self.currency_code, fallback=self.currency_code)


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
    vat_enabled = models.BooleanField(default=False)
    vat_percentage = models.DecimalField(max_digits=6, decimal_places=3, default=0)
    vat_registration_number = models.CharField(max_length=60, blank=True)
    show_vat_on_invoice_receipt = models.BooleanField(default=True)
    prices_include_tax = models.BooleanField(default=False)
    show_tax_on_receipt = models.BooleanField(default=True)

    # Customer More Options labels. Blank labels stay hidden on customer screens.
    more_option_label_1 = models.CharField(max_length=80, blank=True)
    more_option_label_2 = models.CharField(max_length=80, blank=True)
    more_option_label_3 = models.CharField(max_length=80, blank=True)
    more_option_label_4 = models.CharField(max_length=80, blank=True)
    more_option_label_5 = models.CharField(max_length=80, blank=True)
    more_option_label_6 = models.CharField(max_length=80, blank=True)
    more_option_label_7 = models.CharField(max_length=80, blank=True)
    more_option_label_8 = models.CharField(max_length=80, blank=True)
    more_option_label_9 = models.CharField(max_length=80, blank=True)
    more_option_label_10 = models.CharField(max_length=80, blank=True)
    more_option_label_11 = models.CharField(max_length=80, blank=True)
    more_option_label_12 = models.CharField(max_length=80, blank=True)
    more_option_label_13 = models.CharField(max_length=80, blank=True)
    more_option_label_14 = models.CharField(max_length=80, blank=True)
    more_option_label_15 = models.CharField(max_length=80, blank=True)

    # Invoice / receipt
    invoice_prefix = models.CharField(
        max_length=15, default="INV",
        help_text="Used for all new invoice/receipt numbers, e.g. INV → INV-2026-000001.",
    )
    invoice_include_branch_code = models.BooleanField(
        default=False,
        help_text="Append the branch code to invoice numbers "
                  "(e.g. INV-HK-2026-000001) and number each branch separately.",
    )
    invoice_footer = models.TextField(blank=True)
    receipt_footer = models.TextField(blank=True, default="Thank you for your business!")
    terms_and_conditions = models.TextField(blank=True)
    show_logo_on_invoice = models.BooleanField(default=True)

    # Stock / sales policies
    negative_stock_policy = models.CharField(
        max_length=10, choices=NegativeStock.choices, default=NegativeStock.BLOCK
    )
    shared_fabric_warehouse = models.ForeignKey(
        "branches.Warehouse",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="shared_fabric_for_settings",
        verbose_name="Shared Fabric Location",
        help_text="Workshop warehouse used for shared fabric stock.",
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

    def clean(self):
        super().clean()
        warehouse = self.shared_fabric_warehouse
        if warehouse is None:
            return
        from apps.branches.models import Branch

        if (
            warehouse.business_id != self.business_id
            or not warehouse.is_active
            or warehouse.branch_id is None
            or warehouse.branch.business_id != self.business_id
            or not warehouse.branch.is_active
            or warehouse.branch.usage_type != Branch.UsageType.WORKSHOP_STOCK
        ):
            raise ValidationError({
                "shared_fabric_warehouse": (
                    "Select a Workshop / Stock Location from this business."
                )
            })

    @property
    def effective_vat_rate(self):
        return self.vat_percentage if self.vat_enabled else 0

    @property
    def vat_number(self):
        return self.vat_registration_number or self.business.tax_registration_number

    @property
    def more_option_labels(self):
        labels = []
        for index in range(1, 16):
            label = getattr(self, f"more_option_label_{index}", "").strip()
            if label:
                labels.append({"key": str(index), "label": label})
        return labels
