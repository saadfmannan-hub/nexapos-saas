from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.urls import reverse

from apps.accounts.models import Membership
from apps.branches.models import Branch, Warehouse
from apps.inventory import services as inventory
from apps.inventory import workflows
from apps.inventory.forms import AdjustmentForm, CountForm, TransferForm
from apps.inventory.models import StockAdjustment, StockMovement, StockTransfer
from apps.subscriptions.models import Plan, Subscription

from .base import TenantTestCase


class InventoryWarehouseScopeTests(TenantTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other_branch = Branch.objects.create(
            business=cls.business_a,
            name="Other Branch",
            code="OTHER",
            usage_type=Branch.UsageType.SALES_BRANCH,
        )
        cls.other_warehouse = Warehouse.objects.create(
            business=cls.business_a,
            branch=cls.other_branch,
            name="Other Warehouse",
            code="OTHER-WH",
        )
        cls.other_movement = inventory.record_movement(
            business=cls.business_a,
            warehouse=cls.other_warehouse,
            product=cls.product_a,
            movement_type=StockMovement.Type.OPENING,
            quantity=Decimal("7"),
            unit_cost=Decimal("4"),
            reference_type="WarehouseScopeTest",
            user=cls.owner_a,
        )
        membership_a = cls.business_a.memberships.get(user=cls.owner_a)
        cls.allowed_count = workflows.start_count(
            business=cls.business_a,
            warehouse=cls.warehouse_a,
            user=cls.owner_a,
            membership=membership_a,
        )
        cls.other_count = workflows.start_count(
            business=cls.business_a,
            warehouse=cls.other_warehouse,
            user=cls.owner_a,
            membership=membership_a,
        )
        cls.other_count_item = cls.other_count.items.get(product=cls.product_a)
        cls.foreign_count = workflows.start_count(
            business=cls.business_b,
            warehouse=cls.warehouse_b,
            user=cls.owner_b,
            membership=cls.business_b.memberships.get(user=cls.owner_b),
        )
        cls.outbound_transfer = StockTransfer.objects.create(
            business=cls.business_a,
            transfer_number="SCOPE-OUT",
            from_warehouse=cls.warehouse_a,
            to_warehouse=cls.other_warehouse,
            requested_by=cls.owner_a,
        )
        cls.inbound_transfer = StockTransfer.objects.create(
            business=cls.business_a,
            transfer_number="SCOPE-IN",
            from_warehouse=cls.other_warehouse,
            to_warehouse=cls.warehouse_a,
            requested_by=cls.owner_a,
        )
        cls.allowed_adjustment = StockAdjustment.objects.create(
            business=cls.business_a,
            adjustment_number="SCOPE-ADJ-ALLOWED",
            warehouse=cls.warehouse_a,
            reason=StockAdjustment.Reason.OTHER,
            created_by=cls.owner_a,
        )
        cls.other_adjustment = StockAdjustment.objects.create(
            business=cls.business_a,
            adjustment_number="SCOPE-ADJ-OTHER",
            warehouse=cls.other_warehouse,
            reason=StockAdjustment.Reason.OTHER,
            created_by=cls.owner_a,
        )

        subscription = Subscription.objects.get(business=cls.business_a)
        Plan.objects.filter(pk=subscription.plan_id).update(feature_transfers=True)
        cls.business_a.refresh_from_db()

    def setUp(self):
        self.client.force_login(self.owner_a)

    def warehouse_scope(self):
        allowed_id = self.warehouse_a.pk
        return patch.object(
            Membership,
            "allowed_warehouse_ids",
            new=property(lambda _membership: {allowed_id}),
        )

    def test_forms_intersect_branch_and_warehouse_scope(self):
        memberships = (
            SimpleNamespace(
                allowed_branch_ids={self.branch_a.pk, self.other_branch.pk},
                allowed_warehouse_ids={self.warehouse_a.pk},
            ),
            SimpleNamespace(
                allowed_branch_ids={self.branch_a.pk},
                allowed_warehouse_ids={
                    self.warehouse_a.pk,
                    self.other_warehouse.pk,
                },
            ),
        )

        for membership in memberships:
            for form_class in (CountForm, AdjustmentForm, TransferForm):
                with self.subTest(
                    form=form_class.__name__, membership=membership
                ):
                    form = form_class(self.business_a, membership=membership)
                    self.assertEqual(
                        set(
                            form.warehouse_queryset.values_list("pk", flat=True)
                        ),
                        {self.warehouse_a.pk},
                    )

    def test_count_detail_denies_get_and_post_before_item_save(self):
        url = reverse(
            "inventory:count_detail",
            kwargs={"public_id": self.other_count.public_id},
        )
        with self.warehouse_scope():
            self.assertEqual(self.client.get(url).status_code, 404)
            response = self.client.post(
                url,
                {
                    "action": "save",
                    f"counted_{self.other_count_item.pk}": "999",
                },
            )
        self.assertEqual(response.status_code, 404)
        self.other_count_item.refresh_from_db()
        self.assertIsNone(self.other_count_item.counted_quantity)

    def test_count_detail_still_enforces_tenant_scope(self):
        url = reverse(
            "inventory:count_detail",
            kwargs={"public_id": self.foreign_count.public_id},
        )
        with self.warehouse_scope():
            response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_inventory_context_only_offers_allowed_branch_and_warehouse(self):
        url = reverse("inventory:stock_list")
        with self.warehouse_scope():
            response = self.client.get(url)
            disallowed_response = self.client.get(
                url,
                {
                    "branch": self.other_branch.pk,
                    "warehouse": self.other_warehouse.pk,
                },
            )
            foreign_response = self.client.get(
                url,
                {
                    "branch": self.branch_b.pk,
                    "warehouse": self.warehouse_b.pk,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_branch"], self.branch_a)
        self.assertEqual(response.context["selected_warehouse"], self.warehouse_a)
        self.assertEqual(
            set(response.context["branches"].values_list("pk", flat=True)),
            {self.branch_a.pk},
        )
        self.assertEqual(
            set(response.context["warehouses"].values_list("pk", flat=True)),
            {self.warehouse_a.pk},
        )
        self.assertEqual(disallowed_response.status_code, 404)
        self.assertEqual(foreign_response.status_code, 404)

    def test_count_movement_transfer_and_adjustment_lists_are_scoped(self):
        with self.warehouse_scope():
            count_response = self.client.get(reverse("inventory:count_list"))
            movement_response = self.client.get(
                reverse("inventory:movement_list")
            )
            transfer_response = self.client.get(
                reverse("inventory:transfer_list")
            )
            adjustment_response = self.client.get(
                reverse("inventory:adjustment_list")
            )

        for response in (
            count_response,
            movement_response,
            transfer_response,
            adjustment_response,
        ):
            self.assertEqual(response.status_code, 200)

        counts = list(count_response.context["page_obj"].object_list)
        self.assertIn(self.allowed_count, counts)
        self.assertNotIn(self.other_count, counts)
        self.assertNotIn(self.foreign_count, counts)

        movements = list(movement_response.context["page_obj"].object_list)
        self.assertTrue(movements)
        self.assertTrue(
            all(movement.warehouse_id == self.warehouse_a.pk for movement in movements)
        )
        self.assertNotIn(self.other_movement, movements)

        transfers = list(transfer_response.context["page_obj"].object_list)
        self.assertNotIn(self.outbound_transfer, transfers)
        self.assertNotIn(self.inbound_transfer, transfers)

        adjustments = list(adjustment_response.context["page_obj"].object_list)
        self.assertIn(self.allowed_adjustment, adjustments)
        self.assertNotIn(self.other_adjustment, adjustments)
