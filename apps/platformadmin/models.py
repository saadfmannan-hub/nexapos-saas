"""Platform-level models: support access grants and announcements."""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel


class SupportAccessGrant(TimeStampedModel):
    """Time-limited, audited access for platform staff into one tenant.

    Platform admins never see business data without an active grant.
    """

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    business = models.ForeignKey(
        "tenants.Business", on_delete=models.CASCADE, related_name="support_grants"
    )
    granted_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="support_grants"
    )
    reason = models.CharField(max_length=300)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="support_grants_revoked",
    )

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_active(self):
        return self.revoked_at is None and self.expires_at > timezone.now()

    def __str__(self):
        return f"Support access to {self.business} for {self.granted_to}"


class PlatformConfig(TimeStampedModel):
    """Singleton platform-wide settings (row pk=1)."""

    class ExpiryMode(models.TextChoices):
        READ_ONLY = "read_only", "Read-only (view data, block new transactions)"
        SUSPEND = "suspend", "Full suspension (block all access)"

    expiry_mode = models.CharField(
        max_length=12, choices=ExpiryMode.choices, default=ExpiryMode.READ_ONLY,
        help_text="What happens to a business when its subscription expires.",
    )

    class Meta:
        verbose_name = "platform configuration"
        verbose_name_plural = "platform configuration"

    def __str__(self):
        return "Platform configuration"

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Announcement(TimeStampedModel):
    """Platform-wide announcement shown to business users."""

    title = models.CharField(max_length=160)
    body = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-starts_at"]

    def __str__(self):
        return self.title
