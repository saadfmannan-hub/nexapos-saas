from django.contrib import admin

from .models import (
    WmsSalary,
    WmsSalaryDay,
    WmsSalaryLocationSnapshot,
    WmsSalaryPieceLine,
)


class ReadOnlySalaryAdminMixin:
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)


@admin.register(WmsSalary)
class WmsSalaryAdmin(ReadOnlySalaryAdminMixin, admin.ModelAdmin):
    list_display = (
        "employee_name_snapshot",
        "salary_year",
        "salary_month",
        "status",
        "gross_salary",
        "business",
    )
    list_filter = ("status", "salary_year", "salary_month")
    search_fields = ("employee_code_snapshot", "employee_name_snapshot")


@admin.register(WmsSalaryLocationSnapshot)
class WmsSalaryLocationSnapshotAdmin(
    ReadOnlySalaryAdminMixin,
    admin.ModelAdmin,
):
    list_display = ("salary", "location_name_snapshot", "business")


@admin.register(WmsSalaryDay)
class WmsSalaryDayAdmin(ReadOnlySalaryAdminMixin, admin.ModelAdmin):
    list_display = (
        "salary",
        "salary_date",
        "eligible_quantity",
        "daily_amount",
        "business",
    )


@admin.register(WmsSalaryPieceLine)
class WmsSalaryPieceLineAdmin(ReadOnlySalaryAdminMixin, admin.ModelAdmin):
    list_display = (
        "salary_day",
        "category_name_snapshot",
        "quantity",
        "applied_rate",
        "line_amount",
        "business",
    )
