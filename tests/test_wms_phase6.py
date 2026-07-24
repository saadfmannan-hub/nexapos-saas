"""Focused regression coverage for WMS Phase 6 alterations."""

from datetime import date

from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase
from django.urls import reverse

from apps.accounts.models import Membership, Role
from apps.audit.models import AuditLog
from apps.tenants.services import provision_business
from apps.wms_alterations import selectors, services
from apps.wms_alterations.models import WmsAlteration
from apps.wms_attendance.models import WmsAttendance
from apps.wms_core import services as core_services
from apps.wms_core.models import WmsRole, WmsUserAccess
from apps.wms_production.models import WmsProductionEntry
from tests.test_wms_phase1 import make_owner, make_plan
from tests.test_wms_phase2 import make_employee, make_location


class WmsPhase6Base(TestCase):
    alteration_date = date(2026, 7, 27)

    def setUp(self):
        self.plan = make_plan("Phase 6 WMS", wms=True)
        self.owner_a = make_owner("phase6-owner-a@example.com")
        self.owner_b = make_owner("phase6-owner-b@example.com")
        self.business_a = provision_business(
            owner=self.owner_a,
            name="Phase 6 Business A",
            plan=self.plan,
        )
        self.business_b = provision_business(
            owner=self.owner_b,
            name="Phase 6 Business B",
            plan=self.plan,
        )
        self.location_a = make_location(
            self.business_a,
            "P6-A",
            "Phase 6 Workshop A",
        )
        self.location_b = make_location(
            self.business_b,
            "P6-B",
            "Phase 6 Workshop B",
        )
        self.assigned_a = make_employee(
            self.business_a,
            self.location_a,
            "P6-ASSIGNED-A",
        )
        self.mistake_a = make_employee(
            self.business_a,
            self.location_a,
            "P6-MISTAKE-A",
        )
        self.assigned_b = make_employee(
            self.business_b,
            self.location_b,
            "P6-ASSIGNED-B",
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

    def alteration_data(self, **overrides):
        data = {
            "location": self.location_a,
            "original_order_reference": "MB-008",
            "alteration_reference": "ALT-001",
            "reason": WmsAlteration.Reason.SIZE,
            "mistake_by": WmsAlteration.MistakeBy.EMPLOYEE,
            "mistake_by_employee": self.mistake_a,
            "assigned_employee": self.assigned_a,
            "alteration_date": self.alteration_date,
            "notes": "Operational rework only.",
        }
        data.update(overrides)
        return data

    def create_alteration(
        self,
        *,
        business=None,
        access=None,
        user=None,
        **overrides,
    ):
        business = business or self.business_a
        return services.create_alteration(
            business=business,
            user_access=access or self.access_a,
            cleaned_data=self.alteration_data(**overrides),
            user=user or business.owner,
        )

    def create_payload(self, **overrides):
        payload = {
            "location": str(self.location_a.public_id),
            "original_order_reference": "MB-008",
            "alteration_reference": "ALT-001",
            "reason": WmsAlteration.Reason.SIZE,
            "mistake_by": WmsAlteration.MistakeBy.EMPLOYEE,
            "mistake_by_employee": str(self.mistake_a.public_id),
            "assigned_employee": str(self.assigned_a.public_id),
            "alteration_date": self.alteration_date.isoformat(),
            "notes": "Operational rework only.",
        }
        payload.update(overrides)
        return payload

    def correction_data(self, alteration, **overrides):
        data = {
            "location": alteration.location,
            "reason": WmsAlteration.Reason.FINISHING,
            "mistake_by": WmsAlteration.MistakeBy.CUSTOMER,
            "mistake_by_employee": None,
            "assigned_employee": alteration.assigned_employee,
            "alteration_date": alteration.alteration_date,
            "status": alteration.status,
            "notes": "Corrected operational notes.",
            "correction_reason": "Verified against the workshop card.",
        }
        data.update(overrides)
        return data

    def correction_payload(self, alteration, **overrides):
        payload = {
            "location": str(alteration.location.public_id),
            "reason": WmsAlteration.Reason.FINISHING,
            "mistake_by": WmsAlteration.MistakeBy.CUSTOMER,
            "mistake_by_employee": "",
            "assigned_employee": str(
                alteration.assigned_employee.public_id
            ),
            "alteration_date": alteration.alteration_date.isoformat(),
            "status": alteration.status,
            "notes": "Corrected operational notes.",
            "correction_reason": "Verified against the workshop card.",
        }
        payload.update(overrides)
        return payload


class WmsPhase6ModelAndServiceTests(WmsPhase6Base):
    def test_reason_mistake_and_status_constants_are_exact(self):
        self.assertEqual(
            [label for _value, label in WmsAlteration.Reason.choices],
            [
                "Size",
                "Daraz",
                "Finishing",
                "Button",
                "VIP Design",
                "Computer Design",
                "Iron",
                "Other",
            ],
        )
        self.assertEqual(
            [label for _value, label in WmsAlteration.MistakeBy.choices],
            ["Employee", "Customer", "Unknown"],
        )
        self.assertEqual(
            [label for _value, label in WmsAlteration.Status.choices],
            ["Open", "In Progress", "Completed"],
        )

    def test_duplicate_original_order_references_are_allowed_and_independent(self):
        first = self.create_alteration(
            original_order_reference="  mb-008  ",
        )
        second = self.create_alteration(
            original_order_reference="mb-008",
            alteration_reference="ALT-002",
            reason=WmsAlteration.Reason.IRON,
        )

        self.assertEqual(first.original_order_reference, "MB-008")
        self.assertEqual(second.original_order_reference, "MB-008")
        self.assertNotEqual(first.public_id, second.public_id)
        self.assertEqual(
            WmsAlteration.objects.for_business(self.business_a).count(),
            2,
        )

    def test_mistake_by_employee_consistency_is_enforced(self):
        invalid_customer = WmsAlteration(
            business=self.business_a,
            location=self.location_a,
            original_order_reference="MB-009",
            reason=WmsAlteration.Reason.BUTTON,
            mistake_by=WmsAlteration.MistakeBy.CUSTOMER,
            mistake_by_employee=self.mistake_a,
            assigned_employee=self.assigned_a,
            alteration_date=self.alteration_date,
        )
        with self.assertRaises(ValidationError):
            invalid_customer.save()

        invalid_employee = WmsAlteration(
            business=self.business_a,
            location=self.location_a,
            original_order_reference="MB-010",
            reason=WmsAlteration.Reason.BUTTON,
            mistake_by=WmsAlteration.MistakeBy.EMPLOYEE,
            assigned_employee=self.assigned_a,
            alteration_date=self.alteration_date,
        )
        with self.assertRaises(ValidationError):
            invalid_employee.save()

    def test_cross_tenant_and_cross_location_employees_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_alteration(
                assigned_employee=self.assigned_b,
            )
        with self.assertRaises(ValidationError):
            self.create_alteration(
                mistake_by_employee=self.assigned_b,
            )

        other_location = make_location(
            self.business_a,
            "P6-A2",
            "Phase 6 Workshop A2",
        )
        other_employee = make_employee(
            self.business_a,
            other_location,
            "P6-OTHER-A",
        )
        with self.assertRaises(ValidationError):
            self.create_alteration(assigned_employee=other_employee)

    def test_inactive_employee_and_location_are_rejected(self):
        self.assigned_a.is_active = False
        self.assigned_a.save()
        with self.assertRaises(ValidationError):
            self.create_alteration()
        self.assigned_a.is_active = True
        self.assigned_a.save()

        self.location_a.is_active = False
        self.location_a.save()
        with self.assertRaises(ValidationError):
            self.create_alteration()

    def test_explicit_location_scope_is_revalidated_by_service(self):
        other_location = make_location(
            self.business_a,
            "P6-SCOPE",
            "Phase 6 Scope Workshop",
        )
        other_employee = make_employee(
            self.business_a,
            other_location,
            "P6-SCOPE-EMP",
        )
        self.access_a.allowed_locations.set([self.location_a])

        with self.assertRaises(ValidationError):
            self.create_alteration(
                location=other_location,
                assigned_employee=other_employee,
                mistake_by=WmsAlteration.MistakeBy.UNKNOWN,
                mistake_by_employee=None,
            )

    def test_operational_references_are_immutable(self):
        alteration = self.create_alteration()
        alteration.original_order_reference = "AH-999"
        with self.assertRaises(ValidationError):
            alteration.save()
        alteration.refresh_from_db()

        alteration.alteration_reference = "ALT-999"
        with self.assertRaises(ValidationError):
            alteration.save()

    def test_location_and_employee_references_protect_history(self):
        self.create_alteration()
        with self.assertRaises(ProtectedError):
            self.location_a.delete()
        with self.assertRaises(ProtectedError):
            self.assigned_a.delete()
        with self.assertRaises(ProtectedError):
            self.mistake_a.delete()

    def test_creation_audit_and_no_production_or_attendance_side_effects(self):
        production_before = WmsProductionEntry.objects.count()
        attendance_before = WmsAttendance.objects.count()

        alteration = self.create_alteration()

        self.assertEqual(WmsProductionEntry.objects.count(), production_before)
        self.assertEqual(WmsAttendance.objects.count(), attendance_before)
        log = AuditLog.objects.get(
            business=self.business_a,
            action="wms.alteration_created",
            object_id=str(alteration.public_id),
        )
        self.assertEqual(
            log.new_values["original_order_reference"],
            "MB-008",
        )
        self.assertEqual(
            log.new_values["assigned_employee_public_id"],
            str(self.assigned_a.public_id),
        )

    def test_correction_requires_reason_and_audits_old_and_new_values(self):
        alteration = self.create_alteration()
        with self.assertRaises(ValidationError):
            services.correct_alteration(
                business=self.business_a,
                user_access=self.access_a,
                alteration=alteration,
                cleaned_data=self.correction_data(
                    alteration,
                    correction_reason="",
                ),
                user=self.owner_a,
            )
        alteration.refresh_from_db()
        self.assertFalse(alteration.is_corrected)

        alteration = services.correct_alteration(
            business=self.business_a,
            user_access=self.access_a,
            alteration=alteration,
            cleaned_data=self.correction_data(
                alteration,
                status=WmsAlteration.Status.IN_PROGRESS,
            ),
            user=self.owner_a,
        )
        self.assertTrue(alteration.is_corrected)
        self.assertEqual(alteration.reason, WmsAlteration.Reason.FINISHING)
        self.assertEqual(
            alteration.status,
            WmsAlteration.Status.IN_PROGRESS,
        )
        log = AuditLog.objects.get(
            action="wms.alteration_updated",
            object_id=str(alteration.public_id),
        )
        self.assertEqual(log.old_values["reason"], WmsAlteration.Reason.SIZE)
        self.assertEqual(
            log.new_values["reason"],
            WmsAlteration.Reason.FINISHING,
        )
        self.assertEqual(
            log.new_values["correction_reason"],
            "Verified against the workshop card.",
        )

    def test_lifecycle_is_one_way_and_completion_is_audited(self):
        alteration = self.create_alteration()
        production_before = WmsProductionEntry.objects.count()
        attendance_before = WmsAttendance.objects.count()
        with self.assertRaises(ValidationError):
            services.complete_alteration(
                business=self.business_a,
                user_access=self.access_a,
                alteration=alteration,
                user=self.owner_a,
            )

        alteration = services.correct_alteration(
            business=self.business_a,
            user_access=self.access_a,
            alteration=alteration,
            cleaned_data=self.correction_data(
                alteration,
                status=WmsAlteration.Status.IN_PROGRESS,
            ),
            user=self.owner_a,
        )
        alteration = services.complete_alteration(
            business=self.business_a,
            user_access=self.access_a,
            alteration=alteration,
            user=self.owner_a,
        )
        self.assertEqual(alteration.status, WmsAlteration.Status.COMPLETED)
        self.assertIsNotNone(alteration.completed_at)
        self.assertEqual(alteration.completed_by, self.owner_a)
        log = AuditLog.objects.get(
            action="wms.alteration_completed",
            object_id=str(alteration.public_id),
        )
        self.assertEqual(
            log.old_values["status"],
            WmsAlteration.Status.IN_PROGRESS,
        )
        self.assertEqual(
            log.new_values["status"],
            WmsAlteration.Status.COMPLETED,
        )
        self.assertEqual(WmsProductionEntry.objects.count(), production_before)
        self.assertEqual(WmsAttendance.objects.count(), attendance_before)

        alteration.status = WmsAlteration.Status.IN_PROGRESS
        alteration.completed_at = None
        alteration.completed_by = None
        with self.assertRaises(ValidationError):
            alteration.save()

    def test_inactive_employee_remains_visible_historically(self):
        alteration = self.create_alteration()
        self.assigned_a.is_active = False
        self.assigned_a.save()

        visible = selectors.alterations_for_access(self.access_a)
        self.assertTrue(visible.filter(pk=alteration.pk).exists())
        response = self.client.get(
            reverse("wms:alteration_detail", args=[alteration.public_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Inactive")


class WmsPhase6ViewAndSecurityTests(WmsPhase6Base):
    def test_authorized_create_list_detail_and_all_filters(self):
        response = self.client.post(
            reverse("wms:alteration_create"),
            self.create_payload(status=WmsAlteration.Status.COMPLETED),
        )
        alteration = WmsAlteration.objects.for_business(
            self.business_a
        ).get()
        self.assertRedirects(
            response,
            reverse("wms:alteration_detail", args=[alteration.public_id]),
        )
        self.assertEqual(alteration.status, WmsAlteration.Status.OPEN)

        response = self.client.get(
            reverse("wms:alteration_list"),
            {
                "q": "Operational",
                "order_reference": "MB-008",
                "date": self.alteration_date.isoformat(),
                "employee": self.assigned_a.public_id,
                "location": self.location_a.public_id,
                "status": WmsAlteration.Status.OPEN,
                "reason": WmsAlteration.Reason.SIZE,
            },
        )
        self.assertEqual(response.context["record_count"], 1)
        self.assertContains(response, "MB-008")
        self.assertContains(
            self.client.get(
                reverse(
                    "wms:alteration_detail",
                    args=[alteration.public_id],
                )
            ),
            "operational rework only",
        )

    def test_crafted_reference_fields_cannot_change_on_edit(self):
        alteration = self.create_alteration()
        payload = self.correction_payload(
            alteration,
            status=WmsAlteration.Status.IN_PROGRESS,
            original_order_reference="FORGED-ORDER",
            alteration_reference="FORGED-ALT",
        )
        response = self.client.post(
            reverse("wms:alteration_edit", args=[alteration.public_id]),
            payload,
        )
        self.assertRedirects(
            response,
            reverse("wms:alteration_detail", args=[alteration.public_id]),
        )
        alteration.refresh_from_db()
        self.assertEqual(alteration.original_order_reference, "MB-008")
        self.assertEqual(alteration.alteration_reference, "ALT-001")

    def test_cross_tenant_ids_and_crafted_employees_are_rejected(self):
        other = services.create_alteration(
            business=self.business_b,
            user_access=self.access_b,
            cleaned_data={
                "location": self.location_b,
                "original_order_reference": "B-001",
                "alteration_reference": "",
                "reason": WmsAlteration.Reason.DARAZ,
                "mistake_by": WmsAlteration.MistakeBy.UNKNOWN,
                "mistake_by_employee": None,
                "assigned_employee": self.assigned_b,
                "alteration_date": self.alteration_date,
                "notes": "",
            },
            user=self.owner_b,
        )
        self.assertEqual(
            self.client.get(
                reverse("wms:alteration_detail", args=[other.public_id])
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("wms:alteration_edit", args=[other.public_id])
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse("wms:alteration_complete", args=[other.public_id])
            ).status_code,
            404,
        )

        response = self.client.post(
            reverse("wms:alteration_create"),
            self.create_payload(
                location=str(self.location_b.public_id),
                assigned_employee=str(self.assigned_b.public_id),
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            WmsAlteration.objects.for_business(self.business_a).exists()
        )

    def test_explicit_location_scope_hides_other_location_history(self):
        other_location = make_location(
            self.business_a,
            "P6-HIDDEN",
            "Phase 6 Hidden Workshop",
        )
        other_employee = make_employee(
            self.business_a,
            other_location,
            "P6-HIDDEN-EMP",
        )
        alteration = self.create_alteration(
            location=other_location,
            assigned_employee=other_employee,
            original_order_reference="HIDDEN-900",
            mistake_by=WmsAlteration.MistakeBy.UNKNOWN,
            mistake_by_employee=None,
        )
        self.access_a.allowed_locations.set([self.location_a])

        response = self.client.get(reverse("wms:alteration_list"))
        self.assertNotContains(response, alteration.original_order_reference)
        self.assertEqual(
            self.client.get(
                reverse(
                    "wms:alteration_detail",
                    args=[alteration.public_id],
                )
            ).status_code,
            404,
        )

    def test_view_manage_complete_permissions_and_navigation(self):
        viewer = make_owner("phase6-viewer@example.com")
        core_role = Role.objects.create(
            business=self.business_a,
            name="Phase 6 Viewer Core",
            permissions=[],
        )
        membership = Membership.objects.create(
            business=self.business_a,
            user=viewer,
            role=core_role,
        )
        view_role = WmsRole.objects.create(
            business=self.business_a,
            name="Phase 6 Alteration Viewer",
            code="phase6_alteration_viewer",
            permissions=["wms.alterations.view"],
        )
        core_services.save_user_access(
            business=self.business_a,
            membership=membership,
            role=view_role,
            user=self.owner_a,
        )
        alteration = self.create_alteration()
        self.client.force_login(viewer)

        response = self.client.get(reverse("wms:alteration_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Alterations")
        self.assertNotContains(response, "Add Alteration")
        self.assertEqual(
            self.client.get(reverse("wms:alteration_create")).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                reverse("wms:alteration_edit", args=[alteration.public_id])
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse(
                    "wms:alteration_complete",
                    args=[alteration.public_id],
                )
            ).status_code,
            403,
        )

    def test_manage_permission_does_not_grant_completion(self):
        manager = make_owner("phase6-manager@example.com")
        core_role = Role.objects.create(
            business=self.business_a,
            name="Phase 6 Manager Core",
            permissions=[],
        )
        membership = Membership.objects.create(
            business=self.business_a,
            user=manager,
            role=core_role,
        )
        manage_role = WmsRole.objects.create(
            business=self.business_a,
            name="Phase 6 Alteration Manager",
            code="phase6_alteration_manager",
            permissions=[
                "wms.alterations.view",
                "wms.alterations.manage",
            ],
        )
        core_services.save_user_access(
            business=self.business_a,
            membership=membership,
            role=manage_role,
            user=self.owner_a,
        )
        alteration = self.create_alteration()
        alteration = services.correct_alteration(
            business=self.business_a,
            user_access=self.access_a,
            alteration=alteration,
            cleaned_data=self.correction_data(
                alteration,
                status=WmsAlteration.Status.IN_PROGRESS,
            ),
            user=self.owner_a,
        )
        self.client.force_login(manager)

        self.assertEqual(
            self.client.get(
                reverse("wms:alteration_edit", args=[alteration.public_id])
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                reverse(
                    "wms:alteration_complete",
                    args=[alteration.public_id],
                )
            ).status_code,
            403,
        )
        alteration.refresh_from_db()
        self.assertEqual(
            alteration.status,
            WmsAlteration.Status.IN_PROGRESS,
        )

    def test_default_role_matrix_is_conservative(self):
        roles = {
            role.code: set(role.permissions)
            for role in WmsRole.objects.for_business(self.business_a).filter(
                is_system=True
            )
        }
        self.assertIn(
            "wms.alterations.complete",
            roles["owner_admin"],
        )
        self.assertIn(
            "wms.alterations.complete",
            roles["workshop_manager"],
        )
        self.assertIn("wms.alterations.view", roles["report_viewer"])
        self.assertNotIn("wms.alterations.manage", roles["report_viewer"])
        self.assertNotIn("wms.alterations.complete", roles["report_viewer"])
        self.assertFalse(
            roles["production_entry"].intersection(
                {
                    "wms.alterations.view",
                    "wms.alterations.manage",
                    "wms.alterations.complete",
                }
            )
        )
        self.assertFalse(
            roles["attendance_manager"].intersection(
                {
                    "wms.alterations.view",
                    "wms.alterations.manage",
                    "wms.alterations.complete",
                }
            )
        )

    def test_get_requests_do_not_mutate_and_completion_is_post_only(self):
        alteration = self.create_alteration()
        alteration = services.correct_alteration(
            business=self.business_a,
            user_access=self.access_a,
            alteration=alteration,
            cleaned_data=self.correction_data(
                alteration,
                status=WmsAlteration.Status.IN_PROGRESS,
            ),
            user=self.owner_a,
        )
        original_updated_at = alteration.updated_at

        self.assertEqual(
            self.client.get(
                reverse("wms:alteration_edit", args=[alteration.public_id])
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                reverse(
                    "wms:alteration_complete",
                    args=[alteration.public_id],
                )
            ).status_code,
            405,
        )
        alteration.refresh_from_db()
        self.assertEqual(
            alteration.status,
            WmsAlteration.Status.IN_PROGRESS,
        )
        self.assertEqual(alteration.updated_at, original_updated_at)

    def test_csrf_is_required_for_create_edit_and_completion(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner_a)
        self.assertEqual(
            csrf_client.post(
                reverse("wms:alteration_create"),
                self.create_payload(),
            ).status_code,
            403,
        )
        alteration = self.create_alteration()
        self.assertEqual(
            csrf_client.post(
                reverse("wms:alteration_edit", args=[alteration.public_id]),
                self.correction_payload(alteration),
            ).status_code,
            403,
        )
        self.assertEqual(
            csrf_client.post(
                reverse(
                    "wms:alteration_complete",
                    args=[alteration.public_id],
                )
            ).status_code,
            403,
        )

    def test_missing_access_disabled_entitlement_and_pos_only_are_denied(self):
        no_access = make_owner("phase6-no-access@example.com")
        core_role = Role.objects.create(
            business=self.business_a,
            name="Phase 6 No Access",
            permissions=[],
        )
        Membership.objects.create(
            business=self.business_a,
            user=no_access,
            role=core_role,
        )
        self.client.force_login(no_access)
        self.assertEqual(
            self.client.get(reverse("wms:alteration_list")).status_code,
            403,
        )

        self.plan.feature_wms = False
        self.plan.save(update_fields=["feature_wms"])
        self.client.force_login(self.owner_a)
        self.assertEqual(
            self.client.get(reverse("wms:alteration_list")).status_code,
            403,
        )

        pos_plan = make_plan("Phase 6 POS Only", pos=True)
        pos_owner = make_owner("phase6-pos-only@example.com")
        provision_business(
            owner=pos_owner,
            name="Phase 6 POS Only",
            plan=pos_plan,
        )
        self.client.force_login(pos_owner)
        self.assertEqual(
            self.client.get(reverse("wms:alteration_list")).status_code,
            403,
        )
