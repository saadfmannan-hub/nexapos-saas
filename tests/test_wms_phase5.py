"""Focused regression coverage for WMS Phase 5 workshop orders."""

from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase
from django.urls import NoReverseMatch, reverse

from apps.accounts.models import Membership, Role
from apps.audit.models import AuditLog
from apps.tenants.services import provision_business
from apps.wms_core import services as core_services
from apps.wms_core.models import WmsRole, WmsUserAccess
from apps.wms_orders import services
from apps.wms_orders.models import (
    WmsWorkshopOrder,
    WmsWorkshopOrderStatusHistory,
)
from tests.test_wms_phase1 import make_owner, make_plan
from tests.test_wms_phase2 import make_location


class WmsPhase5Base(TestCase):
    received_date = date(2026, 7, 25)
    finished_date = date(2026, 7, 26)

    def setUp(self):
        self.plan = make_plan("Phase 5 WMS", wms=True)
        self.owner_a = make_owner("phase5-owner-a@example.com")
        self.owner_b = make_owner("phase5-owner-b@example.com")
        self.business_a = provision_business(
            owner=self.owner_a,
            name="Phase 5 Business A",
            plan=self.plan,
        )
        self.business_b = provision_business(
            owner=self.owner_b,
            name="Phase 5 Business B",
            plan=self.plan,
        )
        self.location_a = make_location(
            self.business_a,
            "P5-A",
            "Phase 5 Workshop A",
        )
        self.location_b = make_location(
            self.business_b,
            "P5-B",
            "Phase 5 Workshop B",
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

    def create_batch(
        self,
        references=("MB-008",),
        *,
        business=None,
        access=None,
        location=None,
        received_date=None,
        user=None,
    ):
        business = business or self.business_a
        return services.create_order_batch(
            business=business,
            user_access=access or self.access_a,
            location=location or self.location_a,
            received_date=received_date or self.received_date,
            references=references,
            notes="Operational workshop notes.",
            user=user or business.owner,
        )

    def create_payload(
        self,
        references="MB-008",
        *,
        location=None,
        received_date=None,
    ):
        return {
            "location": str((location or self.location_a).public_id),
            "received_date": (
                received_date or self.received_date
            ).isoformat(),
            "references": references,
            "notes": "Operational workshop notes.",
            "status": WmsWorkshopOrder.Status.FINISHED_READY,
        }

    def finish_payload(self, references="MB-008", *, finished_date=None):
        return {
            "finished_date": (
                finished_date or self.finished_date
            ).isoformat(),
            "references": references,
            "status": WmsWorkshopOrder.Status.IN_PROCESS,
        }


class WmsPhase5ModelTests(WmsPhase5Base):
    def test_reference_is_normalized_and_case_insensitively_unique_per_tenant(self):
        order = self.create_batch(("  mb-008  ",))[0]
        self.assertEqual(order.order_reference, "MB-008")

        with self.assertRaises(ValidationError):
            self.create_batch(("mb-008",))
        other = self.create_batch(
            ("mb-008",),
            business=self.business_b,
            access=self.access_b,
            location=self.location_b,
            user=self.owner_b,
        )[0]
        self.assertEqual(other.order_reference, "MB-008")

    def test_only_approved_statuses_and_finished_date_combinations_are_valid(self):
        invalid = WmsWorkshopOrder(
            business=self.business_a,
            location=self.location_a,
            order_reference="MB-009",
            status="PENDING",
            received_date=self.received_date,
        )
        with self.assertRaises(ValidationError):
            invalid.save()

        invalid.status = WmsWorkshopOrder.Status.IN_PROCESS
        invalid.finished_date = self.finished_date
        with self.assertRaises(ValidationError):
            invalid.save()

        invalid.status = WmsWorkshopOrder.Status.FINISHED_READY
        invalid.finished_date = None
        with self.assertRaises(ValidationError):
            invalid.save()

    def test_order_identity_and_finished_status_cannot_be_rolled_back(self):
        order = self.create_batch()[0]
        order.order_reference = "AH-999"
        with self.assertRaises(ValidationError):
            order.save()
        order.refresh_from_db()

        services.finish_order_batch(
            business=self.business_a,
            user_access=self.access_a,
            finished_date=self.finished_date,
            references=[order.order_reference],
            user=self.owner_a,
        )
        order.refresh_from_db()
        order.status = WmsWorkshopOrder.Status.IN_PROCESS
        order.finished_date = None
        with self.assertRaises(ValidationError):
            order.save()

    def test_location_and_status_history_protect_operational_history(self):
        order = self.create_batch()[0]
        history = order.status_history.get()
        with self.assertRaises(ProtectedError):
            self.location_a.delete()
        with self.assertRaises(ProtectedError):
            order.delete()

        history.reason = "Changed"
        with self.assertRaises(ValidationError):
            history.save()
        with self.assertRaises(ValidationError):
            history.delete()

    def test_status_history_allows_only_creation_and_finish_transitions(self):
        order = self.create_batch()[0]
        invalid = WmsWorkshopOrderStatusHistory(
            business=self.business_a,
            order=order,
            previous_status=WmsWorkshopOrder.Status.FINISHED_READY,
            new_status=WmsWorkshopOrder.Status.IN_PROCESS,
        )
        with self.assertRaises(ValidationError):
            invalid.save()


class WmsPhase5CreationTests(WmsPhase5Base):
    def test_single_and_batch_creation_are_in_process_with_history_and_audit(self):
        orders = self.create_batch(("mb-008", " MB-009 ", "AH-006"))

        self.assertEqual(len(orders), 3)
        self.assertEqual(
            {order.status for order in orders},
            {WmsWorkshopOrder.Status.IN_PROCESS},
        )
        self.assertTrue(all(order.finished_date is None for order in orders))
        self.assertEqual(
            WmsWorkshopOrderStatusHistory.objects.for_business(
                self.business_a
            ).count(),
            3,
        )
        self.assertEqual(
            AuditLog.objects.filter(
                business=self.business_a,
                action="wms.order_created",
            ).count(),
            3,
        )
        batch_audit = AuditLog.objects.get(
            business=self.business_a,
            action="wms.order_batch_created",
        )
        self.assertEqual(batch_audit.new_values["batch_count"], 3)
        self.assertEqual(
            batch_audit.new_values["new_status"],
            WmsWorkshopOrder.Status.IN_PROCESS,
        )

    def test_duplicate_batch_and_existing_reference_reject_without_partial_create(self):
        with self.assertRaises(ValidationError):
            self.create_batch(("MB-008", "mb-008"))
        self.assertFalse(
            WmsWorkshopOrder.objects.for_business(self.business_a).exists()
        )

        self.create_batch(("MB-008",))
        with self.assertRaises(ValidationError):
            self.create_batch(("MB-009", "mb-008"))
        self.assertEqual(
            list(
                WmsWorkshopOrder.objects.for_business(
                    self.business_a
                ).values_list("order_reference", flat=True)
            ),
            ["MB-008"],
        )

    def test_cross_tenant_and_inactive_locations_reject_without_partial_create(self):
        with self.assertRaises(ValidationError):
            self.create_batch(("MB-008",), location=self.location_b)
        self.assertFalse(
            WmsWorkshopOrder.objects.for_business(self.business_a).exists()
        )

        self.location_a.is_active = False
        self.location_a.save()
        with self.assertRaises(ValidationError):
            self.create_batch(("MB-008", "MB-009"))
        self.assertFalse(
            WmsWorkshopOrder.objects.for_business(self.business_a).exists()
        )

    def test_explicit_location_scope_is_revalidated_by_service(self):
        other_location = make_location(
            self.business_a,
            "P5-A2",
            "Phase 5 Workshop A2",
        )
        self.access_a.allowed_locations.set([self.location_a])
        with self.assertRaises(ValidationError):
            self.create_batch(
                ("AH-006",),
                location=other_location,
            )


class WmsPhase5FinishTests(WmsPhase5Base):
    def test_single_and_batch_finish_store_date_history_and_audit(self):
        self.create_batch(("MB-008", "MB-009"))
        orders = services.finish_order_batch(
            business=self.business_a,
            user_access=self.access_a,
            finished_date=self.finished_date,
            references=["MB-008", "MB-009"],
            user=self.owner_a,
        )

        self.assertEqual(len(orders), 2)
        self.assertTrue(
            all(
                order.status == WmsWorkshopOrder.Status.FINISHED_READY
                and order.finished_date == self.finished_date
                for order in orders
            )
        )
        self.assertEqual(
            WmsWorkshopOrderStatusHistory.objects.for_business(
                self.business_a
            ).count(),
            4,
        )
        self.assertEqual(
            AuditLog.objects.filter(
                business=self.business_a,
                action="wms.order_finished",
            ).count(),
            2,
        )
        batch = AuditLog.objects.get(
            business=self.business_a,
            action="wms.order_batch_finished",
        )
        self.assertEqual(batch.new_values["batch_count"], 2)

    def test_unknown_reference_rejects_entire_batch(self):
        self.create_batch(("MB-008", "MB-009"))
        with self.assertRaises(ValidationError):
            services.finish_order_batch(
                business=self.business_a,
                user_access=self.access_a,
                finished_date=self.finished_date,
                references=["MB-008", "UNKNOWN"],
                user=self.owner_a,
            )
        self.assertFalse(
            WmsWorkshopOrder.objects.for_business(self.business_a).filter(
                status=WmsWorkshopOrder.Status.FINISHED_READY
            ).exists()
        )

    def test_already_finished_reference_rejects_entire_batch(self):
        self.create_batch(("MB-008", "MB-009"))
        services.finish_order_batch(
            business=self.business_a,
            user_access=self.access_a,
            finished_date=self.finished_date,
            references=["MB-008"],
            user=self.owner_a,
        )
        with self.assertRaises(ValidationError):
            services.finish_order_batch(
                business=self.business_a,
                user_access=self.access_a,
                finished_date=self.finished_date,
                references=["MB-008", "MB-009"],
                user=self.owner_a,
            )
        self.assertEqual(
            WmsWorkshopOrder.objects.for_business(self.business_a).get(
                order_reference="MB-009"
            ).status,
            WmsWorkshopOrder.Status.IN_PROCESS,
        )

    def test_cross_tenant_reference_is_unknown_and_cannot_be_finished(self):
        other = self.create_batch(
            ("AH-006",),
            business=self.business_b,
            access=self.access_b,
            location=self.location_b,
            user=self.owner_b,
        )[0]
        with self.assertRaisesMessage(
            ValidationError,
            "Unknown or unavailable",
        ):
            services.finish_order_batch(
                business=self.business_a,
                user_access=self.access_a,
                finished_date=self.finished_date,
                references=["AH-006"],
                user=self.owner_a,
            )
        other.refresh_from_db()
        self.assertEqual(other.status, WmsWorkshopOrder.Status.IN_PROCESS)

    def test_unauthorized_location_and_invalid_date_reject_without_partial_finish(self):
        other_location = make_location(
            self.business_a,
            "P5-A2",
            "Phase 5 Workshop A2",
        )
        order = self.create_batch(
            ("AH-006",),
            location=other_location,
        )[0]
        self.access_a.allowed_locations.set([self.location_a])
        with self.assertRaises(ValidationError):
            services.finish_order_batch(
                business=self.business_a,
                user_access=self.access_a,
                finished_date=self.finished_date,
                references=["AH-006"],
                user=self.owner_a,
            )
        order.refresh_from_db()
        self.assertEqual(order.status, WmsWorkshopOrder.Status.IN_PROCESS)

        local = self.create_batch(("MB-008",))[0]
        with self.assertRaises(ValidationError):
            services.finish_order_batch(
                business=self.business_a,
                user_access=self.access_a,
                finished_date=self.received_date - timedelta(days=1),
                references=["MB-008"],
                user=self.owner_a,
            )
        local.refresh_from_db()
        self.assertEqual(local.status, WmsWorkshopOrder.Status.IN_PROCESS)


class WmsPhase5ViewPermissionTests(WmsPhase5Base):
    def test_authorized_create_list_detail_finish_and_filters(self):
        create_response = self.client.post(
            reverse("wms:order_create_batch"),
            self.create_payload("mb-008\nMB-009"),
        )
        self.assertRedirects(create_response, reverse("wms:order_list"))
        order = WmsWorkshopOrder.objects.for_business(self.business_a).get(
            order_reference="MB-008"
        )

        response = self.client.get(
            reverse("wms:order_list"),
            {
                "q": "MB-008",
                "date": self.received_date.isoformat(),
                "location": self.location_a.public_id,
                "status": WmsWorkshopOrder.Status.IN_PROCESS,
            },
        )
        self.assertContains(response, "MB-008")
        self.assertEqual(response.context["record_count"], 1)
        self.assertContains(
            self.client.get(
                reverse("wms:order_detail", args=[order.public_id])
            ),
            "Status history",
        )

        finish_response = self.client.post(
            reverse("wms:order_finish_batch"),
            self.finish_payload("MB-008"),
        )
        self.assertRedirects(finish_response, reverse("wms:order_list"))
        order.refresh_from_db()
        self.assertEqual(
            order.status,
            WmsWorkshopOrder.Status.FINISHED_READY,
        )

    def test_crafted_status_values_do_not_override_workflow(self):
        self.client.post(
            reverse("wms:order_create_batch"),
            self.create_payload("MB-008"),
        )
        order = WmsWorkshopOrder.objects.for_business(self.business_a).get()
        self.assertEqual(order.status, WmsWorkshopOrder.Status.IN_PROCESS)

        self.client.post(
            reverse("wms:order_finish_batch"),
            self.finish_payload("MB-008"),
        )
        order.refresh_from_db()
        self.assertEqual(
            order.status,
            WmsWorkshopOrder.Status.FINISHED_READY,
        )

    def test_cross_tenant_ids_and_locations_are_rejected(self):
        other_order = self.create_batch(
            ("AH-006",),
            business=self.business_b,
            access=self.access_b,
            location=self.location_b,
            user=self.owner_b,
        )[0]
        self.assertEqual(
            self.client.get(
                reverse("wms:order_detail", args=[other_order.public_id])
            ).status_code,
            404,
        )
        response = self.client.post(
            reverse("wms:order_create_batch"),
            self.create_payload("MB-008", location=self.location_b),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            WmsWorkshopOrder.objects.for_business(self.business_a).exists()
        )

    def test_view_manage_finish_permissions_and_navigation(self):
        viewer = make_owner("phase5-viewer@example.com")
        core_role = Role.objects.create(
            business=self.business_a,
            name="Phase 5 Viewer Core",
            permissions=[],
        )
        membership = Membership.objects.create(
            business=self.business_a,
            user=viewer,
            role=core_role,
        )
        view_role = WmsRole.objects.create(
            business=self.business_a,
            name="Phase 5 Order Viewer",
            code="phase5_order_viewer",
            permissions=["wms.orders.view"],
        )
        core_services.save_user_access(
            business=self.business_a,
            membership=membership,
            role=view_role,
            user=self.owner_a,
        )
        self.client.force_login(viewer)

        response = self.client.get(reverse("wms:order_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Orders")
        self.assertNotContains(response, "New Orders Received")
        self.assertNotContains(response, "Finished Orders")
        self.assertEqual(
            self.client.get(reverse("wms:order_create_batch")).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("wms:order_finish_batch")).status_code,
            403,
        )

    def test_default_role_matrix_is_conservative(self):
        roles = {
            role.code: set(role.permissions)
            for role in WmsRole.objects.for_business(self.business_a).filter(
                is_system=True
            )
        }
        self.assertIn("wms.orders.finish", roles["workshop_manager"])
        self.assertNotIn("wms.orders.view", roles["production_entry"])
        self.assertNotIn("wms.orders.view", roles["attendance_manager"])
        self.assertIn("wms.orders.view", roles["report_viewer"])
        self.assertNotIn("wms.orders.manage", roles["report_viewer"])
        self.assertNotIn("wms.orders.finish", roles["report_viewer"])

    def test_missing_access_disabled_entitlement_and_pos_only_are_denied(self):
        no_access = make_owner("phase5-no-access@example.com")
        core_role = Role.objects.create(
            business=self.business_a,
            name="Phase 5 No Access",
            permissions=[],
        )
        Membership.objects.create(
            business=self.business_a,
            user=no_access,
            role=core_role,
        )
        self.client.force_login(no_access)
        self.assertEqual(
            self.client.get(reverse("wms:order_list")).status_code,
            403,
        )

        self.plan.feature_wms = False
        self.plan.save(update_fields=["feature_wms"])
        self.client.force_login(self.owner_a)
        self.assertEqual(
            self.client.get(reverse("wms:order_list")).status_code,
            403,
        )

        pos_plan = make_plan("Phase 5 POS Only", pos=True)
        pos_owner = make_owner("phase5-pos-only@example.com")
        provision_business(
            owner=pos_owner,
            name="Phase 5 POS Only",
            plan=pos_plan,
        )
        self.client.force_login(pos_owner)
        self.assertEqual(
            self.client.get(reverse("wms:order_list")).status_code,
            403,
        )

    def test_get_requests_do_not_mutate_and_no_delete_route_exists(self):
        order = self.create_batch()[0]
        original_updated_at = order.updated_at
        self.assertEqual(
            self.client.get(reverse("wms:order_create_batch")).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse("wms:order_finish_batch")).status_code,
            200,
        )
        order.refresh_from_db()
        self.assertEqual(order.updated_at, original_updated_at)
        self.assertEqual(order.status, WmsWorkshopOrder.Status.IN_PROCESS)
        with self.assertRaises(NoReverseMatch):
            reverse("wms:order_delete", args=[order.public_id])

    def test_csrf_is_required_for_create_and_finish(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner_a)
        self.assertEqual(
            csrf_client.post(
                reverse("wms:order_create_batch"),
                self.create_payload(),
            ).status_code,
            403,
        )
        self.create_batch()
        self.assertEqual(
            csrf_client.post(
                reverse("wms:order_finish_batch"),
                self.finish_payload(),
            ).status_code,
            403,
        )

    def test_finished_order_remains_visible_after_location_deactivation(self):
        order = self.create_batch()[0]
        services.finish_order_batch(
            business=self.business_a,
            user_access=self.access_a,
            finished_date=self.finished_date,
            references=[order.order_reference],
            user=self.owner_a,
        )
        self.location_a.is_active = False
        self.location_a.save()

        response = self.client.get(
            reverse("wms:order_detail", args=[order.public_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Finished / Ready")
        self.assertEqual(order.status_history.count(), 2)
