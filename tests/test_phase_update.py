"""Regression tests: delivery dates, multi-payment ledger, customer
statements, product archive/delete, sale void/delete, audit trail."""
import json
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.catalog.models import Product
from apps.sales import services as sales
from apps.sales.models import Sale
from apps.sales.services import SaleError

from .base import TenantTestCase

D = Decimal


class DeliveryDateTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()
        self.future = timezone.localdate() + timedelta(days=15)

    def test_sale_created_with_future_delivery_date(self):
        sale = self.make_sale(delivery_date=self.future)
        self.assertEqual(sale.delivery_date, self.future)
        self.assertEqual(sale.delivery_status, Sale.DeliveryStatus.PENDING)
        self.assertFalse(sale.is_delivery_overdue)

    def test_delivery_date_shows_on_detail_invoice_receipt_and_list(self):
        sale = self.make_sale(delivery_date=self.future)
        self.client.force_login(self.owner_a)
        for name in ("sales:detail", "sales:invoice", "sales:receipt"):
            response = self.client.get(reverse(name, args=[sale.public_id]))
            self.assertContains(response, self.future.strftime("%Y"), msg_prefix=name)
        response = self.client.get(reverse("sales:list"))
        self.assertContains(response, "Pending")

    def test_pos_checkout_accepts_delivery_date(self):
        self.client.force_login(self.owner_a)
        response = self.client.post(
            reverse("sales:pos_checkout"),
            json.dumps({
                "branch_id": self.branch_a.id,
                "customer_id": self.walk_in_a.id,
                "items": [{"product_id": self.product_a.id, "variant_id": None,
                           "quantity": "1", "unit_price": "10.000",
                           "discount_amount": "0"}],
                "payments": [{"method_id": self.cash_a.id, "amount": "10.50"}],
                "delivery_date": str(self.future),
            }),
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["ok"], data)
        sale = Sale.objects.for_business(self.business_a).get(
            public_id=data["sale"]["public_id"])
        self.assertEqual(sale.delivery_date, self.future)

    def test_delivery_filters(self):
        today = timezone.localdate()
        s_today = self.make_sale(delivery_date=today)
        s_future = self.make_sale(delivery_date=today + timedelta(days=5))
        s_overdue = self.make_sale(delivery_date=today - timedelta(days=2))
        self.client.force_login(self.owner_a)

        def invoices(delivery):
            response = self.client.get(reverse("sales:list"),
                                       {"delivery": delivery})
            return [s.invoice_number for s in response.context["page_obj"]]

        self.assertEqual(invoices("today"), [s_today.invoice_number])
        self.assertEqual(invoices("upcoming"), [s_future.invoice_number])
        self.assertEqual(invoices("overdue"), [s_overdue.invoice_number])
        self.assertEqual(len(invoices("scheduled")), 3)

    def test_delivery_status_update_and_audit(self):
        sale = self.make_sale(delivery_date=self.future)
        sales.set_delivery_status(sale=sale, status="delivered", user=self.owner_a)
        sale.refresh_from_db()
        self.assertEqual(sale.delivery_status, "delivered")
        self.assertTrue(AuditLog.objects.filter(
            business=self.business_a, action="sale.delivery_status").exists())

    def test_delivered_sale_not_overdue(self):
        sale = self.make_sale(delivery_date=timezone.localdate() - timedelta(days=3))
        self.assertTrue(sale.is_delivery_overdue)
        sales.set_delivery_status(sale=sale, status="delivered", user=self.owner_a)
        sale.refresh_from_db()
        self.assertFalse(sale.is_delivery_overdue)


class MultiPaymentLedgerTests(TenantTestCase):
    """Spec example: invoice 100.000 → cash 60.000 today, bank 40.000 later."""

    def setUp(self):
        from apps.customers.models import Customer

        self.allow_no_shift()
        self.no_tax = Product.objects.create(
            business=self.business_a, name="Tailored Suit", sku="SUIT-1",
            sale_price=D("100.000"), product_type="non_stock",
            track_inventory=False,
        )
        self.customer = Customer.objects.create(
            business=self.business_a, code="LEDG", full_name="Ledger Customer",
            credit_limit=D("500"),
        )
        self.bank_a = self.business_a.sales_paymentmethod_set.get(kind="bank")
        self.sale = self.make_sale(
            customer=self.customer,
            items=[{"product": self.no_tax, "quantity": D("1"),
                    "unit_price": D("100.000")}],
            payments=[{"method": self.credit_a, "amount": D("100.000")}],
        )

    def test_two_payments_on_different_dates_settle_the_invoice(self):
        self.assertEqual(self.sale.payment_state, "Unpaid")
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, D("100.000"))

        today = timezone.localdate()
        sales.add_sale_payment(
            sale=self.sale, amount=D("60.000"), method=self.cash_a,
            user=self.owner_a, payment_date=today,
        )
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.amount_paid, D("60.000"))
        self.assertEqual(self.sale.balance, D("40.000"))
        self.assertEqual(self.sale.status, Sale.Status.PARTIAL)
        self.assertEqual(self.sale.payment_state, "Partially Paid")

        later = today + timedelta(days=8)
        sales.add_sale_payment(
            sale=self.sale, amount=D("40.000"), method=self.bank_a,
            user=self.owner_a, payment_date=later, reference="TRF-991",
        )
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.balance, D("0.000"))
        self.assertEqual(self.sale.status, Sale.Status.COMPLETED)
        self.assertEqual(self.sale.payment_state, "Paid")

        # Each payment keeps its own date
        dates = list(self.sale.payments.values_list("payment_date", flat=True))
        self.assertIn(today, dates)
        self.assertIn(later, dates)

        # Customer receivable fully settled
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, D("0.000"))

    def test_overpayment_rejected(self):
        with self.assertRaises(SaleError):
            sales.add_sale_payment(sale=self.sale, amount=D("150.000"),
                                   method=self.cash_a, user=self.owner_a)

    def test_payment_on_voided_sale_rejected(self):
        cash_sale = self.make_sale()
        sales.void_sale(sale=cash_sale, user=self.owner_a, reason="test")
        with self.assertRaises(SaleError):
            sales.add_sale_payment(sale=cash_sale, amount=D("1.000"),
                                   method=self.cash_a, user=self.owner_a)

    def test_payment_add_creates_audit_log(self):
        sales.add_sale_payment(sale=self.sale, amount=D("10.000"),
                               method=self.cash_a, user=self.owner_a)
        log = AuditLog.objects.filter(business=self.business_a,
                                      action="sale.payment_added").first()
        self.assertIsNotNone(log)
        self.assertIn(self.sale.invoice_number, log.description)

    def test_payment_add_view(self):
        self.client.force_login(self.owner_a)
        response = self.client.post(
            reverse("sales:payment_add", args=[self.sale.public_id]),
            {"amount": "25.000", "method_id": self.cash_a.id,
             "payment_date": str(timezone.localdate()), "reference": "R1",
             "notes": "first instalment"},
        )
        self.assertEqual(response.status_code, 302)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.amount_paid, D("25.000"))
        payment = self.sale.payments.get(method=self.cash_a)
        self.assertEqual(payment.notes, "first instalment")
        self.assertEqual(payment.received_by, self.owner_a)

    def test_payment_history_shows_on_detail_and_invoice(self):
        later = timezone.localdate() + timedelta(days=8)
        sales.add_sale_payment(sale=self.sale, amount=D("60.000"),
                               method=self.cash_a, user=self.owner_a)
        sales.add_sale_payment(sale=self.sale, amount=D("10.000"),
                               method=self.bank_a, user=self.owner_a,
                               payment_date=later, reference="TRF-7")
        self.client.force_login(self.owner_a)
        response = self.client.get(reverse("sales:detail",
                                           args=[self.sale.public_id]))
        self.assertContains(response, "Payment history")
        self.assertContains(response, "TRF-7")
        self.assertContains(response, later.strftime("%Y-%m-%d"))
        response = self.client.get(reverse("sales:invoice",
                                           args=[self.sale.public_id]))
        self.assertContains(response, "BALANCE DUE")
        self.assertContains(response, "PAYMENT HISTORY")


class CustomerStatementTests(TenantTestCase):
    def setUp(self):
        from apps.customers.models import Customer

        self.allow_no_shift()
        self.customer = Customer.objects.create(
            business=self.business_a, code="STMT", full_name="Statement Co",
            credit_limit=D("500"),
        )
        # Credit sale 21.000 (debit), later payment 6.000 (credit)
        self.sale = self.make_sale(
            customer=self.customer,
            payments=[{"method": self.credit_a, "amount": D("21.000")}],
        )
        self.payment_date = timezone.localdate() + timedelta(days=3)
        sales.add_sale_payment(
            sale=self.sale, amount=D("6.000"), method=self.cash_a,
            user=self.owner_a,
            payment_date=self.payment_date,
        )
        self.client.force_login(self.owner_a)

    def test_running_balance_debits_and_credits(self):
        response = self.client.get(
            reverse("customers:statement", args=[self.customer.public_id]),
            {"to": str(self.payment_date)},
        )
        entries = response.context["entries"]
        self.assertEqual(entries[0]["type"], "Credit sale")
        self.assertEqual(entries[0]["debit"], D("21.000"))
        self.assertEqual(entries[0]["balance"], D("21.000"))
        self.assertEqual(entries[1]["type"], "Invoice payment")
        self.assertEqual(entries[1]["credit"], D("6.000"))
        self.assertEqual(entries[1]["balance"], D("15.000"))
        self.assertEqual(response.context["closing_balance"], D("15.000"))
        # Matches the live customer balance
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, D("15.000"))

    def test_date_filter_shows_brought_forward(self):
        cutoff = timezone.localdate() + timedelta(days=1)
        response = self.client.get(
            reverse("customers:statement", args=[self.customer.public_id]),
            {"from": str(cutoff), "to": str(self.payment_date)},
        )
        self.assertEqual(response.context["brought_forward"], D("21.000"))
        types = [e["type"] for e in response.context["entries"]]
        self.assertEqual(types, ["Invoice payment"])

    def test_csv_export_and_audit(self):
        response = self.client.get(
            reverse("customers:statement", args=[self.customer.public_id]),
            {"export": "csv"},
        )
        content = response.content.decode()
        self.assertIn("Credit sale", content)
        self.assertIn("CLOSING BALANCE", content)
        self.assertTrue(AuditLog.objects.filter(
            business=self.business_a,
            action="customer.statement_exported").exists())

    def test_pdf_export(self):
        response = self.client.get(
            reverse("customers:statement", args=[self.customer.public_id]),
            {"export": "pdf"},
        )
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))


class ProductArchiveDeleteTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()
        self.client.force_login(self.owner_a)

    def test_unused_product_can_be_hard_deleted(self):
        product = Product.objects.create(
            business=self.business_a, name="Never Sold", sku="NSOLD",
            track_inventory=False, product_type="non_stock",
        )
        response = self.client.post(
            reverse("catalog:product_delete", args=[product.public_id]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Product.objects.filter(pk=product.pk).exists())
        self.assertTrue(AuditLog.objects.filter(
            business=self.business_a, action="product.deleted").exists())

    def test_product_with_sales_history_cannot_be_deleted(self):
        self.make_sale()  # sells product_a
        self.client.post(
            reverse("catalog:product_delete", args=[self.product_a.public_id]))
        self.assertTrue(Product.objects.filter(pk=self.product_a.pk).exists())

    def test_product_with_history_can_be_archived_and_restored(self):
        self.make_sale()
        self.client.post(
            reverse("catalog:product_archive", args=[self.product_a.public_id]))
        self.product_a.refresh_from_db()
        self.assertTrue(self.product_a.is_archived)
        self.assertTrue(AuditLog.objects.filter(
            business=self.business_a, action="product.archived").exists())
        self.client.post(
            reverse("catalog:product_restore", args=[self.product_a.public_id]))
        self.product_a.refresh_from_db()
        self.assertFalse(self.product_a.is_archived)
        self.assertTrue(self.product_a.is_active)
        self.assertTrue(AuditLog.objects.filter(
            business=self.business_a, action="product.restored").exists())

    def test_archived_product_hidden_from_pos_and_default_list(self):
        self.product_a.is_archived = True
        self.product_a.is_active = False
        self.product_a.save()
        response = self.client.get(reverse("sales:pos_products"),
                                   {"q": "Widget"})
        self.assertEqual(response.json()["items"], [])
        response = self.client.get(reverse("sales:pos_barcode"),
                                   {"code": "WID-A"})
        self.assertFalse(response.json()["found"])
        response = self.client.get(reverse("catalog:product_list"))
        self.assertNotContains(response, "Widget A")
        response = self.client.get(reverse("catalog:product_list"),
                                   {"status": "archived"})
        self.assertContains(response, "Widget A")

    def test_archived_product_still_on_historical_invoice(self):
        sale = self.make_sale()
        self.product_a.is_archived = True
        self.product_a.save()
        response = self.client.get(reverse("sales:invoice", args=[sale.public_id]))
        self.assertContains(response, "Widget A")

    def test_cashier_cannot_archive_or_delete(self):
        self.client.force_login(self.cashier_a)
        response = self.client.post(
            reverse("catalog:product_archive", args=[self.product_a.public_id]))
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            reverse("catalog:product_delete", args=[self.product_a.public_id]))
        self.assertEqual(response.status_code, 403)


class SaleVoidDeleteTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()

    def test_void_reverses_stock_balance_and_audits(self):
        from apps.customers.models import Customer
        from apps.inventory import services as inventory

        customer = Customer.objects.create(
            business=self.business_a, code="VC", full_name="Void Customer",
            credit_limit=D("500"),
        )
        sale = self.make_sale(
            customer=customer,
            payments=[{"method": self.credit_a, "amount": D("21.000")}],
        )
        customer.refresh_from_db()
        self.assertEqual(customer.balance, D("21.000"))
        sales.void_sale(sale=sale, user=self.owner_a, reason="entry error")
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.VOIDED)
        self.assertEqual(sale.void_reason, "entry error")
        self.assertEqual(sale.voided_by, self.owner_a)
        self.assertIsNotNone(sale.voided_at)
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_a),
            D("100"),
        )
        customer.refresh_from_db()
        self.assertEqual(customer.balance, D("0.000"))
        self.assertTrue(AuditLog.objects.filter(
            business=self.business_a, action="sale.voided").exists())
        # Invoice number is preserved
        self.assertTrue(sale.invoice_number)

    def test_completed_sale_cannot_be_hard_deleted(self):
        sale = self.make_sale()
        self.client.force_login(self.owner_a)
        response = self.client.post(reverse("sales:delete", args=[sale.public_id]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Sale.objects.filter(pk=sale.pk).exists())

    def test_draft_sale_can_be_deleted_and_audited(self):
        draft = Sale.objects.create(
            business=self.business_a, branch=self.branch_a,
            warehouse=self.warehouse_a, cashier=self.owner_a,
            customer=self.walk_in_a, status=Sale.Status.DRAFT,
            sale_date=timezone.now(),
        )
        self.client.force_login(self.owner_a)
        response = self.client.post(reverse("sales:delete", args=[draft.public_id]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Sale.objects.filter(pk=draft.pk).exists())
        self.assertTrue(AuditLog.objects.filter(
            business=self.business_a, action="sale.deleted").exists())

    def test_cashier_cannot_delete_sale(self):
        draft = Sale.objects.create(
            business=self.business_a, branch=self.branch_a,
            warehouse=self.warehouse_a, cashier=self.cashier_a,
            customer=self.walk_in_a, status=Sale.Status.DRAFT,
            sale_date=timezone.now(),
        )
        self.client.force_login(self.cashier_a)
        response = self.client.post(reverse("sales:delete", args=[draft.public_id]))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Sale.objects.filter(pk=draft.pk).exists())
