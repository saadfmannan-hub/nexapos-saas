"""Immutable audit trail.

Records are append-only: save() refuses updates and there is no
business-facing delete path. Platform admins can read everything;
business admins can only read their own tenant's log.
"""
from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    business = models.ForeignKey(
        "tenants.Business", on_delete=models.CASCADE, related_name="audit_logs",
        null=True, blank=True,  # platform-level events have no business
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    action = models.CharField(max_length=60, db_index=True)
    module = models.CharField(max_length=40, blank=True)
    object_type = models.CharField(max_length=60, blank=True)
    object_id = models.CharField(max_length=60, blank=True)
    description = models.CharField(max_length=400, blank=True)
    old_values = models.JSONField(null=True, blank=True)
    new_values = models.JSONField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["business", "-created_at"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("Audit logs are immutable.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.action} by {self.user} at {self.created_at:%Y-%m-%d %H:%M}"
