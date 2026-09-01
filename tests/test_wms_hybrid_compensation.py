"""Focused regression coverage for Hybrid WMS compensation."""

from datetime import date, time
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.backups.engine.logical_export_registry import (
    get_logical_export_registry,
)
from apps.tenants.services import provision_business
from apps.wms_attendance import services as attendance_services
from apps.wms_core.models import WmsUserAccess
from apps.wms_orders.models import WmsWorkshopOrder
from apps.wms_production import services as production_services
from apps.wms_salary import services as salary_services
from apps.wms_salary.models import (
    WmsSalary,
    WmsSalaryDay,
    WmsSalaryPieceLine,
)
from apps.wms_workforce import services as workforce_services
from apps.wms_workforce.models import (
    WmsEmployee,
    WmsEmployeeCategoryAssignment,
)
from tests.test_wms_phase1 import make_owner, make_plan
from tests.test_wms_phase2 import make_category, make_employee, make_location


class WmsHybridCompensationTests(TestCase):
    salary_year = 2026
    salary_month = 7

    def setUp(self):
        self.plan = make_plan("Hybrid Compensation WMS", wms=True)
        self.owner_a = make_owner("hybrid-owner-a@example.com")
        self.business_a = provision_business(
            owner=self.owner_a,
            name="Hybrid Business A",
            plan=self.plan,
        )
        self.business_a.currency_code = "OMR"
        self.business_a.currency_symbol = "ر.ع."
        self.business_a.currency_precision = 3
        self.business_a.timezone = "Asia/Muscat"
        self.business_a.save()
        self.location_a1 = make_location(
            self.business_a,
            "HYB-A1",
            "Hybrid Workshop A1",
        )
        self.location_a2 = make_location(
            self.business_a,
            "HYB-A2",
            "Hybrid Workshop A2",
        )
        membership = self.business_a.memberships.get(user=self.owner_a)
        self.access_a = WmsUserAccess.objects.for_business(self.business_a).get(
            membership=membership
        )

        self.hybrid_employee = make_employee(
            self.business_a,
            self.location_a1,
            "HYBRID-001",
            compensation_type=WmsEmployee.CompensationType.HYBRID,
            fixed_salary=Decimal("200.000"),
            piece_rate=Decimal("0.500"),
        )
        self.fixed_employee = make_employee(
            self.business_a,
            self.location_a1,
            "HYBRID-FIXED",
            fixed_salary=Decimal("120.000"),
        )
        self.piece_employee = make_employee(
            self.business_a,
            self.location_a1,
            "HYBRID-PIECE",
            compensation_type=WmsEmployee.CompensationType.PER_PIECE,
            fixed_salary=None,
            piece_rate=Decimal("2.000"),
        )

        self.daraz = make_category(self.business_a, "Daraz", "DARAZ")
        self.computer_design = make_category(
            self.business_a,
            "Computer Design",
            "COMPUTER",
        )
        self.fallback = make_category(
            self.business_a,
            "Fallback Category",
            "FALLBACK",
        )
        self.daraz_assignment = self._assign(
            self.hybrid_employee,
            self.daraz,
            Decimal("0.700"),
        )
        self.computer_assignment = self._assign(
            self.hybrid_employee,
            self.computer_design,
            Decimal("1.500"),
        )
        self.fallback_assignment = self._assign(
            self.hybrid_employee,
            self.fallback,
            None,
        )
        self.piece_assignment = self._assign(
            self.piece_employee,
            self.fallback,
            None,
        )
        self.production_order = WmsWorkshopOrder.objects.create(
            business=self.business_a,
            location=self.location_a1,
            order_reference="HYBRID-PRODUCTION",
            eligible_piece_count=10000,
            received_date=date(2026, 7, 1),
        )

    def _assign(self, employee, category, rate):
        return WmsEmployeeCategoryAssignment.objects.create(
            business=employee.business,
            employee=employee,
            category=category,
            per_piece_rate=rate,
        )

    def _calculate(
        self,
        employee=None,
        *,
        business=None,
        access=None,
        user=None,
    ):
        employee = employee or self.hybrid_employee
        business = business or employee.business
        return salary_services.calculate_salary(
            business=business,
            user_access=access or self.access_a,
            employee=employee,
            salary_year=self.salary_year,
            salary_month=self.salary_month,
            user=user or business.owner,
        )

    def _create_production(
        self,
        quantities,
        *,
        employee=None,
        production_date=date(2026, 7, 12),
    ):
        employee = employee or self.hybrid_employee
        assignments = list(
            employee.category_assignments.filter(
                is_active=True,
                category__is_active=True,
            ).select_related("category")
        )
        assignment_quantities = {
            str(assignment.public_id): quantities.get(
                assignment.category.code,
                0,
            )
            for assignment in assignments
        }
        access = WmsUserAccess.objects.for_business(employee.business).get(
            membership__user=employee.business.owner
        )
        order = WmsWorkshopOrder.objects.for_business(employee.business).filter(
            location=employee.location,
            status=WmsWorkshopOrder.Status.IN_PROCESS,
            eligible_piece_count__gt=0,
        ).first()
        if order is None:
            order = WmsWorkshopOrder.objects.create(
                business=employee.business,
                location=employee.location,
                order_reference=f"PROD-{employee.employee_code}",
                eligible_piece_count=10000,
                received_date=date(2026, 1, 1),
            )
        return production_services.create_production_entry(
            business=employee.business,
            user_access=access,
            location=employee.location,
            employee=employee,
            production_date=production_date,
            daily_total_pieces=sum(assignment_quantities.values()),
            notes="Hybrid compensation test production.",
            production_rows=[
                {
                    "order": order,
                    "assignment": assignment,
                    "quantity": assignment_quantities[str(assignment.public_id)],
                }
                for assignment in assignments
                if assignment_quantities[str(assignment.public_id)] > 0
            ],
            user=employee.business.owner,
        )

    def _create_attendance(
        self,
        *,
        employee=None,
        attendance_date=date(2026, 7, 12),
    ):
        employee = employee or self.hybrid_employee
        return attendance_services.create_attendance(
            business=employee.business,
            employee=employee,
            attendance_date=attendance_date,
            time_values={
                "morning_time_in": time(10, 0),
                "morning_time_out": time(13, 0),
                "evening_time_in": time(16, 30),
                "evening_time_out": time(22, 0),
            },
            user=employee.business.owner,
        )

    def _correct_to_daraz_quantity(self, entry, quantity):
        line_quantities = {
            str(line.public_id): (quantity if line.category_id == self.daraz.pk else 0)
            for line in entry.lines.all()
        }
        return production_services.correct_production_entry(
            business=self.business_a,
            user_access=self.access_a,
            entry=entry,
            daily_total_pieces=quantity,
            notes="Corrected Hybrid production.",
            line_quantities=line_quantities,
            correction_reason="Verified Hybrid test correction.",
            user=self.owner_a,
        )

    def test_hybrid_fixed_200_plus_production_100_equals_gross_300(self):
        self._create_production({"DARAZ": 100, "COMPUTER": 20})

        salary = self._calculate()

        self.assertEqual(salary.compensation_type_snapshot, "hybrid")
        self.assertEqual(
            salary.fixed_monthly_salary_snapshot,
            Decimal("200.000"),
        )
        self.assertEqual(
            salary.default_per_piece_rate_snapshot,
            Decimal("0.500"),
        )
        self.assertEqual(salary.fixed_salary_component, Decimal("200.000"))
        self.assertEqual(
            salary.production_salary_component,
            Decimal("100.000"),
        )
        self.assertEqual(salary.gross_salary, Decimal("300.000"))
        self.assertEqual(salary.total_eligible_quantity, 120)
        production_day = salary.days.get(salary_date=date(2026, 7, 12))
        self.assertIsNone(production_day.attendance_id)
        self.assertIsNotNone(production_day.production_entry_id)
        amounts = {
            line.category_code_snapshot: line.line_amount
            for line in WmsSalaryPieceLine.objects.filter(salary_day__salary=salary)
        }
        self.assertEqual(amounts["DARAZ"], Decimal("70.000"))
        self.assertEqual(amounts["COMPUTER"], Decimal("30.000"))

    def test_hybrid_rate_precedence_fallback_and_multiple_categories(self):
        self._create_production({"DARAZ": 5, "COMPUTER": 5, "FALLBACK": 5})

        salary = self._calculate()
        lines = {
            line.category_code_snapshot: line
            for line in WmsSalaryPieceLine.objects.filter(salary_day__salary=salary)
        }

        self.assertEqual(lines["DARAZ"].applied_rate, Decimal("0.700"))
        self.assertEqual(
            lines["DARAZ"].rate_source,
            WmsSalaryPieceLine.RateSource.ASSIGNMENT,
        )
        self.assertEqual(lines["COMPUTER"].applied_rate, Decimal("1.500"))
        self.assertEqual(
            lines["COMPUTER"].rate_source,
            WmsSalaryPieceLine.RateSource.ASSIGNMENT,
        )
        self.assertEqual(lines["FALLBACK"].applied_rate, Decimal("0.500"))
        self.assertEqual(
            lines["FALLBACK"].rate_source,
            WmsSalaryPieceLine.RateSource.EMPLOYEE_DEFAULT,
        )
        self.assertEqual(lines["DARAZ"].line_amount, Decimal("3.500"))
        self.assertEqual(lines["COMPUTER"].line_amount, Decimal("7.500"))
        self.assertEqual(lines["FALLBACK"].line_amount, Decimal("2.500"))
        self.assertEqual(
            salary.production_salary_component,
            Decimal("13.500"),
        )
        self.assertEqual(salary.gross_salary, Decimal("213.500"))

    def test_hybrid_zero_production_retains_full_fixed_component(self):
        salary = self._calculate()

        self.assertEqual(salary.days.count(), 31)
        self.assertEqual(salary.total_eligible_quantity, 0)
        self.assertEqual(salary.fixed_salary_component, Decimal("200.000"))
        self.assertEqual(
            salary.production_salary_component,
            Decimal("0.000"),
        )
        self.assertEqual(salary.gross_salary, Decimal("200.000"))
        self.assertFalse(salary.days.filter(production_entry__isnull=False).exists())
        self.assertFalse(WmsSalaryPieceLine.objects.filter(salary_day__salary=salary).exists())

    def test_hybrid_same_date_attendance_and_production_merge_into_one_day(self):
        attendance_only = self._create_attendance(attendance_date=date(2026, 7, 11))
        attendance = self._create_attendance()
        production = self._create_production({"DARAZ": 2})

        salary = self._calculate()
        merged = salary.days.get(salary_date=date(2026, 7, 12))

        self.assertEqual(salary.days.count(), 31)
        self.assertEqual(
            salary.days.filter(salary_date=date(2026, 7, 12)).count(),
            1,
        )
        self.assertEqual(merged.attendance_id, attendance.pk)
        self.assertEqual(merged.production_entry_id, production.pk)
        self.assertEqual(merged.location_id, self.location_a1.pk)
        self.assertEqual(merged.eligible_quantity, 2)
        self.assertEqual(merged.daily_amount, Decimal("1.400"))
        self.assertEqual(merged.worked_minutes_snapshot, 510)
        self.assertEqual(merged.piece_lines.count(), 1)
        attendance_day = salary.days.get(salary_date=date(2026, 7, 11))
        self.assertEqual(attendance_day.attendance_id, attendance_only.pk)
        self.assertIsNone(attendance_day.production_entry_id)
        self.assertEqual(attendance_day.daily_amount, Decimal("0.000"))
        empty_day = salary.days.get(salary_date=date(2026, 7, 13))
        self.assertIsNone(empty_day.attendance_id)
        self.assertIsNone(empty_day.production_entry_id)

    def test_hybrid_recalculation_uses_corrected_historical_local_date_data(self):
        attendance = self._create_attendance(
            attendance_date=date(2026, 7, 1),
        )
        original_salary = self._calculate()
        original_day = original_salary.days.get(salary_date=date(2026, 7, 1))
        self.assertEqual(original_day.worked_minutes_snapshot, 510)

        attendance_services.correct_attendance(
            business=self.business_a,
            attendance=attendance,
            time_values={
                "morning_time_in": None,
                "morning_time_out": None,
                "evening_time_in": None,
                "evening_time_out": None,
            },
            correction_reason="Verified absence after local-day review.",
            user=self.owner_a,
        )
        july_first_production = self._create_production(
            {"DARAZ": 2},
            production_date=date(2026, 7, 1),
        )
        july_second_production = self._create_production(
            {"DARAZ": 3},
            production_date=date(2026, 7, 2),
        )
        june_production = self._create_production(
            {"DARAZ": 7},
            production_date=date(2026, 6, 30),
        )
        august_production = self._create_production(
            {"DARAZ": 11},
            production_date=date(2026, 8, 1),
        )
        workforce_services.set_employee_active(
            business=self.business_a,
            employee=self.hybrid_employee,
            is_active=False,
            user=self.owner_a,
        )
        workforce_services.set_category_active(
            business=self.business_a,
            category=self.daraz,
            is_active=False,
            user=self.owner_a,
        )

        salary = self._calculate()
        absent_production_day = salary.days.get(salary_date=date(2026, 7, 1))
        production_only_day = salary.days.get(salary_date=date(2026, 7, 2))

        self.assertEqual(salary.pk, original_salary.pk)
        self.assertEqual(salary.period_start, date(2026, 7, 1))
        self.assertEqual(salary.period_end, date(2026, 7, 31))
        self.assertEqual(salary.days.count(), 31)
        self.assertEqual(absent_production_day.attendance_id, attendance.pk)
        self.assertEqual(
            absent_production_day.production_entry_id,
            july_first_production.pk,
        )
        self.assertEqual(absent_production_day.morning_status_snapshot, "absent")
        self.assertEqual(absent_production_day.evening_status_snapshot, "absent")
        self.assertEqual(absent_production_day.worked_minutes_snapshot, 0)
        self.assertEqual(absent_production_day.daily_amount, Decimal("1.400"))
        self.assertIsNone(production_only_day.attendance_id)
        self.assertEqual(
            production_only_day.production_entry_id,
            july_second_production.pk,
        )
        self.assertEqual(production_only_day.daily_amount, Decimal("2.100"))
        self.assertEqual(salary.total_eligible_quantity, 5)
        self.assertEqual(
            salary.production_salary_component,
            Decimal("3.500"),
        )
        self.assertEqual(salary.gross_salary, Decimal("203.500"))
        snapshotted_entry_ids = set(
            salary.days.exclude(production_entry=None).values_list(
                "production_entry_id",
                flat=True,
            )
        )
        self.assertEqual(
            snapshotted_entry_ids,
            {july_first_production.pk, july_second_production.pk},
        )
        self.assertNotIn(june_production.pk, snapshotted_entry_ids)
        self.assertNotIn(august_production.pk, snapshotted_entry_ids)

    def test_fixed_and_per_piece_component_regressions(self):
        fixed_salary = self._calculate(self.fixed_employee)
        self._create_production({"FALLBACK": 4}, employee=self.piece_employee)
        piece_salary = self._calculate(self.piece_employee)

        self.assertEqual(
            (
                fixed_salary.fixed_salary_component,
                fixed_salary.production_salary_component,
                fixed_salary.gross_salary,
            ),
            (
                Decimal("120.000"),
                Decimal("0.000"),
                Decimal("120.000"),
            ),
        )
        self.assertEqual(
            (
                piece_salary.fixed_salary_component,
                piece_salary.production_salary_component,
                piece_salary.gross_salary,
            ),
            (
                Decimal("0.000"),
                Decimal("8.000"),
                Decimal("8.000"),
            ),
        )

    def test_hybrid_recalculation_refreshes_components_rates_and_quantities(self):
        entry = self._create_production({"DARAZ": 10})
        salary = self._calculate()
        original_pk = salary.pk
        self.assertEqual(salary.gross_salary, Decimal("207.000"))

        self.hybrid_employee.fixed_monthly_salary = Decimal("250.000")
        self.hybrid_employee.save()
        self.daraz_assignment.per_piece_rate = Decimal("0.900")
        self.daraz_assignment.save()
        self._correct_to_daraz_quantity(entry, 20)

        salary = self._calculate()
        daraz_line = WmsSalaryPieceLine.objects.get(
            salary_day__salary=salary,
            category_code_snapshot="DARAZ",
        )

        self.assertEqual(salary.pk, original_pk)
        self.assertEqual(salary.fixed_salary_component, Decimal("250.000"))
        self.assertEqual(
            salary.production_salary_component,
            Decimal("18.000"),
        )
        self.assertEqual(salary.gross_salary, Decimal("268.000"))
        self.assertEqual(salary.total_eligible_quantity, 20)
        self.assertEqual(daraz_line.quantity, 20)
        self.assertEqual(daraz_line.applied_rate, Decimal("0.900"))
        self.assertEqual(WmsSalary.objects.filter(pk=original_pk).count(), 1)

    def test_finalized_hybrid_salary_remains_immutable_after_source_edits(self):
        entry = self._create_production({"DARAZ": 10})
        salary = salary_services.finalize_salary(
            business=self.business_a,
            user_access=self.access_a,
            salary=self._calculate(),
            user=self.owner_a,
        )
        original_values = (
            salary.compensation_type_snapshot,
            salary.fixed_monthly_salary_snapshot,
            salary.default_per_piece_rate_snapshot,
            salary.fixed_salary_component,
            salary.production_salary_component,
            salary.gross_salary,
            salary.total_eligible_quantity,
        )
        original_lines = list(
            WmsSalaryPieceLine.objects.filter(salary_day__salary=salary)
            .order_by("category_code_snapshot")
            .values_list(
                "category_code_snapshot",
                "quantity",
                "applied_rate",
                "line_amount",
            )
        )

        self.hybrid_employee.compensation_type = (
            WmsEmployee.CompensationType.PER_PIECE
        )
        self.hybrid_employee.fixed_monthly_salary = None
        self.hybrid_employee.default_per_piece_rate = Decimal("4.000")
        self.hybrid_employee.save()
        self.daraz_assignment.per_piece_rate = Decimal("9.000")
        self.daraz_assignment.save()
        self._correct_to_daraz_quantity(entry, 99)

        with self.assertRaises(ValidationError):
            self._calculate()

        salary.refresh_from_db()
        self.assertEqual(salary.status, WmsSalary.Status.FINALIZED)
        self.assertEqual(
            (
                salary.compensation_type_snapshot,
                salary.fixed_monthly_salary_snapshot,
                salary.default_per_piece_rate_snapshot,
                salary.fixed_salary_component,
                salary.production_salary_component,
                salary.gross_salary,
                salary.total_eligible_quantity,
            ),
            original_values,
        )
        self.assertEqual(
            list(
                WmsSalaryPieceLine.objects.filter(salary_day__salary=salary)
                .order_by("category_code_snapshot")
                .values_list(
                    "category_code_snapshot",
                    "quantity",
                    "applied_rate",
                    "line_amount",
                )
            ),
            original_lines,
        )

    def test_hybrid_salary_is_tenant_isolated(self):
        owner_b = make_owner("hybrid-owner-b@example.com")
        business_b = provision_business(
            owner=owner_b,
            name="Hybrid Business B",
            plan=self.plan,
        )
        location_b = make_location(business_b, "HYB-B", "Hybrid Workshop B")
        employee_b = make_employee(
            business_b,
            location_b,
            "HYBRID-B",
            compensation_type=WmsEmployee.CompensationType.HYBRID,
            fixed_salary=Decimal("500.000"),
            piece_rate=Decimal("10.000"),
        )
        category_b = make_category(business_b, "Tenant B Category", "TENANT-B")
        self._assign(employee_b, category_b, Decimal("10.000"))
        self._create_production({"TENANT-B": 100}, employee=employee_b)
        self._create_production({"DARAZ": 1})

        salary_a = self._calculate()

        self.assertEqual(salary_a.total_eligible_quantity, 1)
        self.assertEqual(
            salary_a.production_salary_component,
            Decimal("0.700"),
        )
        self.assertTrue(
            salary_a.days.filter(
                piece_lines__production_line__entry__business=self.business_a
            ).exists()
        )
        self.assertFalse(
            salary_a.days.filter(piece_lines__production_line__entry__business=business_b).exists()
        )
        with self.assertRaises(ValidationError):
            self._calculate(
                employee_b,
                business=self.business_a,
                access=self.access_a,
                user=self.owner_a,
            )
        self.assertFalse(
            WmsSalary.objects.filter(
                business=self.business_a,
                employee=employee_b,
            ).exists()
        )

    def test_mismatched_same_date_locations_roll_back_salary_snapshots(self):
        attendance = self._create_attendance()
        self.hybrid_employee.location = self.location_a2
        self.hybrid_employee.save()
        production = self._create_production({"DARAZ": 3})

        with self.assertRaisesMessage(ValidationError, "different locations"):
            self._calculate()

        self.assertFalse(WmsSalary.objects.filter(employee=self.hybrid_employee).exists())
        self.assertFalse(
            WmsSalaryDay.objects.filter(salary__employee=self.hybrid_employee).exists()
        )
        self.assertFalse(
            WmsSalaryPieceLine.objects.filter(
                salary_day__salary__employee=self.hybrid_employee
            ).exists()
        )
        self.assertTrue(type(attendance).objects.filter(pk=attendance.pk).exists())
        self.assertTrue(type(production).objects.filter(pk=production.pk).exists())

    def test_hybrid_components_are_registered_for_logical_backup_export(self):
        registry = get_logical_export_registry()
        salary_spec = registry.get("wms_salary.WmsSalary")

        self.assertTrue(registry.validate_complete())
        self.assertEqual(salary_spec.component_key, "wms.salary")
        self.assertIn("fixed_salary_component", salary_spec.scalar_fields)
        self.assertIn("production_salary_component", salary_spec.scalar_fields)
