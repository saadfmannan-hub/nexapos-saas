"""Focused regression coverage for WMS Phase 4 daily production entry."""

from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase
from django.urls import reverse

from apps.accounts.models import Membership, Role
from apps.audit.models import AuditLog
from apps.tenants.services import provision_business
from apps.wms_core import services as core_services
from apps.wms_core.models import WmsRole, WmsUserAccess
from apps.wms_production import services
from apps.wms_production.forms import ProductionEntryForm
from apps.wms_production.models import (
    WmsProductionEntry,
    WmsProductionEntryLine,
)
from apps.wms_workforce.models import WmsEmployeeCategoryAssignment
from tests.test_wms_phase1 import make_owner, make_plan
from tests.test_wms_phase2 import (
    make_category,
    make_employee,
    make_location,
)


class WmsPhase4Base(TestCase):
    production_date = date(2026, 7, 25)

    def setUp(self):
        self.plan = make_plan("Phase 4 WMS", wms=True)
        self.owner_a = make_owner("phase4-owner-a@example.com")
        self.owner_b = make_owner("phase4-owner-b@example.com")
        self.business_a = provision_business(
            owner=self.owner_a,
            name="Phase 4 Business A",
            plan=self.plan,
        )
        self.business_b = provision_business(
            owner=self.owner_b,
            name="Phase 4 Business B",
            plan=self.plan,
        )
        self.location_a = make_location(
            self.business_a,
            "P4-A",
            "Phase 4 Workshop A",
        )
        self.location_b = make_location(
            self.business_b,
            "P4-B",
            "Phase 4 Workshop B",
        )
        self.employee_a = make_employee(
            self.business_a,
            self.location_a,
            "P4-EMP-A",
        )
        self.employee_b = make_employee(
            self.business_b,
            self.location_b,
            "P4-EMP-B",
        )
        self.category_a1 = make_category(
            self.business_a,
            "Daraz",
            "DARAZ",
        )
        self.category_a2 = make_category(
            self.business_a,
            "Iron",
            "IRON",
        )
        self.category_b = make_category(
            self.business_b,
            "Other Tenant Category",
            "OTHER",
        )
        self.assignment_a1 = self.make_assignment(
            self.employee_a,
            self.category_a1,
        )
        self.assignment_a2 = self.make_assignment(
            self.employee_a,
            self.category_a2,
        )
        self.assignment_b = self.make_assignment(
            self.employee_b,
            self.category_b,
        )
        self.membership_a = self.business_a.memberships.get(user=self.owner_a)
        self.access_a = WmsUserAccess.objects.for_business(
            self.business_a
        ).get(membership=self.membership_a)
        self.client.force_login(self.owner_a)

    def make_assignment(self, employee, category):
        return WmsEmployeeCategoryAssignment.objects.create(
            business=employee.business,
            employee=employee,
            category=category,
            is_active=True,
        )

    def assignment_quantities(self, employee=None, first=10, second=9):
        employee = employee or self.employee_a
        assignments = list(
            employee.category_assignments.filter(
                is_active=True,
                category__is_active=True,
            ).order_by("category__display_order", "category__name")
        )
        values = (first, second)
        return {
            str(assignment.public_id): values[index]
            for index, assignment in enumerate(assignments)
        }

    def create_entry(
        self,
        *,
        employee=None,
        location=None,
        production_date=None,
        daily_total=10,
        quantities=None,
        business=None,
        user=None,
    ):
        employee = employee or self.employee_a
        location = location or employee.location
        return services.create_production_entry(
            business=business or employee.business,
            location=location,
            employee=employee,
            production_date=production_date or self.production_date,
            daily_total_pieces=daily_total,
            notes="Daily workshop entry.",
            assignment_quantities=(
                quantities
                if quantities is not None
                else self.assignment_quantities(employee)
            ),
            user=user or employee.business.owner,
        )

    def entry_payload(
        self,
        *,
        employee=None,
        location=None,
        production_date=None,
        daily_total=10,
        quantities=None,
    ):
        employee = employee or self.employee_a
        location = location or employee.location
        quantities = (
            quantities
            if quantities is not None
            else self.assignment_quantities(employee)
        )
        return {
            "employee": str(employee.public_id),
            "location": str(location.public_id),
            "production_date": (
                production_date or self.production_date
            ).isoformat(),
            "daily_total_pieces": str(daily_total),
            "notes": "Daily workshop entry.",
            **{
                f"quantity_{assignment_id}": str(quantity)
                for assignment_id, quantity in quantities.items()
            },
        }

    def correction_payload(
        self,
        entry,
        *,
        daily_total=11,
        quantity=8,
        reason="Corrected from signed production sheet.",
    ):
        return {
            "daily_total_pieces": str(daily_total),
            "notes": "Corrected workshop entry.",
            "correction_reason": reason,
            **{
                f"quantity_{line.public_id}": str(quantity)
                for line in entry.lines.all()
            },
        }


class WmsPhase4ModelTests(WmsPhase4Base):
    def test_entry_is_unique_per_tenant_employee_and_date(self):
        self.create_entry()
        with self.assertRaises(ValidationError):
            self.create_entry()

        other = self.create_entry(employee=self.employee_b)
        self.assertEqual(other.production_date, self.production_date)
        self.assertEqual(
            WmsProductionEntry.objects.filter(
                production_date=self.production_date
            ).count(),
            2,
        )

    def test_cross_tenant_employee_and_location_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_entry(
                employee=self.employee_b,
                business=self.business_a,
            )
        with self.assertRaises(ValidationError):
            self.create_entry(location=self.location_b)

    def test_employee_location_mismatch_is_rejected(self):
        other_location = make_location(
            self.business_a,
            "P4-A2",
            "Phase 4 Workshop A2",
        )
        with self.assertRaises(ValidationError):
            self.create_entry(location=other_location)

    def test_inactive_employee_and_location_are_rejected_for_new_entry(self):
        self.employee_a.is_active = False
        self.employee_a.save()
        with self.assertRaises(ValidationError):
            self.create_entry()
        self.employee_a.is_active = True
        self.employee_a.save()

        self.location_a.is_active = False
        self.location_a.save()
        with self.assertRaises(ValidationError):
            self.create_entry()

    def test_inactive_assignment_and_category_are_rejected(self):
        self.assignment_a1.is_active = False
        self.assignment_a1.save()
        crafted = self.assignment_quantities()
        crafted[str(self.assignment_a1.public_id)] = 5
        with self.assertRaises(ValidationError):
            self.create_entry(quantities=crafted)

        self.assignment_a1.is_active = True
        self.assignment_a1.save()
        self.category_a1.is_active = False
        self.category_a1.save()
        crafted = self.assignment_quantities()
        crafted[str(self.assignment_a1.public_id)] = 5
        with self.assertRaises(ValidationError):
            self.create_entry(quantities=crafted)

    def test_quantities_must_be_whole_nonnegative_numbers(self):
        negative = self.assignment_quantities()
        negative[str(self.assignment_a1.public_id)] = -1
        with self.assertRaises(ValidationError):
            self.create_entry(quantities=negative)

        decimal = self.assignment_quantities()
        decimal[str(self.assignment_a1.public_id)] = Decimal("1.5")
        with self.assertRaises(ValidationError):
            self.create_entry(quantities=decimal)

        with self.assertRaises(ValidationError):
            self.create_entry(daily_total=-1)

    def test_duplicate_assignment_or_category_line_is_prevented(self):
        entry = self.create_entry()
        duplicate = WmsProductionEntryLine(
            business=self.business_a,
            entry=entry,
            assignment=self.assignment_a1,
            category=self.category_a1,
            quantity=1,
        )
        with self.assertRaises(ValidationError):
            duplicate.save()

    def test_daily_total_is_preserved_independently_from_category_sum(self):
        entry = self.create_entry(
            daily_total=10,
            quantities=self.assignment_quantities(first=10, second=10),
        )

        self.assertEqual(entry.daily_total_pieces, 10)
        self.assertEqual(
            sum(entry.lines.values_list("quantity", flat=True)),
            20,
        )

    def test_entry_and_line_identity_are_immutable(self):
        entry = self.create_entry()
        entry.production_date += timedelta(days=1)
        with self.assertRaises(ValidationError):
            entry.save()
        entry.refresh_from_db()

        line = entry.lines.first()
        line.assignment = self.assignment_a2
        line.category = self.category_a2
        with self.assertRaises(ValidationError):
            line.save()

    def test_historical_entry_survives_deactivation_and_remains_correctable(self):
        entry = self.create_entry()
        original_label = entry.lines.get(
            assignment=self.assignment_a1
        ).category_name_snapshot
        self.assignment_a1.is_active = False
        self.assignment_a1.save()
        self.category_a1.name = "Renamed Daraz"
        self.category_a1.is_active = False
        self.category_a1.save()
        self.employee_a.is_active = False
        self.employee_a.save()
        self.location_a.is_active = False
        self.location_a.save()

        quantities = {
            str(line.public_id): 7 for line in entry.lines.all()
        }
        corrected = services.correct_production_entry(
            business=self.business_a,
            entry=entry,
            daily_total_pieces=7,
            notes="Historical correction.",
            line_quantities=quantities,
            correction_reason="Confirmed historical paper sheet.",
            user=self.owner_a,
        )

        self.assertTrue(corrected.is_corrected)
        self.assertEqual(
            corrected.lines.get(
                assignment=self.assignment_a1
            ).category_name_snapshot,
            original_label,
        )
        response = self.client.get(
            reverse(
                "wms:production_entry_detail",
                args=[entry.public_id],
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, original_label)

    def test_operational_references_and_lines_are_protected(self):
        entry = self.create_entry()
        with self.assertRaises(ProtectedError):
            entry.delete()
        with self.assertRaises(ProtectedError):
            self.employee_a.delete()
        with self.assertRaises(ProtectedError):
            self.location_a.delete()
        with self.assertRaises(ProtectedError):
            self.assignment_a1.delete()
        with self.assertRaises(ProtectedError):
            self.category_a1.delete()

    def test_compensation_change_does_not_rewrite_production(self):
        entry = self.create_entry()
        quantities_before = list(
            entry.lines.order_by("pk").values_list("quantity", flat=True)
        )
        self.employee_a.fixed_monthly_salary = Decimal("250.000")
        self.employee_a.save()

        entry.refresh_from_db()
        self.assertEqual(
            list(entry.lines.order_by("pk").values_list("quantity", flat=True)),
            quantities_before,
        )

    def test_entry_form_uses_only_valid_employee_assignments(self):
        form = ProductionEntryForm(
            self.business_a,
            self.access_a,
            selected_employee=self.employee_a,
        )

        field_names = {field.name for field in form.quantity_fields}
        self.assertEqual(
            field_names,
            {
                f"quantity_{self.assignment_a1.public_id}",
                f"quantity_{self.assignment_a2.public_id}",
            },
        )
        self.assertNotIn(
            f"quantity_{self.assignment_b.public_id}",
            field_names,
        )


class WmsPhase4ViewTests(WmsPhase4Base):
    def test_authorized_create_list_detail_and_filters(self):
        create_response = self.client.post(
            reverse("wms:production_entry_create"),
            self.entry_payload(),
        )
        entry = WmsProductionEntry.objects.for_business(self.business_a).get()
        self.assertRedirects(
            create_response,
            reverse(
                "wms:production_entry_detail",
                args=[entry.public_id],
            ),
        )

        response = self.client.get(
            reverse("wms:production_entry_list"),
            {
                "q": self.employee_a.employee_code,
                "date": self.production_date.isoformat(),
                "employee": self.employee_a.public_id,
                "location": self.location_a.public_id,
                "category": self.category_a1.public_id,
            },
        )
        self.assertContains(response, self.employee_a.full_name)
        self.assertEqual(response.context["record_count"], 1)
        self.assertContains(
            self.client.get(
                reverse(
                    "wms:production_entry_detail",
                    args=[entry.public_id],
                )
            ),
            "Daily Total Pieces",
        )

    def test_creation_audit_contains_tenant_employee_location_and_quantities(self):
        response = self.client.post(
            reverse("wms:production_entry_create"),
            self.entry_payload(daily_total=10),
        )
        self.assertEqual(response.status_code, 302)
        entry = WmsProductionEntry.objects.for_business(self.business_a).get()
        audit = AuditLog.objects.get(
            action="wms.production_entry_created",
            object_id=str(entry.public_id),
        )

        self.assertEqual(audit.business, self.business_a)
        self.assertEqual(audit.user, self.owner_a)
        self.assertEqual(
            audit.new_values["business_public_id"],
            str(self.business_a.public_id),
        )
        self.assertEqual(audit.new_values["daily_total_pieces"], 10)
        self.assertEqual(len(audit.new_values["category_quantities"]), 2)

    def test_correction_requires_reason_and_audits_old_and_new_values(self):
        entry = self.create_entry()
        correction_url = reverse(
            "wms:production_entry_correct",
            args=[entry.public_id],
        )
        invalid = self.client.post(
            correction_url,
            self.correction_payload(entry, reason=""),
        )
        self.assertEqual(invalid.status_code, 200)
        entry.refresh_from_db()
        self.assertFalse(entry.is_corrected)

        response = self.client.post(
            correction_url,
            self.correction_payload(entry, daily_total=11, quantity=8),
        )
        self.assertRedirects(
            response,
            reverse(
                "wms:production_entry_detail",
                args=[entry.public_id],
            ),
        )
        entry.refresh_from_db()
        self.assertTrue(entry.is_corrected)
        self.assertEqual(entry.daily_total_pieces, 11)
        self.assertEqual(
            set(entry.lines.values_list("quantity", flat=True)),
            {8},
        )
        correction = AuditLog.objects.get(
            action="wms.production_entry_corrected",
            object_id=str(entry.public_id),
        )
        self.assertEqual(correction.old_values["daily_total_pieces"], 10)
        self.assertEqual(correction.new_values["daily_total_pieces"], 11)
        self.assertTrue(
            AuditLog.objects.filter(
                action="wms.production_entry_updated",
                object_id=str(entry.public_id),
            ).exists()
        )

    def test_cross_tenant_entry_employee_location_and_assignment_ids_are_rejected(self):
        other_entry = self.create_entry(employee=self.employee_b)
        self.assertEqual(
            self.client.get(
                reverse(
                    "wms:production_entry_detail",
                    args=[other_entry.public_id],
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse(
                    "wms:production_entry_correct",
                    args=[other_entry.public_id],
                ),
                {"correction_reason": "Forged"},
            ).status_code,
            404,
        )

        payload = self.entry_payload()
        payload["employee"] = str(self.employee_b.public_id)
        payload["location"] = str(self.location_b.public_id)
        payload[f"quantity_{self.assignment_b.public_id}"] = "99"
        response = self.client.post(
            reverse("wms:production_entry_create"),
            payload,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            WmsProductionEntry.objects.for_business(self.business_a).exists()
        )

    def test_crafted_unassigned_category_id_is_rejected(self):
        unassigned = make_category(
            self.business_a,
            "Unassigned",
            "UNASSIGNED",
        )
        forged_assignment = self.make_assignment(
            make_employee(
                self.business_a,
                self.location_a,
                "P4-FORGED-EMP",
            ),
            unassigned,
        )
        payload = self.entry_payload()
        payload[f"quantity_{forged_assignment.public_id}"] = "100"

        response = self.client.post(
            reverse("wms:production_entry_create"),
            payload,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            WmsProductionEntry.objects.for_business(self.business_a).exists()
        )

    def test_explicit_location_scope_hides_other_location_production(self):
        other_location = make_location(
            self.business_a,
            "P4-A2",
            "Phase 4 Workshop A2",
        )
        other_employee = make_employee(
            self.business_a,
            other_location,
            "P4-OTHER-LOCATION",
        )
        other_category = make_category(
            self.business_a,
            "Other Location Work",
            "OTHER-LOC",
        )
        other_assignment = self.make_assignment(
            other_employee,
            other_category,
        )
        entry = self.create_entry(
            employee=other_employee,
            quantities={str(other_assignment.public_id): 3},
            daily_total=3,
        )
        self.access_a.allowed_locations.set([self.location_a])

        response = self.client.get(
            reverse("wms:production_entry_list"),
            {"date": self.production_date.isoformat()},
        )
        self.assertNotContains(response, other_employee.full_name)
        self.assertEqual(
            self.client.get(
                reverse(
                    "wms:production_entry_detail",
                    args=[entry.public_id],
                )
            ).status_code,
            404,
        )

    def test_view_manage_correct_permissions_and_navigation(self):
        viewer = make_owner("phase4-viewer@example.com")
        core_role = Role.objects.create(
            business=self.business_a,
            name="Phase 4 Viewer Core",
            permissions=[],
        )
        membership = Membership.objects.create(
            business=self.business_a,
            user=viewer,
            role=core_role,
        )
        view_role = WmsRole.objects.create(
            business=self.business_a,
            name="Phase 4 Production Viewer",
            code="phase4_production_viewer",
            permissions=["wms.production.view"],
        )
        core_services.save_user_access(
            business=self.business_a,
            membership=membership,
            role=view_role,
            user=self.owner_a,
        )
        self.client.force_login(viewer)

        response = self.client.get(reverse("wms:production_entry_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Production Entry")
        self.assertNotContains(response, "Add production")
        self.assertEqual(
            self.client.get(
                reverse("wms:production_entry_create")
            ).status_code,
            403,
        )

        entry = self.create_entry()
        self.assertEqual(
            self.client.get(
                reverse(
                    "wms:production_entry_correct",
                    args=[entry.public_id],
                )
            ).status_code,
            403,
        )

    def test_default_role_matrix_is_conservative(self):
        roles = {
            role.code: set(role.permissions)
            for role in WmsRole.objects.for_business(self.business_a).filter(
                is_system=True
            )
        }
        self.assertIn("wms.production.manage", roles["workshop_manager"])
        self.assertIn("wms.production.correct", roles["workshop_manager"])
        self.assertIn("wms.production.manage", roles["production_entry"])
        self.assertNotIn("wms.production.correct", roles["production_entry"])
        self.assertEqual(
            roles["attendance_manager"].intersection(
                {
                    "wms.production.view",
                    "wms.production.manage",
                    "wms.production.correct",
                }
            ),
            set(),
        )
        self.assertIn("wms.production.view", roles["report_viewer"])
        self.assertNotIn("wms.production.manage", roles["report_viewer"])

    def test_missing_access_disabled_entitlement_and_pos_only_are_denied(self):
        no_access_user = make_owner("phase4-no-access@example.com")
        core_role = Role.objects.create(
            business=self.business_a,
            name="Phase 4 No WMS",
            permissions=[],
        )
        Membership.objects.create(
            business=self.business_a,
            user=no_access_user,
            role=core_role,
        )
        self.client.force_login(no_access_user)
        self.assertEqual(
            self.client.get(
                reverse("wms:production_entry_list")
            ).status_code,
            403,
        )

        self.plan.feature_wms = False
        self.plan.save(update_fields=["feature_wms"])
        self.client.force_login(self.owner_a)
        self.assertEqual(
            self.client.get(
                reverse("wms:production_entry_list")
            ).status_code,
            403,
        )

        pos_plan = make_plan("Phase 4 POS Only", pos=True)
        pos_owner = make_owner("phase4-pos-only@example.com")
        provision_business(
            owner=pos_owner,
            name="Phase 4 POS Only",
            plan=pos_plan,
        )
        self.client.force_login(pos_owner)
        self.assertEqual(
            self.client.get(
                reverse("wms:production_entry_list")
            ).status_code,
            403,
        )

    def test_get_requests_do_not_mutate_production(self):
        entry = self.create_entry()
        original_updated_at = entry.updated_at
        quantities = list(
            entry.lines.order_by("pk").values_list("quantity", flat=True)
        )

        response = self.client.get(
            reverse(
                "wms:production_entry_correct",
                args=[entry.public_id],
            )
        )

        self.assertEqual(response.status_code, 200)
        entry.refresh_from_db()
        self.assertEqual(entry.updated_at, original_updated_at)
        self.assertFalse(entry.is_corrected)
        self.assertEqual(
            list(entry.lines.order_by("pk").values_list("quantity", flat=True)),
            quantities,
        )

    def test_csrf_is_required_for_create_and_correction(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner_a)
        self.assertEqual(
            csrf_client.post(
                reverse("wms:production_entry_create"),
                self.entry_payload(),
            ).status_code,
            403,
        )
        entry = self.create_entry()
        self.assertEqual(
            csrf_client.post(
                reverse(
                    "wms:production_entry_correct",
                    args=[entry.public_id],
                ),
                self.correction_payload(entry),
            ).status_code,
            403,
        )
