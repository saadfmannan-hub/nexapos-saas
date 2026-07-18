"""Focused Phase 2B enforcement tests for the Inventory module boundary."""

from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, Role, User
from apps.branches.models import Branch, Warehouse
from apps.inventory import services, workflows
from apps.inventory.models import (
    StockAdjustment,
    StockCount,
    StockMovement,
    StockTransfer,
)
from apps.subscriptions.exceptions import DenialCode, ModuleAccessDenied
from apps.subscriptions.models import Plan, Subscription

from .base import TenantTestCase

D = Decimal


class InventoryEnforcementTests(TenantTestCase):
    password = "StrongPass123!"

    def subscription(self):
        return Subscription.objects.select_related("plan").get(business=self.business_a)

    def set_plan(self, **fields):
        subscription = self.subscription()
        Plan.objects.filter(pk=subscription.plan_id).update(**fields)

    def set_inventory(self, enabled):
        self.set_plan(feature_sales=True, feature_inventory=enabled)

    def set_subscription_status(self, status):
        Subscription.objects.filter(business=self.business_a).update(
            status=status,
            trial_ends_at=None,
            current_period_end=timezone.now() + timedelta(days=30),
        )

    def make_staff(self, permissions, *, email, branches=None):
        role = Role.objects.create(
            business=self.business_a,
            name=f"Phase 2B role {email}",
            permissions=list(permissions),
        )
        user = User.objects.create_user(
            email=email,
            password=self.password,
            full_name="Phase 2B Staff",
        )
        membership = Membership.objects.create(
            business=self.business_a,
            user=user,
            role=role,
        )
        if branches is not None:
            membership.branches.set(branches)
        return user, membership

    def assert_service_denied(self, callback, code):
        with self.assertRaises(ModuleAccessDenied) as caught:
            callback()
        self.assertEqual(caught.exception.denial.code, code)

    def make_secondary_locations(self):
        branch = Branch.objects.create(
            business=self.business_a,
            name="Inventory Other Branch",
            code="INV-B2",
        )
        warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=branch,
            name="Inventory Other Warehouse",
            code="INV-W2",
        )
        central = Warehouse.objects.create(
            business=self.business_a,
            branch=None,
            name="Inventory Central Warehouse",
            code="INV-CENTRAL",
        )
        return branch, warehouse, central

    @staticmethod
    def list_urls():
        return (
            reverse("inventory:stock_list"),
            reverse("inventory:export"),
            reverse("inventory:import"),
            reverse("inventory:import_template"),
            reverse("inventory:movement_list"),
            reverse("inventory:item_search"),
            reverse("inventory:transfer_list"),
            reverse("inventory:transfer_create"),
            reverse("inventory:adjustment_list"),
            reverse("inventory:adjustment_create"),
            reverse("inventory:count_list"),
        )

    def object_urls(self):
        _branch, other_warehouse, _central = self.make_secondary_locations()
        transfer = StockTransfer.objects.create(
            business=self.business_a,
            transfer_number="TRF-SEC-1",
            from_warehouse=self.warehouse_a,
            to_warehouse=other_warehouse,
            requested_by=self.owner_a,
        )
        adjustment = StockAdjustment.objects.create(
            business=self.business_a,
            adjustment_number="ADJ-SEC-1",
            warehouse=self.warehouse_a,
            reason=StockAdjustment.Reason.LOSS,
            status=StockAdjustment.Status.PENDING,
            created_by=self.owner_a,
        )
        count = StockCount.objects.create(
            business=self.business_a,
            count_number="CNT-SEC-1",
            warehouse=self.warehouse_a,
            created_by=self.owner_a,
        )
        return (
            reverse(
                "inventory:transfer_action",
                args=[transfer.public_id, "cancel"],
            ),
            reverse(
                "inventory:adjustment_action",
                args=[adjustment.public_id, "approve"],
            ),
            reverse("inventory:count_detail", args=[count.public_id]),
        )

    def test_every_inventory_url_uses_the_central_module_guard(self):
        from apps.inventory.urls import urlpatterns

        self.assertEqual(len(urlpatterns), 14)
        for pattern in urlpatterns:
            with self.subTest(route=pattern.name):
                self.assertTrue(getattr(pattern.callback, "_subscription_module_guarded", False))

    def test_enabled_owner_can_open_inventory_routes(self):
        self.set_plan(
            feature_sales=True,
            feature_inventory=True,
            feature_transfers=True,
        )
        self.client.force_login(self.owner_a)

        for url in self.list_urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_disabled_inventory_denies_owner_without_bypass_on_every_url(self):
        urls = (*self.list_urls(), *self.object_urls())
        self.set_inventory(False)
        self.client.force_login(self.owner_a)

        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_disabled_inventory_denies_staff_with_permissions(self):
        user, _membership = self.make_staff(
            {
                "inventory.view",
                "inventory.export",
                "inventory.import",
                "inventory.transfer",
                "inventory.adjust",
                "inventory.adjust_approve",
                "inventory.count",
            },
            email="phase2b-disabled@example.com",
            branches=[self.branch_a],
        )
        self.set_inventory(False)
        self.client.force_login(user)

        for url in self.list_urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_enabled_inventory_does_not_replace_role_permissions(self):
        user, _membership = self.make_staff(
            [],
            email="phase2b-no-permissions@example.com",
        )
        self.set_inventory(True)
        self.client.force_login(user)

        for url in self.list_urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_disabled_write_urls_fail_before_payload_or_mutation(self):
        self.set_inventory(False)
        self.client.force_login(self.owner_a)
        adjustment_count = StockAdjustment.objects.for_business(self.business_a).count()
        count_count = StockCount.objects.for_business(self.business_a).count()

        for route_name in (
            "inventory:import",
            "inventory:transfer_create",
            "inventory:adjustment_create",
            "inventory:count_list",
        ):
            with self.subTest(route=route_name):
                self.assertEqual(
                    self.client.post(reverse(route_name), data={}).status_code,
                    403,
                )

        self.assertEqual(
            StockAdjustment.objects.for_business(self.business_a).count(),
            adjustment_count,
        )
        self.assertEqual(
            StockCount.objects.for_business(self.business_a).count(),
            count_count,
        )

    def test_read_only_subscription_allows_inventory_reads_but_denies_writes(self):
        self.set_inventory(True)
        self.set_subscription_status(Subscription.Status.PAST_DUE)
        self.client.force_login(self.owner_a)

        for route_name in (
            "inventory:stock_list",
            "inventory:export",
            "inventory:import_template",
            "inventory:movement_list",
        ):
            with self.subTest(route=route_name):
                self.assertEqual(self.client.get(reverse(route_name)).status_code, 200)
        for route_name in (
            "inventory:import",
            "inventory:transfer_create",
            "inventory:adjustment_create",
            "inventory:count_list",
        ):
            with self.subTest(route=route_name):
                self.assertEqual(
                    self.client.post(reverse(route_name), data={}).status_code,
                    403,
                )

    def test_suspended_subscription_denies_inventory_history(self):
        self.set_inventory(True)
        self.set_subscription_status(Subscription.Status.SUSPENDED)
        self.client.force_login(self.owner_a)

        for route_name in (
            "inventory:stock_list",
            "inventory:export",
            "inventory:movement_list",
        ):
            with self.subTest(route=route_name):
                self.assertEqual(self.client.get(reverse(route_name)).status_code, 403)

    def test_disabled_inventory_denies_opening_stock_and_import_services(self):
        self.set_inventory(False)
        before = services.get_stock(
            self.business_a,
            self.warehouse_a,
            self.product_a,
        )

        self.assert_service_denied(
            lambda: services.set_opening_stock(
                business=self.business_a,
                warehouse=self.warehouse_a,
                product=self.product_a,
                quantity=D("1"),
                unit_cost=D("4"),
                user=self.owner_a,
            ),
            DenialCode.MODULE_DISABLED,
        )
        self.assert_service_denied(
            lambda: services.import_inventory(
                business=self.business_a,
                rows=[],
                mode="add",
                user=self.owner_a,
            ),
            DenialCode.MODULE_DISABLED,
        )
        self.assertEqual(
            services.get_stock(
                self.business_a,
                self.warehouse_a,
                self.product_a,
            ),
            before,
        )

    def test_read_only_subscription_denies_inventory_workflows(self):
        self.set_inventory(True)
        self.set_subscription_status(Subscription.Status.PAST_DUE)

        callbacks = (
            lambda: workflows.create_adjustment(
                business=self.business_a,
                warehouse=self.warehouse_a,
                reason="loss",
                rows=[
                    {
                        "product": self.product_a,
                        "variant": None,
                        "quantity": D("-1"),
                    }
                ],
                user=self.owner_a,
            ),
            lambda: workflows.start_count(
                business=self.business_a,
                warehouse=self.warehouse_a,
                user=self.owner_a,
            ),
        )
        for callback in callbacks:
            with self.subTest(callback=callback):
                self.assert_service_denied(
                    callback,
                    DenialCode.SUBSCRIPTION_READ_ONLY,
                )

    def test_enabled_owner_inventory_services_mutate_through_guarded_boundary(self):
        self.set_inventory(True)
        before = services.get_stock(
            self.business_a,
            self.warehouse_a,
            self.product_a,
        )

        services.set_opening_stock(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=self.product_a,
            quantity=D("2"),
            unit_cost=D("4"),
            user=self.owner_a,
        )
        count = workflows.start_count(
            business=self.business_a,
            warehouse=self.warehouse_a,
            user=self.owner_a,
        )

        self.assertEqual(
            services.get_stock(
                self.business_a,
                self.warehouse_a,
                self.product_a,
            ),
            before + D("2"),
        )
        self.assertEqual(count.business, self.business_a)

    def test_restricted_membership_cannot_mutate_another_branch_warehouse(self):
        _branch, other_warehouse, central = self.make_secondary_locations()
        user, membership = self.make_staff(
            {"inventory.adjust", "inventory.count", "inventory.import"},
            email="phase2b-scoped@example.com",
            branches=[self.branch_a],
        )
        self.set_inventory(True)

        self.assert_service_denied(
            lambda: services.set_opening_stock(
                business=self.business_a,
                warehouse=other_warehouse,
                product=self.product_a,
                quantity=D("1"),
                unit_cost=D("4"),
                user=user,
                membership=membership,
            ),
            DenialCode.SCOPE_DENIED,
        )
        services.set_opening_stock(
            business=self.business_a,
            warehouse=central,
            product=self.product_a,
            quantity=D("1"),
            unit_cost=D("4"),
            user=user,
            membership=membership,
        )
        summary, errors = services.import_inventory(
            business=self.business_a,
            rows=[
                {
                    "sku": self.product_a.sku,
                    "warehouse": other_warehouse.name,
                    "quantity": "5",
                }
            ],
            mode="add",
            user=user,
            membership=membership,
        )
        self.assertEqual(summary["failed"], 1)
        self.assertTrue(errors)
        self.assertEqual(
            services.get_stock(self.business_a, other_warehouse, self.product_a),
            D("0"),
        )
        self.assertEqual(
            services.get_stock(self.business_a, central, self.product_a),
            D("1"),
        )

    def test_cross_tenant_service_scope_fails_closed_before_stock_write(self):
        self.set_inventory(True)
        before = StockMovement.objects.for_business(self.business_b).count()

        for warehouse in (self.warehouse_a, self.warehouse_b):
            with self.subTest(warehouse=warehouse):
                self.assert_service_denied(
                    lambda warehouse=warehouse: services.set_opening_stock(
                        business=self.business_a,
                        warehouse=warehouse,
                        product=self.product_b,
                        quantity=D("1"),
                        unit_cost=D("2"),
                        user=self.owner_a,
                    ),
                    DenialCode.SCOPE_DENIED,
                )
        self.assertEqual(
            StockMovement.objects.for_business(self.business_b).count(),
            before,
        )

    def test_foreign_count_direct_url_is_not_visible(self):
        foreign_count = StockCount.objects.create(
            business=self.business_b,
            count_number="CNT-FOREIGN",
            warehouse=self.warehouse_b,
            created_by=self.owner_b,
        )
        self.set_inventory(True)
        self.client.force_login(self.owner_a)

        response = self.client.get(
            reverse("inventory:count_detail", args=[foreign_count.public_id])
        )

        self.assertEqual(response.status_code, 404)

    def test_disabled_inventory_hides_navigation_and_dashboard_widgets(self):
        self.set_inventory(False)
        self.client.force_login(self.owner_a)

        response = self.client.get(reverse("dashboard"))
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(reverse("inventory:stock_list"), content)
        self.assertNotIn("Low Stock Items", content)
        self.assertNotIn("Inventory Value", content)
        self.assertNotIn("Inventory movement (14 days)", content)
        self.assertNotIn("movement-data", content)
        self.assertIn(reverse("catalog:product_list"), content)

    def test_enabled_inventory_shows_navigation_and_dashboard_widgets(self):
        self.set_inventory(True)
        self.client.force_login(self.owner_a)

        response = self.client.get(reverse("dashboard"))
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn(reverse("inventory:stock_list"), content)
        self.assertIn("Low Stock Items", content)
        self.assertIn("Inventory Value", content)
        self.assertIn("Inventory movement (14 days)", content)
        self.assertIn("movement-data", content)

    def test_disabled_inventory_dashboard_does_not_query_stock_tables(self):
        self.set_inventory(False)
        self.client.force_login(self.owner_a)

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        sql = " ".join(query["sql"].lower() for query in queries.captured_queries)
        self.assertNotIn("inventory_stocklevel", sql)
        self.assertNotIn("inventory_stockmovement", sql)
