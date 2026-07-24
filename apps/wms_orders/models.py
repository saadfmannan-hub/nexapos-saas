"""Tenant-scoped workshop orders and append-only workflow history."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower

from apps.wms_core.models import ValidatedTenantModel, WmsLocation


def normalize_order_reference(value):
    return (value or "").strip().upper()


class WmsWorkshopOrder(ValidatedTenantModel):
    class Status(models.TextChoices):
        IN_PROCESS = "IN_PROCESS", "In Process"
        FINISHED_READY = "FINISHED_READY", "Finished / Ready"

    location = models.ForeignKey(
        WmsLocation,
        on_delete=models.PROTECT,
        related_name="workshop_orders",
    )
    order_reference = models.CharField(max_length=80)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.IN_PROCESS,
    )
    received_date = models.DateField()
    finished_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_workshop_orders_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_workshop_orders_updated",
    )

    class Meta:
        ordering = ["-received_date", "order_reference"]
        constraints = [
            models.UniqueConstraint(
                Lower("order_reference"),
                "business",
                name="uniq_wms_order_reference_ci_business",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=("IN_PROCESS", "FINISHED_READY")
                ),
                name="valid_wms_workshop_order_status",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status="IN_PROCESS",
                        finished_date__isnull=True,
                    )
                    | models.Q(
                        status="FINISHED_READY",
                        finished_date__isnull=False,
                    )
                ),
                name="valid_wms_order_finished_date",
            ),
        ]
        indexes = [
            models.Index(
                fields=["business", "status", "received_date"],
                name="wms_order_status_date_idx",
            ),
            models.Index(
                fields=["business", "location", "received_date"],
                name="wms_order_location_date_idx",
            ),
            models.Index(
                fields=["business", "order_reference"],
                name="wms_order_reference_idx",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        self.order_reference = normalize_order_reference(self.order_reference)
        self.notes = (self.notes or "").strip()
        if not self.order_reference:
            errors["order_reference"] = "Order reference is required."
        elif "\n" in self.order_reference or "\r" in self.order_reference:
            errors["order_reference"] = "Enter one order reference at a time."

        if (
            self.business_id
            and self.location_id
            and self.location.business_id != self.business_id
        ):
            errors["location"] = (
                "The WMS location must belong to the same business."
            )

        original = None
        if self.pk:
            original = (
                type(self)
                .objects.filter(pk=self.pk)
                .values(
                    "location_id",
                    "order_reference",
                    "received_date",
                    "status",
                    "finished_date",
                )
                .first()
            )
        if original is None:
            if self.status != self.Status.IN_PROCESS:
                errors["status"] = "New orders must start In Process."
            if (
                self.location_id
                and (
                    not self.location.is_active
                    or not self.location.branch.is_active
                )
            ):
                errors["location"] = (
                    "Inactive WMS locations cannot receive new orders."
                )
        else:
            if (
                original["location_id"] != self.location_id
                or normalize_order_reference(original["order_reference"])
                != self.order_reference
                or original["received_date"] != self.received_date
            ):
                errors["order_reference"] = (
                    "Order reference, location, and received date cannot "
                    "change after creation."
                )
            if (
                original["status"] == self.Status.FINISHED_READY
                and (
                    self.status != self.Status.FINISHED_READY
                    or original["finished_date"] != self.finished_date
                )
            ):
                errors["status"] = (
                    "Finished orders cannot return to In Process or change "
                    "their finished date."
                )
            if (
                original["status"] == self.Status.IN_PROCESS
                and self.status
                not in (self.Status.IN_PROCESS, self.Status.FINISHED_READY)
            ):
                errors["status"] = "Only the approved finish transition is allowed."

        if (
            self.status == self.Status.IN_PROCESS
            and self.finished_date is not None
        ):
            errors["finished_date"] = (
                "In Process orders cannot have a finished date."
            )
        if (
            self.status == self.Status.FINISHED_READY
            and self.finished_date is None
        ):
            errors["finished_date"] = (
                "Finished / Ready orders require a finished date."
            )
        if (
            self.finished_date is not None
            and self.received_date is not None
            and self.finished_date < self.received_date
        ):
            errors["finished_date"] = (
                "Finished date cannot be before received date."
            )
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.order_reference


class WmsWorkshopOrderStatusHistory(ValidatedTenantModel):
    order = models.ForeignKey(
        WmsWorkshopOrder,
        on_delete=models.PROTECT,
        related_name="status_history",
    )
    previous_status = models.CharField(
        max_length=20,
        choices=WmsWorkshopOrder.Status.choices,
        blank=True,
    )
    new_status = models.CharField(
        max_length=20,
        choices=WmsWorkshopOrder.Status.choices,
    )
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_order_status_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["changed_at", "pk"]
        indexes = [
            models.Index(
                fields=["business", "order", "changed_at"],
                name="wms_order_history_date_idx",
            ),
        ]

    def clean(self):
        super().clean()
        self.reason = (self.reason or "").strip()
        if (
            self.business_id
            and self.order_id
            and self.order.business_id != self.business_id
        ):
            raise ValidationError(
                {"order": "The workshop order belongs to another business."}
            )
        transition = (self.previous_status, self.new_status)
        allowed = (
            ("", WmsWorkshopOrder.Status.IN_PROCESS),
            (
                WmsWorkshopOrder.Status.IN_PROCESS,
                WmsWorkshopOrder.Status.FINISHED_READY,
            ),
        )
        if transition not in allowed:
            raise ValidationError(
                {"new_status": "This order-status transition is not allowed."}
            )

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Order status history is append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Order status history cannot be deleted.")

    def __str__(self):
        return (
            f"{self.order.order_reference}: "
            f"{self.previous_status or 'Created'} → {self.new_status}"
        )
