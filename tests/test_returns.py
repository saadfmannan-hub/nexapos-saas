"""Sales return / refund tests."""
from decimal import Decimal

from apps.inventory import services as inventory
from apps.sales import services as sales
from apps.sales.models import Sale, SaleReturn
from apps.sales.services import SaleError

from .base import TenantTestCase

D = Decimal


class ReturnTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()
        self.sale = self.make_sale(items=[{
            "product": self.product_a, "quantity": D("4"),
            "unit_price": D("10.000"),
        }])  # total 42.000 (40 + 5% tax)

    def test_partial_return_restores_stock_and_refund(self):
        item = self.sale.items.get()
        ret = sales.process_return(
            sale=self.sale, items=[{"sale_item": item, "quantity": D("1")}],
            refund_method=SaleReturn.RefundMethod.CASH, user=self.owner_a,
        )
        self.assertEqual(ret.refund_amount, D("10.500"))
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_a),
            D("97"),
        )
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.status, Sale.Status.PART_RETURNED)

    def test_full_return(self):
        item = self.sale.items.get()
        ret = sales.process_return(
            sale=self.sale, items=[{"sale_item": item, "quantity": D("4")}],
            refund_method=SaleReturn.RefundMethod.CASH, user=self.owner_a,
        )
        self.assertEqual(ret.refund_amount, D("42.000"))
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.status, Sale.Status.RETURNED)
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_a),
            D("100"),
        )

    def test_cannot_return_more_than_sold(self):
        item = self.sale.items.get()
        with self.assertRaises(SaleError):
            sales.process_return(
                sale=self.sale, items=[{"sale_item": item, "quantity": D("5")}],
                refund_method=SaleReturn.RefundMethod.CASH, user=self.owner_a,
            )
        # And across multiple returns
        sales.process_return(
            sale=self.sale, items=[{"sale_item": item, "quantity": D("3")}],
            refund_method=SaleReturn.RefundMethod.CASH, user=self.owner_a,
        )
        item.refresh_from_db()
        with self.assertRaises(SaleError):
            sales.process_return(
                sale=self.sale, items=[{"sale_item": item, "quantity": D("2")}],
                refund_method=SaleReturn.RefundMethod.CASH, user=self.owner_a,
            )

    def test_store_credit_refund(self):
        from apps.customers.models import Customer

        customer = Customer.objects.create(
            business=self.business_a, code="RC", full_name="Returner",
        )
        sale = self.make_sale(customer=customer)
        item = sale.items.get()
        sales.process_return(
            sale=sale, items=[{"sale_item": item, "quantity": D("2")}],
            refund_method=SaleReturn.RefundMethod.STORE_CREDIT, user=self.owner_a,
        )
        customer.refresh_from_db()
        self.assertEqual(customer.store_credit, D("21.000"))

    def test_customer_account_refund_reduces_balance(self):
        from apps.customers.models import Customer

        customer = Customer.objects.create(
            business=self.business_a, code="CB", full_name="Credit Buyer",
            credit_limit=D("100"),
        )
        sale = self.make_sale(
            customer=customer,
            payments=[{"method": self.credit_a, "amount": D("21.000")}],
        )
        customer.refresh_from_db()
        self.assertEqual(customer.balance, D("21.000"))
        item = sale.items.get()
        sales.process_return(
            sale=sale, items=[{"sale_item": item, "quantity": D("2")}],
            refund_method=SaleReturn.RefundMethod.CUSTOMER_ACCOUNT,
            user=self.owner_a,
        )
        customer.refresh_from_db()
        self.assertEqual(customer.balance, D("0.000"))

    def test_non_restock_return_keeps_stock_out(self):
        item = self.sale.items.get()
        sales.process_return(
            sale=self.sale,
            items=[{"sale_item": item, "quantity": D("1"), "restock": False}],
            refund_method=SaleReturn.RefundMethod.CASH, user=self.owner_a,
            restock=False,
        )
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_a),
            D("96"),
        )

    def test_voided_sale_cannot_be_returned(self):
        sales.void_sale(sale=self.sale, user=self.owner_a, reason="mistake")
        item = self.sale.items.get()
        with self.assertRaises(SaleError):
            sales.process_return(
                sale=self.sale, items=[{"sale_item": item, "quantity": D("1")}],
                refund_method=SaleReturn.RefundMethod.CASH, user=self.owner_a,
            )
