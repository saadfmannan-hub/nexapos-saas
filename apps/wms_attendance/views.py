from datetime import date
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import redirect, render

from apps.core.date_ranges import business_localdate
from apps.subscriptions.access import AccessAction
from apps.wms_core.access import wms_permission_required

from . import selectors, services
from .forms import AttendanceCorrectionForm, AttendanceEntryForm
from .models import WmsAttendance


def _querystring_without(request, *keys):
    params = request.GET.copy()
    for key in keys:
        params.pop(key, None)
    return urlencode(params, doseq=True)


def _selected_date(request):
    raw_value = request.GET.get("date", "").strip()
    if raw_value:
        try:
            return date.fromisoformat(raw_value)
        except ValueError:
            pass
    return business_localdate(request.business)


@wms_permission_required("wms.attendance.view", action=AccessAction.READ)
def attendance_list(request):
    query = request.GET.get("q", "").strip()
    attendance_date = _selected_date(request)
    locations = list(
        selectors.attendance_locations_for_access(request.wms_user_access)
    )
    location_map = {str(location.public_id): location for location in locations}
    location_id = request.GET.get("location", "")
    if location_id not in location_map:
        location_id = ""
    status = request.GET.get("status", "")
    valid_statuses = {value for value, _label in WmsAttendance.Status.choices}
    if status not in valid_statuses:
        status = ""
    records = selectors.filtered_attendance(
        request.wms_user_access,
        query=query,
        attendance_date=attendance_date,
        location_id=location_id,
        status=status,
    )
    page = Paginator(records, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "wms/attendance/index.html",
        {
            "page": page,
            "record_count": records.count(),
            "q": query,
            "selected_date": attendance_date,
            "locations": locations,
            "location_id": location_id,
            "status": status,
            "status_choices": WmsAttendance.Status.choices,
            "querystring": _querystring_without(request, "page"),
            "can_manage_attendance": request.wms_user_access.has_perm(
                "wms.attendance.manage"
            ),
            "can_correct_attendance": request.wms_user_access.has_perm(
                "wms.attendance.correct"
            ),
            "active_nav": "wms",
            "wms_active_nav": "attendance",
        },
    )


@wms_permission_required("wms.attendance.view", action=AccessAction.READ)
def attendance_detail(request, public_id):
    attendance = selectors.get_attendance_for_access(
        request.wms_user_access,
        public_id,
    )
    return render(
        request,
        "wms/attendance/detail.html",
        {
            "attendance": attendance,
            "can_correct_attendance": request.wms_user_access.has_perm(
                "wms.attendance.correct"
            ),
            "active_nav": "wms",
            "wms_active_nav": "attendance",
        },
    )


@wms_permission_required("wms.attendance.manage", action=AccessAction.WRITE)
def attendance_create(request):
    form = AttendanceEntryForm(
        request.business,
        request.wms_user_access,
        request.POST or None,
    )
    if request.method == "POST" and form.is_valid():
        try:
            attendance = services.create_attendance(
                business=request.business,
                employee=form.cleaned_data["employee"],
                attendance_date=form.cleaned_data["attendance_date"],
                time_values={
                    field: form.cleaned_data.get(field)
                    for field in services.ATTENDANCE_TIME_FIELDS
                },
                request=request,
            )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            messages.success(request, "Attendance saved.")
            return redirect(
                "wms:attendance_detail",
                public_id=attendance.public_id,
            )
    return render(
        request,
        "wms/attendance/form.html",
        {
            "form": form,
            "attendance": None,
            "is_correction": False,
            "active_nav": "wms",
            "wms_active_nav": "attendance",
        },
    )


@wms_permission_required("wms.attendance.correct", action=AccessAction.WRITE)
def attendance_correct(request, public_id):
    attendance = selectors.get_attendance_for_access(
        request.wms_user_access,
        public_id,
    )
    form = AttendanceCorrectionForm(
        request.business,
        request.POST or None,
        instance=attendance,
    )
    if request.method == "POST" and form.is_valid():
        try:
            attendance = services.correct_attendance(
                business=request.business,
                attendance=attendance,
                time_values={
                    field: form.cleaned_data.get(field)
                    for field in services.ATTENDANCE_TIME_FIELDS
                },
                correction_reason=form.cleaned_data["correction_reason"],
                request=request,
            )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            messages.success(request, "Attendance correction saved.")
            return redirect(
                "wms:attendance_detail",
                public_id=attendance.public_id,
            )
    return render(
        request,
        "wms/attendance/form.html",
        {
            "form": form,
            "attendance": attendance,
            "is_correction": True,
            "active_nav": "wms",
            "wms_active_nav": "attendance",
        },
    )
