from django.urls import path

from . import views

urlpatterns = [
    path("", views.report_index, name="report_index"),
    path(
        "daily-production/",
        views.daily_production_report,
        name="report_daily_production",
    ),
    path(
        "daily-production/export.xlsx",
        views.daily_production_export,
        name="report_daily_production_export",
    ),
    path(
        "monthly-production/",
        views.monthly_production_report,
        name="report_monthly_production",
    ),
    path(
        "monthly-production/export.xlsx",
        views.monthly_production_export,
        name="report_monthly_production_export",
    ),
    path(
        "attendance-summary/",
        views.attendance_summary_report,
        name="report_attendance_summary",
    ),
    path(
        "attendance-summary/export.xlsx",
        views.attendance_summary_export,
        name="report_attendance_summary_export",
    ),
    path(
        "individual-attendance/",
        views.individual_attendance_report,
        name="report_individual_attendance",
    ),
    path(
        "individual-attendance/export.xlsx",
        views.individual_attendance_export,
        name="report_individual_attendance_export",
    ),
    path(
        "salary/",
        views.salary_report,
        name="report_salary",
    ),
    path(
        "salary/export.xlsx",
        views.salary_export,
        name="report_salary_export",
    ),
]
