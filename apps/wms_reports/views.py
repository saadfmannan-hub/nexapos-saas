"""Read-only WMS report pages and permission-matched Excel exports."""

from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import render

from apps.core.date_ranges import business_localdate
from apps.subscriptions.access import AccessAction
from apps.wms_core.access import wms_permission_required

from . import selectors, services
from .forms import (
    AttendanceSummaryReportForm,
    DailyProductionReportForm,
    IndividualAttendanceReportForm,
    MonthlyProductionReportForm,
    SalaryReportForm,
)


def _reject_invalid_object_ids(form):
    """Turn scoped ModelChoice misses into the project-standard not-found."""

    errors = form.errors.as_data()
    for field_name, field in form.fields.items():
        if not hasattr(field, "queryset") or not form.data.get(field_name):
            continue
        if any(error.code == "invalid_choice" for error in errors.get(field_name, ())):
            raise Http404


def _cleaned_or_defaults(form, defaults):
    if not form.is_bound:
        return defaults
    if form.is_valid():
        return form.cleaned_data
    _reject_invalid_object_ids(form)
    return None


def _querystring(request):
    return request.GET.urlencode()


def _base_context(request, *, form, report, export_route):
    return {
        "form": form,
        "report": report,
        "export_route": export_route,
        "querystring": _querystring(request),
        "can_export_reports": request.wms_user_access.has_perm("wms.reports.export"),
        "active_nav": "wms",
        "wms_active_nav": "reports",
    }


def _invalid_export():
    return HttpResponseBadRequest("Correct the report filters before exporting.")


@wms_permission_required("wms.reports.view", action=AccessAction.READ)
def report_index(request):
    return render(
        request,
        "wms/reports/index.html",
        {
            "active_nav": "wms",
            "wms_active_nav": "reports",
        },
    )


def _daily_result(request):
    form = DailyProductionReportForm(
        request.business,
        request.wms_user_access,
        request.GET or None,
    )
    cleaned = _cleaned_or_defaults(
        form,
        {
            "report_date": business_localdate(request.business),
            "location": None,
            "employee": None,
            "category": None,
        },
    )
    if cleaned is None:
        return form, None, None
    result = selectors.daily_production(
        request.wms_user_access,
        **cleaned,
    )
    category_names = [item["name"] for item in result["categories"]]
    report = {
        **result,
        "title": "Branch-wise Daily Finished Pcs Report",
        "sheet_name": "Daily Finished Pcs",
        "filename": f"wms-daily-finished-pcs-{cleaned['report_date']}",
        "period_label": cleaned["report_date"].strftime("%B %d, %Y"),
        "columns": [
            "Employee",
            "Employee Code",
            "Location / Branch",
            *category_names,
            "Daily Total",
        ],
        "export_rows": [
            [
                row["employee_name"],
                row["employee_code"],
                row["location_name"],
                *row["values"],
                row["total"],
            ]
            for row in result["rows"]
        ],
        "export_totals": [
            "Category totals",
            "",
            "",
            *result["category_totals"],
            result["grand_total"],
        ],
    }
    metadata = [
        ("Period", report["period_label"]),
        (
            "Location",
            cleaned["location"].branch.name
            if cleaned["location"] is not None
            else "All permitted locations",
        ),
        (
            "Employee",
            cleaned["employee"].full_name
            if cleaned["employee"] is not None
            else "All permitted employees",
        ),
        (
            "Category",
            cleaned["category"].name
            if cleaned["category"] is not None
            else "All production categories",
        ),
    ]
    return form, report, metadata


@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.production.view", action=AccessAction.READ)
def daily_production_report(request):
    form, report, _metadata = _daily_result(request)
    return render(
        request,
        "wms/reports/daily_production.html",
        _base_context(
            request,
            form=form,
            report=report,
            export_route="wms:report_daily_production_export",
        ),
    )


@wms_permission_required("wms.reports.export", action=AccessAction.READ)
@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.production.view", action=AccessAction.READ)
def daily_production_export(request):
    _form, report, metadata = _daily_result(request)
    if report is None:
        return _invalid_export()
    return services.export_xlsx(
        report=report,
        business=request.business,
        metadata=metadata,
    )


def _monthly_production_result(request):
    form = MonthlyProductionReportForm(
        request.business,
        request.wms_user_access,
        request.GET or None,
    )
    cleaned = _cleaned_or_defaults(form, None)
    if cleaned is None:
        return form, None, None
    result = selectors.monthly_employee_production(
        request.wms_user_access,
        **cleaned,
    )
    employee = cleaned["employee"]
    category_names = [item["name"] for item in result["categories"]]
    report = {
        **result,
        "title": "Monthly Production per Employee Report",
        "sheet_name": "Monthly Production",
        "filename": (
            f"wms-monthly-production-{employee.employee_code}-{cleaned['report_month']:%Y-%m}"
        ),
        "period_label": cleaned["report_month"].strftime("%B %Y"),
        "employee": employee,
        "location": cleaned["location"],
        "columns": ["Date", *category_names, "Daily Total"],
        "export_rows": [[row["date"], *row["values"], row["total"]] for row in result["rows"]],
        "export_totals": [
            "Monthly totals",
            *result["category_totals"],
            result["grand_total"],
        ],
        "column_formats": {0: "yyyy-mm-dd"},
    }
    metadata = [
        ("Period", report["period_label"]),
        ("Employee", f"{employee.employee_code} — {employee.full_name}"),
        (
            "Location",
            cleaned["location"].branch.name
            if cleaned["location"] is not None
            else "All permitted locations",
        ),
    ]
    return form, report, metadata


@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.production.view", action=AccessAction.READ)
def monthly_production_report(request):
    form, report, _metadata = _monthly_production_result(request)
    return render(
        request,
        "wms/reports/monthly_production.html",
        _base_context(
            request,
            form=form,
            report=report,
            export_route="wms:report_monthly_production_export",
        ),
    )


@wms_permission_required("wms.reports.export", action=AccessAction.READ)
@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.production.view", action=AccessAction.READ)
def monthly_production_export(request):
    _form, report, metadata = _monthly_production_result(request)
    if report is None:
        return _invalid_export()
    return services.export_xlsx(
        report=report,
        business=request.business,
        metadata=metadata,
    )


def _attendance_summary_result(request):
    form = AttendanceSummaryReportForm(
        request.business,
        request.wms_user_access,
        request.GET or None,
    )
    today = business_localdate(request.business)
    cleaned = _cleaned_or_defaults(
        form,
        {
            "date_from": today.replace(day=1),
            "date_to": today,
            "location": None,
            "employee_status": "",
        },
    )
    if cleaned is None:
        return form, None, None
    records = selectors.attendance_summary_records(
        request.wms_user_access,
        **cleaned,
    )
    result = services.attendance_report(records)
    report = {
        **result,
        "title": "Attendance Summary Report",
        "sheet_name": "Attendance Summary",
        "filename": (f"wms-attendance-summary-{cleaned['date_from']}-{cleaned['date_to']}"),
        "period_label": (f"{cleaned['date_from']:%B %d, %Y} – {cleaned['date_to']:%B %d, %Y}"),
        "columns": [
            "Employee",
            "Employee Code",
            "Date",
            "Location / Branch",
            "Time In (Morning / Evening)",
            "Time Out (Morning / Evening)",
            "Status",
            "Present",
            "Late",
            "Absent",
            "Working Hours",
            "Missing / Incomplete",
        ],
        "export_rows": [
            [
                row["employee_name"],
                row["employee_code"],
                row["date"],
                row["location_name"],
                row["time_in"],
                row["time_out"],
                row["status_label"],
                "Yes" if row["present"] else "",
                "Yes" if row["late"] else "",
                "Yes" if row["absent"] else "",
                row["worked_label"],
                row["indicator"],
            ]
            for row in result["rows"]
        ],
        "export_totals": [
            "Totals",
            "",
            "",
            "",
            "",
            "",
            "",
            result["totals"]["present"],
            result["totals"]["late"],
            result["totals"]["absent"],
            result["totals"]["worked_label"],
            (
                f"Missing {result['totals']['missing_label']}; "
                f"Incomplete {result['totals']['incomplete']}"
            ),
        ],
        "column_formats": {2: "yyyy-mm-dd"},
    }
    metadata = [
        ("Period", report["period_label"]),
        (
            "Location",
            cleaned["location"].branch.name
            if cleaned["location"] is not None
            else "All permitted locations",
        ),
        (
            "Employee status",
            dict(AttendanceSummaryReportForm.EMPLOYEE_STATUS_CHOICES).get(
                cleaned["employee_status"],
                "All employee statuses",
            ),
        ),
    ]
    return form, report, metadata


@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.attendance.view", action=AccessAction.READ)
def attendance_summary_report(request):
    form, report, _metadata = _attendance_summary_result(request)
    return render(
        request,
        "wms/reports/attendance_summary.html",
        _base_context(
            request,
            form=form,
            report=report,
            export_route="wms:report_attendance_summary_export",
        ),
    )


@wms_permission_required("wms.reports.export", action=AccessAction.READ)
@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.attendance.view", action=AccessAction.READ)
def attendance_summary_export(request):
    _form, report, metadata = _attendance_summary_result(request)
    if report is None:
        return _invalid_export()
    return services.export_xlsx(
        report=report,
        business=request.business,
        metadata=metadata,
    )


def _individual_attendance_result(request):
    form = IndividualAttendanceReportForm(
        request.business,
        request.wms_user_access,
        request.GET or None,
    )
    cleaned = _cleaned_or_defaults(form, None)
    if cleaned is None:
        return form, None, None
    records = selectors.individual_attendance_records(
        request.wms_user_access,
        **cleaned,
    )
    result = services.attendance_report(records)
    employee = cleaned["employee"]
    report = {
        **result,
        "title": "Individual Attendance Report",
        "sheet_name": "Individual Attendance",
        "filename": (
            f"wms-individual-attendance-{employee.employee_code}-{cleaned['report_month']:%Y-%m}"
        ),
        "period_label": cleaned["report_month"].strftime("%B %Y"),
        "employee": employee,
        "columns": [
            "Date",
            "Time In (Morning / Evening)",
            "Time Out (Morning / Evening)",
            "Status",
            "Present",
            "Late",
            "Absent",
            "Worked Duration",
            "Required Duration",
            "Missing Duration",
            "Incomplete Indicator",
        ],
        "export_rows": [
            [
                row["date"],
                row["time_in"],
                row["time_out"],
                row["status_label"],
                "Yes" if row["present"] else "",
                "Yes" if row["late"] else "",
                "Yes" if row["absent"] else "",
                row["worked_label"],
                row["required_label"],
                row["missing_label"],
                row["indicator"],
            ]
            for row in result["rows"]
        ],
        "export_totals": [
            "Monthly totals",
            "",
            "",
            "",
            result["totals"]["present"],
            result["totals"]["late"],
            result["totals"]["absent"],
            result["totals"]["worked_label"],
            "",
            result["totals"]["missing_label"],
            f"Incomplete records: {result['totals']['incomplete']}",
        ],
        "column_formats": {0: "yyyy-mm-dd"},
    }
    metadata = [
        ("Period", report["period_label"]),
        ("Employee", f"{employee.employee_code} — {employee.full_name}"),
        ("Location", employee.location.branch.name),
    ]
    return form, report, metadata


@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.attendance.view", action=AccessAction.READ)
def individual_attendance_report(request):
    form, report, _metadata = _individual_attendance_result(request)
    return render(
        request,
        "wms/reports/individual_attendance.html",
        _base_context(
            request,
            form=form,
            report=report,
            export_route="wms:report_individual_attendance_export",
        ),
    )


@wms_permission_required("wms.reports.export", action=AccessAction.READ)
@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.attendance.view", action=AccessAction.READ)
def individual_attendance_export(request):
    _form, report, metadata = _individual_attendance_result(request)
    if report is None:
        return _invalid_export()
    return services.export_xlsx(
        report=report,
        business=request.business,
        metadata=metadata,
    )


def _salary_result(request):
    form = SalaryReportForm(
        request.business,
        request.wms_user_access,
        request.GET or None,
    )
    cleaned = _cleaned_or_defaults(
        form,
        {
            "report_month": business_localdate(request.business).replace(day=1),
            "employee": None,
            "location": None,
            "salary_type": "",
            "status": "",
        },
    )
    if cleaned is None:
        return form, None, None
    records = list(
        selectors.salary_records(
            request.wms_user_access,
            **cleaned,
        )
    )
    result = services.salary_report(records, request.business)
    report = {
        **result,
        "title": "Monthly Salary Report",
        "sheet_name": "Monthly Salary",
        "filename": f"wms-monthly-salary-{cleaned['report_month']:%Y-%m}",
        "period_label": cleaned["report_month"].strftime("%B %Y"),
        "currency_display": request.business.currency_display,
        "currency_precision": request.business.currency_precision,
        "columns": [
            "Employee",
            "Employee Code",
            "Salary Type",
            "Fixed / Base Salary",
            "Eligible Production Pieces",
            "Piece-rate Earnings",
            "Final Calculated Salary",
            "Calculation Status",
            "Calculated / Last Updated",
        ],
        "export_rows": [
            [
                row["employee_name"],
                row["employee_code"],
                row["salary_type"],
                row["base_salary"],
                row["eligible_pieces"],
                row["piece_earnings"],
                row["final_salary"],
                row["status"],
                row["calculated_at"],
            ]
            for row in result["rows"]
        ],
        "export_totals": [
            "Totals",
            "",
            "",
            result["totals"]["base_salary"],
            result["totals"]["eligible_pieces"],
            result["totals"]["piece_earnings"],
            result["totals"]["final_salary"],
            "",
            "",
        ],
        "column_formats": {
            3: "#,##0.000",
            5: "#,##0.000",
            6: "#,##0.000",
            8: "yyyy-mm-dd hh:mm",
        },
    }
    metadata = [
        ("Period", report["period_label"]),
        (
            "Employee",
            cleaned["employee"].full_name
            if cleaned["employee"] is not None
            else "All permitted employees",
        ),
        (
            "Location",
            cleaned["location"].branch.name
            if cleaned["location"] is not None
            else "All contributing locations",
        ),
        (
            "Salary type",
            dict(form.fields["salary_type"].choices).get(
                cleaned["salary_type"],
                "All salary types",
            ),
        ),
        (
            "Status",
            dict(form.fields["status"].choices).get(
                cleaned["status"],
                "All calculation statuses",
            ),
        ),
        ("Currency", request.business.currency_code),
    ]
    return form, report, metadata


@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.salary.view", action=AccessAction.READ)
def salary_report(request):
    form, report, _metadata = _salary_result(request)
    return render(
        request,
        "wms/reports/salary.html",
        _base_context(
            request,
            form=form,
            report=report,
            export_route="wms:report_salary_export",
        ),
    )


@wms_permission_required("wms.reports.export", action=AccessAction.READ)
@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.salary.view", action=AccessAction.READ)
def salary_export(request):
    _form, report, metadata = _salary_result(request)
    if report is None:
        return _invalid_export()
    return services.export_xlsx(
        report=report,
        business=request.business,
        metadata=metadata,
    )
