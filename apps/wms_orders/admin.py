from django.contrib import admin

from .models import WmsWorkshopOrder, WmsWorkshopOrderStatusHistory


class WmsWorkshopOrderStatusHistoryInline(admin.TabularInline):
    model = WmsWorkshopOrderStatusHistory
    extra = 0
    can_delete = False
    readonly_fields = (
        "previous_status",
        "new_status",
        "changed_by",
        "changed_at",
        "reason",
    )

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(WmsWorkshopOrder)
class WmsWorkshopOrderAdmin(admin.ModelAdmin):
    list_display = (
        "order_reference",
        "location",
        "status",
        "received_date",
        "finished_date",
    )
    list_filter = ("status", "received_date", "finished_date")
    search_fields = ("order_reference", "location__branch__name")
    readonly_fields = (
        "business",
        "location",
        "order_reference",
        "status",
        "received_date",
        "finished_date",
        "notes",
        "created_by",
        "updated_by",
        "created_at",
        "updated_at",
    )
    inlines = (WmsWorkshopOrderStatusHistoryInline,)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WmsWorkshopOrderStatusHistory)
class WmsWorkshopOrderStatusHistoryAdmin(admin.ModelAdmin):
    list_display = (
        "order",
        "previous_status",
        "new_status",
        "changed_by",
        "changed_at",
    )
    readonly_fields = (
        "business",
        "order",
        "previous_status",
        "new_status",
        "changed_by",
        "changed_at",
        "reason",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in {"GET", "HEAD"}

    def has_delete_permission(self, request, obj=None):
        return False
