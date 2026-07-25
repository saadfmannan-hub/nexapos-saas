"""Focused regression coverage for WMS Phase 7 salary calculation."""

from datetime import date, time
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase
from django.urls import reverse

from apps.accounts.models import Membership, Role
from apps.audit.models import AuditLog
from apps.tenants.services import provision_business
from apps.wms_alterations import services as alteration_services
from apps.wms_alterations.models import WmsAlteration
from apps.wms_attendance import services as attendance_services
from apps.wms_attendance.models import WmsAttendance
from apps.wms_core import services as core_services
from apps.wms_core.models import WmsRole, WmsUserAccess
from apps.wms_orders import services as order_services
from apps.wms_production import services as production_services
from apps.wms_salary import selectors, services
from apps.wms_salary.models import (
    WmsSalary,
    WmsSalaryDay,
    WmsSalaryLocationSnapshot,
    WmsSalaryPieceLine,
)
from apps.wms_workforce.models import (
    WmsEmployee,
    WmsEmployeeCategoryAssignment,
)
from tests.test_wms_phase1 import make_owner, make_plan
from tests.test_wms_phase2 import (
    make_category,
    make_employee,
    make_location,
)


class WmsPhase7Base(TestCase):
    salary_year = 2026
    salary_month = 7

    def setUp(self):
        self.plan = make_plan("Phase 7 WMS", wms=True)
        self.owner_a = make_owner("phase7-owner-a@example.com")
        self.owner_b = make_owner("phase7-owner-b@example.com")
        self.business_a = provision_business(
            owner=self.owner_a,
            name="Phase 7 Business A",
            plan=self.plan,
        )
        self.business_a.currency_code = "OMR"
        self.business_a.currency_symbol = "ر.ع."
        self.business_a.currency_precision = 3
        self.business_a.timezone = "Asia/Muscat"
        self.business_a.save()
        self.business_b = provision_business(
            owner=self.owner_b,
            name="Phase 7 Business B",
            plan=self.plan,
        )
        self.location_a1 = make_location(
            self.business_a,
            "P7-A1",
            "Phase 7 Workshop A1",
        )
        self.location_a2 = make_location(
            self.business_a,
            "P7-A2",
            "Phase 7 Workshop A2",
        )
        self.location_b = make_location(
            self.business_b,
            "P7-B",
            "Phase 7 Workshop B",
        )
        self.fixed_employee = make_employee(
            self.business_a,
            self.location_a1,
            "P7-FIXED",
            fixed_salary=Decimal("300.000"),
        )
        self.piece_employee = make_employee(
            self.business_a,
            self.location_a1,
            "P7-PIECE",
            compensation_type=WmsEmployee.CompensationType.PER_PIECE,
            fixed_salary=None,
            piece_rate=Decimal("2.000"),
        )
        self.employee_b = make_employee(
            self.business_b,
            self.location_b,
            "P7-B-EMP",
        )
        self.category_override = make_category(
            self.business_a,
            "Override Category",
            "OVERRIDE",
        )
        self.category_default = make_category(
            self.business_a,
            "Default Category",
            "DEFAULT",
        )
        self.assignment_override = WmsEmployeeCategoryAssignment.objects.create(
            business=self.business_a,
            employee=self.piece_employee,
            category=self.category_override,
            per_piece_rate=Decimal("1.500"),
        )
        self.assignment_default = WmsEmployeeCategoryAssignment.objects.create(
            business=self.business_a,
            employee=self.piece_employee,
            category=self.category_default,
            per_piece_rate=None,
        )
        self.membership_a = self.business_a.memberships.get(user=self.owner_a)
        self.access_a = WmsUserAccess.objects.for_business(
            self.business_a
        ).get(membership=self.membership_a)
        self.membership_b = self.business_b.memberships.get(user=self.owner_b)
        self.access_b = WmsUserAccess.objects.for_business(
            self.business_b
        ).get(membership=self.membership_b)
        self.client.force_login(self.owner_a)

    def calculate(
        self,
        employee=None,
        *,
        business=None,
        access=None,
        user=None,
        salary_year=None,
        salary_month=None,
    ):
        employee = employee or self.fixed_employee
        business = business or employee.business
        return services.calculate_salary(
            business=business,
            user_access=access or self.access_a,
            employee=employee,
            salary_year=salary_year or self.salary_year,
            salary_month=salary_month or self.salary_month,
            user=user or business.owner,
        )

    def create_attendance(
        self,
        *,
        employee=None,
        attendance_date=date(2026, 7, 10),
        morning_in=time(10, 30),
        morning_out=time(13, 0),
        evening_in=None,
        evening_out=None,
    ):
        employee = employee or self.fixed_employee
        return attendance_services.create_attendance(
            business=employee.business,
            employee=employee,
            attendance_date=attendance_date,
            time_values={
                "morning_time_in": morning_in,
                "morning_time_out": morning_out,
                "evening_time_in": evening_in,
                "evening_time_out": evening_out,
            },
            user=employee.business.owner,
        )

    def create_production(
        self,
        *,
        employee=None,
        production_date=date(2026, 7, 12),
        daily_total=999,
        override_quantity=3,
        default_quantity=4,
    ):
        employee = employee or self.piece_employee
        assignments = list(
            employee.category_assignments.filter(
                is_active=True,
                category__is_active=True,
            ).order_by("category__display_order", "category__name")
        )
        quantities = {}
        for assignment in assignments:
            quantities[str(assignment.public_id)] = (
                override_quantity
                if assignment.category_id == self.category_override.pk
                else default_quantity
            )
        return production_services.create_production_entry(
            business=employee.business,
            location=employee.location,
            employee=employee,
            production_date=production_date,
            daily_total_pieces=daily_total,
            notes="Phase 7 eligible production.",
            assignment_quantities=quantities,
            user=employee.business.owner,
        )

    def make_staff(self, name, permissions, *, allowed_locations=()):
        user = make_owner(f"phase7-{name}@example.com")
        shared_role = Role.objects.create(
            business=self.business_a,
            name=f"Shared {name}",
            permissions=[],
        )
        membership = Membership.objects.create(
            business=self.business_a,
            user=user,
            role=shared_role,
        )
        wms_role = WmsRole.objects.create(
            business=self.business_a,
            name=f"WMS {name}",
            code=f"phase7_{name}",
            permissions=permissions,
        )
        access = core_services.save_user_access(
            business=self.business_a,
            membership=membership,
            role=wms_role,
            allowed_locations=allowed_locations,
            user=self.owner_a,
        )
        return user, access

    def calculate_payload(self, employee=None, month="2026-07"):
        return {
            "employee": str((employee or self.fixed_employee).public_id),
            "salary_month": month,
        }


class WmsPhase7ModelTests(WmsPhase7Base):
    def test_salary_is_tenant_owned_and_cross_tenant_calculation_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.calculate(
                self.employee_b,
                business=self.business_a,
                access=self.access_a,
                user=self.owner_a,
            )
        self.assertFalse(WmsSalary.objects.exists())

    def test_one_salary_per_employee_calendar_month(self):
        salary = self.calculate()
        same = self.calculate()
        self.assertEqual(salary.pk, same.pk)
        self.assertEqual(
            WmsSalary.objects.filter(
                business=self.business_a,
                employee=self.fixed_employee,
                salary_year=2026,
                salary_month=7,
            ).count(),
            1,
        )

    def test_salary_month_status_and_snapshot_constraints(self):
        salary = self.calculate()
        salary.salary_month = 13
        with self.assertRaises(ValidationError):
            salary.full_clean()
        salary.refresh_from_db()
        salary.status = "UNKNOWN"
        with self.assertRaises(ValidationError):
            salary.full_clean()

    def test_cross_tenant_snapshot_relationships_are_rejected(self):
        salary = self.calculate()
        snapshot = WmsSalaryLocationSnapshot(
            business=self.business_a,
            salary=salary,
            location=self.location_b,
            location_name_snapshot="Other",
            location_type_snapshot=self.location_b.location_type,
        )
        with self.assertRaises(ValidationError):
            snapshot.save()

    def test_finalized_salary_and_children_reject_supported_mutation(self):
        salary = self.calculate()
        salary = services.finalize_salary(
            business=self.business_a,
            user_access=self.access_a,
            salary=salary,
            user=self.owner_a,
        )
        salary.gross_salary = Decimal("1.000")
        with self.assertRaises(ValidationError):
            salary.save()
        day = salary.days.first()
        day.daily_amount = Decimal("1.000")
        with self.assertRaises(ValidationError):
            day.save()
        with self.assertRaises(ValidationError):
            day.delete()

    def test_salary_source_relations_protect_historical_records(self):
        attendance = self.create_attendance()
        salary = self.calculate()
        self.assertTrue(salary.days.filter(attendance=attendance).exists())
        with self.assertRaises(ProtectedError):
            attendance.delete()


class WmsPhase7FixedSalaryTests(WmsPhase7Base):
    def test_fixed_salary_uses_full_configured_monthly_amount(self):
        salary = self.calculate()
        self.assertEqual(salary.gross_salary, Decimal("300.000"))
        self.assertEqual(
            salary.fixed_monthly_salary_snapshot,
            Decimal("300.000"),
        )
        self.assertEqual(salary.total_eligible_quantity, 0)
        self.assertEqual(salary.days.count(), 31)

    def test_attendance_is_snapshotted_without_automatic_deduction(self):
        attendance = self.create_attendance()
        salary = self.calculate()
        day = salary.days.get(salary_date=attendance.attendance_date)
        self.assertEqual(
            day.morning_status_snapshot,
            WmsAttendance.Status.LATE,
        )
        self.assertEqual(day.worked_minutes_snapshot, 150)
        self.assertGreater(day.missing_minutes_snapshot, 0)
        self.assertEqual(day.daily_amount, Decimal("0.000"))
        self.assertEqual(salary.gross_salary, Decimal("300.000"))

    def test_absent_and_missing_attendance_do_not_reduce_salary(self):
        self.create_attendance(
            attendance_date=date(2026, 7, 11),
            morning_in=None,
            morning_out=None,
        )
        salary = self.calculate()
        absent = salary.days.get(salary_date=date(2026, 7, 11))
        missing = salary.days.get(salary_date=date(2026, 7, 12))
        self.assertEqual(
            absent.morning_status_snapshot,
            WmsAttendance.Status.ABSENT,
        )
        self.assertIsNone(missing.attendance)
        self.assertEqual(salary.gross_salary, Decimal("300.000"))

    def test_partial_joining_month_starts_breakdown_without_proration(self):
        self.fixed_employee.joining_date = date(2026, 7, 15)
        self.fixed_employee.save()
        salary = self.calculate()
        self.assertEqual(salary.days.count(), 17)
        self.assertEqual(
            salary.days.first().salary_date,
            date(2026, 7, 15),
        )
        self.assertEqual(salary.gross_salary, Decimal("300.000"))

    def test_month_wholly_before_joining_is_rejected(self):
        self.fixed_employee.joining_date = date(2026, 8, 1)
        self.fixed_employee.save()
        with self.assertRaises(ValidationError):
            self.calculate()
        self.assertFalse(WmsSalary.objects.exists())

    def test_compensation_edit_does_not_silently_mutate_snapshot(self):
        salary = self.calculate()
        self.fixed_employee.fixed_monthly_salary = Decimal("450.000")
        self.fixed_employee.save()
        salary.refresh_from_db()
        self.assertEqual(salary.gross_salary, Decimal("300.000"))
        self.assertEqual(
            salary.fixed_monthly_salary_snapshot,
            Decimal("300.000"),
        )

    def test_compensation_edit_after_finalization_does_not_mutate_result(self):
        salary = services.finalize_salary(
            business=self.business_a,
            user_access=self.access_a,
            salary=self.calculate(),
            user=self.owner_a,
        )
        self.fixed_employee.fixed_monthly_salary = Decimal("450.000")
        self.fixed_employee.save()
        salary.refresh_from_db()
        self.assertEqual(salary.gross_salary, Decimal("300.000"))
        self.assertEqual(salary.status, WmsSalary.Status.FINALIZED)


class WmsPhase7PerPieceSalaryTests(WmsPhase7Base):
    def test_assignment_override_and_employee_default_rates_are_applied(self):
        self.create_production()
        salary = self.calculate(self.piece_employee)
        lines = {
            line.category_code_snapshot: line
            for line in WmsSalaryPieceLine.objects.filter(
                salary_day__salary=salary
            )
        }
        self.assertEqual(lines["OVERRIDE"].applied_rate, Decimal("1.500"))
        self.assertEqual(
            lines["OVERRIDE"].rate_source,
            WmsSalaryPieceLine.RateSource.ASSIGNMENT,
        )
        self.assertEqual(lines["DEFAULT"].applied_rate, Decimal("2.000"))
        self.assertEqual(
            lines["DEFAULT"].rate_source,
            WmsSalaryPieceLine.RateSource.EMPLOYEE_DEFAULT,
        )

    def test_piece_daily_and_monthly_totals_use_line_quantities(self):
        self.create_production(daily_total=999)
        salary = self.calculate(self.piece_employee)
        day = salary.days.get()
        self.assertEqual(day.eligible_quantity, 7)
        self.assertEqual(day.daily_amount, Decimal("12.500"))
        self.assertEqual(salary.total_eligible_quantity, 7)
        self.assertEqual(salary.gross_salary, Decimal("12.500"))
        self.assertNotEqual(salary.total_eligible_quantity, 999)

    def test_cross_month_and_pre_joining_production_are_excluded(self):
        self.create_production(production_date=date(2026, 6, 30))
        self.create_production(production_date=date(2026, 7, 12))
        self.piece_employee.joining_date = date(2026, 7, 13)
        self.piece_employee.save()
        salary = self.calculate(self.piece_employee)
        self.assertEqual(salary.total_eligible_quantity, 0)
        self.assertEqual(salary.gross_salary, Decimal("0.000"))

    def test_later_inactive_assignment_preserves_saved_production(self):
        self.create_production()
        self.assignment_override.is_active = False
        self.assignment_override.save()
        salary = self.calculate(self.piece_employee)
        self.assertEqual(salary.total_eligible_quantity, 7)
        self.assertEqual(salary.gross_salary, Decimal("12.500"))

    def test_alterations_and_workshop_orders_do_not_affect_salary(self):
        self.create_production(override_quantity=1, default_quantity=1)
        order_services.create_order_batch(
            business=self.business_a,
            user_access=self.access_a,
            location=self.location_a1,
            received_date=date(2026, 7, 1),
            references=["SALARY-IGNORED"],
            notes="Must not affect salary.",
            user=self.owner_a,
        )
        alteration_services.create_alteration(
            business=self.business_a,
            user_access=self.access_a,
            cleaned_data={
                "location": self.location_a1,
                "original_order_reference": "SALARY-IGNORED",
                "alteration_reference": "",
                "reason": WmsAlteration.Reason.IRON,
                "mistake_by": WmsAlteration.MistakeBy.UNKNOWN,
                "mistake_by_employee": None,
                "assigned_employee": self.piece_employee,
                "alteration_date": date(2026, 7, 12),
                "notes": "Must not affect salary.",
            },
            user=self.owner_a,
        )
        salary = self.calculate(self.piece_employee)
        self.assertEqual(salary.total_eligible_quantity, 2)
        self.assertEqual(salary.gross_salary, Decimal("3.500"))

    def test_rate_edit_does_not_silently_mutate_calculated_snapshot(self):
        self.create_production()
        salary = self.calculate(self.piece_employee)
        self.assignment_override.per_piece_rate = Decimal("9.000")
        self.assignment_override.save()
        salary.refresh_from_db()
        line = salary.days.get().piece_lines.get(
            category_code_snapshot="OVERRIDE"
        )
        self.assertEqual(line.applied_rate, Decimal("1.500"))
        self.assertEqual(salary.gross_salary, Decimal("12.500"))


class WmsPhase7LifecycleServiceTests(WmsPhase7Base):
    def test_recalculate_refreshes_same_unfinalized_record(self):
        entry = self.create_production()
        salary = self.calculate(self.piece_employee)
        original_pk = salary.pk
        production_services.correct_production_entry(
            business=self.business_a,
            entry=entry,
            daily_total_pieces=1,
            notes="Corrected.",
            line_quantities={
                str(line.public_id): 1 for line in entry.lines.all()
            },
            correction_reason="Signed correction.",
            user=self.owner_a,
        )
        salary = self.calculate(self.piece_employee)
        self.assertEqual(salary.pk, original_pk)
        self.assertEqual(salary.total_eligible_quantity, 2)
        self.assertEqual(salary.gross_salary, Decimal("3.500"))

    def test_finalize_succeeds_once_and_preserves_snapshots(self):
        self.create_production()
        salary = self.calculate(self.piece_employee)
        original_amount = salary.gross_salary
        original_lines = list(
            salary.days.values_list(
                "piece_lines__quantity",
                "piece_lines__applied_rate",
            )
        )
        salary = services.finalize_salary(
            business=self.business_a,
            user_access=self.access_a,
            salary=salary,
            user=self.owner_a,
        )
        self.assertEqual(salary.status, WmsSalary.Status.FINALIZED)
        self.assertIsNotNone(salary.finalized_at)
        with self.assertRaises(ValidationError):
            services.finalize_salary(
                business=self.business_a,
                user_access=self.access_a,
                salary=salary,
                user=self.owner_a,
            )
        salary.refresh_from_db()
        self.assertEqual(salary.gross_salary, original_amount)
        self.assertEqual(
            list(
                salary.days.values_list(
                    "piece_lines__quantity",
                    "piece_lines__applied_rate",
                )
            ),
            original_lines,
        )

    def test_finalized_salary_cannot_be_recalculated(self):
        salary = services.finalize_salary(
            business=self.business_a,
            user_access=self.access_a,
            salary=self.calculate(),
            user=self.owner_a,
        )
        with self.assertRaises(ValidationError):
            self.calculate()
        salary.refresh_from_db()
        self.assertEqual(salary.status, WmsSalary.Status.FINALIZED)

    def test_calculation_failure_rolls_back_record_and_children(self):
        original_save = WmsSalaryDay.save
        calls = {"count": 0}

        def failing_save(instance, *args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise ValidationError("Injected salary-day failure.")
            return original_save(instance, *args, **kwargs)

        with patch.object(WmsSalaryDay, "save", failing_save):
            with self.assertRaises(ValidationError):
                self.calculate()
        self.assertFalse(WmsSalary.objects.exists())
        self.assertFalse(WmsSalaryDay.objects.exists())

    def test_failed_recalculation_restores_previous_snapshots(self):
        salary = self.calculate()
        original_days = salary.days.count()
        with patch.object(
            WmsSalaryDay,
            "save",
            side_effect=ValidationError("Injected refresh failure."),
        ):
            with self.assertRaises(ValidationError):
                self.calculate()
        salary.refresh_from_db()
        self.assertEqual(salary.days.count(), original_days)
        self.assertEqual(salary.gross_salary, Decimal("300.000"))

    def test_calculate_recalculate_and_finalize_emit_safe_audit_events(self):
        salary = self.calculate()
        self.calculate()
        services.finalize_salary(
            business=self.business_a,
            user_access=self.access_a,
            salary=salary,
            user=self.owner_a,
        )
        logs = AuditLog.objects.filter(
            business=self.business_a,
            action__in=[
                "wms.salary_calculated",
                "wms.salary_recalculated",
                "wms.salary_finalized",
            ],
        ).order_by("created_at")
        self.assertEqual(logs.count(), 3)
        serialized = " ".join(
            f"{log.description} {log.old_values} {log.new_values}"
            for log in logs
        )
        self.assertNotIn("300.000", serialized)
        self.assertNotIn("gross_salary", serialized)
        self.assertNotIn("applied_rate", serialized)


class WmsPhase7ViewAndSecurityTests(WmsPhase7Base):
    def test_salary_list_defaults_to_business_local_current_month(self):
        response = self.client.get(reverse("wms:salary_list"))
        self.assertEqual(response.status_code, 200)
        expected = date.today().replace(day=1)
        self.assertEqual(
            (
                response.context["selected_month"].year,
                response.context["selected_month"].month,
            ),
            (expected.year, expected.month),
        )

    def test_salary_list_filters_employee_location_and_status(self):
        fixed = self.calculate()
        self.create_production()
        piece = self.calculate(self.piece_employee)
        piece = services.finalize_salary(
            business=self.business_a,
            user_access=self.access_a,
            salary=piece,
            user=self.owner_a,
        )
        response = self.client.get(
            reverse("wms:salary_list"),
            {
                "month": "2026-07",
                "employee": str(self.fixed_employee.public_id),
                "location": str(self.location_a1.public_id),
                "status": WmsSalary.Status.CALCULATED,
            },
        )
        self.assertEqual(list(response.context["page"]), [fixed])
        self.assertNotIn(piece, list(response.context["page"]))

    def test_salary_list_paginates_and_renders_empty_state(self):
        for index in range(26):
            employee = make_employee(
                self.business_a,
                self.location_a1,
                f"P7-PAGE-{index:02d}",
            )
            self.calculate(employee)
        response = self.client.get(
            reverse("wms:salary_list"),
            {"month": "2026-07"},
        )
        self.assertEqual(len(response.context["page"]), 25)
        self.assertTrue(response.context["page"].has_next())
        empty = self.client.get(
            reverse("wms:salary_list"),
            {"month": "2025-01"},
        )
        self.assertContains(empty, "No salary calculations match these filters.")

    def test_navigation_and_salary_data_require_view_permission(self):
        user, _access = self.make_staff(
            "calculate-only",
            ["wms.salary.calculate"],
        )
        self.client.force_login(user)
        calculate_page = self.client.get(reverse("wms:salary_calculate"))
        self.assertEqual(calculate_page.status_code, 200)
        self.assertNotContains(calculate_page, ">Salary</a>", html=False)
        self.assertEqual(
            self.client.get(reverse("wms:salary_list")).status_code,
            403,
        )

    def test_view_calculate_and_finalize_permissions_are_separate(self):
        salary = self.calculate()
        user, _access = self.make_staff(
            "view-only",
            ["wms.salary.view"],
        )
        self.client.force_login(user)
        self.assertEqual(
            self.client.get(reverse("wms:salary_list")).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse("wms:salary_calculate")).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse("wms:salary_finalize", args=[salary.public_id])
            ).status_code,
            403,
        )

    def test_calculate_only_action_does_not_return_salary_values(self):
        user, _access = self.make_staff(
            "calculate-action",
            ["wms.salary.calculate"],
        )
        self.client.force_login(user)
        response = self.client.post(
            reverse("wms:salary_calculate"),
            self.calculate_payload(),
        )
        self.assertEqual(response.status_code, 204)
        self.assertNotContains(
            response,
            "300.000",
            status_code=204,
        )

    def test_cross_tenant_and_crafted_employee_ids_are_not_disclosed(self):
        salary_b = self.calculate(
            self.employee_b,
            business=self.business_b,
            access=self.access_b,
            user=self.owner_b,
        )
        self.assertEqual(
            self.client.get(
                reverse("wms:salary_detail", args=[salary_b.public_id])
            ).status_code,
            404,
        )
        response = self.client.post(
            reverse("wms:salary_calculate"),
            self.calculate_payload(self.employee_b),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a valid choice")
        self.assertFalse(
            WmsSalary.objects.filter(
                business=self.business_a,
                employee=self.employee_b,
            ).exists()
        )

    def test_multi_location_salary_is_all_or_nothing(self):
        self.create_production(production_date=date(2026, 7, 10))
        self.piece_employee.location = self.location_a2
        self.piece_employee.save()
        self.create_production(production_date=date(2026, 7, 11))
        salary = self.calculate(self.piece_employee)
        user, restricted = self.make_staff(
            "location-a2-view",
            ["wms.salary.view", "wms.salary.finalize"],
            allowed_locations=[self.location_a2],
        )
        self.assertFalse(
            selectors.salary_records_for_access(restricted).filter(
                pk=salary.pk
            ).exists()
        )
        self.client.force_login(user)
        self.assertEqual(
            self.client.get(
                reverse("wms:salary_detail", args=[salary.public_id])
            ).status_code,
            404,
        )
        with self.assertRaises(ValidationError):
            services.finalize_salary(
                business=self.business_a,
                user_access=restricted,
                salary=salary,
                user=user,
            )
        self.assertEqual(
            self.client.post(
                reverse("wms:salary_finalize", args=[salary.public_id])
            ).status_code,
            404,
        )
        salary.refresh_from_db()
        self.assertEqual(salary.status, WmsSalary.Status.CALCULATED)

    def test_location_restricted_calculation_rejects_contributing_location(self):
        self.create_production(production_date=date(2026, 7, 10))
        self.piece_employee.location = self.location_a2
        self.piece_employee.save()
        self.create_production(production_date=date(2026, 7, 11))
        user, restricted = self.make_staff(
            "location-a2-calculate",
            ["wms.salary.calculate"],
            allowed_locations=[self.location_a2],
        )
        with self.assertRaises(ValidationError):
            self.calculate(
                self.piece_employee,
                access=restricted,
                user=user,
            )
        self.assertFalse(
            WmsSalary.objects.filter(employee=self.piece_employee).exists()
        )

    def test_inactive_location_history_remains_visible(self):
        salary = self.calculate()
        self.location_a1.is_active = False
        self.location_a1.save()
        salary.refresh_from_db()
        self.assertTrue(
            selectors.salary_records_for_access(self.access_a).filter(
                pk=salary.pk
            ).exists()
        )
        response = self.client.get(
            reverse("wms:salary_detail", args=[salary.public_id])
        )
        self.assertEqual(response.status_code, 200)

    def test_calculate_and_finalize_are_csrf_protected(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner_a)
        self.assertEqual(
            csrf_client.post(
                reverse("wms:salary_calculate"),
                self.calculate_payload(),
            ).status_code,
            403,
        )
        salary = self.calculate()
        self.assertEqual(
            csrf_client.post(
                reverse("wms:salary_finalize", args=[salary.public_id])
            ).status_code,
            403,
        )

    def test_get_requests_do_not_calculate_or_finalize(self):
        calculate = self.client.get(reverse("wms:salary_calculate"))
        self.assertEqual(calculate.status_code, 200)
        self.assertFalse(WmsSalary.objects.exists())
        salary = self.calculate()
        self.assertEqual(
            self.client.get(
                reverse("wms:salary_finalize", args=[salary.public_id])
            ).status_code,
            405,
        )
        salary.refresh_from_db()
        self.assertEqual(salary.status, WmsSalary.Status.CALCULATED)
