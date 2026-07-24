from django.contrib import admin

from .models import WmsAttendance


@admin.register(WmsAttendance)
class WmsAttendanceAdmin(admin.ModelAdmin):
    list_display = (
        "attendance_date",
        "employee",
        "location",
        "morning_status",
        "evening_status",
        "worked_minutes",
        "correction_flag",
    )
    list_filter = (
        "attendance_date",
        "morning_status",
        "evening_status",
        "correction_flag",
    )
    search_fields = (
        "employee__employee_code",
        "employee__full_name",
        "location__branch__name",
    )
