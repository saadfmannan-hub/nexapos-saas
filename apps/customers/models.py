"""Customers, groups, payments (receivable collections) and store credit."""
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


class CustomerGroup(TenantModel):
    name = models.CharField(max_length=80)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "name"], name="uniq_customer_group_per_business"
            )
        ]

    def __str__(self):
        return self.name


class Customer(TenantModel):
    home_branch = models.ForeignKey(
        "branches.Branch",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="customers",
        help_text="Owning operational branch. Unassigned legacy customers are owner-only.",
    )
    code = models.CharField(max_length=30)
    full_name = models.CharField(max_length=160)
    mobile = models.CharField(max_length=30, blank=True)
    whatsapp = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    group = models.ForeignKey(
        CustomerGroup, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="customers",
    )
    tax_number = models.CharField(max_length=60, blank=True)

    credit_limit = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    opening_balance = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    balance = models.DecimalField(
        max_digits=14, decimal_places=3, default=0,
        help_text="Amount the customer owes (receivable). Updated transactionally.",
    )
    store_credit = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    loyalty_points = models.DecimalField(max_digits=14, decimal_places=3, default=0)

    more_options = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)
    is_walk_in = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["full_name"]
        indexes = [
            models.Index(fields=["business", "full_name"]),
            models.Index(fields=["business", "mobile"]),
            models.Index(fields=["business", "home_branch", "full_name"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "home_branch", "code"],
                condition=models.Q(home_branch__isnull=False),
                name="uniq_customer_code_per_branch",
            ),
            models.UniqueConstraint(
                fields=["business", "code"],
                condition=models.Q(home_branch__isnull=True),
                name="uniq_unassigned_customer_code",
            ),
            models.UniqueConstraint(
                fields=["business", "home_branch"],
                condition=models.Q(is_walk_in=True, home_branch__isnull=False),
                name="uniq_walk_in_customer_per_branch",
            ),
        ]

    def __str__(self):
        return self.full_name

    def save(self, *args, **kwargs):
        if self.home_branch_id is None and self.business_id:
            from apps.branches.models import Branch

            branches = Branch.objects.filter(
                business_id=self.business_id,
                is_active=True,
                usage_type=Branch.UsageType.SALES_BRANCH,
            ).order_by("id")[:2]
            if len(branches) == 1:
                self.home_branch = branches[0]
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.is_walk_in:
            raise ValueError("The walk-in customer cannot be deleted.")
        super().delete(*args, **kwargs)


class CustomerPayment(TenantModel):
    """A collection against the customer's outstanding balance."""

    class Kind(models.TextChoices):
        COLLECTION = "collection", _("Payment received")
        REFUND_TO_CREDIT = "refund_credit", _("Refund issued as store credit")
        STORE_CREDIT_USED = "credit_used", _("Store credit used")

    receipt_number = models.CharField(max_length=30)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="payments")
    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.PROTECT, null=True, blank=True
    )
    kind = models.CharField(max_length=15, choices=Kind.choices, default=Kind.COLLECTION)
    amount = models.DecimalField(max_digits=14, decimal_places=3)
    payment_method = models.ForeignKey(
        "sales.PaymentMethod", on_delete=models.PROTECT, null=True, blank=True
    )
    reference = models.CharField(max_length=120, blank=True)
    notes = models.CharField(max_length=300, blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    shift = models.ForeignKey(
        "registers.Shift", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="customer_payments",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "receipt_number"],
                name="uniq_customer_receipt_per_business",
            )
        ]

    def __str__(self):
        return f"{self.receipt_number} — {self.customer} {self.amount}"
