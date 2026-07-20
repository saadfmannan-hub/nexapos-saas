"""POS / sale completion tests."""
import json
from decimal import Decimal
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone

from apps.inventory import services as inventory
from apps.inventory.services import InsufficientStock
from apps.sales import services as sales
from apps.sales.models import Sale, SaleReturn
from apps.sales.services import SaleError
from apps.subscriptions.exceptions import DenialCode, ModuleAccessDenied

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
        # Numbering uses the sale branch's configured invoice prefix.
        self.assertTrue(s1.invoice_number.startswith("HO-"))
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
        with self.assertRaises(InsufficientStock):
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
        with self.assertRaises(ModuleAccessDenied) as caught:
            sales.complete_sale(
                business=self.business_a, branch=self.branch_a,
                warehouse=self.warehouse_a, cashier=self.cashier_a,
                customer=customer,
                items=[{"product": self.product_a, "quantity": D("1"),
                        "unit_price": D("10.000")}],
                payments=[{"method": self.credit_a, "amount": D("10.500")}],
                membership=cashier_membership,
            )
        self.assertEqual(caught.exception.denial.code, DenialCode.PERMISSION_DENIED)

    def test_discount_permission_enforced(self):
        from apps.accounts.models import Membership, Role

        viewer_role = Role.objects.create(
            business=self.business_a,
            name="Seller without discount",
            permissions=["sales.create"],
        )
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
        self.checkout_sequence = 0

    def checkout(self, payload):
        self.checkout_sequence += 1
        payload = dict(payload)
        payload.setdefault(
            "checkout_token", f"pos-endpoint-{self.checkout_sequence}"
        )
        return self.client.post(
            reverse("sales:pos_checkout"), json.dumps(payload),
            content_type="application/json",
        )

    def enable_tailoring_product(self):
        self.product_a.is_tailoring_item = True
        self.product_a.save(update_fields=["is_tailoring_item"])

    def discounted_credit_sale(self):
        from apps.customers.models import Customer

        customer = Customer.objects.create(
            business=self.business_a, code="INV-DISC", full_name="Invoice Discount",
            credit_limit=D("500.000"),
        )
        return self.make_sale(
            customer=customer,
            items=[{"product": self.product_a, "quantity": D("4.000"),
                    "unit_price": D("25.000")}],
            payments=[
                {"method": self.cash_a, "amount": D("80.500")},
                {"method": self.credit_a, "amount": D("14.000")},
            ],
            invoice_discount=D("10.000"),
        )

    def test_checkout_endpoint_completes_sale(self):
        delivery_date = timezone.localdate()
        response = self.checkout({
            "branch_id": self.branch_a.id,
            "customer_id": self.walk_in_a.id,
            "items": [{"product_id": self.product_a.id, "variant_id": None,
                       "quantity": "2", "unit_price": "10.000",
                       "discount_amount": "0"}],
            "payments": [{"method_id": self.cash_a.id, "amount": "21.000"}],
            "invoice_discount": "0",
            "delivery_date": str(delivery_date),
        })
        data = response.json()
        self.assertTrue(data["ok"], data)
        self.assertIn("invoice_number", data["sale"])

    def test_disabled_business_vat_overrides_product_and_held_cart_rates(self):
        from apps.sales.models import HeldSale

        settings_obj = self.business_a.settings
        settings_obj.vat_enabled = False
        settings_obj.vat_percentage = D("0.000")
        settings_obj.save(update_fields=["vat_enabled", "vat_percentage"])

        pos_response = self.client.get(reverse("sales:pos"))
        self.assertEqual(pos_response.context["vat_rate"], 0)

        products_response = self.client.get(
            reverse("sales:pos_products"),
            {"warehouse_id": self.warehouse_a.id},
        )
        product = next(
            item for item in products_response.json()["items"]
            if item["product_id"] == self.product_a.id
        )
        self.assertEqual(product["tax_rate"], "0")

        held = HeldSale.objects.create(
            business=self.business_a,
            branch=self.branch_a,
            cashier=self.owner_a,
            cart={
                "checkout_token": "disabled-vat-held-cart",
                "items": [{
                    "product_id": self.product_a.id,
                    "variant_id": None,
                    "quantity": 1,
                    "unit_price": "10.000",
                    "tax_rate": "5.000",
                }],
            },
        )
        held_response = self.client.get(reverse("sales:pos_held_list"))
        held_payload = next(
            item for item in held_response.json()["held"] if item["id"] == held.id
        )
        self.assertEqual(held_payload["cart"]["items"][0]["tax_rate"], "0")

        response = self.checkout({
            "branch_id": self.branch_a.id,
            "customer_id": self.walk_in_a.id,
            "items": [{
                "product_id": self.product_a.id,
                "variant_id": None,
                "quantity": "2",
                "unit_price": "10.000",
                "tax_rate": "5.000",
            }],
            "payments": [{"method_id": self.cash_a.id, "amount": "20.000"}],
            "invoice_discount": "0",
        })
        self.assertTrue(response.json()["ok"], response.json())
        sale = Sale.objects.get(public_id=response.json()["sale"]["public_id"])
        self.assertEqual(sale.tax_amount, D("0.000"))
        self.assertEqual(sale.total, D("20.000"))

    def test_checkout_endpoint_requires_delivery_date(self):
        self.enable_tailoring_product()
        response = self.checkout({
            "branch_id": self.branch_a.id,
            "customer_id": self.walk_in_a.id,
            "items": [{"product_id": self.product_a.id, "variant_id": None,
                       "quantity": "1", "unit_price": "10.000",
                       "discount_amount": "0",
                       "garment_classification": "adult",
                       "collection_type": "normal"}],
            "payments": [{"method_id": self.cash_a.id, "amount": "10.500"}],
            "invoice_discount": "0",
        })
        data = response.json()
        self.assertFalse(data["ok"])
        self.assertEqual(
            data["error"],
            "Please select delivery date before completing the tailoring booking.",
        )

    def test_checkout_endpoint_stores_item_tailoring_details(self):
        self.enable_tailoring_product()
        response = self.checkout({
            "branch_id": self.branch_a.id,
            "customer_id": self.walk_in_a.id,
            "items": [
                {"product_id": self.product_a.id, "variant_id": None,
                 "quantity": "1", "unit_price": "10.000",
                 "discount_amount": "0",
                 "garment_classification": "adult",
                 "collection_type": "normal",
                 "tailoring_details": {
                     "design_type": "Daraz",
                     "daraz_details": "3 Line",
                     "customer_notes": "Loose fitting",
                     "workshop_notes": "Press before packing",
                 }},
                {"product_id": self.product_a.id, "variant_id": None,
                 "quantity": "1", "unit_price": "10.000",
                 "discount_amount": "0",
                 "garment_classification": "child",
                 "collection_type": "normal",
                 "tailoring_details": {
                     "design_type": "VIP 3D Design",
                     "vip_3d_design": "MM3",
                     "computer_design": "Sultani",
                 }},
            ],
            "payments": [{"method_id": self.cash_a.id, "amount": "21.000"}],
            "invoice_discount": "0",
            "delivery_date": str(timezone.localdate()),
            "priority": "high",
        })
        data = response.json()
        self.assertTrue(data["ok"], data)
        sale = Sale.objects.for_business(self.business_a).get(
            invoice_number=data["sale"]["invoice_number"])
        items = list(sale.items.order_by("id"))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].tailoring_details["design_type"], "Daraz")
        self.assertEqual(items[0].tailoring_details["daraz_details"], "3 Line")
        self.assertEqual(items[0].garment_classification, "adult")
        self.assertEqual(items[1].tailoring_details["design_type"], "VIP 3D Design")
        self.assertEqual(items[1].tailoring_details["vip_3d_design"], "MM3")
        self.assertEqual(items[1].tailoring_details["computer_design"], "Sultani")
        self.assertEqual(items[1].garment_classification, "child")
        self.assertEqual(sale.priority, Sale.Priority.HIGH)
        for item in items:
            self.assertNotIn("priority", item.tailoring_details)
            self.assertNotIn("fabric", item.tailoring_details)
            self.assertNotIn("measurements", item.tailoring_details)
            self.assertNotIn("expected_delivery", item.tailoring_details)

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
        cart = {
            "items": [{"product_id": self.product_a.id, "quantity": 1}],
            "checkout_token": "pos-hold-resume",
        }
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

    def test_invoice_detail_shows_invoice_discount_summary_and_item_subtotal(self):
        sale = self.discounted_credit_sale()
        response = self.client.get(reverse("sales:detail", args=[sale.public_id]))
        html = response.content.decode()
        self.assertIn('<td class="text-end fw-semibold">100.00</td>', html)
        self.assertNotIn('<td class="text-end fw-semibold">94.50</td>', html)
        self.assertContains(response, "Invoice Discount")
        self.assertContains(response, "-10.00")
        self.assertContains(response, "Discounted Subtotal")
        self.assertContains(response, "90.00")
        self.assertContains(response, "4.50")
        self.assertContains(response, "94.50")
        self.assertContains(response, "80.50")
        self.assertContains(response, "14.00")

    def test_a4_invoice_shows_discounted_summary_and_item_subtotal(self):
        sale = self.discounted_credit_sale()
        self.business_a.phone = "24500000"
        self.business_a.email = "accounts@example.com"
        self.business_a.address = "Main Business Address"
        self.business_a.save(update_fields=["phone", "email", "address"])
        sale.customer.mobile = "99001122"
        sale.customer.save(update_fields=["mobile"])
        sale.branch.address = "Branch Address"
        sale.branch.save(update_fields=["address"])
        settings_obj = self.business_a.settings
        settings_obj.vat_enabled = True
        settings_obj.vat_registration_number = "VAT-12345"
        settings_obj.terms_and_conditions = "Payment due on receipt."
        settings_obj.save(update_fields=[
            "vat_enabled", "vat_registration_number", "terms_and_conditions",
        ])
        response = self.client.get(reverse("sales:invoice", args=[sale.public_id]))
        html = response.content.decode()
        self.assertIn('<td class="r"><strong>100.000</strong></td>', html)
        self.assertNotIn('<td class="r"><strong>94.500</strong></td>', html)
        self.assertContains(response, "TAX INVOICE")
        self.assertContains(response, "ORIGINAL COPY")
        self.assertContains(response, "24500000")
        self.assertContains(response, "accounts@example.com")
        self.assertContains(response, "Branch Address")
        self.assertNotContains(response, "Customer ID")
        self.assertNotContains(response, "INV-DISC")
        self.assertContains(response, "99001122")
        self.assertNotContains(response, "Main Business Address")
        self.assertContains(response, "VAT No: VAT-12345")
        self.assertContains(response, "Invoice Discount")
        self.assertContains(response, "-10.000")
        self.assertContains(response, "Discounted Subtotal")
        self.assertContains(response, "90.000")
        self.assertContains(response, "VAT 5.000%")
        self.assertContains(response, "4.500")
        self.assertContains(response, "94.500")
        self.assertContains(response, "80.500")
        self.assertContains(response, "BALANCE DUE")
        self.assertContains(response, "14.000")
        self.assertContains(response, "Payment due on receipt.")
        self.assertContains(response, "Powered by Nexa Business Solutions")

    def test_a4_invoice_without_vat_uses_normal_invoice_label(self):
        from apps.catalog.models import Product

        settings_obj = self.business_a.settings
        settings_obj.vat_enabled = False
        settings_obj.save(update_fields=["vat_enabled"])
        product = Product.objects.create(
            business=self.business_a, name="No VAT Service", sku="NO-VAT-POS",
            sale_price=D("20.000"), track_inventory=False, product_type="non_stock",
        )
        sale = self.make_sale(
            items=[{"product": product, "quantity": D("1.000"),
                    "unit_price": D("20.000")}],
            payments=[{"method": self.cash_a, "amount": D("20.000")}],
        )
        response = self.client.get(reverse("sales:invoice", args=[sale.public_id]))
        self.assertContains(response, "<h1>INVOICE</h1>", html=True)
        self.assertNotContains(response, "TAX INVOICE")
        self.assertNotContains(response, "VAT 0.000%")

    def test_invoice_and_receipt_show_partial_and_split_payment_history(self):
        from apps.customers.models import Customer

        customer = Customer.objects.create(
            business=self.business_a, code="SPLIT-CUST", full_name="Split Customer",
            credit_limit=D("100.000"),
        )
        sale = self.make_sale(
            customer=customer,
            items=[{"product": self.product_a, "quantity": D("2.000"),
                    "unit_price": D("25.000")}],
            payments=[
                {"method": self.cash_a, "amount": D("30.000"), "reference": "CASH-1"},
                {"method": self.card_a, "amount": D("12.500"), "reference": "CARD-1"},
                {"method": self.credit_a, "amount": D("10.000"), "reference": "CR-1"},
            ],
        )
        response = self.client.get(reverse("sales:invoice", args=[sale.public_id]))
        self.assertContains(response, "Cash")
        self.assertContains(response, "CASH-1")
        self.assertContains(response, "Card")
        self.assertContains(response, "CARD-1")
        self.assertContains(response, self.owner_a.full_name)
        self.assertContains(response, "BALANCE DUE")
        self.assertContains(response, "10.000")

        receipt = self.client.get(reverse("sales:receipt", args=[sale.public_id]))
        self.assertContains(receipt, "Ref: CASH-1")
        self.assertContains(receipt, "Ref: CARD-1")
        self.assertContains(receipt, f"By: {self.owner_a.full_name}")

    def test_invoice_outputs_show_partial_return_refund_summary(self):
        sale = self.discounted_credit_sale()
        item = sale.items.get()
        sales.process_return(
            sale=sale,
            items=[{"sale_item": item, "quantity": D("1.000")}],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=self.owner_a,
        )
        sale.refresh_from_db()

        invoice = self.client.get(reverse("sales:invoice", args=[sale.public_id]))
        self.assertContains(invoice, "RETURN / REFUND SUMMARY")
        self.assertContains(invoice, "1.000 returned")
        self.assertContains(invoice, "Cash")
        self.assertContains(invoice, "NET TOTAL")
        self.assertContains(invoice, "70.875")
        self.assertContains(invoice, "Net Paid")
        self.assertContains(invoice, "56.875")

        receipt = self.client.get(reverse("sales:receipt", args=[sale.public_id]))
        self.assertContains(receipt, "REFUND")
        self.assertContains(receipt, "1.000 returned - Cash")
        self.assertContains(receipt, "NET TOTAL")
        self.assertContains(receipt, "NET PAID")

    def test_invoice_pdf_uses_same_context_values_as_a4_invoice(self):
        sale = self.discounted_credit_sale()
        with patch("apps.reports.pdf.render_pdf", return_value=b"%PDF fake") as render_pdf:
            response = self.client.get(reverse("sales:invoice_pdf",
                                               args=[sale.public_id]))
        self.assertEqual(response.status_code, 200)
        template, context = render_pdf.call_args.args
        self.assertEqual(template, "invoices/invoice_a4.html")
        self.assertEqual(context["items"][0].display_subtotal, D("100.000"))
        self.assertEqual(context["discounted_subtotal"], D("90.000"))
        self.assertEqual(context["sale"].discount_amount, D("10.000"))
        self.assertEqual(context["sale"].tax_amount, D("4.500"))
        self.assertEqual(context["sale"].total, D("94.500"))
        self.assertEqual(context["sale"].amount_paid, D("80.500"))
        self.assertEqual(context["sale"].balance, D("14.000"))
        self.assertEqual(context["invoice_label"], "TAX INVOICE")
        self.assertEqual(context["copy_label"], "ORIGINAL COPY")
        self.assertEqual(context["returns"], [])

    def test_pdf_context_includes_return_summary_values(self):
        sale = self.discounted_credit_sale()
        item = sale.items.get()
        sales.process_return(
            sale=sale,
            items=[{"sale_item": item, "quantity": D("1.000")}],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=self.owner_a,
        )
        with patch("apps.reports.pdf.render_pdf", return_value=b"%PDF fake") as render_pdf:
            response = self.client.get(reverse("sales:invoice_pdf",
                                               args=[sale.public_id]))
        self.assertEqual(response.status_code, 200)
        context = render_pdf.call_args.args[1]
        self.assertEqual(context["sale"].net_total, D("70.875"))
        self.assertEqual(context["sale"].net_amount_paid, D("56.875"))
        self.assertEqual(context["returns"][0].display_returned_quantity, D("1.000"))

    def test_receipt_context_values_include_commercial_labels(self):
        sale = self.discounted_credit_sale()
        response = self.client.get(reverse("sales:receipt", args=[sale.public_id]))
        self.assertEqual(response.context["invoice_label"], "TAX INVOICE")
        self.assertEqual(response.context["copy_label"], "ORIGINAL COPY")
        self.assertEqual(response.context["discounted_subtotal"], D("90.000"))
        self.assertEqual(response.context["vat_rate"], D("5.000"))

    def test_invoice_list_shows_invoice_discount_and_final_total(self):
        sale = self.discounted_credit_sale()
        response = self.client.get(reverse("sales:list"))
        self.assertContains(response, sale.invoice_number)
        self.assertContains(response, "10.00")
        self.assertContains(response, "94.50")

    def test_invoice_pdf_downloads(self):
        sale = self.make_sale()
        response = self.client.get(reverse("sales:invoice_pdf",
                                           args=[sale.public_id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_workshop_job_card_renders_premium_tailoring_sections(self):
        from django.template.loader import render_to_string

        settings_obj = self.business_a.settings
        labels = [
            "Toul", "Shoulders", "Chest", "Side", "Sleeves", "Design 3d No",
            "Daraz (1,2,3) Line", "Computer Design",
        ]
        for index, label in enumerate(labels, start=1):
            setattr(settings_obj, f"more_option_label_{index}", label)
        settings_obj.save(update_fields=[
            f"more_option_label_{index}" for index in range(1, len(labels) + 1)
        ])
        from apps.customers.models import Customer

        customer = Customer.objects.create(
            business=self.business_a, code="TAILOR-JOB",
            full_name="Tailoring Job Customer",
        )
        customer.mobile = "99001122"
        customer.address = "Do not show this address"
        customer.email = "hidden@example.com"
        customer.more_options = {
            "1": "60", "2": "18", "3": "40", "4": "22",
            "5": "24", "6": "D3-10", "7": "Line A", "8": "Logo",
        }
        customer.save(update_fields=["mobile", "address", "email", "more_options"])
        self.product_a.description = "Use premium cuff finish."
        self.product_a.save(update_fields=["description"])
        self.enable_tailoring_product()
        sale = self.make_sale(
            customer=customer,
            delivery_date=timezone.localdate(),
            priority=Sale.Priority.HIGH,
            items=[{
                "product": self.product_a,
                "quantity": D("1.000"),
                "unit_price": D("10.000"),
                "garment_classification": "adult",
                "tailoring_details": {
                    "design_type": "Daraz",
                    "daraz_details": "3 Line",
                    "customer_notes": "Loose fitting",
                    "workshop_notes": "Press before packing",
                },
            }],
        )
        item = sale.items.select_related("product__unit", "variant").get()
        card = {
            "sale": sale,
            "items": [item],
            "job_item": item,
            "business": self.business_a,
            "job_card_number": f"JC-{sale.invoice_number}-01",
            "workshop_copy_number": 1,
            "copy_type": "Original",
            "priority_label": "High",
            "priority_class": "high",
            "tailoring": item.tailoring_details,
            "job_delivery_date": sale.delivery_date,
            "more_options": [
                {"label": label, "value": customer.more_options[str(index)]}
                for index, label in enumerate(labels, start=1)
            ],
        }
        html = render_to_string("invoices/workshop_job_card.html", {
            "job_cards": [card],
        })
        self.assertIn("JOB CARD", html)
        self.assertIn(self.business_a.name, html)
        self.assertNotIn("NexaPOS", html)
        self.assertIn("Powered by Nexa Business Solutions", html)
        self.assertIn("Job Card Number", html)
        self.assertIn(f"JC-{sale.invoice_number}-01", html)
        self.assertIn("Workshop Copy Number", html)
        self.assertIn("Original", html)
        self.assertIn("Copy", html)
        self.assertIn("Reprint", html)
        self.assertIn("VIP", html)
        self.assertIn("Phone Number", html)
        self.assertIn("99001122", html)
        self.assertNotIn('<div class="label">Product</div>', html)
        self.assertIn("Product / Fabric", html)
        self.assertIn(self.product_a.name, html)
        self.assertIn("Quantity", html)
        self.assertIn("Design Type", html)
        self.assertIn("Daraz", html)
        self.assertIn("Daraz Details", html)
        self.assertIn("3 Line", html)
        self.assertIn("VIP 3D Design", html)
        self.assertIn("Computer Design", html)
        self.assertIn("Loose fitting", html)
        self.assertIn("Press before packing", html)
        self.assertNotIn("Product Information", html)
        self.assertNotIn("Do not show this address", html)
        self.assertNotIn("hidden@example.com", html)
        self.assertNotIn("Customer ID", html)
        for label in labels:
            self.assertIn(label, html)
        self.assertIn("Line A", html)
        self.assertIn("Customer Requirements", html)
        self.assertIn("Workshop Notes", html)
        self.assertEqual(
            html.count('<div class="label">Customer Requirements</div>'), 1
        )
        self.assertEqual(html.count('<div class="label">Workshop Notes</div>'), 1)
        self.assertNotIn('<div class="label">Customer Notes</div>', html)
        self.assertEqual(html.count("Loose fitting"), 1)
        self.assertEqual(html.count("Press before packing"), 1)
        self.assertIn("Production Tracking", html)
        self.assertIn("Stage", html)
        self.assertIn("Done", html)
        self.assertIn("Date", html)
        self.assertIn("Initials", html)
        self.assertIn("Cutting", html)
        self.assertIn("Body", html)
        self.assertIn("Sleeves", html)
        self.assertIn("Collar", html)
        self.assertIn("Daraz", html)
        self.assertIn("Design", html)
        self.assertIn("Button", html)
        self.assertIn("Iron", html)
        self.assertIn("QC", html)
        self.assertIn("Ready", html)
        self.assertNotIn("Payment Status", html)
        self.assertNotIn(sale.payment_state, html)
        self.assertIn("Order Status", html)
        self.assertIn("Booked", html)
        self.assertIn("In Process", html)
        self.assertIn("Finished", html)
        self.assertIn("Booked By", html)
        self.assertIn("Received By", html)
        self.assertIn("Customer Signature", html)
        self.assertNotIn("Assigned Tailor", html)
        self.assertNotIn("Trial Required", html)
        self.assertNotIn("QR Code", html)
        self.assertNotIn("Product Photo", html)

    def test_workshop_job_card_pdf_downloads(self):
        self.enable_tailoring_product()
        sale = self.make_sale(
            items=[{
                "product": self.product_a,
                "quantity": D("1.000"),
                "unit_price": D("10.000"),
                "garment_classification": "adult",
            }],
            delivery_date=timezone.localdate(),
        )
        response = self.client.get(
            reverse("sales:workshop_job_card_pdf", args=[sale.public_id]),
            {"priority": "urgent", "copy": "copy"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_tailoring_sale_creates_one_production_job_per_sale_item(self):
        self.enable_tailoring_product()
        sale = self.make_sale(
            items=[
                {
                    "product": self.product_a, "quantity": D("1.000"),
                    "unit_price": D("10.000"),
                    "garment_classification": "adult",
                    "tailoring_details": {
                        "design_type": "Daraz",
                        "daraz_details": "3 Line",
                        "customer_notes": "Loose fitting",
                    },
                },
                {
                    "product": self.product_a, "quantity": D("1.000"),
                    "unit_price": D("10.000"),
                    "garment_classification": "child",
                    "tailoring_details": {
                        "design_type": "Computer Design",
                        "computer_design": "Sultani",
                    },
                },
                {
                    "product": self.product_a, "quantity": D("1.000"),
                    "unit_price": D("10.000"),
                    "garment_classification": "adult",
                    "tailoring_details": {
                        "design_type": "VIP 3D Design",
                        "vip_3d_design": "MM3",
                        "workshop_notes": "Premium stitching",
                    },
                },
            ],
            payments=[{"method": self.cash_a, "amount": D("31.500")}],
            delivery_date=timezone.localdate(),
            priority=Sale.Priority.URGENT,
        )
        self.assertEqual(sale.items.count(), 3)
        items = list(sale.items.order_by("id"))
        self.assertEqual(items[0].tailoring_details["design_type"], "Daraz")
        self.assertEqual(items[0].tailoring_details["daraz_details"], "3 Line")
        self.assertEqual(items[0].garment_classification, "adult")
        self.assertEqual(items[1].tailoring_details["design_type"], "Computer Design")
        self.assertEqual(items[1].tailoring_details["computer_design"], "Sultani")
        self.assertEqual(items[1].garment_classification, "child")
        self.assertEqual(items[2].tailoring_details["design_type"], "VIP 3D Design")
        self.assertEqual(items[2].tailoring_details["vip_3d_design"], "MM3")
        self.assertEqual(sale.priority, Sale.Priority.URGENT)

        with patch("apps.reports.pdf.render_pdf", return_value=b"%PDF fake") as render_pdf:
            response = self.client.get(reverse(
                "sales:sale_item_workshop_job_card_pdf",
                args=[sale.public_id, items[1].id],
            ))
        self.assertEqual(response.status_code, 200)
        context = render_pdf.call_args.args[1]
        self.assertEqual(context["sale"], sale)
        self.assertEqual(context["items"], [items[1]])
        self.assertEqual(context["tailoring"]["computer_design"], "Sultani")
        self.assertEqual(context["priority_label"], "Urgent")
        self.assertEqual(context["job_card_number"], f"JC-{sale.invoice_number}-02")

        with patch("apps.reports.pdf.render_pdf", return_value=b"%PDF fake") as render_pdf:
            response = self.client.get(
                reverse("sales:workshop_job_card_pdf", args=[sale.public_id])
            )
        self.assertEqual(response.status_code, 200)
        template, context = render_pdf.call_args.args
        self.assertEqual(template, "invoices/workshop_job_card.html")
        self.assertEqual(len(context["job_cards"]), 3)
        self.assertEqual(
            [card["job_card_number"] for card in context["job_cards"]],
            [
                f"JC-{sale.invoice_number}-01",
                f"JC-{sale.invoice_number}-02",
                f"JC-{sale.invoice_number}-03",
            ],
        )
        self.assertEqual(context["job_cards"][0]["items"], [items[0]])
        self.assertEqual(context["job_cards"][1]["items"], [items[1]])
        self.assertEqual(context["job_cards"][2]["items"], [items[2]])

        detail = self.client.get(reverse("sales:detail", args=[sale.public_id]))
        self.assertContains(detail, "Download All Job Cards")
        self.assertContains(detail, 'target="_blank">Job Card</a>', count=3)

    def test_retail_sale_items_have_no_tailoring_production_jobs(self):
        sale = self.make_sale(
            items=[{"product": self.product_a, "quantity": D("5.000"),
                    "unit_price": D("10.000")}],
            payments=[{"method": self.cash_a, "amount": D("52.500")}],
        )
        item = sale.items.get()
        self.assertEqual(item.tailoring_details, {})
        self.assertFalse(item.has_tailoring_details)
