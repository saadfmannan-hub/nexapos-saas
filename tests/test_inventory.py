"""Inventory: purchases, transfers, adjustments, counts."""
from decimal import Decimal

from django.core.exceptions import ValidationError

from apps.inventory import services as inventory
from apps.inventory import workflows
from apps.inventory.models import StockMovement
from apps.purchases import services as purchases
from apps.suppliers.models import Supplier

from .base import TenantTestCase

D = Decimal


class PurchaseTests(TenantTestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            business=self.business_a, code="SUP-1", name="Acme Supplies",
        )

    def _create_purchase(self, qty="10", cost="3.500"):
        return purchases.create_purchase(
            business=self.business_a, supplier=self.supplier,
            branch=self.branch_a, warehouse=self.warehouse_a,
            rows=[{"product": self.product_a, "variant": None,
                   "quantity": D(qty), "unit_cost": D(cost)}],
            user=self.owner_a, purchase_date="2026-01-15",
        )

    def test_purchase_order_does_not_change_stock(self):
        self._create_purchase()
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_a),
            D("100"),
        )

    def test_receiving_increases_stock_and_payable(self):
        purchase = self._create_purchase()
        item = purchase.items.get()
        purchases.receive_purchase(purchase=purchase,
                                   quantities={item.pk: D("10")},
                                   user=self.owner_a)
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_a),
            D("110"),
        )
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.balance, D("35.000"))
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, "received")

    def test_partial_receiving(self):
        purchase = self._create_purchase()
        item = purchase.items.get()
        purchases.receive_purchase(purchase=purchase,
                                   quantities={item.pk: D("4")},
                                   user=self.owner_a)
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, "partially_received")
        with self.assertRaises(ValidationError):
            purchases.receive_purchase(purchase=purchase,
                                       quantities={item.pk: D("7")},
                                       user=self.owner_a)

    def test_purchase_payment_reduces_payable(self):
        purchase = self._create_purchase()
        item = purchase.items.get()
        purchases.receive_purchase(purchase=purchase,
                                   quantities={item.pk: D("10")},
                                   user=self.owner_a)
        purchases.pay_purchase(purchase=purchase, amount=D("20.000"),
                               method=self.cash_a, user=self.owner_a)
        purchase.refresh_from_db()
        self.supplier.refresh_from_db()
        self.assertEqual(purchase.amount_paid, D("20.000"))
        self.assertEqual(purchase.outstanding, D("15.000"))
        self.assertEqual(self.supplier.balance, D("15.000"))

    def test_purchase_return_decreases_stock_and_payable(self):
        purchase = self._create_purchase()
        item = purchase.items.get()
        purchases.receive_purchase(purchase=purchase,
                                   quantities={item.pk: D("10")},
                                   user=self.owner_a)
        purchases.return_purchase(purchase=purchase,
                                  quantities={item.pk: D("3")},
                                  user=self.owner_a, reason="damaged")
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_a),
            D("107"),
        )
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.balance, D("24.500"))
        with self.assertRaises(ValidationError):
            purchases.return_purchase(purchase=purchase,
                                      quantities={item.pk: D("8")},
                                      user=self.owner_a)

    def test_average_cost_updates_on_receipt(self):
        purchase = self._create_purchase(qty="100", cost="6.000")
        item = purchase.items.get()
        purchases.receive_purchase(purchase=purchase,
                                   quantities={item.pk: D("100")},
                                   user=self.owner_a)
        self.product_a.refresh_from_db()
        # 100 @ 4.000 + 100 @ 6.000 → avg 5.000
        self.assertEqual(self.product_a.average_cost, D("5.000"))


class TransferTests(TenantTestCase):
    def setUp(self):
        from apps.branches.models import Warehouse

        self.warehouse_a2 = Warehouse.objects.create(
            business=self.business_a, name="Second", code="W2",
        )

    def _transfer(self, qty="10"):
        return workflows.create_transfer(
            business=self.business_a, from_warehouse=self.warehouse_a,
            to_warehouse=self.warehouse_a2,
            rows=[{"product": self.product_a, "variant": None,
                   "quantity": D(qty)}],
            user=self.owner_a,
        )

    def test_dispatch_and_receive_moves_stock(self):
        transfer = self._transfer()
        workflows.dispatch_transfer(transfer=transfer, user=self.owner_a)
        self.assertEqual(inventory.get_stock(
            self.business_a, self.warehouse_a, self.product_a), D("90"))
        self.assertEqual(inventory.get_stock(
            self.business_a, self.warehouse_a2, self.product_a), D("0"))
        workflows.receive_transfer(transfer=transfer, user=self.owner_a)
        self.assertEqual(inventory.get_stock(
            self.business_a, self.warehouse_a2, self.product_a), D("10"))

    def test_transfer_blocked_when_insufficient(self):
        transfer = self._transfer(qty="500")
        with self.assertRaises(ValidationError):
            workflows.dispatch_transfer(transfer=transfer, user=self.owner_a)

    def test_cancel_dispatched_transfer_returns_stock(self):
        transfer = self._transfer()
        workflows.dispatch_transfer(transfer=transfer, user=self.owner_a)
        workflows.cancel_transfer(transfer=transfer, user=self.owner_a)
        self.assertEqual(inventory.get_stock(
            self.business_a, self.warehouse_a, self.product_a), D("100"))


class AdjustmentTests(TenantTestCase):
    def test_adjustment_changes_stock(self):
        workflows.create_adjustment(
            business=self.business_a, warehouse=self.warehouse_a,
            reason="damage",
            rows=[{"product": self.product_a, "variant": None,
                   "quantity": D("-5")}],
            user=self.owner_a,
        )
        self.assertEqual(inventory.get_stock(
            self.business_a, self.warehouse_a, self.product_a), D("95"))
        movement = StockMovement.objects.for_business(self.business_a).filter(
            movement_type="damage").first()
        self.assertIsNotNone(movement)

    def test_pending_adjustment_applies_only_after_approval(self):
        adjustment = workflows.create_adjustment(
            business=self.business_a, warehouse=self.warehouse_a,
            reason="loss",
            rows=[{"product": self.product_a, "variant": None,
                   "quantity": D("-2")}],
            user=self.cashier_a, requires_approval=True,
        )
        self.assertEqual(inventory.get_stock(
            self.business_a, self.warehouse_a, self.product_a), D("100"))
        workflows.approve_adjustment(adjustment=adjustment, user=self.owner_a)
        self.assertEqual(inventory.get_stock(
            self.business_a, self.warehouse_a, self.product_a), D("98"))


class StockCountTests(TenantTestCase):
    def test_count_variance_applied_on_approval(self):
        count = workflows.start_count(
            business=self.business_a, warehouse=self.warehouse_a,
            user=self.owner_a,
        )
        item = count.items.get(product=self.product_a)
        self.assertEqual(item.expected_quantity, D("100"))
        item.counted_quantity = D("96")
        item.save()
        self.assertEqual(item.variance, D("-4"))
        workflows.approve_count(count=count, user=self.owner_a)
        self.assertEqual(inventory.get_stock(
            self.business_a, self.warehouse_a, self.product_a), D("96"))

    def test_movement_history_always_written(self):
        before = StockMovement.objects.for_business(self.business_a).count()
        workflows.create_adjustment(
            business=self.business_a, warehouse=self.warehouse_a,
            reason="other",
            rows=[{"product": self.product_a, "variant": None,
                   "quantity": D("1")}],
            user=self.owner_a,
        )
        after = StockMovement.objects.for_business(self.business_a).count()
        self.assertEqual(after, before + 1)
