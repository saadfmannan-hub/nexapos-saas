from django.contrib import admin

from .models import WmsProductionEntry, WmsProductionEntryLine


class WmsProductionEntryLineInline(admin.TabularInline):
    model = WmsProductionEntryLine
    extra = 0
    can_delete = False
    readonly_fields = (
        "order",
        "assignment",
        "category",
        "category_name_snapshot",
        "category_code_snapshot",
        "quantity",
        "is_removed",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(WmsProductionEntry)
class WmsProductionEntryAdmin(admin.ModelAdmin):
    list_display = (
        "production_date",
        "employee",
        "location",
        "daily_total_pieces",
        "is_corrected",
    )
    list_filter = ("production_date", "is_corrected")
    search_fields = (
        "employee__employee_code",
        "employee__full_name",
        "location__branch__name",
    )
    inlines = (WmsProductionEntryLineInline,)

    readonly_fields = (
        "business",
        "location",
        "employee",
        "production_date",
        "daily_total_pieces",
        "notes",
        "is_corrected",
        "correction_reason",
        "created_by",
        "updated_by",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WmsProductionEntryLine)
class WmsProductionEntryLineAdmin(admin.ModelAdmin):
    list_display = (
        "entry",
        "order",
        "category_name_snapshot",
        "quantity",
        "is_removed",
    )
    list_filter = ("is_removed",)
    search_fields = (
        "entry__employee__employee_code",
        "entry__employee__full_name",
        "category_name_snapshot",
        "order__order_reference",
    )

    readonly_fields = (
        "business",
        "entry",
        "order",
        "assignment",
        "category",
        "category_name_snapshot",
        "category_code_snapshot",
        "quantity",
        "is_removed",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in {"GET", "HEAD"}

    def has_delete_permission(self, request, obj=None):
        return False
