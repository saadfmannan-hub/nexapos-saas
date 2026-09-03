"""Regression coverage for full-row WMS production corrections."""

from unittest import mock

from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.wms_orders.models import WmsWorkshopOrder
from apps.wms_production import services
from apps.wms_production.models import WmsProductionEntry, WmsProductionEntryLine
from apps.wms_reports import selectors as report_selectors
from tests.test_wms_phase2 import make_category
from tests.test_wms_phase4 import WmsPhase4Base


class WmsProductionCorrectionTests(WmsPhase4Base):
    def _append_row(self, payload, *, order, assignment, quantity):
        index = int(payload["rows-TOTAL_FORMS"])
        payload["rows-TOTAL_FORMS"] = str(index + 1)
        payload.update(
            {
                f"rows-{index}-line_id": "",
                f"rows-{index}-order": str(order.public_id),
                f"rows-{index}-assignment": str(assignment.public_id),
                f"rows-{index}-quantity": str(quantity),
                f"rows-{index}-DELETE": "",
            }
        )
        return index

    def _third_assignment(self):
        category = make_category(
            self.business_a,
            "Body",
            "BODY",
        )
        return self.make_assignment(self.employee_a, category)

    def test_existing_daily_entry_shows_correction_action_without_duplicate_parent(self):
        entry = self.create_entry()

        response = self.client.post(
            reverse("wms:production_entry_create"),
            self.entry_payload(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Production already exists")
        self.assertContains(response, "Correct Existing Production")
        self.assertContains(
            response,
            reverse("wms:production_entry_correct", args=[entry.public_id]),
        )
        self.assertEqual(
            WmsProductionEntry.objects.for_business(self.business_a).count(),
            1,
        )

    def test_correction_screen_supports_full_rows_and_keeps_identity_read_only(self):
        entry = self.create_entry()

        response = self.client.get(
            reverse("wms:production_entry_correct", args=[entry.public_id])
        )

        self.assertContains(response, "Add Row")
        self.assertContains(response, "Workshop Order")
        self.assertContains(response, "Operation")
        self.assertContains(response, entry.production_date.strftime("%d %b %Y"))
        self.assertNotContains(response, 'name="employee"')
        self.assertNotContains(response, 'name="production_date"')

    def test_missed_row_can_be_added_and_daily_total_remains_independent(self):
        entry = self.create_entry(daily_total=5)
        assignment = self._third_assignment()
        payload = self.correction_payload(entry, daily_total=5, quantity=5)
        self._append_row(
            payload,
            order=self.order_a,
            assignment=assignment,
            quantity=5,
        )

        response = self.client.post(
            reverse("wms:production_entry_correct", args=[entry.public_id]),
            payload,
        )

        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.daily_total_pieces, 5)
        self.assertEqual(
            sum(
                entry.lines.filter(is_removed=False).values_list(
                    "quantity", flat=True
                )
            ),
            15,
        )
        self.assertTrue(
            entry.lines.filter(
                is_removed=False,
                assignment=assignment,
                quantity=5,
            ).exists()
        )

    def test_existing_row_order_operation_and_quantity_can_be_corrected(self):
        entry = self.create_entry()
        replacement_order = self.make_order(self.employee_a, "P4-REPLACEMENT")
        replacement_assignment = self._third_assignment()
        payload = self.correction_payload(entry, quantity=7)
        payload["rows-0-order"] = str(replacement_order.public_id)
        payload["rows-0-assignment"] = str(replacement_assignment.public_id)

        response = self.client.post(
            reverse("wms:production_entry_correct", args=[entry.public_id]),
            payload,
        )

        self.assertEqual(response.status_code, 302)
        line = entry.lines.filter(is_removed=False).order_by("pk").first()
        line.refresh_from_db()
        self.assertEqual(line.order, replacement_order)
        self.assertEqual(line.assignment, replacement_assignment)
        self.assertEqual(line.category, replacement_assignment.category)
        self.assertEqual(line.quantity, 7)

    def test_historical_finished_order_and_inactive_assignment_remain_editable(self):
        entry = self.create_entry()
        WmsWorkshopOrder.objects.filter(pk=self.order_a.pk).update(
            status=WmsWorkshopOrder.Status.FINISHED_READY,
            finished_date=self.production_date,
        )
        type(self.assignment_a1).objects.filter(pk=self.assignment_a1.pk).update(
            is_active=False
        )
        payload = self.correction_payload(entry, quantity=6)

        response = self.client.post(
            reverse("wms:production_entry_correct", args=[entry.public_id]),
            payload,
        )

        self.assertEqual(response.status_code, 302)
        line = entry.lines.get(assignment=self.assignment_a1)
        self.assertEqual(line.quantity, 6)

    def test_existing_row_can_be_removed_without_touching_finalized_snapshots(self):
        entry = self.create_entry()
        removed_line = entry.lines.filter(is_removed=False).order_by("pk").first()
        payload = self.correction_payload(entry)
        payload["rows-0-DELETE"] = "on"

        response = self.client.post(
            reverse("wms:production_entry_correct", args=[entry.public_id]),
            payload,
        )

        self.assertEqual(response.status_code, 302)
        removed_line.refresh_from_db()
        self.assertTrue(removed_line.is_removed)
        self.assertEqual(entry.lines.filter(is_removed=False).count(), 1)

    def test_duplicate_order_operation_is_rejected_on_create_and_correction(self):
        create_payload = self.entry_payload(
            quantities={str(self.assignment_a1.public_id): 3}
        )
        create_payload["rows-TOTAL_FORMS"] = "2"
        create_payload.update(
            {
                "rows-1-order": str(self.order_a.public_id),
                "rows-1-assignment": str(self.assignment_a1.public_id),
                "rows-1-quantity": "2",
            }
        )
        create_response = self.client.post(
            reverse("wms:production_entry_create"),
            create_payload,
        )
        self.assertEqual(create_response.status_code, 200)
        self.assertContains(create_response, "Update the existing row instead")
        self.assertFalse(
            WmsProductionEntry.objects.for_business(self.business_a).exists()
        )

        entry = self.create_entry()
        payload = self.correction_payload(entry)
        self._append_row(
            payload,
            order=self.order_a,
            assignment=self.assignment_a1,
            quantity=1,
        )
        correction_response = self.client.post(
            reverse("wms:production_entry_correct", args=[entry.public_id]),
            payload,
        )
        self.assertEqual(correction_response.status_code, 200)
        self.assertContains(correction_response, "Update the existing row instead")
        self.assertEqual(entry.lines.filter(is_removed=False).count(), 2)

    def test_legacy_duplicate_rows_remain_correctable_without_db_constraint(self):
        entry = self.create_entry()
        WmsProductionEntryLine.objects.create(
            business=self.business_a,
            entry=entry,
            order=self.order_a,
            assignment=self.assignment_a1,
            category=self.category_a1,
            quantity=1,
        )
        lines = list(entry.lines.filter(is_removed=False).order_by("pk"))

        corrected = services.correct_production_entry(
            business=self.business_a,
            user_access=self.access_a,
            entry=entry,
            daily_total_pieces=10,
            notes="Legacy duplicate retained for safe compatibility.",
            production_rows=[
                {
                    "line_id": str(line.public_id),
                    "order": line.order,
                    "assignment": line.assignment,
                    "quantity": line.quantity + 1,
                }
                for line in lines
            ],
            correction_reason="Corrected legacy duplicate quantities.",
            user=self.owner_a,
        )

        self.assertEqual(corrected.lines.filter(is_removed=False).count(), 3)

    def test_unauthorized_order_cannot_be_injected_into_correction(self):
        entry = self.create_entry()
        line = entry.lines.filter(is_removed=False).order_by("pk").first()
        original_order_id = line.order_id
        payload = self.correction_payload(entry)
        payload["rows-0-order"] = str(self.order_b.public_id)

        response = self.client.post(
            reverse("wms:production_entry_correct", args=[entry.public_id]),
            payload,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a valid choice")
        line.refresh_from_db()
        self.assertEqual(line.order_id, original_order_id)

    def test_correction_is_atomic_when_a_new_row_fails_to_save(self):
        entry = self.create_entry(daily_total=10)
        assignment = self._third_assignment()
        original_notes = entry.notes
        original_quantities = list(
            entry.lines.filter(is_removed=False)
            .order_by("pk")
            .values_list("quantity", flat=True)
        )
        existing_rows = [
            {
                "line_id": str(line.public_id),
                "order": line.order,
                "assignment": line.assignment,
                "quantity": line.quantity + 1,
            }
            for line in entry.lines.filter(is_removed=False).order_by("pk")
        ]
        existing_rows.append(
            {
                "line_id": None,
                "order": self.order_a,
                "assignment": assignment,
                "quantity": 1,
            }
        )
        original_save = WmsProductionEntryLine.save

        def fail_new_line(instance, *args, **kwargs):
            if instance.pk is None:
                raise ValidationError("Forced row failure.")
            return original_save(instance, *args, **kwargs)

        with mock.patch.object(WmsProductionEntryLine, "save", fail_new_line):
            with self.assertRaisesMessage(ValidationError, "Forced row failure"):
                services.correct_production_entry(
                    business=self.business_a,
                    user_access=self.access_a,
                    entry=entry,
                    daily_total_pieces=99,
                    notes="Must roll back.",
                    production_rows=existing_rows,
                    correction_reason="Atomic correction check.",
                    user=self.owner_a,
                )

        entry.refresh_from_db()
        self.assertEqual(entry.daily_total_pieces, 10)
        self.assertEqual(entry.notes, original_notes)
        self.assertFalse(entry.is_corrected)
        self.assertEqual(
            list(
                entry.lines.filter(is_removed=False)
                .order_by("pk")
                .values_list("quantity", flat=True)
            ),
            original_quantities,
        )

    def test_audit_identifies_added_updated_and_removed_rows(self):
        entry = self.create_entry()
        assignment = self._third_assignment()
        payload = self.correction_payload(entry, daily_total=12, quantity=6)
        payload["rows-1-DELETE"] = "on"
        self._append_row(
            payload,
            order=self.order_a,
            assignment=assignment,
            quantity=4,
        )

        response = self.client.post(
            reverse("wms:production_entry_correct", args=[entry.public_id]),
            payload,
        )

        self.assertEqual(response.status_code, 302)
        audit = AuditLog.objects.get(
            action="wms.production_entry_corrected",
            object_id=str(entry.public_id),
        )
        self.assertEqual(audit.user, self.owner_a)
        changes = audit.new_values["change_summary"]
        self.assertIn("daily_total_pieces", changes["parent_fields"])
        self.assertEqual(
            {item["change"] for item in changes["production_rows"]},
            {"added", "updated", "removed"},
        )
        self.assertTrue(audit.old_values["production_rows"])
        self.assertTrue(audit.new_values["production_rows"])

    def test_reports_use_added_rows_and_ignore_removed_rows(self):
        entry = self.create_entry(daily_total=5)
        assignment = self._third_assignment()
        payload = self.correction_payload(entry, daily_total=5, quantity=5)
        payload["rows-1-DELETE"] = "on"
        self._append_row(
            payload,
            order=self.order_a,
            assignment=assignment,
            quantity=4,
        )
        response = self.client.post(
            reverse("wms:production_entry_correct", args=[entry.public_id]),
            payload,
        )
        self.assertEqual(response.status_code, 302)

        report = report_selectors.daily_production(
            self.access_a,
            report_date=self.production_date,
        )

        self.assertEqual(report["grand_total"], 5)
        self.assertEqual(
            {row["operation"] for row in report["detail_rows"]},
            {self.category_a1.name, assignment.category.name},
        )
        self.assertEqual(
            sum(row["quantity"] for row in report["detail_rows"]),
            9,
        )
