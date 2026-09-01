"""Phase 1 Workshop Order-linked production verification coverage."""

from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.db.models.query import QuerySet
from django.urls import reverse

from apps.backups.engine.logical_export_registry import (
    get_logical_export_registry,
)
from apps.backups.registry import get_component_definition
from apps.wms_orders import services as order_services
from apps.wms_orders.models import WmsWorkshopOrder
from apps.wms_production import selectors as production_selectors
from apps.wms_production import services as production_services
from apps.wms_salary import services as salary_services
from apps.wms_workforce.models import WmsEmployeeCategoryAssignment
from tests.test_wms_phase2 import make_employee, make_location
from tests.test_wms_phase4 import WmsPhase4Base
from tests.test_wms_phase7 import WmsPhase7Base


class LinkedProductionCapTests(WmsPhase4Base):
    def make_capped_order(self, reference, pieces=4, *, location=None, business=None):
        business = business or self.business_a
        return WmsWorkshopOrder.objects.create(
            business=business,
            location=location or self.location_a,
            order_reference=reference,
            eligible_piece_count=pieces,
            received_date=self.production_date,
        )

    def claim(self, *, employee, order, assignment, quantity, production_date):
        access = self.access_a if employee.business_id == self.business_a.pk else self.access_b
        return production_services.create_production_entry(
            business=employee.business,
            user_access=access,
            location=employee.location,
            employee=employee,
            production_date=production_date,
            daily_total_pieces=quantity,
            notes="Linked production claim.",
            production_rows=[
                {
                    "order": order,
                    "assignment": assignment,
                    "quantity": quantity,
                }
            ],
            user=employee.business.owner,
        )

    def test_new_order_requires_positive_authoritative_pcs(self):
        with self.assertRaisesMessage(ValidationError, "Eligible PCS"):
            WmsWorkshopOrder.objects.create(
                business=self.business_a,
                location=self.location_a,
                order_reference="NO-PCS",
                received_date=self.production_date,
            )
        with self.assertRaises(ValidationError):
            order_services.create_order_batch(
                business=self.business_a,
                user_access=self.access_a,
                location=self.location_a,
                received_date=self.production_date,
                order_rows=[("ZERO-PCS", 0)],
                user=self.owner_a,
            )

    def test_partial_claims_share_one_order_operation_cap_across_dates(self):
        order = self.make_capped_order("PARTIAL-4")
        self.claim(
            employee=self.employee_a,
            order=order,
            assignment=self.assignment_a1,
            quantity=2,
            production_date=self.production_date,
        )
        self.claim(
            employee=self.employee_a,
            order=order,
            assignment=self.assignment_a1,
            quantity=2,
            production_date=self.production_date + timedelta(days=1),
        )
        with self.assertRaisesMessage(ValidationError, "Production quantity exceeded"):
            self.claim(
                employee=self.employee_a,
                order=order,
                assignment=self.assignment_a1,
                quantity=1,
                production_date=self.production_date + timedelta(days=2),
            )
        self.assertEqual(order.production_lines.filter(category=self.category_a1).count(), 2)

    def test_same_operation_is_one_shared_bucket_across_employees(self):
        order = self.make_capped_order("SHARED-4")
        employee_two = make_employee(
            self.business_a,
            self.location_a,
            "P4-SHARED-EMP",
        )
        assignment_two = WmsEmployeeCategoryAssignment.objects.create(
            business=self.business_a,
            employee=employee_two,
            category=self.category_a1,
        )
        self.claim(
            employee=self.employee_a,
            order=order,
            assignment=self.assignment_a1,
            quantity=2,
            production_date=self.production_date,
        )
        self.claim(
            employee=employee_two,
            order=order,
            assignment=assignment_two,
            quantity=2,
            production_date=self.production_date,
        )
        with self.assertRaisesMessage(ValidationError, "Already recorded: 4 PCS"):
            self.claim(
                employee=employee_two,
                order=order,
                assignment=assignment_two,
                quantity=1,
                production_date=self.production_date + timedelta(days=1),
            )

    def test_operations_have_independent_capacity_and_rows_aggregate_in_request(self):
        order = self.make_capped_order("OPERATIONS-4")
        entry = production_services.create_production_entry(
            business=self.business_a,
            user_access=self.access_a,
            location=self.location_a,
            employee=self.employee_a,
            production_date=self.production_date,
            daily_total_pieces=4,
            notes="Independent operations.",
            production_rows=[
                {"order": order, "assignment": self.assignment_a1, "quantity": 4},
                {"order": order, "assignment": self.assignment_a2, "quantity": 4},
            ],
            user=self.owner_a,
        )
        self.assertEqual(entry.lines.count(), 2)

        second_order = self.make_capped_order("AGGREGATE-4")
        with self.assertRaisesMessage(ValidationError, "Requested additional: 6 PCS"):
            production_services.create_production_entry(
                business=self.business_a,
                user_access=self.access_a,
                location=self.location_a,
                employee=self.employee_a,
                production_date=self.production_date + timedelta(days=1),
                daily_total_pieces=6,
                notes="Must reject atomically.",
                production_rows=[
                    {"order": second_order, "assignment": self.assignment_a1, "quantity": 3},
                    {"order": second_order, "assignment": self.assignment_a1, "quantity": 3},
                ],
                user=self.owner_a,
            )
        self.assertFalse(second_order.production_lines.exists())

    def test_multiple_orders_same_operation_same_day_are_allowed(self):
        first = self.make_capped_order("MULTI-1", pieces=2)
        second = self.make_capped_order("MULTI-2", pieces=2)
        entry = production_services.create_production_entry(
            business=self.business_a,
            user_access=self.access_a,
            location=self.location_a,
            employee=self.employee_a,
            production_date=self.production_date,
            daily_total_pieces=4,
            notes="Two orders.",
            production_rows=[
                {"order": first, "assignment": self.assignment_a1, "quantity": 2},
                {"order": second, "assignment": self.assignment_a1, "quantity": 2},
            ],
            user=self.owner_a,
        )
        self.assertEqual(entry.lines.count(), 2)

    def test_selector_excludes_finished_wrong_location_and_null_pcs(self):
        eligible = self.make_capped_order("ELIGIBLE")
        null_pcs = self.make_capped_order("NULL-PCS")
        WmsWorkshopOrder.objects.filter(pk=null_pcs.pk).update(eligible_piece_count=None)
        finished = self.make_capped_order("FINISHED")
        order_services.finish_order_batch(
            business=self.business_a,
            user_access=self.access_a,
            finished_date=self.production_date,
            references=[finished.order_reference],
            user=self.owner_a,
        )
        other_location = make_location(
            self.business_a,
            "P4-SELECTOR-OTHER",
            "P4 Selector Other",
        )
        wrong_location_order = self.make_capped_order(
            "WRONG-LOCATION",
            location=other_location,
        )
        available = production_selectors.eligible_orders_for_production(
            self.access_a,
            self.location_a,
        )
        self.assertIn(eligible, available)
        self.assertNotIn(null_pcs, available)
        self.assertNotIn(finished, available)
        self.assertNotIn(wrong_location_order, available)

    def test_finished_order_remains_visible_and_protected_historically(self):
        order = self.make_capped_order("HISTORICAL")
        entry = self.claim(
            employee=self.employee_a,
            order=order,
            assignment=self.assignment_a1,
            quantity=2,
            production_date=self.production_date,
        )
        order_services.finish_order_batch(
            business=self.business_a,
            user_access=self.access_a,
            finished_date=self.production_date,
            references=[order.order_reference],
            user=self.owner_a,
        )
        response = self.client.get(reverse("wms:production_entry_detail", args=[entry.public_id]))
        self.assertContains(response, "HISTORICAL")
        with self.assertRaises(ProtectedError):
            order.delete()

    def test_order_detail_shows_aggregated_progress_and_granular_history_after_finish(self):
        order = self.make_capped_order("DETAIL-PROGRESS", pieces=4)
        employee_two = make_employee(
            self.business_a,
            self.location_a,
            "P4-DETAIL-EMP",
        )
        assignment_two = WmsEmployeeCategoryAssignment.objects.create(
            business=self.business_a,
            employee=employee_two,
            category=self.category_a1,
        )
        production_services.create_production_entry(
            business=self.business_a,
            user_access=self.access_a,
            location=self.location_a,
            employee=self.employee_a,
            production_date=self.production_date,
            daily_total_pieces=5,
            notes="Two operations for order detail.",
            production_rows=[
                {
                    "order": order,
                    "assignment": self.assignment_a1,
                    "quantity": 2,
                },
                {
                    "order": order,
                    "assignment": self.assignment_a2,
                    "quantity": 3,
                },
            ],
            user=self.owner_a,
        )
        self.claim(
            employee=employee_two,
            order=order,
            assignment=assignment_two,
            quantity=1,
            production_date=self.production_date + timedelta(days=1),
        )
        order_services.finish_order_batch(
            business=self.business_a,
            user_access=self.access_a,
            finished_date=self.production_date + timedelta(days=1),
            references=[order.order_reference],
            user=self.owner_a,
        )

        response = self.client.get(reverse("wms:order_detail", args=[order.public_id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [
                (row["operation"], row["completed_pieces"])
                for row in response.context["production_progress"]
            ],
            [(self.category_a1.name, 3), (self.category_a2.name, 3)],
        )
        self.assertEqual(len(response.context["production_history"]), 3)
        self.assertEqual(
            response.context["production_history"][0].entry.employee,
            employee_two,
        )
        self.assertContains(response, "Production Progress")
        self.assertContains(response, "Production History")
        self.assertContains(response, "3 / 4 PCS", count=2)
        self.assertContains(response, self.employee_a.full_name)
        self.assertContains(response, employee_two.full_name)

    def test_order_detail_has_clean_empty_and_legacy_denominator_states(self):
        empty_order = self.make_capped_order("DETAIL-EMPTY")
        empty_response = self.client.get(
            reverse("wms:order_detail", args=[empty_order.public_id])
        )
        self.assertContains(empty_response, "No production recorded yet.")
        self.assertNotContains(empty_response, "Production History")

        legacy_order = self.make_capped_order("DETAIL-LEGACY", pieces=4)
        self.claim(
            employee=self.employee_a,
            order=legacy_order,
            assignment=self.assignment_a1,
            quantity=3,
            production_date=self.production_date,
        )
        WmsWorkshopOrder.objects.filter(pk=legacy_order.pk).update(
            eligible_piece_count=None
        )
        legacy_response = self.client.get(
            reverse("wms:order_detail", args=[legacy_order.public_id])
        )
        self.assertContains(legacy_response, "3 PCS recorded")
        self.assertContains(legacy_response, "Production History")

    def test_order_print_contains_finished_legacy_record_and_browser_print_action(self):
        order = self.make_capped_order("PRINT-DETAIL", pieces=4)
        self.claim(
            employee=self.employee_a,
            order=order,
            assignment=self.assignment_a1,
            quantity=3,
            production_date=self.production_date,
        )
        order_services.finish_order_batch(
            business=self.business_a,
            user_access=self.access_a,
            finished_date=self.production_date,
            references=[order.order_reference],
            user=self.owner_a,
        )
        WmsWorkshopOrder.objects.filter(pk=order.pk).update(
            eligible_piece_count=None
        )

        detail_response = self.client.get(
            reverse("wms:order_detail", args=[order.public_id])
        )
        print_response = self.client.get(
            reverse("wms:order_print", args=[order.public_id]),
            {"autoprint": "1"},
        )

        self.assertContains(detail_response, "Print / PDF")
        self.assertContains(print_response, "Workshop Order Production Record")
        self.assertContains(print_response, self.business_a.name)
        self.assertContains(print_response, order.order_reference)
        self.assertContains(print_response, "Finished / Ready")
        self.assertContains(print_response, self.location_a.branch.name)
        self.assertContains(print_response, "Not recorded")
        self.assertContains(print_response, "3 PCS recorded")
        self.assertContains(print_response, self.employee_a.full_name)
        self.assertContains(print_response, self.category_a1.name)
        self.assertContains(print_response, "Status History")
        self.assertContains(print_response, 'onload="window.print()"')

    def test_order_print_reuses_order_access_controls(self):
        response = self.client.get(
            reverse("wms:order_print", args=[self.order_b.public_id])
        )

        self.assertEqual(response.status_code, 404)

    def test_claim_and_finish_paths_lock_the_order_for_race_safety(self):
        order = self.make_capped_order("LOCKED-FLOW")
        original_select_for_update = QuerySet.select_for_update

        def record_order_lock(locked_models):
            def tracked(queryset, *args, **kwargs):
                if queryset.model is WmsWorkshopOrder:
                    locked_models.append(queryset.model)
                return original_select_for_update(queryset, *args, **kwargs)

            return tracked

        claim_locks = []
        with mock.patch.object(
            QuerySet,
            "select_for_update",
            autospec=True,
            side_effect=record_order_lock(claim_locks),
        ):
            self.claim(
                employee=self.employee_a,
                order=order,
                assignment=self.assignment_a1,
                quantity=1,
                production_date=self.production_date,
            )
        self.assertIn(WmsWorkshopOrder, claim_locks)

        finish_locks = []
        with mock.patch.object(
            QuerySet,
            "select_for_update",
            autospec=True,
            side_effect=record_order_lock(finish_locks),
        ):
            order_services.finish_order_batch(
                business=self.business_a,
                user_access=self.access_a,
                finished_date=self.production_date,
                references=[order.order_reference],
                user=self.owner_a,
            )
        self.assertIn(WmsWorkshopOrder, finish_locks)

    def test_correction_revalidates_shared_cap_and_allows_finished_unchanged_order(self):
        order = self.make_capped_order("CORRECT-4")
        employee_two = make_employee(self.business_a, self.location_a, "P4-CORRECT-2")
        assignment_two = WmsEmployeeCategoryAssignment.objects.create(
            business=self.business_a,
            employee=employee_two,
            category=self.category_a1,
        )
        first = self.claim(
            employee=self.employee_a,
            order=order,
            assignment=self.assignment_a1,
            quantity=2,
            production_date=self.production_date,
        )
        self.claim(
            employee=employee_two,
            order=order,
            assignment=assignment_two,
            quantity=2,
            production_date=self.production_date,
        )
        line = first.lines.get()
        with self.assertRaisesMessage(ValidationError, "Production quantity exceeded"):
            production_services.correct_production_entry(
                business=self.business_a,
                user_access=self.access_a,
                entry=first,
                daily_total_pieces=3,
                notes="Over cap.",
                line_quantities={str(line.public_id): 3},
                correction_reason="Verified correction.",
                user=self.owner_a,
            )
        order_services.finish_order_batch(
            business=self.business_a,
            user_access=self.access_a,
            finished_date=self.production_date,
            references=[order.order_reference],
            user=self.owner_a,
        )
        corrected = production_services.correct_production_entry(
            business=self.business_a,
            user_access=self.access_a,
            entry=first,
            daily_total_pieces=1,
            notes="Reduced after finish.",
            line_quantities={str(line.public_id): 1},
            correction_reason="Verified reduction.",
            user=self.owner_a,
        )
        self.assertEqual(corrected.lines.get().quantity, 1)

    def test_order_pcs_cannot_be_reduced_below_recorded_operation(self):
        order = self.make_capped_order("NO-REDUCE", pieces=4)
        self.claim(
            employee=self.employee_a,
            order=order,
            assignment=self.assignment_a1,
            quantity=4,
            production_date=self.production_date,
        )
        order.eligible_piece_count = 3
        with self.assertRaisesMessage(ValidationError, "already recorded production"):
            order.save()

    def test_order_reference_search_and_legacy_null_display(self):
        order = self.make_capped_order("SEARCH-ME")
        entry = self.claim(
            employee=self.employee_a,
            order=order,
            assignment=self.assignment_a1,
            quantity=1,
            production_date=self.production_date,
        )
        results = production_selectors.filtered_production_entries(
            self.access_a,
            query="SEARCH-ME",
            production_date=self.production_date,
        )
        self.assertEqual(results.get(), entry)
        entry.lines.update(order=None)
        response = self.client.get(reverse("wms:production_entry_detail", args=[entry.public_id]))
        self.assertContains(response, "Legacy / Not recorded")


class LinkedProductionSalaryTests(WmsPhase7Base):
    def test_salary_snapshots_linked_order_without_changing_math(self):
        entry = self.create_production(
            daily_total=999,
            override_quantity=3,
            default_quantity=4,
        )
        salary = self.calculate(self.piece_employee)
        snapshots = list(salary.days.get(production_entry=entry).piece_lines.all())
        self.assertEqual(salary.total_eligible_quantity, 7)
        self.assertTrue(snapshots)
        self.assertTrue(
            all(
                line.order_public_id_snapshot == self.production_order.public_id
                and line.order_reference_snapshot == self.production_order.order_reference
                for line in snapshots
            )
        )
        salary_services.finalize_salary(
            business=self.business_a,
            user_access=self.access_a,
            salary=salary,
            user=self.owner_a,
        )
        saved = [
            (line.order_public_id_snapshot, line.order_reference_snapshot, line.quantity)
            for line in snapshots
        ]
        source_lines = list(entry.lines.all())
        production_services.correct_production_entry(
            business=self.business_a,
            user_access=self.access_a,
            entry=entry,
            daily_total_pieces=1,
            notes="Source correction after finalization.",
            line_quantities={str(line.public_id): 1 for line in source_lines},
            correction_reason="Verified source correction.",
            user=self.owner_a,
        )
        self.assertEqual(
            [
                (line.order_public_id_snapshot, line.order_reference_snapshot, line.quantity)
                for line in salary.days.get(production_entry=entry).piece_lines.all()
            ],
            saved,
        )


class LinkedProductionBackupPolicyTests(WmsPhase4Base):
    def test_backup_specs_and_component_order_cover_linked_order_fields(self):
        registry = get_logical_export_registry()
        order_spec = registry.get("wms_orders.WmsWorkshopOrder")
        production_spec = registry.get("wms_production.WmsProductionEntryLine")
        salary_spec = registry.get("wms_salary.WmsSalaryPieceLine")
        self.assertIn("eligible_piece_count", order_spec.scalar_fields)
        relation_map = {
            relation.field_name: relation for relation in production_spec.relation_fields
        }
        self.assertTrue(relation_map["order"].nullable)
        self.assertIn("order_public_id_snapshot", salary_spec.scalar_fields)
        self.assertIn("order_reference_snapshot", salary_spec.scalar_fields)
        orders_component = get_component_definition("wms.orders")
        production_component = get_component_definition("wms.production")
        self.assertLess(orders_component.import_order, production_component.import_order)
        self.assertIn("wms.orders", production_component.required_component_keys)
