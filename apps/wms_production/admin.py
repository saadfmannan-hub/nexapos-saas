from django.contrib import admin

from .models import WmsProductionEntry, WmsProductionEntryLine


class WmsProductionEntryLineInline(admin.TabularInline):
    model = WmsProductionEntryLine
    extra = 0
    can_delete = False
    readonly_fields = (
        "assignment",
        "category",
        "category_name_snapshot",
        "category_code_snapshot",
        "quantity",
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

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WmsProductionEntryLine)
class WmsProductionEntryLineAdmin(admin.ModelAdmin):
    list_display = (
        "entry",
        "category_name_snapshot",
        "quantity",
    )
    search_fields = (
        "entry__employee__employee_code",
        "entry__employee__full_name",
        "category_name_snapshot",
    )

    def has_delete_permission(self, request, obj=None):
        return False
