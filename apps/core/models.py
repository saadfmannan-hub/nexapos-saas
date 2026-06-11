"""Reusable tenant-aware base models and managers.

Every business-scoped table in the platform inherits TenantModel, which
provides:
  * a mandatory `business` foreign key,
  * a non-guessable UUID `public_id` used in URLs,
  * created/updated timestamps,
  * a manager whose `for_business()` is the single funnel for tenant
    filtering.
"""
import uuid

from django.db import models


class TenantQuerySet(models.QuerySet):
    def for_business(self, business):
        if business is None:
            return self.none()
        return self.filter(business=business)


class TenantManager(models.Manager.from_queryset(TenantQuerySet)):
    pass


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TenantModel(TimeStampedModel):
    """Abstract base for every business-owned record."""

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    business = models.ForeignKey(
        "tenants.Business",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        db_index=True,
    )

    objects = TenantManager()

    class Meta:
        abstract = True
