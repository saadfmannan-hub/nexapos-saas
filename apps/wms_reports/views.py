"""Read-only WMS report pages and permission-matched Excel exports."""

from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import render

from apps.core.date_ranges import business_localdate, business_localtime
from apps.subscriptions.access import AccessAction
from apps.wms_core.access import wms_permission_required

from . import selectors, services
from .forms import (
    AttendanceSummaryReportForm,
    DailyAlterationsReportForm,
    DailyFinishedReportForm,
    DailyOrdersReportForm,
    DailyProductionReportForm,
    IndividualAttendanceReportForm,
    MonthlyAlterationsReportForm,
    MonthlyOrdersReportForm,
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
        "report_generated_at": business_localtime(request.business),
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
            "report_generated_at": business_localtime(request.business),
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
        "title": "Daily Production Report",
        "sheet_name": "Daily Production",
        "filename": f"wms-daily-production-{cleaned['report_date']}",
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


def _location_metadata(location, default="All permitted locations"):
    return location.branch.name if location is not None else default


def _daily_finished_result(request):
    form = DailyFinishedReportForm(
        request.business,
        request.wms_user_access,
        request.GET or None,
    )
    cleaned = _cleaned_or_defaults(
        form,
        {
            "report_date": business_localdate(request.business),
            "location": None,
        },
    )
    if cleaned is None:
        return form, None, None
    result = selectors.daily_finished_orders(
        request.wms_user_access,
        **cleaned,
    )
    report = {
        **result,
        "title": "Daily Finished / Ready PCS Report",
        "sheet_name": "Daily Finished Ready",
        "filename": f"wms-daily-finished-ready-{cleaned['report_date']}",
        "period_label": cleaned["report_date"].strftime("%B %d, %Y"),
        "columns": [
            "Order / Invoice Reference",
            "Source Branch",
            "WMS Location",
            "Received Date",
            "Finished Date",
            "Status",
        ],
        "export_rows": [
            [
                order.order_reference,
                order.location.branch.name,
                order.location.branch.name,
                order.received_date,
                order.finished_date,
                order.get_status_display(),
            ]
            for order in result["rows"]
        ],
        "export_totals": [
            "Daily Finished / Ready total",
            "",
            "",
            "",
            "",
            result["total_finished"],
        ],
        "column_formats": {3: "yyyy-mm-dd", 4: "yyyy-mm-dd"},
    }
    metadata = [
        ("Period", report["period_label"]),
        ("Location", _location_metadata(cleaned["location"])),
    ]
    return form, report, metadata


@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.orders.view", action=AccessAction.READ)
def daily_finished_report(request):
    form, report, _metadata = _daily_finished_result(request)
    return render(
        request,
        "wms/reports/daily_finished.html",
        _base_context(
            request,
            form=form,
            report=report,
            export_route="wms:report_daily_finished_export",
        ),
    )


@wms_permission_required("wms.reports.export", action=AccessAction.READ)
@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.orders.view", action=AccessAction.READ)
def daily_finished_export(request):
    _form, report, metadata = _daily_finished_result(request)
    if report is None:
        return _invalid_export()
    return services.export_xlsx(
        report=report,
        business=request.business,
        metadata=metadata,
    )


def _daily_orders_result(request):
    form = DailyOrdersReportForm(
        request.business,
        request.wms_user_access,
        request.GET or None,
    )
    cleaned = _cleaned_or_defaults(
        form,
        {
            "report_date": business_localdate(request.business),
            "location": None,
            "status": "",
        },
    )
    if cleaned is None:
        return form, None, None
    result = selectors.daily_orders(
        request.wms_user_access,
        **cleaned,
    )
    report = {
        **result,
        "title": "Daily Orders Report",
        "sheet_name": "Daily Orders",
        "filename": f"wms-daily-orders-{cleaned['report_date']}",
        "period_label": cleaned["report_date"].strftime("%B %d, %Y"),
        "columns": [
            "Order / Invoice Reference",
            "Source Branch",
            "WMS Location",
            "Received Date",
            "Finished Date",
            "Status",
        ],
        "export_rows": [
            [
                order.order_reference,
                order.location.branch.name,
                order.location.branch.name,
                order.received_date,
                order.finished_date or "",
                order.get_status_display(),
            ]
            for order in result["rows"]
        ],
        "export_totals": [
            (
                f"Received {result['received']}; "
                f"Finished / Ready {result['finished']}; "
                f"In Process {result['in_process']}; "
                f"Completion {result['completion_percentage']}%"
            ),
            "",
            "",
            "",
            "",
            "",
        ],
        "column_formats": {3: "yyyy-mm-dd", 4: "yyyy-mm-dd"},
    }
    metadata = [
        ("Period", report["period_label"]),
        ("Location", _location_metadata(cleaned["location"])),
        (
            "Status",
            dict(form.fields["status"].choices).get(
                cleaned["status"],
                "All statuses",
            ),
        ),
    ]
    return form, report, metadata


@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.orders.view", action=AccessAction.READ)
def daily_orders_report(request):
    form, report, _metadata = _daily_orders_result(request)
    return render(
        request,
        "wms/reports/daily_orders.html",
        _base_context(
            request,
            form=form,
            report=report,
            export_route="wms:report_daily_orders_export",
        ),
    )


@wms_permission_required("wms.reports.export", action=AccessAction.READ)
@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.orders.view", action=AccessAction.READ)
def daily_orders_export(request):
    _form, report, metadata = _daily_orders_result(request)
    if report is None:
        return _invalid_export()
    return services.export_xlsx(
        report=report,
        business=request.business,
        metadata=metadata,
    )


def _monthly_orders_result(request):
    form = MonthlyOrdersReportForm(
        request.business,
        request.wms_user_access,
        request.GET or None,
    )
    cleaned = _cleaned_or_defaults(
        form,
        {
            "report_month": business_localdate(request.business).replace(day=1),
            "location": None,
        },
    )
    if cleaned is None:
        return form, None, None
    result = selectors.monthly_orders(
        request.wms_user_access,
        **cleaned,
    )
    report = {
        **result,
        "title": "Monthly Orders Report",
        "sheet_name": "Monthly Orders",
        "filename": f"wms-monthly-orders-{cleaned['report_month']:%Y-%m}",
        "period_label": cleaned["report_month"].strftime("%B %Y"),
        "columns": ["Date", "Orders Received", "Orders Finished / Ready"],
        "export_rows": [
            [row["date"], row["received"], row["finished"]]
            for row in result["rows"]
        ],
        "export_totals": [
            "Monthly totals",
            result["received"],
            result["finished"],
        ],
        "column_formats": {0: "yyyy-mm-dd"},
    }
    metadata = [
        ("Period", report["period_label"]),
        ("Location", _location_metadata(cleaned["location"])),
    ]
    return form, report, metadata


@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.orders.view", action=AccessAction.READ)
def monthly_orders_report(request):
    form, report, _metadata = _monthly_orders_result(request)
    return render(
        request,
        "wms/reports/monthly_orders.html",
        _base_context(
            request,
            form=form,
            report=report,
            export_route="wms:report_monthly_orders_export",
        ),
    )


@wms_permission_required("wms.reports.export", action=AccessAction.READ)
@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.orders.view", action=AccessAction.READ)
def monthly_orders_export(request):
    _form, report, metadata = _monthly_orders_result(request)
    if report is None:
        return _invalid_export()
    return services.export_xlsx(
        report=report,
        business=request.business,
        metadata=metadata,
    )


def _daily_alterations_result(request):
    form = DailyAlterationsReportForm(
        request.business,
        request.wms_user_access,
        request.GET or None,
    )
    cleaned = _cleaned_or_defaults(
        form,
        {
            "report_date": business_localdate(request.business),
            "location": None,
            "reason": "",
            "mistake_by_employee": None,
            "assigned_employee": None,
            "status": "",
        },
    )
    if cleaned is None:
        return form, None, None
    result = selectors.daily_alterations(
        request.wms_user_access,
        **cleaned,
    )
    report = {
        **result,
        "title": "Daily Alterations Report",
        "sheet_name": "Daily Alterations",
        "filename": f"wms-daily-alterations-{cleaned['report_date']}",
        "period_label": cleaned["report_date"].strftime("%B %d, %Y"),
        "columns": [
            "Order / Invoice Reference",
            "Alteration Reference",
            "Date",
            "Source Branch / Location",
            "Reason",
            "Mistake By",
            "Assigned To",
            "Status",
            "Completed At",
        ],
        "export_rows": [
            [
                alteration.original_order_reference,
                alteration.alteration_reference or "",
                alteration.alteration_date,
                alteration.location.branch.name,
                alteration.get_reason_display(),
                (
                    f"{alteration.get_mistake_by_display()}"
                    + (
                        f" — {alteration.mistake_by_employee.full_name}"
                        if alteration.mistake_by_employee
                        else ""
                    )
                ),
                alteration.assigned_employee.full_name,
                alteration.get_status_display(),
                (
                    business_localtime(
                        request.business,
                        value=alteration.completed_at,
                    ).replace(tzinfo=None)
                    if alteration.completed_at
                    else ""
                ),
            ]
            for alteration in result["rows"]
        ],
        "export_totals": [
            (
                f"New {result['total']}; Completed {result['completed']}; "
                f"Pending {result['pending']}; "
                f"Completion {result['completion_percentage']}%"
            ),
            *[""] * 8,
        ],
        "column_formats": {2: "yyyy-mm-dd", 8: "yyyy-mm-dd hh:mm"},
    }
    metadata = [
        ("Period", report["period_label"]),
        ("Location", _location_metadata(cleaned["location"])),
        (
            "Reason",
            dict(form.fields["reason"].choices).get(
                cleaned["reason"],
                "All reasons",
            ),
        ),
        (
            "Mistake by employee",
            cleaned["mistake_by_employee"].full_name
            if cleaned["mistake_by_employee"] is not None
            else "Any employee",
        ),
        (
            "Assigned to",
            cleaned["assigned_employee"].full_name
            if cleaned["assigned_employee"] is not None
            else "All permitted employees",
        ),
        (
            "Status",
            dict(form.fields["status"].choices).get(
                cleaned["status"],
                "All statuses",
            ),
        ),
    ]
    return form, report, metadata


@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.alterations.view", action=AccessAction.READ)
def daily_alterations_report(request):
    form, report, _metadata = _daily_alterations_result(request)
    return render(
        request,
        "wms/reports/daily_alterations.html",
        _base_context(
            request,
            form=form,
            report=report,
            export_route="wms:report_daily_alterations_export",
        ),
    )


@wms_permission_required("wms.reports.export", action=AccessAction.READ)
@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.alterations.view", action=AccessAction.READ)
def daily_alterations_export(request):
    _form, report, metadata = _daily_alterations_result(request)
    if report is None:
        return _invalid_export()
    return services.export_xlsx(
        report=report,
        business=request.business,
        metadata=metadata,
    )


def _monthly_alterations_result(request):
    form = MonthlyAlterationsReportForm(
        request.business,
        request.wms_user_access,
        request.GET or None,
    )
    cleaned = _cleaned_or_defaults(
        form,
        {
            "report_month": business_localdate(request.business).replace(day=1),
            "location": None,
            "reason": "",
            "employee": None,
            "status": "",
        },
    )
    if cleaned is None:
        return form, None, None
    result = selectors.monthly_alterations(
        request.wms_user_access,
        **cleaned,
    )
    report = {
        **result,
        "title": "Monthly Alterations Report",
        "sheet_name": "Monthly Alterations",
        "filename": f"wms-monthly-alterations-{cleaned['report_month']:%Y-%m}",
        "period_label": cleaned["report_month"].strftime("%B %Y"),
        "columns": ["Date", "Alterations"],
        "export_rows": [
            [row["date"], row["total"]] for row in result["day_rows"]
        ],
        "export_totals": ["Monthly total", result["total"]],
        "column_formats": {0: "yyyy-mm-dd"},
    }
    metadata = [
        ("Period", report["period_label"]),
        ("Location", _location_metadata(cleaned["location"])),
        (
            "Reason",
            dict(form.fields["reason"].choices).get(
                cleaned["reason"],
                "All reasons",
            ),
        ),
        (
            "Assigned to",
            cleaned["employee"].full_name
            if cleaned["employee"] is not None
            else "All permitted employees",
        ),
        (
            "Status",
            dict(form.fields["status"].choices).get(
                cleaned["status"],
                "All statuses",
            ),
        ),
        ("Completed", str(result["completed"])),
        ("Pending", str(result["pending"])),
        ("Completion", f"{result['completion_percentage']}%"),
        ("Most common reason", result["most_common_reason"]),
    ]
    return form, report, metadata


@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.alterations.view", action=AccessAction.READ)
def monthly_alterations_report(request):
    form, report, _metadata = _monthly_alterations_result(request)
    return render(
        request,
        "wms/reports/monthly_alterations.html",
        _base_context(
            request,
            form=form,
            report=report,
            export_route="wms:report_monthly_alterations_export",
        ),
    )


@wms_permission_required("wms.reports.export", action=AccessAction.READ)
@wms_permission_required("wms.reports.view", action=AccessAction.READ)
@wms_permission_required("wms.alterations.view", action=AccessAction.READ)
def monthly_alterations_export(request):
    _form, report, metadata = _monthly_alterations_result(request)
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
