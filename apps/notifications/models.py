"""In-app notifications."""
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


class Notification(TenantModel):
    class Severity(models.TextChoices):
        INFO = "info", _("Information")
        WARNING = "warning", _("Warning")
        HIGH = "high", _("High")
        CRITICAL = "critical", _("Critical")

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    title = models.CharField(max_length=160)
    body = models.CharField(max_length=500, blank=True)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.INFO)
    category = models.CharField(max_length=40, blank=True, db_index=True)
    link = models.CharField(max_length=300, blank=True)
    is_read = models.BooleanField(default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["business", "recipient", "is_read"])]

    def __str__(self):
        return self.title
