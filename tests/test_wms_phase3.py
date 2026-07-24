"""Focused regression coverage for WMS Phase 3 attendance management."""

from datetime import date, time

from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase
from django.urls import reverse

from apps.accounts.models import Membership, Role
from apps.audit.models import AuditLog
from apps.tenants.services import provision_business
from apps.wms_attendance import services
from apps.wms_attendance.forms import AttendanceEntryForm
from apps.wms_attendance.models import WmsAttendance
from apps.wms_core import services as core_services
from apps.wms_core.models import WmsRole, WmsSettings, WmsUserAccess
from tests.test_wms_phase1 import make_owner, make_plan
from tests.test_wms_phase2 import make_employee, make_location


class WmsPhase3Base(TestCase):
    attendance_date = date(2026, 7, 24)

    def setUp(self):
        self.plan = make_plan("Phase 3 WMS", wms=True)
        self.owner_a = make_owner("phase3-owner-a@example.com")
        self.owner_b = make_owner("phase3-owner-b@example.com")
        self.business_a = provision_business(
            owner=self.owner_a,
            name="Phase 3 Business A",
            plan=self.plan,
        )
        self.business_b = provision_business(
            owner=self.owner_b,
            name="Phase 3 Business B",
            plan=self.plan,
        )
        self.location_a = make_location(
            self.business_a,
            "P3-A",
            "Phase 3 Workshop A",
        )
        self.location_b = make_location(
            self.business_b,
            "P3-B",
            "Phase 3 Workshop B",
        )
        self.employee_a = make_employee(
            self.business_a,
            self.location_a,
            "P3-EMP-A",
        )
        self.employee_b = make_employee(
            self.business_b,
            self.location_b,
            "P3-EMP-B",
        )
        self.membership_a = self.business_a.memberships.get(user=self.owner_a)
        self.access_a = WmsUserAccess.objects.for_business(
            self.business_a
        ).get(membership=self.membership_a)
        self.settings_a = WmsSettings.objects.for_business(self.business_a).get()
        self.client.force_login(self.owner_a)

    def time_values(
        self,
        *,
        morning_in=time(10, 0),
        morning_out=time(13, 0),
        evening_in=time(16, 30),
        evening_out=time(22, 0),
    ):
        return {
            "morning_time_in": morning_in,
            "morning_time_out": morning_out,
            "evening_time_in": evening_in,
            "evening_time_out": evening_out,
        }

    def create_attendance(
        self,
        *,
        employee=None,
        attendance_date=None,
        time_values=None,
        business=None,
        user=None,
    ):
        employee = employee or self.employee_a
        return services.create_attendance(
            business=business or employee.business,
            employee=employee,
            attendance_date=attendance_date or self.attendance_date,
            time_values=time_values or self.time_values(),
            user=user or employee.business.owner,
        )

    def entry_payload(self, *, employee=None, attendance_date=None, **times):
        values = self.time_values(**times)
        return {
            "employee": (employee or self.employee_a).pk,
            "attendance_date": (
                attendance_date or self.attendance_date
            ).isoformat(),
            **{
                key: value.strftime("%H:%M") if value is not None else ""
                for key, value in values.items()
            },
        }


class WmsPhase3ModelTests(WmsPhase3Base):
    def test_configured_shifts_grace_and_statuses_are_snapshotted(self):
        self.settings_a.first_shift_start = time(9, 0)
        self.settings_a.first_shift_end = time(12, 0)
        self.settings_a.second_shift_start = time(14, 0)
        self.settings_a.second_shift_end = time(18, 0)
        self.settings_a.grace_period_minutes = 10
        self.settings_a.save()

        attendance = self.create_attendance(
            time_values=self.time_values(
                morning_in=time(9, 10),
                morning_out=time(12, 0),
                evening_in=time(14, 11),
                evening_out=time(18, 0),
            )
        )

        self.assertEqual(attendance.morning_status, WmsAttendance.Status.PRESENT)
        self.assertEqual(attendance.evening_status, WmsAttendance.Status.LATE)
        self.assertEqual(attendance.morning_shift_start, time(9, 0))
        self.assertEqual(attendance.evening_shift_start, time(14, 0))
        self.assertEqual(attendance.grace_period_minutes, 10)

    def test_time_out_alone_is_absent_and_zero_worked_minutes(self):
        attendance = self.create_attendance(
            time_values=self.time_values(
                morning_in=None,
                morning_out=time(13, 0),
                evening_in=None,
                evening_out=time(22, 0),
            )
        )

        self.assertEqual(attendance.morning_status, WmsAttendance.Status.ABSENT)
        self.assertEqual(attendance.evening_status, WmsAttendance.Status.ABSENT)
        self.assertEqual(attendance.worked_minutes, 0)
        self.assertEqual(attendance.missing_minutes, 510)

    def test_worked_minutes_are_capped_and_missing_minutes_do_not_include_overtime(self):
        attendance = self.create_attendance(
            time_values=self.time_values(
                morning_in=time(9, 30),
                morning_out=time(13, 30),
                evening_in=time(16, 0),
                evening_out=time(23, 0),
            )
        )

        self.assertEqual(attendance.morning_worked_minutes, 180)
        self.assertEqual(attendance.evening_worked_minutes, 330)
        self.assertEqual(attendance.worked_minutes, 510)
        self.assertEqual(attendance.missing_minutes, 0)

    def test_partial_shift_and_missing_time_out_calculation(self):
        attendance = self.create_attendance(
            time_values=self.time_values(
                morning_in=time(10, 20),
                morning_out=time(12, 30),
                evening_in=time(16, 40),
                evening_out=None,
            )
        )

        self.assertEqual(attendance.morning_status, WmsAttendance.Status.LATE)
        self.assertEqual(attendance.evening_status, WmsAttendance.Status.PRESENT)
        self.assertEqual(attendance.morning_worked_minutes, 130)
        self.assertEqual(attendance.evening_worked_minutes, 0)
        self.assertEqual(attendance.worked_minutes, 130)
        self.assertEqual(attendance.missing_minutes, 380)

    def test_shift_and_time_pair_validation(self):
        self.settings_a.first_shift_end = time(9, 0)
        with self.assertRaises(ValidationError):
            self.settings_a.save()
        self.settings_a.refresh_from_db()

        with self.assertRaises(ValidationError):
            self.create_attendance(
                time_values=self.time_values(
                    morning_in=time(11, 0),
                    morning_out=time(10, 30),
                )
            )

    def test_attendance_is_unique_per_employee_and_date_but_tenant_safe(self):
        self.create_attendance()
        with self.assertRaises(ValidationError):
            self.create_attendance()
        other = self.create_attendance(employee=self.employee_b)
        self.assertEqual(other.attendance_date, self.attendance_date)

    def test_cross_tenant_and_inactive_employee_or_location_are_rejected(self):
        with self.assertRaises(ValidationError):
            services.create_attendance(
                business=self.business_a,
                employee=self.employee_b,
                attendance_date=self.attendance_date,
                time_values=self.time_values(),
                user=self.owner_a,
            )

        self.employee_a.is_active = False
        self.employee_a.save()
        with self.assertRaises(ValidationError):
            self.create_attendance()
        self.employee_a.is_active = True
        self.employee_a.save()
        self.location_a.is_active = False
        self.location_a.save()
        with self.assertRaises(ValidationError):
            self.create_attendance()

    def test_historical_record_remains_correctable_after_employee_and_location_deactivate(self):
        attendance = self.create_attendance()
        self.employee_a.is_active = False
        self.employee_a.save()
        self.location_a.is_active = False
        self.location_a.save()

        corrected = services.correct_attendance(
            business=self.business_a,
            attendance=attendance,
            time_values=self.time_values(morning_in=time(10, 20)),
            correction_reason="Corrected from signed register.",
            user=self.owner_a,
        )

        self.assertTrue(corrected.correction_flag)
        self.assertEqual(corrected.morning_status, WmsAttendance.Status.LATE)
        response = self.client.get(
            reverse("wms:attendance_detail", args=[attendance.public_id])
        )
        self.assertEqual(response.status_code, 200)

    def test_shift_snapshot_stays_stable_when_settings_change(self):
        attendance = self.create_attendance()
        self.settings_a.first_shift_start = time(11, 0)
        self.settings_a.first_shift_end = time(14, 0)
        self.settings_a.second_shift_start = time(17, 0)
        self.settings_a.second_shift_end = time(23, 0)
        self.settings_a.save()

        corrected = services.correct_attendance(
            business=self.business_a,
            attendance=attendance,
            time_values=self.time_values(morning_in=time(10, 16)),
            correction_reason="Corrected original clock entry.",
            user=self.owner_a,
        )

        self.assertEqual(corrected.morning_shift_start, time(10, 0))
        self.assertEqual(corrected.morning_status, WmsAttendance.Status.LATE)

    def test_attendance_protects_employee_and_location_deletion(self):
        self.create_attendance()
        with self.assertRaises(ProtectedError):
            self.employee_a.delete()
        with self.assertRaises(ProtectedError):
            self.location_a.delete()

    def test_entry_form_only_offers_active_permitted_employees(self):
        inactive = make_employee(
            self.business_a,
            self.location_a,
            "P3-INACTIVE",
            active=False,
        )
        form = AttendanceEntryForm(self.business_a, self.access_a)

        self.assertIn(self.employee_a, form.fields["employee"].queryset)
        self.assertNotIn(inactive, form.fields["employee"].queryset)
        self.assertNotIn(self.employee_b, form.fields["employee"].queryset)


class WmsPhase3ViewTests(WmsPhase3Base):
    def test_authorized_daily_list_detail_create_and_filters(self):
        create_response = self.client.post(
            reverse("wms:attendance_create"),
            self.entry_payload(),
        )
        attendance = WmsAttendance.objects.for_business(self.business_a).get()
        self.assertRedirects(
            create_response,
            reverse("wms:attendance_detail", args=[attendance.public_id]),
        )

        response = self.client.get(
            reverse("wms:attendance_list"),
            {
                "q": self.employee_a.employee_code,
                "date": self.attendance_date.isoformat(),
                "location": self.location_a.public_id,
                "status": WmsAttendance.Status.PRESENT,
            },
        )
        self.assertContains(response, self.employee_a.full_name)
        self.assertEqual(response.context["record_count"], 1)
        self.assertContains(
            self.client.get(
                reverse("wms:attendance_detail", args=[attendance.public_id])
            ),
            "Total worked",
        )

    def test_correction_requires_reason_recalculates_and_audits_old_values(self):
        attendance = self.create_attendance()
        correction_url = reverse(
            "wms:attendance_correct",
            args=[attendance.public_id],
        )
        invalid = self.client.post(
            correction_url,
            {
                **self.entry_payload(),
                "morning_time_in": "10:20",
                "correction_reason": "",
            },
        )
        self.assertEqual(invalid.status_code, 200)
        attendance.refresh_from_db()
        self.assertEqual(attendance.morning_status, WmsAttendance.Status.PRESENT)

        response = self.client.post(
            correction_url,
            {
                "morning_time_in": "10:20",
                "morning_time_out": "13:00",
                "evening_time_in": "16:30",
                "evening_time_out": "22:00",
                "correction_reason": "Paper register confirmed 10:20.",
            },
        )
        self.assertRedirects(
            response,
            reverse("wms:attendance_detail", args=[attendance.public_id]),
        )
        attendance.refresh_from_db()
        self.assertTrue(attendance.correction_flag)
        self.assertEqual(attendance.morning_status, WmsAttendance.Status.LATE)
        correction_audit = AuditLog.objects.get(
            action="wms.attendance_corrected",
            object_id=str(attendance.public_id),
        )
        self.assertEqual(
            correction_audit.old_values["morning_time_in"],
            "10:00:00",
        )
        self.assertEqual(
            correction_audit.new_values["morning_time_in"],
            "10:20:00",
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="wms.attendance_updated",
                object_id=str(attendance.public_id),
            ).exists()
        )

    def test_creation_emits_attendance_audit(self):
        response = self.client.post(
            reverse("wms:attendance_create"),
            self.entry_payload(),
        )
        self.assertEqual(response.status_code, 302)
        attendance = WmsAttendance.objects.for_business(self.business_a).get()
        audit = AuditLog.objects.get(
            action="wms.attendance_created",
            object_id=str(attendance.public_id),
        )
        self.assertEqual(audit.business, self.business_a)
        self.assertEqual(audit.user, self.owner_a)

    def test_cross_tenant_employee_and_attendance_ids_are_rejected(self):
        other_attendance = self.create_attendance(employee=self.employee_b)
        self.assertEqual(
            self.client.get(
                reverse(
                    "wms:attendance_detail",
                    args=[other_attendance.public_id],
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse(
                    "wms:attendance_correct",
                    args=[other_attendance.public_id],
                ),
                {
                    "correction_reason": "Forged",
                },
            ).status_code,
            404,
        )
        response = self.client.post(
            reverse("wms:attendance_create"),
            self.entry_payload(employee=self.employee_b),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            WmsAttendance.objects.for_business(self.business_a).exists()
        )

    def test_explicit_location_scope_hides_other_location_attendance(self):
        second_location = make_location(
            self.business_a,
            "P3-A2",
            "Phase 3 Workshop A2",
        )
        second_employee = make_employee(
            self.business_a,
            second_location,
            "P3-OTHER-LOCATION",
        )
        attendance = self.create_attendance(employee=second_employee)
        self.access_a.allowed_locations.set([self.location_a])

        response = self.client.get(
            reverse("wms:attendance_list"),
            {"date": self.attendance_date.isoformat()},
        )
        self.assertNotContains(response, second_employee.full_name)
        self.assertEqual(
            self.client.get(
                reverse("wms:attendance_detail", args=[attendance.public_id])
            ).status_code,
            404,
        )

    def test_view_manage_correct_permissions_and_navigation(self):
        viewer = make_owner("phase3-viewer@example.com")
        core_role = Role.objects.create(
            business=self.business_a,
            name="Phase 3 Viewer Core",
            permissions=[],
        )
        membership = Membership.objects.create(
            business=self.business_a,
            user=viewer,
            role=core_role,
        )
        view_role = WmsRole.objects.create(
            business=self.business_a,
            name="Phase 3 Attendance Viewer",
            code="phase3_attendance_viewer",
            permissions=["wms.attendance.view"],
        )
        core_services.save_user_access(
            business=self.business_a,
            membership=membership,
            role=view_role,
            user=self.owner_a,
        )
        self.client.force_login(viewer)

        response = self.client.get(reverse("wms:attendance_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Attendance")
        self.assertNotContains(response, "Add attendance")
        self.assertEqual(
            self.client.get(reverse("wms:attendance_create")).status_code,
            403,
        )

        attendance = self.create_attendance()
        self.assertEqual(
            self.client.get(
                reverse(
                    "wms:attendance_correct",
                    args=[attendance.public_id],
                )
            ).status_code,
            403,
        )

    def test_missing_explicit_access_disabled_entitlement_and_pos_only_are_denied(self):
        no_access_user = make_owner("phase3-no-access@example.com")
        core_role = Role.objects.create(
            business=self.business_a,
            name="Phase 3 No WMS",
            permissions=[],
        )
        Membership.objects.create(
            business=self.business_a,
            user=no_access_user,
            role=core_role,
        )
        self.client.force_login(no_access_user)
        self.assertEqual(
            self.client.get(reverse("wms:attendance_list")).status_code,
            403,
        )

        self.plan.feature_wms = False
        self.plan.save(update_fields=["feature_wms"])
        self.client.force_login(self.owner_a)
        self.assertEqual(
            self.client.get(reverse("wms:attendance_list")).status_code,
            403,
        )

        pos_plan = make_plan("Phase 3 POS Only", pos=True)
        pos_owner = make_owner("phase3-pos-only@example.com")
        provision_business(
            owner=pos_owner,
            name="Phase 3 POS Only",
            plan=pos_plan,
        )
        self.client.force_login(pos_owner)
        self.assertEqual(
            self.client.get(reverse("wms:attendance_list")).status_code,
            403,
        )

    def test_get_requests_do_not_mutate_attendance(self):
        attendance = self.create_attendance()
        original_updated_at = attendance.updated_at

        response = self.client.get(
            reverse("wms:attendance_correct", args=[attendance.public_id])
        )

        self.assertEqual(response.status_code, 200)
        attendance.refresh_from_db()
        self.assertEqual(attendance.updated_at, original_updated_at)
        self.assertFalse(attendance.correction_flag)

    def test_csrf_is_required_for_create_and_correction(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner_a)
        self.assertEqual(
            csrf_client.post(
                reverse("wms:attendance_create"),
                self.entry_payload(),
            ).status_code,
            403,
        )
        attendance = self.create_attendance()
        self.assertEqual(
            csrf_client.post(
                reverse(
                    "wms:attendance_correct",
                    args=[attendance.public_id],
                ),
                {"correction_reason": "No CSRF"},
            ).status_code,
            403,
        )
