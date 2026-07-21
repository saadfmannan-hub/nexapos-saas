"""Cash register shift tests."""
from decimal import Decimal

from apps.registers import services as registers
from apps.registers.services import ShiftError

from .base import TenantTestCase

D = Decimal


class ShiftTests(TenantTestCase):
    def open_shift(self, cash="50.000", cashier=None):
        return registers.open_shift(
            business=self.business_a, register=self.register_a,
            cashier=cashier or self.owner_a, opening_cash=D(cash),
        )

    def test_open_and_close_with_expected_cash(self):
        shift = self.open_shift()
        # Cash sale of 21.000 inside the shift
        self.make_sale(register=self.register_a, shift=shift)
        totals = registers.shift_totals(shift)
        self.assertEqual(totals["cash_sales"], D("21.000"))
        self.assertEqual(totals["expected_cash"], D("71.000"))
        registers.close_shift(shift=shift, actual_cash=D("70.000"), user=self.owner_a)
        shift.refresh_from_db()
        self.assertEqual(shift.expected_cash, D("71.000"))
        self.assertEqual(shift.difference, D("-1.000"))
        self.assertEqual(shift.status, "closed")

    def test_double_open_blocked(self):
        self.open_shift()
        with self.assertRaises(ShiftError):
            self.open_shift(cashier=self.cashier_a)

    def test_cash_refund_reduces_expected_cash(self):
        from apps.sales import services as sales
        from apps.sales.models import SaleReturn

        shift = self.open_shift()
        sale = self.make_sale(register=self.register_a, shift=shift)
        item = sale.items.get()
        sales.process_return(
            sale=sale, items=[{"sale_item": item, "quantity": D("1")}],
            refund_method=SaleReturn.RefundMethod.CASH, user=self.owner_a,
            shift=shift,
        )
        totals = registers.shift_totals(shift)
        self.assertEqual(totals["gross_cash_sales"], D("21.000"))
        self.assertEqual(totals["cash_sales"], D("10.500"))
        self.assertEqual(totals["cash_refunds"], D("10.500"))
        self.assertEqual(totals["expected_cash"], D("60.500"))

    def test_reopen_requires_closed_shift(self):
        shift = self.open_shift()
        with self.assertRaises(ShiftError):
            registers.reopen_shift(shift=shift, user=self.owner_a)
        registers.close_shift(shift=shift, actual_cash=D("50.000"), user=self.owner_a)
        registers.reopen_shift(shift=shift, user=self.owner_a)
        shift.refresh_from_db()
        self.assertEqual(shift.status, "open")
        self.assertEqual(shift.reopened_count, 1)

    def test_unauthorized_reopen_blocked_at_view(self):
        from django.urls import reverse

        shift = self.open_shift(cashier=self.cashier_a)
        registers.close_shift(shift=shift, actual_cash=D("50.000"), user=self.cashier_a)
        self.client.force_login(self.cashier_a)
        response = self.client.post(
            reverse("registers:shift_reopen", args=[shift.public_id])
        )
        self.assertEqual(response.status_code, 403)
        shift.refresh_from_db()
        self.assertEqual(shift.status, "closed")
