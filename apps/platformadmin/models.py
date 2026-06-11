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
