"""Focused regression coverage for the WMS Phase 9 executive dashboard."""

from datetime import date, time
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.wms_alterations.models import WmsAlteration
from apps.wms_attendance import services as attendance_services
from apps.wms_core import dashboard as dashboard_selectors
from apps.wms_core.models import WmsUserAccess
from apps.wms_orders import services as order_services
from apps.wms_production import services as production_services
from apps.wms_production.models import WmsProductionEntry
from apps.wms_workforce.models import (
    WmsEmployee,
    WmsEmployeeCategoryAssignment,
)
from tests.test_wms_phase2 import make_employee
from tests.test_wms_phase7 import WmsPhase7Base


class WmsPhase9DashboardTests(WmsPhase7Base):
    today = date(2026, 7, 25)
    yesterday = date(2026, 7, 24)

    def dashboard_response(self):
        with patch(
            "apps.wms_core.views.business_localdate",
            return_value=self.today,
        ):
            return self.client.get(reverse("wms:dashboard"))

    def create_order(
        self,
        reference,
        *,
        business=None,
        access=None,
        location=None,
        received_date=None,
        finish=False,
        user=None,
    ):
        business = business or self.business_a
        access = access or self.access_a
        location = location or self.location_a1
        user = user or business.owner
        order = order_services.create_order_batch(
            business=business,
            user_access=access,
            location=location,
            received_date=received_date or self.today,
            references=[reference],
            notes="Phase 9 dashboard order.",
            user=user,
        )[0]
        if finish:
            order = order_services.finish_order_batch(
                business=business,
                user_access=access,
                finished_date=self.today,
                references=[reference],
                user=user,
            )[0]
        return order

    def create_dashboard_attendance(
        self,
        employee,
        *,
        status="present",
        attendance_date=None,
    ):
        values = {
            "morning_time_in": time(10, 0),
            "morning_time_out": time(13, 0),
            "evening_time_in": time(16, 30),
            "evening_time_out": time(22, 0),
        }
        if status == "late":
            values["morning_time_in"] = time(10, 30)
        elif status == "absent":
            values = {
                "morning_time_in": None,
                "morning_time_out": None,
                "evening_time_in": None,
                "evening_time_out": None,
            }
        return attendance_services.create_attendance(
            business=employee.business,
            employee=employee,
            attendance_date=attendance_date or self.today,
            time_values=values,
            user=employee.business.owner,
        )

    def create_alteration(
        self,
        reference,
        *,
        business=None,
        location=None,
        employee=None,
    ):
        business = business or self.business_a
        location = location or self.location_a1
        employee = employee or self.fixed_employee
        return WmsAlteration.objects.create(
            business=business,
            location=location,
            original_order_reference=reference,
            alteration_reference=f"ALT-{reference}",
            reason=WmsAlteration.Reason.IRON,
            mistake_by=WmsAlteration.MistakeBy.CUSTOMER,
            assigned_employee=employee,
            alteration_date=self.today,
            status=WmsAlteration.Status.OPEN,
            created_by=business.owner,
            updated_by=business.owner,
        )

    def create_second_producer(self, *, location=None, code="P9-TOP"):
        employee = make_employee(
            self.business_a,
            location or self.location_a1,
            code,
            compensation_type=WmsEmployee.CompensationType.PER_PIECE,
            fixed_salary=None,
            piece_rate=Decimal("1.000"),
        )
        assignment = WmsEmployeeCategoryAssignment.objects.create(
            business=self.business_a,
            employee=employee,
            category=self.category_override,
        )
        return employee, assignment

    def create_production_for(
        self,
        employee,
        assignment,
        *,
        production_date=None,
        total,
        quantity=None,
    ):
        return production_services.create_production_entry(
            business=employee.business,
            location=employee.location,
            employee=employee,
            production_date=production_date or self.today,
            daily_total_pieces=total,
            notes="Phase 9 dashboard production.",
            assignment_quantities={
                str(assignment.public_id): (total if quantity is None else quantity)
            },
            user=employee.business.owner,
        )

    def test_dashboard_aggregates_cards_charts_rankings_and_recent_activity(self):
        self.create_order("P9-TODAY-OPEN")
        self.create_order("P9-TODAY-DONE", finish=True)
        self.create_order(
            "P9-OLDER-OPEN",
            received_date=self.yesterday,
        )
        self.create_alteration("P9-TODAY-OPEN")

        self.create_production(
            production_date=self.yesterday,
            daily_total=5,
            override_quantity=1,
            default_quantity=2,
        )
        self.create_production(
            production_date=self.today,
            daily_total=9,
            override_quantity=3,
            default_quantity=4,
        )
        top_employee, top_assignment = self.create_second_producer()
        self.create_production_for(
            top_employee,
            top_assignment,
            total=12,
        )

        self.create_dashboard_attendance(self.fixed_employee)
        self.create_dashboard_attendance(
            self.piece_employee,
            status="late",
        )
        self.create_dashboard_attendance(
            top_employee,
            status="absent",
        )

        response = self.dashboard_response()

        self.assertEqual(response.status_code, 200)
        dashboard = response.context["dashboard"]
        self.assertEqual(
            (
                dashboard["orders"]["received_today"],
                dashboard["orders"]["in_progress"],
                dashboard["orders"]["finished_today"],
            ),
            (2, 2, 1),
        )
        self.assertEqual(dashboard["alterations"]["pending"], 1)
        self.assertEqual(dashboard["production"]["total_today"], 21)
        self.assertEqual(
            dashboard["production"]["comparison"],
            {
                "today": 21,
                "yesterday": 5,
                "difference": 16,
                "difference_label": "+16",
                "percentage": 320.0,
                "percentage_label": "+320.0%",
                "direction": "up",
            },
        )
        self.assertEqual(
            dashboard["production"]["trend"]["data"][-2:],
            [5, 21],
        )
        category_totals = dict(
            zip(
                dashboard["production"]["categories"]["labels"],
                dashboard["production"]["categories"]["data"],
                strict=True,
            )
        )
        self.assertEqual(
            category_totals,
            {
                "Override Category": 16,
                "Default Category": 6,
            },
        )
        self.assertEqual(
            dashboard["production"]["top_today"][0]["employee__employee_code"],
            top_employee.employee_code,
        )
        self.assertEqual(
            dashboard["production"]["top_month"][0]["employee__employee_code"],
            self.piece_employee.employee_code,
        )
        self.assertEqual(
            (
                dashboard["attendance"]["present"],
                dashboard["attendance"]["late"],
                dashboard["attendance"]["absent"],
                dashboard["attendance"]["percentage"],
            ),
            (1, 1, 1, 66.7),
        )
        self.assertEqual(
            dashboard["attendance"]["most_punctual"][0]["employee__employee_code"],
            self.fixed_employee.employee_code,
        )
        self.assertContains(response, "Daily production trend")
        self.assertContains(response, "Production by category")
        self.assertContains(response, "Recent finished orders")
        self.assertContains(response, "Recent alterations")
        self.assertContains(response, "Dashboard</a>", html=False)

    def test_dashboard_is_strictly_tenant_isolated(self):
        self.create_order("P9-A-ONLY")
        WmsProductionEntry.objects.create(
            business=self.business_a,
            location=self.location_a1,
            employee=self.fixed_employee,
            production_date=self.today,
            daily_total_pieces=7,
            created_by=self.owner_a,
            updated_by=self.owner_a,
        )
        self.create_dashboard_attendance(self.fixed_employee)
        self.create_alteration("P9-A-ONLY")

        self.create_order(
            "P9-B-SECRET",
            business=self.business_b,
            access=self.access_b,
            location=self.location_b,
            user=self.owner_b,
        )
        WmsProductionEntry.objects.create(
            business=self.business_b,
            location=self.location_b,
            employee=self.employee_b,
            production_date=self.today,
            daily_total_pieces=999,
            created_by=self.owner_b,
            updated_by=self.owner_b,
        )
        self.create_dashboard_attendance(self.employee_b)
        self.create_alteration(
            "P9-B-SECRET",
            business=self.business_b,
            location=self.location_b,
            employee=self.employee_b,
        )

        response = self.dashboard_response()
        dashboard = response.context["dashboard"]

        self.assertEqual(dashboard["orders"]["received_today"], 1)
        self.assertEqual(dashboard["production"]["total_today"], 7)
        self.assertEqual(dashboard["attendance"]["present"], 1)
        self.assertEqual(dashboard["alterations"]["pending"], 1)
        self.assertContains(response, "P9-A-ONLY")
        self.assertNotContains(response, "P9-B-SECRET")
        self.assertNotContains(response, self.employee_b.full_name)

    def test_explicit_location_scope_applies_to_every_widget(self):
        location_two_employee, location_two_assignment = self.create_second_producer(
            location=self.location_a2,
            code="P9-LOCATION-TWO",
        )
        self.create_order("P9-LOC1", location=self.location_a1)
        self.create_order("P9-LOC2", location=self.location_a2)
        WmsProductionEntry.objects.create(
            business=self.business_a,
            location=self.location_a1,
            employee=self.fixed_employee,
            production_date=self.today,
            daily_total_pieces=8,
            created_by=self.owner_a,
            updated_by=self.owner_a,
        )
        self.create_production_for(
            location_two_employee,
            location_two_assignment,
            total=80,
        )
        self.create_dashboard_attendance(self.fixed_employee)
        self.create_dashboard_attendance(location_two_employee)
        self.create_alteration("P9-LOC1", location=self.location_a1)
        self.create_alteration(
            "P9-LOC2",
            location=self.location_a2,
            employee=location_two_employee,
        )
        user, _access = self.make_staff(
            "phase9-location-one",
            [
                "wms.dashboard.view",
                "wms.orders.view",
                "wms.alterations.view",
                "wms.attendance.view",
                "wms.production.view",
                "wms.employees.view",
            ],
            allowed_locations=(self.location_a1,),
        )
        self.client.force_login(user)

        response = self.dashboard_response()
        dashboard = response.context["dashboard"]

        self.assertEqual(dashboard["orders"]["received_today"], 1)
        self.assertEqual(dashboard["production"]["total_today"], 8)
        self.assertEqual(dashboard["attendance"]["present"], 1)
        self.assertEqual(dashboard["alterations"]["pending"], 1)
        self.assertContains(response, "P9-LOC1")
        self.assertNotContains(response, "P9-LOC2")
        self.assertNotContains(response, location_two_employee.full_name)

    def test_domain_permissions_hide_data_and_skip_restricted_queries(self):
        self.create_order("P9-PERMISSION-SECRET")
        WmsProductionEntry.objects.create(
            business=self.business_a,
            location=self.location_a1,
            employee=self.fixed_employee,
            production_date=self.today,
            daily_total_pieces=77,
            created_by=self.owner_a,
            updated_by=self.owner_a,
        )
        self.create_dashboard_attendance(self.fixed_employee)
        self.create_alteration("P9-PERMISSION-SECRET")
        dashboard_user, dashboard_access = self.make_staff(
            "phase9-dashboard-only",
            ["wms.dashboard.view"],
        )
        self.client.force_login(dashboard_user)

        response = self.dashboard_response()

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["dashboard"]["orders"])
        self.assertIsNone(response.context["dashboard"]["production"])
        self.assertIsNone(response.context["dashboard"]["attendance"])
        self.assertIsNone(response.context["dashboard"]["alterations"])
        self.assertNotContains(response, "P9-PERMISSION-SECRET")
        self.assertNotContains(response, self.fixed_employee.full_name)

        dashboard_access = (
            WmsUserAccess.objects.select_related("role")
            .prefetch_related("allowed_locations")
            .get(pk=dashboard_access.pk)
        )
        with CaptureQueriesContext(connection) as queries:
            data = dashboard_selectors.executive_dashboard(
                dashboard_access,
                today=self.today,
            )
        self.assertFalse(data["permissions"]["any_operational"])
        self.assertEqual(len(queries), 0)

        orders_user, _access = self.make_staff(
            "phase9-orders-only",
            ["wms.dashboard.view", "wms.orders.view"],
        )
        self.client.force_login(orders_user)
        orders_response = self.dashboard_response()
        self.assertContains(orders_response, "P9-PERMISSION-SECRET")
        self.assertNotContains(orders_response, self.fixed_employee.full_name)

        no_dashboard_user, _access = self.make_staff(
            "phase9-no-dashboard",
            ["wms.orders.view"],
        )
        self.client.force_login(no_dashboard_user)
        self.assertEqual(self.dashboard_response().status_code, 403)

    def test_full_dashboard_selector_has_a_bounded_query_count(self):
        self.create_order("P9-QUERY")
        self.create_alteration("P9-QUERY")
        WmsProductionEntry.objects.create(
            business=self.business_a,
            location=self.location_a1,
            employee=self.fixed_employee,
            production_date=self.today,
            daily_total_pieces=5,
            created_by=self.owner_a,
            updated_by=self.owner_a,
        )
        self.create_dashboard_attendance(self.fixed_employee)
        access = (
            WmsUserAccess.objects.select_related("business", "role")
            .prefetch_related("allowed_locations")
            .get(pk=self.access_a.pk)
        )

        with CaptureQueriesContext(connection) as queries:
            dashboard = dashboard_selectors.executive_dashboard(
                access,
                today=self.today,
            )

        self.assertEqual(dashboard["production"]["total_today"], 5)
        self.assertLessEqual(
            len(queries),
            10,
            "\n".join(query["sql"] for query in queries.captured_queries),
        )

    def test_empty_production_baseline_and_empty_dashboard_render_cleanly(self):
        response = self.dashboard_response()

        self.assertEqual(response.status_code, 200)
        comparison = response.context["dashboard"]["production"]["comparison"]
        self.assertEqual(comparison["today"], 0)
        self.assertEqual(comparison["yesterday"], 0)
        self.assertEqual(comparison["percentage_label"], "No baseline")
        self.assertContains(response, "No production recorded today.")
        self.assertContains(response, "No check-ins recorded today.")
