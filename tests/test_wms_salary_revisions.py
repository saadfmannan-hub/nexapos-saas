"""Finalized salary corrections through the Owner workflow."""

from datetime import date
from decimal import Decimal
from io import BytesIO

from django.core.exceptions import ValidationError
from django.urls import reverse
from openpyxl import load_workbook

from apps.audit.models import AuditLog
from apps.platformadmin.middleware import SESSION_KEY
from apps.wms_core.models import WmsRole
from apps.wms_production.services import correct_production_entry
from apps.wms_salary import revisions, services
from apps.wms_salary.models import WmsSalary, WmsSalaryPieceLine
from apps.wms_workforce.models import WmsEmployee, WmsEmployeeCategoryAssignment
from tests.test_wms_phase2 import make_category
from tests.test_wms_phase7 import WmsPhase7Base


class SalaryRevisionTests(WmsPhase7Base):
    salary_month = 10
    production_date = date(2026, 10, 12)

    def finalize(self, salary):
        return services.finalize_salary(
            business=self.business_a,
            user_access=self.access_a,
            salary=salary,
            user=self.owner_a,
        )

    def start(self, salary, reason="Correct operation quantities"):
        response = self.client.post(
            reverse("wms:salary_revision_start", args=[salary.public_id]),
            {"reason": reason},
        )
        draft = revisions.salary_period(salary).get(status=WmsSalary.Status.CALCULATED)
        self.assertRedirects(response, self.revision_url(draft))
        return draft

    @staticmethod
    def revision_url(draft):
        return reverse("wms:salary_revision", args=[draft.public_id])

    def recalculate(self, draft, **rates):
        page = self.client.get(self.revision_url(draft))
        response = self.client.post(
            reverse("wms:salary_revision_calculate", args=[draft.public_id]),
            {"fingerprint": page.context["form"]["fingerprint"].value(), **rates},
        )
        draft.refresh_from_db()
        return response

    def finalize_revision(self, draft, *, preview=None):
        return self.client.post(
            reverse("wms:salary_revision_finalize", args=[draft.public_id]),
            {"preview": preview or draft.calculated_at.isoformat()},
        )

    def correct(self, entry, draft, *, override, default):
        url = reverse("wms:production_entry_correct", args=[entry.public_id])
        url += f"?salary_revision={draft.public_id}"
        lines = list(entry.lines.select_related("assignment", "order"))
        payload = {
            "daily_total_pieces": "999",
            "correction_reason": "Verified operation counts",
            "rows-TOTAL_FORMS": len(lines),
            "rows-INITIAL_FORMS": len(lines),
        }
        for i, line in enumerate(lines):
            payload.update(
                {
                    f"rows-{i}-line_id": str(line.public_id),
                    f"rows-{i}-order": str(line.order.public_id),
                    f"rows-{i}-assignment": str(line.assignment.public_id),
                    f"rows-{i}-quantity": override
                    if line.assignment_id == self.assignment_override.pk
                    else default,
                }
            )
        self.assertContains(self.client.get(url), "Back to Salary Revision")
        self.assertRedirects(self.client.post(url, payload), self.revision_url(draft))

    def assert_effective_report(self, salary, *, pieces, amount):
        params = {"report_month": f"{salary.period_start:%Y-%m}"}
        report = self.client.get(reverse("wms:report_salary"), params).context["report"]
        self.assertEqual([row["public_id"] for row in report["rows"]], [salary.public_id])
        self.assertEqual(report["totals"]["eligible_pieces"], pieces)
        self.assertEqual(report["totals"]["final_salary"], amount)
        finalized_report = self.client.get(
            reverse("wms:report_salary"),
            {**params, "status": "FINALIZED"},
        ).context["report"]
        self.assertEqual([row["public_id"] for row in finalized_report["rows"]], [salary.public_id])
        export = self.client.get(reverse("wms:report_salary_export"), params)
        self.assertEqual(export.status_code, 200)
        with BytesIO(export.content) as stream:
            workbook = load_workbook(stream, data_only=True)
            rows = [
                row
                for row in workbook.active.values
                if len(row) > 6 and row[1] == salary.employee_code_snapshot
            ]
            workbook.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][4], pieces)
        self.assertEqual(Decimal(str(rows[0][6])), amount)
        page = self.client.get(reverse("wms:salary_list"), {"month": params["report_month"]})
        self.assertEqual([item.public_id for item in page.context["page"]], [salary.public_id])

    def test_october_revision_and_second_revision_preserve_snapshots_and_reporting(self):
        entry = self.create_production(production_date=self.production_date)
        original = self.finalize(self.calculate(self.piece_employee))
        self.assertContains(
            self.client.get(reverse("wms:salary_detail", args=[original.public_id])),
            "Revise Finalized Salary",
        )
        draft = self.start(original)
        self.assertRedirects(self.finalize_revision(draft), self.revision_url(draft))
        self.assert_effective_report(original, pieces=7, amount=Decimal("12.500"))

        self.assignment_override.per_piece_rate = Decimal("9.000")
        self.assignment_override.save()
        self.piece_employee.default_per_piece_rate = Decimal("8.000")
        self.piece_employee.save()
        self.correct(entry, draft, override=6, default=4)
        self.assertRedirects(self.recalculate(draft), self.revision_url(draft))
        self.assertEqual(draft.gross_salary, Decimal("17.000"))
        review = self.client.get(self.revision_url(draft))
        self.assertEqual(review.context["difference"], Decimal("4.500"))
        stale_preview = draft.calculated_at.isoformat()
        self.correct(entry, draft, override=8, default=4)
        self.assertContains(self.client.get(self.revision_url(draft)), "Payable production changed")
        self.assertRedirects(self.finalize_revision(draft), self.revision_url(draft))
        self.assert_effective_report(original, pieces=7, amount=Decimal("12.500"))
        self.assertRedirects(self.recalculate(draft), self.revision_url(draft))
        self.assertRedirects(
            self.finalize_revision(draft, preview=stale_preview), self.revision_url(draft)
        )
        self.assertRedirects(
            self.finalize_revision(draft),
            reverse("wms:salary_detail", args=[draft.public_id]),
        )
        self.assert_effective_report(draft, pieces=12, amount=Decimal("20.000"))

        draft.refresh_from_db()
        second = self.start(draft, "Second count verification")
        self.assertEqual(second.revision_number, 2)
        self.correct(entry, second, override=10, default=5)
        self.recalculate(second)
        self.assertRedirects(
            self.finalize_revision(second),
            reverse("wms:salary_detail", args=[second.public_id]),
        )
        self.assert_effective_report(second, pieces=15, amount=Decimal("25.000"))
        self.assertEqual(
            list(
                revisions.salary_period(original)
                .order_by("revision_number")
                .values_list("gross_salary", flat=True)
            ),
            [Decimal("12.500"), Decimal("20.000"), Decimal("25.000")],
        )
        history = self.client.get(reverse("wms:salary_detail", args=[second.public_id]))
        self.assertContains(history, "Second count verification")
        self.assertContains(history, "Original salary")
        self.assertTrue(
            AuditLog.objects.filter(
                business=self.business_a,
                user=self.owner_a,
                action="wms.salary_revision_finalized",
            ).exists()
        )

    def test_different_month_and_reassigned_line_require_explicit_rate(self):
        self.piece_employee.compensation_type = WmsEmployee.CompensationType.HYBRID
        self.piece_employee.fixed_monthly_salary = Decimal("200")
        self.piece_employee.save()
        entry = self.create_production(production_date=date(2027, 1, 12), default_quantity=0)
        original = self.finalize(
            self.calculate(self.piece_employee, salary_year=2027, salary_month=1)
        )
        draft = self.start(original)
        new_assignment = WmsEmployeeCategoryAssignment.objects.create(
            business=self.business_a,
            employee=self.piece_employee,
            category=make_category(self.business_a, "New operation", "NEW-OP"),
            per_piece_rate=Decimal("99"),
        )
        line = entry.lines.get()
        self.assignment_override.per_piece_rate = Decimal("10")
        self.assignment_override.save()
        correct_production_entry(
            business=self.business_a,
            user_access=self.access_a,
            entry=entry,
            daily_total_pieces=999,
            notes="",
            correction_reason="Wrong operation",
            production_rows=[
                {
                    "line_id": str(line.public_id),
                    "order": line.order,
                    "assignment": new_assignment,
                    "quantity": 9,
                },
                {
                    "order": line.order,
                    "assignment": self.assignment_override,
                    "quantity": 4,
                },
            ],
            user=self.owner_a,
        )
        self.piece_employee.fixed_monthly_salary = Decimal("400")
        self.piece_employee.save()
        self.assertContains(self.recalculate(draft), "This field is required")
        self.assertFalse(draft.production_fingerprint)
        self.assertRedirects(
            self.recalculate(draft, **{f"rate_{line.public_id}": "2.750"}),
            self.revision_url(draft),
        )
        saved = WmsSalaryPieceLine.objects.get(salary_day__salary=draft, production_line=line)
        self.assertEqual(saved.rate_source, WmsSalaryPieceLine.RateSource.OWNER_CONFIRMED)
        self.assertEqual(saved.applied_rate, Decimal("2.750"))
        historical = (
            WmsSalaryPieceLine.objects.filter(salary_day__salary=draft).exclude(pk=saved.pk).get()
        )
        self.assertEqual(historical.applied_rate, Decimal("1.500"))
        self.assertEqual(historical.rate_source, WmsSalaryPieceLine.RateSource.HISTORICAL)
        self.assertEqual(draft.gross_salary, Decimal("230.750"))
        self.assertRedirects(
            self.finalize_revision(draft),
            reverse("wms:salary_detail", args=[draft.public_id]),
        )
        self.assert_effective_report(draft, pieces=13, amount=Decimal("230.750"))

    def test_manager_drafts_and_owner_only_actions_even_with_custom_grants(self):
        original = self.finalize(self.calculate(self.fixed_employee))
        pending = self.start(original)
        role_page = self.client.get(reverse("wms:role_create"))
        for label in (
            "View Salary",
            "Calculate Salary",
            "Finalize Salary",
            "Revise Finalized Salary",
        ):
            self.assertContains(role_page, label)
        staff, access = self.make_staff(
            "revision-manager", [], allowed_locations=[self.location_a1]
        )
        custom_role = access.role
        access.role = WmsRole.objects.for_business(self.business_a).get(code="workshop_manager")
        access.save()
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse("wms:salary_list")).status_code, 200)
        response = self.client.post(
            reverse("wms:salary_calculate"),
            self.calculate_payload(month="2026-11"),
        )
        regular = WmsSalary.objects.get(employee=self.fixed_employee, salary_month=11)
        self.assertRedirects(response, reverse("wms:salary_detail", args=[regular.public_id]))
        self.assertNotContains(self.client.get(response.url), "Finalize salary")
        self.piece_employee.location = self.location_a2
        self.piece_employee.save()
        self.client.post(
            reverse("wms:salary_calculate"),
            self.calculate_payload(self.piece_employee, month="2026-11"),
        )
        self.assertFalse(
            WmsSalary.objects.filter(employee=self.piece_employee, salary_month=11).exists()
        )

        custom_role.permissions = [
            "wms.salary.view",
            "wms.salary.calculate",
            "wms.salary.finalize",
            "wms.salary.revise",
        ]
        custom_role.save()
        access.role = custom_role
        access.save()
        actions = [
            ("salary_finalize", regular, {}),
            ("salary_revision_start", original, {"reason": "Disallowed"}),
            ("salary_revision_calculate", pending, {}),
            ("salary_revision_finalize", pending, {}),
        ]
        for action, salary, data in actions:
            self.assertEqual(
                self.client.post(
                    reverse(f"wms:{action}", args=[salary.public_id]), data
                ).status_code,
                403,
            )
        with self.assertRaisesMessage(ValidationError, "Only the Business Owner"):
            revisions.start_revision(
                business=self.business_a,
                user_access=access,
                salary=original,
                reason="Disallowed",
                user=staff,
            )
        self.client.force_login(self.owner_b)
        self.assertEqual(
            self.client.post(
                reverse("wms:salary_revision_start", args=[original.public_id]),
                {"reason": "Cross-tenant attempt"},
            ).status_code,
            404,
        )
        staff.is_platform_admin = True
        staff.save(update_fields=["is_platform_admin"])
        self.client.force_login(staff)
        session = self.client.session
        session[SESSION_KEY] = {
            "admin_id": staff.pk,
            "owner_id": self.owner_a.pk,
            "business_id": self.business_a.pk,
        }
        session.save()
        for action, salary, data in actions:
            self.assertEqual(
                self.client.post(
                    reverse(f"wms:{action}", args=[salary.public_id]), data
                ).status_code,
                403,
            )
        self.assertEqual(self.client.get(self.revision_url(pending)).status_code, 403)
