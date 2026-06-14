"""SaaS subscription plans, subscriptions, coupons and payments."""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


class Plan(TimeStampedModel):
    """A sellable subscription plan. Managed by the platform admin —
    nothing here is hardcoded; seed data only provides editable examples."""

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    name = models.CharField(max_length=80, unique=True)
    description = models.TextField(blank=True)
    monthly_price = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    annual_price = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    currency_code = models.CharField(max_length=10, default="USD")
    trial_days = models.PositiveIntegerField(default=14)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    # Limits — 0 means unlimited
    max_branches = models.PositiveIntegerField(default=1)
    max_users = models.PositiveIntegerField(default=2)
    max_warehouses = models.PositiveIntegerField(default=1)
    max_products = models.PositiveIntegerField(default=0)
    max_customers = models.PositiveIntegerField(default=0)
    max_monthly_invoices = models.PositiveIntegerField(default=0)
    storage_limit_mb = models.PositiveIntegerField(default=0)

    # Feature switches
    feature_purchases = models.BooleanField(default=True)
    feature_expenses = models.BooleanField(default=True)
    feature_returns = models.BooleanField(default=True)
    feature_transfers = models.BooleanField(default=False)
    feature_advanced_reports = models.BooleanField(default=False)
    feature_customer_credit = models.BooleanField(default=True)
    feature_api_access = models.BooleanField(default=False)
    feature_white_label = models.BooleanField(default=False)
    feature_custom_roles = models.BooleanField(default=False)
    feature_audit_logs = models.BooleanField(default=True)
    support_level = models.CharField(max_length=40, default="standard")

    class Meta:
        ordering = ["sort_order", "monthly_price"]

    def __str__(self):
        return self.name


class Subscription(TimeStampedModel):
    class Status(models.TextChoices):
        TRIAL = "trial", _("Trial")
        ACTIVE = "active", _("Active")
        GRACE = "grace", _("Grace Period")
        PAST_DUE = "past_due", _("Past Due")
        SUSPENDED = "suspended", _("Suspended")
        CANCELLED = "cancelled", _("Cancelled")
        EXPIRED = "expired", _("Expired")

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    business = models.OneToOneField(
        "tenants.Business", on_delete=models.CASCADE, related_name="subscription"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.TRIAL)
    billing_cycle = models.CharField(
        max_length=10,
        choices=[("monthly", _("Monthly")), ("annual", _("Annual"))],
        default="monthly",
    )
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    grace_days = models.PositiveIntegerField(default=7)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    def __str__(self):
        return f"{self.business} — {self.plan} ({self.get_status_display()})"

    # ----- status helpers -------------------------------------------------
    @property
    def effective_status(self) -> str:
        """Status after applying time-based transitions (trial expiry,
        period expiry, grace). Does not write to the database."""
        now = timezone.now()
        if self.status == self.Status.TRIAL:
            if self.trial_ends_at and self.trial_ends_at < now:
                return self.Status.EXPIRED
            return self.Status.TRIAL
        if self.status == self.Status.ACTIVE:
            if self.current_period_end and self.current_period_end < now:
                grace_end = self.current_period_end + timezone.timedelta(days=self.grace_days)
                if now <= grace_end:
                    return self.Status.GRACE
                return self.Status.EXPIRED
            return self.Status.ACTIVE
        return self.status

    EXPIRING_SOON_DAYS = 7

    @property
    def period_ends_on(self):
        """The date this subscription lapses (trial end or period end)."""
        if self.status == self.Status.TRIAL:
            return self.trial_ends_at
        return self.current_period_end

    @property
    def days_until_expiry(self):
        end = self.period_ends_on
        if not end:
            return None
        return (end - timezone.now()).days

    @property
    def is_expiring_soon(self) -> bool:
        """Operational but lapses within EXPIRING_SOON_DAYS."""
        if self.effective_status not in (self.Status.TRIAL, self.Status.ACTIVE):
            return False
        days = self.days_until_expiry
        return days is not None and 0 <= days <= self.EXPIRING_SOON_DAYS

    @property
    def display_status(self) -> str:
        """Status used for badges across the platform admin. Layers
        manual suspension and 'expiring soon' on top of effective_status:
        suspended > expiring_soon > effective_status."""
        if self.business_id and not self.business.is_active:
            return "suspended"
        if self.is_expiring_soon:
            return "expiring_soon"
        return self.effective_status

    @property
    def is_operational(self) -> bool:
        """Can the business create new transactions?"""
        if self.business_id and not self.business.is_active:
            return False
        return self.effective_status in (
            self.Status.TRIAL, self.Status.ACTIVE, self.Status.GRACE,
        )

    @property
    def is_read_only(self) -> bool:
        return not self.is_operational


class Coupon(TimeStampedModel):
    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    code = models.CharField(max_length=40, unique=True)
    description = models.CharField(max_length=200, blank=True)
    percent_off = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    amount_off = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    extra_trial_days = models.PositiveIntegerField(default=0)
    max_redemptions = models.PositiveIntegerField(default=0, help_text="0 = unlimited")
    redemption_count = models.PositiveIntegerField(default=0)
    valid_until = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.code


class SubscriptionPayment(TimeStampedModel):
    """Platform revenue record (manual activation / bank transfer /
    future gateway). Gateway-agnostic by design."""

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="payments"
    )
    amount = models.DecimalField(max_digits=14, decimal_places=3)
    currency_code = models.CharField(max_length=10, default="USD")
    method = models.CharField(
        max_length=20,
        choices=[
            ("manual", _("Manual")),
            ("bank_transfer", _("Bank Transfer")),
            ("gateway", _("Online Gateway")),
        ],
        default="manual",
    )
    reference = models.CharField(max_length=120, blank=True)
    coupon = models.ForeignKey(Coupon, on_delete=models.SET_NULL, null=True, blank=True)
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.subscription.business} {self.amount} {self.currency_code}"
