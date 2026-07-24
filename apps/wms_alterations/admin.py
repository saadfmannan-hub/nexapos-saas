from django.contrib import admin

from .models import WmsAlteration


@admin.register(WmsAlteration)
class WmsAlterationAdmin(admin.ModelAdmin):
    list_display = (
        "alteration_date",
        "original_order_reference",
        "location",
        "reason",
        "assigned_employee",
        "status",
    )
    list_filter = ("alteration_date", "reason", "mistake_by", "status")
    search_fields = (
        "original_order_reference",
        "alteration_reference",
        "assigned_employee__employee_code",
        "assigned_employee__full_name",
        "location__branch__name",
    )
    readonly_fields = (
        "business",
        "location",
        "original_order_reference",
        "alteration_reference",
        "reason",
        "mistake_by",
        "mistake_by_employee",
        "assigned_employee",
        "alteration_date",
        "status",
        "notes",
        "is_corrected",
        "correction_reason",
        "completed_at",
        "completed_by",
        "created_by",
        "updated_by",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
