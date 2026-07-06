"""POS / sale completion tests."""
import json
from decimal import Decimal

from django.urls import reverse

from apps.inventory import services as inventory
from apps.sales import services as sales
from apps.sales.models import Sale
from apps.sales.services import SaleError

from .base import TenantTestCase

D = Decimal


class SaleServiceTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()

    def test_cash_sale_totals_and_stock(self):
        sale = self.make_sale()
        # 2 x 10.000 = 20.000 base, 5% tax = 1.000 → total 21.000
        self.assertEqual(sale.subtotal, D("20.000"))
        self.assertEqual(sale.tax_amount, D("1.000"))
        self.assertEqual(sale.total, D("21.000"))
        self.assertEqual(sale.status, Sale.Status.COMPLETED)
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_a),
            D("98"),
        )

    def test_gross_profit_uses_cost_snapshot(self):
        sale = self.make_sale()
        # cost 2 x 4.000 = 8.000; profit = 20.000 - 8.000
        self.assertEqual(sale.total_cost, D("8.000"))
        self.assertEqual(sale.gross_profit, D("12.000"))
        # Changing product price later must not affect the recorded sale
        self.product_a.purchase_price = D("9.999")
        self.product_a.save()
        sale.refresh_from_db()
        self.assertEqual(sale.gross_profit, D("12.000"))

    def test_invoice_numbers_unique_and_sequential(self):
        s1 = self.make_sale()
        s2 = self.make_sale()
        self.assertNotEqual(s1.invoice_number, s2.invoice_number)
        # Numbering uses the configurable Business Settings prefix (default
        # "INV"), not the branch's prefix. See tests/test_invoice_prefix.py.
        self.assertTrue(s1.invoice_number.startswith("INV-"))
        n1 = int(s1.invoice_number.rsplit("-", 1)[1])
        n2 = int(s2.invoice_number.rsplit("-", 1)[1])
        self.assertEqual(n2, n1 + 1)

    def test_split_payment(self):
        sale = self.make_sale(payments=[
            {"method": self.cash_a, "amount": D("11.000")},
            {"method": self.card_a, "amount": D("10.000")},
        ])
        self.assertEqual(sale.payments.count(), 2)
        self.assertEqual(sale.amount_paid, D("21.000"))

    def test_change_calculation(self):
        sale = self.make_sale(payments=[
            {"method": self.cash_a, "amount": D("25.000")},
        ])
        self.assertEqual(sale.change_due, D("4.000"))
        self.assertEqual(sale.amount_paid, D("21.000"))
        # Stored cash payment is net of change
        self.assertEqual(sale.payments.get().amount, D("21.000"))

    def test_card_overpayment_rejected(self):
        with self.assertRaises(SaleError):
            self.make_sale(payments=[{"method": self.card_a, "amount": D("25.000")}])

    def test_underpayment_rejected(self):
        with self.assertRaises(SaleError):
            self.make_sale(payments=[{"method": self.cash_a, "amount": D("5.000")}])

    def test_insufficient_stock_blocked(self):
        with self.assertRaises(Exception):
            self.make_sale(items=[{"product": self.product_a,
                                   "quantity": D("1000"),
                                   "unit_price": D("10.000")}])
        # Stock unchanged (transaction rolled back)
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_a),
            D("100"),
        )

    def test_credit_sale_updates_customer_balance(self):
        from apps.customers.models import Customer

        customer = Customer.objects.create(
            business=self.business_a, code="C1", full_name="Credit Customer",
            credit_limit=D("100"),
        )
        sale = self.make_sale(
            customer=customer,
            payments=[{"method": self.credit_a, "amount": D("21.000")}],
        )
        customer.refresh_from_db()
        self.assertEqual(customer.balance, D("21.000"))
        self.assertEqual(sale.status, Sale.Status.CREDIT)
        self.assertEqual(sale.amount_paid, D("0.000"))

    def test_credit_sale_blocked_for_walk_in(self):
        with self.assertRaises(SaleError):
            self.make_sale(payments=[{"method": self.credit_a,
                                      "amount": D("21.000")}])

    def test_credit_limit_enforced(self):
        from apps.accounts.models import Membership
        from apps.customers.models import Customer

        customer = Customer.objects.create(
            business=self.business_a, code="C2", full_name="Limited",
            credit_limit=D("10"),
        )
        cashier_membership = Membership.objects.get(
            business=self.business_a, user=self.cashier_a)
        # Cashier role lacks sales.credit → blocked
        with self.assertRaises(SaleError):
            sales.complete_sale(
                business=self.business_a, branch=self.branch_a,
                warehouse=self.warehouse_a, cashier=self.cashier_a,
                customer=customer,
                items=[{"product": self.product_a, "quantity": D("1"),
                        "unit_price": D("10.000")}],
                payments=[{"method": self.credit_a, "amount": D("10.500")}],
                membership=cashier_membership,
            )

    def test_discount_permission_enforced(self):
        from apps.accounts.models import Membership, Role

        viewer_role = Role.objects.for_business(self.business_a).get(
            name="Read-Only Viewer")
        from apps.accounts.models import User

        viewer = User.objects.create_user(email="viewer@example.com",
                                          password="x" * 10, full_name="Viewer")
        membership = Membership.objects.create(
            business=self.business_a, user=viewer, role=viewer_role)
        with self.assertRaises(SaleError):
            sales.complete_sale(
                business=self.business_a, branch=self.branch_a,
                warehouse=self.warehouse_a, cashier=viewer,
                customer=self.walk_in_a,
                items=[{"product": self.product_a, "quantity": D("1"),
                        "unit_price": D("10.000"),
                        "discount_amount": D("2.000")}],
                payments=[{"method": self.cash_a, "amount": D("8.400")}],
                membership=membership,
            )

    def test_shift_required_when_configured(self):
        settings_obj = self.business_a.settings
        settings_obj.allow_sale_without_shift = False
        settings_obj.save()
        with self.assertRaises(SaleError):
            self.make_sale()

    def test_void_restores_stock(self):
        sale = self.make_sale()
        sales.void_sale(sale=sale, user=self.owner_a, reason="test")
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.VOIDED)
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_a),
            D("100"),
        )


class PosEndpointTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()
        self.client.force_login(self.owner_a)

    def checkout(self, payload):
        return self.client.post(
            reverse("sales:pos_checkout"), json.dumps(payload),
            content_type="application/json",
        )

    def test_checkout_endpoint_completes_sale(self):
        response = self.checkout({
            "branch_id": self.branch_a.id,
            "customer_id": self.walk_in_a.id,
            "items": [{"product_id": self.product_a.id, "variant_id": None,
                       "quantity": "2", "unit_price": "10.000",
                       "discount_amount": "0"}],
            "payments": [{"method_id": self.cash_a.id, "amount": "21.000"}],
            "invoice_discount": "0",
        })
        data = response.json()
        self.assertTrue(data["ok"], data)
        self.assertIn("invoice_number", data["sale"])

    def test_checkout_rejects_cross_tenant_product(self):
        response = self.checkout({
            "branch_id": self.branch_a.id,
            "customer_id": self.walk_in_a.id,
            "items": [{"product_id": self.product_b.id, "variant_id": None,
                       "quantity": "1", "unit_price": "5.000"}],
            "payments": [{"method_id": self.cash_a.id, "amount": "5.000"}],
        })
        self.assertFalse(response.json()["ok"])

    def test_hold_and_resume(self):
        cart = {"items": [{"product_id": self.product_a.id, "quantity": 1}]}
        response = self.client.post(
            reverse("sales:pos_hold"),
            json.dumps({"branch_id": self.branch_a.id, "label": "Mr X",
                        "cart": cart}),
            content_type="application/json",
        )
        self.assertTrue(response.json()["ok"])
        response = self.client.get(reverse("sales:pos_held_list"))
        held = response.json()["held"]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["label"], "Mr X")

    def test_receipt_and_invoice_render(self):
        sale = self.make_sale()
        for name in ("sales:receipt", "sales:invoice"):
            response = self.client.get(reverse(name, args=[sale.public_id]))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, sale.invoice_number)

    def test_receipt_and_invoice_show_item_subtotal_before_invoice_discount_and_vat(self):
        sale = self.make_sale(
            items=[{"product": self.product_a, "quantity": D("4.000"),
                    "unit_price": D("25.000")}],
            payments=[{"method": self.cash_a, "amount": D("99.750")}],
            invoice_discount=D("5.000"),
        )
        self.assertEqual(sale.items.get().line_total, D("99.750"))

        response = self.client.get(reverse("sales:receipt", args=[sale.public_id]))
        html = response.content.decode()
        self.assertIn(
            '<td>4.000 x 25.000</td>\n  <td class="r">100.000</td>',
            html,
        )
        self.assertContains(response, "TOTAL</td><td class=\"r\">99.750")

        self.register_a.receipt_printer = "58mm"
        self.register_a.save(update_fields=["receipt_printer"])
        sale.register = self.register_a
        sale.save(update_fields=["register"])
        response = self.client.get(reverse("sales:receipt", args=[sale.public_id]))
        html = response.content.decode()
        self.assertIn(
            '<td>4.000 x 25.000</td>\n  <td class="r">100.000</td>',
            html,
        )

        response = self.client.get(reverse("sales:invoice", args=[sale.public_id]))
        html = response.content.decode()
        self.assertIn('<td class="r"><strong>100.000</strong></td>', html)
        self.assertNotIn('<td class="r"><strong>99.750</strong></td>', html)

    def test_invoice_pdf_downloads(self):
        sale = self.make_sale()
        response = self.client.get(reverse("sales:invoice_pdf",
                                           args=[sale.public_id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
