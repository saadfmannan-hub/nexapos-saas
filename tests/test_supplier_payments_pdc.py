"""Phase 4A supplier payments and post-dated cheque coverage."""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.core.date_ranges import business_localdate
from apps.purchases import services as purchases
from apps.reports.queries import cash_flow, purchases_summary
from apps.sales.models import PaymentMethod
from apps.suppliers.models import Supplier, SupplierPayment

from .base import TenantTestCase

D = Decimal


class SupplierPaymentPhase4ATests(TenantTestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            business=self.business_a, code="PDC-SUP", name="PDC Supplier",
        )
        self.bank_a = PaymentMethod.objects.for_business(self.business_a).get(
            kind=PaymentMethod.Kind.BANK,
        )
        self.tomorrow = business_localdate(self.business_a) + timedelta(days=1)

    def make_purchase(self, *, receive=True):
        purchase = purchases.create_purchase(
            business=self.business_a,
            supplier=self.supplier,
            branch=self.branch_a,
            warehouse=self.warehouse_a,
            rows=[{
                "product": self.product_a,
                "variant": None,
                "quantity": D("100"),
                "unit_cost": D("5.000"),
            }],
            user=self.owner_a,
            purchase_date=business_localdate(self.business_a),
        )
        if receive:
            item = purchase.items.get()
            purchases.receive_purchase(
                purchase=purchase,
                quantities={item.pk: item.quantity_ordered},
                user=self.owner_a,
            )
            purchase.refresh_from_db()
            self.supplier.refresh_from_db()
        return purchase

    def cheque(self, amount, number="CHQ-001", **overrides):
        row = {
            "method": SupplierPayment.Method.CHEQUE,
            "amount": D(amount),
            "cheque_number": number,
            "bank_name": "Bank Muscat",
            "due_date": self.tomorrow,
        }
        row.update(overrides)
        return row

    def immediate(self, method, amount, **overrides):
        row = {"method": method, "amount": D(amount)}
        row.update(overrides)
        return row

    def record(self, purchase, rows):
        return purchases.record_purchase_payments(
            purchase=purchase, rows=rows, user=self.owner_a,
        )

    def test_cash_card_and_bank_transfer_settle_immediately(self):
        purchase = self.make_purchase()
        payments = self.record(purchase, [
            self.immediate(SupplierPayment.Method.CASH, "100"),
            self.immediate(SupplierPayment.Method.CARD, "50"),
            self.immediate(SupplierPayment.Method.BANK, "75"),
        ])

        purchase.refresh_from_db()
        self.supplier.refresh_from_db()
        self.assertEqual(
            [payment.method for payment in payments],
            ["cash", "card", "bank"],
        )
        self.assertEqual(purchase.amount_paid, D("225.000"))
        self.assertEqual(purchase.cheques_pending, D("0"))
        self.assertEqual(purchase.remaining_balance, D("275.000"))
        self.assertEqual(purchase.supplier_balance, D("275.000"))
        self.assertEqual(self.supplier.balance, D("275.000"))

    def test_partial_payment_and_multiple_pending_cheques(self):
        purchase = self.make_purchase()
        self.record(purchase, [
            self.immediate(SupplierPayment.Method.CASH, "100"),
            self.cheque("150", "CHQ-150"),
            self.cheque("250", "CHQ-250"),
        ])

        purchase.refresh_from_db()
        self.supplier.refresh_from_db()
        self.assertEqual(purchase.total, D("500.000"))
        self.assertEqual(purchase.amount_paid, D("100.000"))
        self.assertEqual(purchase.cheques_pending, D("400.000"))
        self.assertEqual(purchase.remaining_balance, D("0.000"))
        self.assertEqual(purchase.supplier_balance, D("400.000"))
        self.assertEqual(self.supplier.balance, D("400.000"))
        cheques = purchase.payments.filter(method="cheque").order_by("amount")
        self.assertEqual(list(cheques.values_list("cheque_status", flat=True)), [
            "pending", "pending",
        ])

    def test_pending_cheque_clears_once(self):
        purchase = self.make_purchase()
        self.record(purchase, [
            self.immediate(SupplierPayment.Method.CASH, "100"),
            self.cheque("150", "CHQ-CLEAR"),
            self.cheque("250", "CHQ-STILL-PENDING"),
        ])
        cheque = purchase.payments.get(cheque_number="CHQ-CLEAR")

        purchases.update_cheque_status(
            payment=cheque,
            status=SupplierPayment.ChequeStatus.CLEARED,
            user=self.owner_a,
        )
        purchases.update_cheque_status(
            payment=cheque,
            status=SupplierPayment.ChequeStatus.CLEARED,
            user=self.owner_a,
        )

        purchase.refresh_from_db()
        cheque.refresh_from_db()
        self.supplier.refresh_from_db()
        self.assertEqual(purchase.amount_paid, D("250.000"))
        self.assertEqual(purchase.cheques_pending, D("250.000"))
        self.assertEqual(purchase.remaining_balance, D("0.000"))
        self.assertEqual(purchase.supplier_balance, D("250.000"))
        self.assertEqual(self.supplier.balance, D("250.000"))
        self.assertIsNotNone(cheque.cleared_at)

    def test_bounced_cheque_frees_room_for_replacement(self):
        purchase = self.make_purchase()
        self.record(purchase, [
            self.immediate(SupplierPayment.Method.CASH, "100"),
            self.cheque("150", "CHQ-BOUNCE"),
            self.cheque("250", "CHQ-PENDING"),
        ])
        cheque = purchase.payments.get(cheque_number="CHQ-BOUNCE")
        purchases.update_cheque_status(
            payment=cheque,
            status=SupplierPayment.ChequeStatus.BOUNCED,
            user=self.owner_a,
        )

        purchase.refresh_from_db()
        self.supplier.refresh_from_db()
        self.assertEqual(purchase.amount_paid, D("100.000"))
        self.assertEqual(purchase.cheques_pending, D("250.000"))
        self.assertEqual(purchase.remaining_balance, D("150.000"))
        self.assertEqual(purchase.supplier_balance, D("400.000"))
        self.assertEqual(self.supplier.balance, D("400.000"))

        self.record(purchase, [self.cheque("150", "CHQ-REPLACEMENT")])
        purchase.refresh_from_db()
        self.assertEqual(purchase.cheques_pending, D("400.000"))
        self.assertEqual(purchase.remaining_balance, D("0.000"))

    def test_cancelled_cheque_frees_remaining_balance(self):
        purchase = self.make_purchase()
        cheque = self.record(purchase, [self.cheque("500", "CHQ-CANCEL")])[0]
        purchases.update_cheque_status(
            payment=cheque,
            status=SupplierPayment.ChequeStatus.CANCELLED,
            user=self.owner_a,
        )

        purchase.refresh_from_db()
        self.supplier.refresh_from_db()
        self.assertEqual(purchase.amount_paid, D("0.000"))
        self.assertEqual(purchase.cheques_pending, D("0.000"))
        self.assertEqual(purchase.remaining_balance, D("500.000"))
        self.assertEqual(purchase.supplier_balance, D("500.000"))
        self.assertEqual(self.supplier.balance, D("500.000"))

    def test_terminal_cheque_status_cannot_be_changed(self):
        purchase = self.make_purchase()
        cheque = self.record(purchase, [self.cheque("200")])[0]
        purchases.update_cheque_status(
            payment=cheque,
            status=SupplierPayment.ChequeStatus.BOUNCED,
            user=self.owner_a,
        )
        with self.assertRaisesMessage(
            ValidationError, "This cheque status can no longer be changed.",
        ):
            purchases.update_cheque_status(
                payment=cheque,
                status=SupplierPayment.ChequeStatus.CLEARED,
                user=self.owner_a,
            )

    def test_over_allocation_is_rejected_atomically(self):
        purchase = self.make_purchase()
        with self.assertRaisesMessage(
            ValidationError,
            "Paid plus Pending Cheques cannot exceed Purchase Total.",
        ):
            self.record(purchase, [
                self.immediate(SupplierPayment.Method.CASH, "100"),
                self.cheque("401"),
            ])

        purchase.refresh_from_db()
        self.supplier.refresh_from_db()
        self.assertEqual(purchase.payments.count(), 0)
        self.assertEqual(purchase.amount_paid, D("0.000"))
        self.assertEqual(self.supplier.balance, D("500.000"))

    def test_existing_pending_allocation_blocks_overpayment(self):
        purchase = self.make_purchase()
        self.record(purchase, [self.cheque("400")])
        with self.assertRaises(ValidationError):
            self.record(purchase, [
                self.immediate(SupplierPayment.Method.CARD, "100.001"),
            ])

    def test_cheque_validation_and_immediate_field_ignoring(self):
        purchase = self.make_purchase()
        invalid_rows = [
            self.cheque("100", cheque_number=""),
            self.cheque("100", bank_name=""),
            self.cheque("100", due_date=business_localdate(self.business_a)),
            self.cheque("100", due_date="2026-99-99"),
            self.cheque("0"),
            {"method": "crypto", "amount": "100"},
        ]
        for row in invalid_rows:
            with self.subTest(row=row), self.assertRaises(ValidationError):
                self.record(purchase, [row])

        payment = self.record(purchase, [self.immediate(
            SupplierPayment.Method.CASH,
            "25",
            cheque_number="IGNORED",
            bank_name="IGNORED",
            due_date=self.tomorrow,
        )])[0]
        self.assertEqual(payment.cheque_number, "")
        self.assertEqual(payment.bank_name, "")
        self.assertIsNone(payment.due_date)
        self.assertEqual(payment.cheque_status, "")

    def test_cross_tenant_payment_method_is_rejected(self):
        purchase = self.make_purchase()
        cash_b = PaymentMethod.objects.for_business(self.business_b).get(kind="cash")
        with self.assertRaisesMessage(
            ValidationError, "Invalid payment method for this business.",
        ):
            purchases.pay_purchase(
                purchase=purchase,
                amount=D("10"),
                method=cash_b,
                user=self.owner_a,
            )

    def test_multi_row_purchase_view_and_locked_labels(self):
        purchase = self.make_purchase()
        self.client.force_login(self.owner_a)
        response = self.client.post(
            reverse("purchases:pay", args=[purchase.public_id]),
            {
                "method": ["cash", "cheque"],
                "amount": ["100", "400"],
                "reference": ["CASH-REF", ""],
                "cheque_number": ["", "CHQ-WEB"],
                "bank_name": ["", "Bank Muscat"],
                "due_date": ["", self.tomorrow.isoformat()],
            },
        )
        self.assertRedirects(
            response, reverse("purchases:detail", args=[purchase.public_id]),
        )
        detail = self.client.get(reverse("purchases:detail", args=[purchase.public_id]))
        for label in (
            "Purchase Total", "Paid", "Cheques Pending", "Remaining Balance",
            "Supplier Balance", "Cheque Number", "Bank Name", "Due Date",
        ):
            self.assertContains(detail, label)
        purchase.refresh_from_db()
        self.assertEqual(purchase.amount_paid, D("100.000"))
        self.assertEqual(purchase.cheques_pending, D("400.000"))

    def test_cross_tenant_view_access_is_hidden(self):
        supplier_b = Supplier.objects.create(
            business=self.business_b, code="PDC-B", name="Other Tenant Supplier",
        )
        purchase_b = purchases.create_purchase(
            business=self.business_b,
            supplier=supplier_b,
            branch=self.branch_b,
            warehouse=self.warehouse_b,
            rows=[{
                "product": self.product_b,
                "variant": None,
                "quantity": D("1"),
                "unit_cost": D("5"),
            }],
            user=self.owner_b,
            purchase_date=business_localdate(self.business_b),
        )
        self.client.force_login(self.owner_a)
        response = self.client.post(
            reverse("purchases:pay", args=[purchase_b.public_id]),
            {"method": "cash", "amount": "1"},
        )
        self.assertEqual(response.status_code, 404)

    def test_purchase_manage_permission_is_required(self):
        purchase = self.make_purchase()
        self.client.force_login(self.cashier_a)
        response = self.client.post(
            reverse("purchases:pay", args=[purchase.public_id]),
            {"method": "cash", "amount": "1"},
        )
        self.assertEqual(response.status_code, 403)

    def test_cash_flow_counts_only_settled_money(self):
        purchase = self.make_purchase()
        cheque = self.record(purchase, [
            self.immediate(SupplierPayment.Method.CASH, "100"),
            self.cheque("150"),
        ])[1]
        today = business_localdate(self.business_a)
        filters = {"date_from": today, "date_to": today}
        report = cash_flow(self.business_a, filters)
        supplier_row = next(row for row in report["rows"] if row[0] == "  Supplier payments")
        self.assertEqual(supplier_row[1], D("-100.000"))

        purchases.update_cheque_status(
            payment=cheque,
            status=SupplierPayment.ChequeStatus.CLEARED,
            user=self.owner_a,
        )
        report = cash_flow(self.business_a, filters)
        supplier_row = next(row for row in report["rows"] if row[0] == "  Supplier payments")
        self.assertEqual(supplier_row[1], D("-250.000"))

    def test_purchase_report_uses_phase_4a_calculations(self):
        purchase = self.make_purchase()
        self.record(purchase, [
            self.immediate(SupplierPayment.Method.CASH, "100"),
            self.cheque("150"),
        ])
        today = business_localdate(self.business_a)
        report = purchases_summary(
            self.business_a, {"date_from": today, "date_to": today},
        )
        self.assertEqual(report["columns"][3:8], [
            "Purchase Total", "Paid", "Cheques Pending", "Remaining Balance",
            "Supplier Balance",
        ])
        row = report["rows"][0]
        self.assertEqual(row[3:8], [
            D("500.000"), D("100.000"), D("150.000"), D("250.000"),
            D("400.000"),
        ])

    def test_existing_immediate_payment_entry_point_remains_compatible(self):
        purchase = self.make_purchase()
        payment = purchases.pay_purchase(
            purchase=purchase,
            amount=D("20"),
            method=self.cash_a,
            user=self.owner_a,
        )
        self.assertEqual(payment.method, SupplierPayment.Method.CASH)
        self.assertEqual(payment.payment_method, self.cash_a)
        purchase.refresh_from_db()
        self.assertEqual(purchase.amount_paid, D("20.000"))

    def test_pre_phase4a_supplier_payment_row_remains_readable(self):
        purchase = self.make_purchase()
        payment = SupplierPayment.objects.create(
            business=self.business_a,
            payment_number="SPY-LEGACY",
            supplier=self.supplier,
            purchase=purchase,
            amount=D("20.000"),
            payment_method=self.cash_a,
            paid_by=self.owner_a,
        )
        purchase.amount_paid = D("20.000")
        purchase.save(update_fields=["amount_paid", "updated_at"])

        payment.refresh_from_db()
        self.assertEqual(payment.method, "")
        self.assertEqual(payment.method_label, "Cash")
        self.client.force_login(self.owner_a)
        detail = self.client.get(
            reverse("purchases:detail", args=[purchase.public_id]),
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Cash")
