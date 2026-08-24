"""Focused no-500 coverage for commerce write boundaries."""

import json
from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError
from django.urls import reverse

from apps.branches.models import Warehouse
from apps.customers.models import Customer
from apps.expenses.models import ExpenseCategory
from apps.expenses.views import ExpenseCategoryForm
from apps.inventory import workflows
from apps.inventory.models import StockTransfer
from apps.purchases.models import Purchase
from apps.registers import services as register_services
from apps.registers.models import Shift
from apps.sales.models import HeldSale, Sale, SaleReturn
from apps.subscriptions.models import Plan, Subscription
from apps.suppliers.models import Supplier

from .base import TenantTestCase

D = Decimal


class ExpenseCategoryIntegrityTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)
        self.url = reverse("expenses:categories")

    def post_category(self, *, name, parent=None, edit=None):
        url = self.url
        if edit is not None:
            url = f"{url}?edit={edit.public_id}"
        return self.client.post(
            url,
            {
                "name": name,
                "parent": parent.pk if parent is not None else "",
                "is_active": "on",
            },
        )

    def test_root_duplicate_is_trimmed_and_rejected_case_insensitively(self):
        existing = ExpenseCategory.objects.create(
            business=self.business_a,
            name="Audit Travel",
        )

        for submitted_name in ("Audit Travel", "audit travel", "  AUDIT TRAVEL  "):
            with self.subTest(submitted_name=submitted_name):
                response = self.post_category(name=submitted_name)

                self.assertEqual(response.status_code, 200)
                self.assertIn("name", response.context["form"].errors)
                self.assertEqual(
                    ExpenseCategory.objects.filter(
                        business=self.business_a,
                        name__iexact=existing.name,
                        parent__isnull=True,
                    ).count(),
                    1,
                )

    def test_duplicate_name_is_scoped_by_parent(self):
        parent_one = ExpenseCategory.objects.create(
            business=self.business_a,
            name="Audit Operations One",
        )
        parent_two = ExpenseCategory.objects.create(
            business=self.business_a,
            name="Audit Operations Two",
        )
        ExpenseCategory.objects.create(
            business=self.business_a,
            name="Courier",
            parent=parent_one,
        )

        duplicate = self.post_category(name=" courier ", parent=parent_one)
        allowed = self.post_category(name=" courier ", parent=parent_two)

        self.assertEqual(duplicate.status_code, 200)
        self.assertIn("name", duplicate.context["form"].errors)
        self.assertRedirects(allowed, self.url)
        self.assertTrue(
            ExpenseCategory.objects.filter(
                business=self.business_a,
                name="courier",
                parent=parent_two,
            ).exists()
        )

    def test_edit_collision_leaves_both_categories_unchanged(self):
        existing = ExpenseCategory.objects.create(
            business=self.business_a,
            name="Audit Utilities",
        )
        edited = ExpenseCategory.objects.create(
            business=self.business_a,
            name="Audit Maintenance",
        )

        response = self.post_category(name=" audit utilities ", edit=edited)

        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)
        existing.refresh_from_db()
        edited.refresh_from_db()
        self.assertEqual(existing.name, "Audit Utilities")
        self.assertEqual(edited.name, "Audit Maintenance")

    def test_same_root_name_is_allowed_in_another_tenant(self):
        ExpenseCategory.objects.create(
            business=self.business_b,
            name="Audit Tenant Shared",
        )

        response = self.post_category(name="  audit tenant shared  ")

        self.assertRedirects(response, self.url)
        self.assertEqual(
            ExpenseCategory.objects.filter(
                business=self.business_a,
                name__iexact="Audit Tenant Shared",
                parent__isnull=True,
            ).count(),
            1,
        )
        self.assertEqual(
            ExpenseCategory.objects.filter(
                business=self.business_b,
                name__iexact="Audit Tenant Shared",
                parent__isnull=True,
            ).count(),
            1,
        )

    def test_expected_integrity_race_becomes_a_name_error(self):
        parent = ExpenseCategory.objects.create(
            business=self.business_a,
            name="Audit Race Parent",
        )
        ExpenseCategory.objects.create(
            business=self.business_a,
            name="Audit Race Child",
            parent=parent,
        )

        with patch.object(
            ExpenseCategoryForm,
            "_duplicate_name_exists",
            return_value=False,
        ):
            response = self.post_category(
                name="Audit Race Child",
                parent=parent,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)
        self.assertEqual(
            ExpenseCategory.objects.filter(
                business=self.business_a,
                name="Audit Race Child",
                parent=parent,
            ).count(),
            1,
        )

    def test_unrelated_integrity_error_is_not_swallowed(self):
        with patch.object(
            ExpenseCategory,
            "save",
            side_effect=IntegrityError("different database constraint"),
        ):
            with self.assertRaisesMessage(
                IntegrityError,
                "different database constraint",
            ):
                self.post_category(name="Audit Unrelated Constraint")


class MalformedCommerceInputTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)
        self.allow_no_shift()
        subscription = Subscription.objects.get(business=self.business_a)
        Plan.objects.filter(pk=subscription.plan_id).update(feature_transfers=True)

    def checkout_payload(self):
        return {
            "branch_id": self.branch_a.pk,
            "customer_id": self.walk_in_a.pk,
            "checkout_token": "audit-malformed-checkout",
            "items": [
                {
                    "product_id": self.product_a.pk,
                    "quantity": "1",
                    "unit_price": "10.000",
                }
            ],
            "payments": [
                {
                    "method_id": self.cash_a.pk,
                    "amount": "10.500",
                }
            ],
        }

    def post_checkout(self, payload):
        return self.client.post(
            reverse("sales:pos_checkout"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_checkout_rejects_malformed_numeric_ids(self):
        payloads = []

        payload = self.checkout_payload()
        payload["branch_id"] = "not-a-number"
        payloads.append(("branch", payload))

        payload = self.checkout_payload()
        payload["customer_id"] = {"not": "an id"}
        payloads.append(("customer", payload))

        payload = self.checkout_payload()
        payload["items"][0]["product_id"] = "not-a-number"
        payloads.append(("product", payload))

        payload = self.checkout_payload()
        payload["items"][0]["variant_id"] = "not-a-number"
        payloads.append(("variant", payload))

        payload = self.checkout_payload()
        payload["payments"][0]["method_id"] = []
        payloads.append(("payment method", payload))

        payload = self.checkout_payload()
        payload["held_id"] = "not-a-number"
        payloads.append(("held sale", payload))

        payload = self.checkout_payload()
        payload["held_id"] = 10**100
        payloads.append(("oversized held sale", payload))

        for label, payload in payloads:
            with self.subTest(label=label):
                response = self.post_checkout(payload)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertFalse(response.json()["ok"])

        self.assertFalse(
            Sale.objects.for_business(self.business_a)
            .filter(checkout_token="audit-malformed-checkout")
            .exists()
        )

    def test_checkout_rejects_malformed_money_and_quantity_values(self):
        payloads = []

        payload = self.checkout_payload()
        payload["items"][0]["quantity"] = "not-a-number"
        payloads.append(("quantity", payload))

        payload = self.checkout_payload()
        payload["items"][0]["unit_price"] = "Infinity"
        payloads.append(("unit price", payload))

        payload = self.checkout_payload()
        payload["payments"][0]["amount"] = "not-a-number"
        payloads.append(("payment amount", payload))

        payload = self.checkout_payload()
        payload["invoice_discount"] = "NaN"
        payloads.append(("invoice discount", payload))

        for label, payload in payloads:
            with self.subTest(label=label):
                response = self.post_checkout(payload)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertFalse(response.json()["ok"])

        self.assertFalse(
            Sale.objects.for_business(self.business_a)
            .filter(checkout_token="audit-malformed-checkout")
            .exists()
        )

    def test_hold_sale_rejects_invalid_json_shapes_and_product_id(self):
        valid_cart = {
            "items": [{"product_id": self.product_a.pk}],
            "checkout_token": "audit-hold-token",
        }
        payloads = (
            [],
            {"branch_id": self.branch_a.pk, "cart": []},
            {"branch_id": self.branch_a.pk, "cart": {"items": {"bad": "shape"}}},
            {"branch_id": "bad-id", "cart": valid_cart},
            {
                "branch_id": self.branch_a.pk,
                "cart": {
                    "items": [{"product_id": "bad-id"}],
                    "checkout_token": "audit-hold-bad-product",
                },
            },
            {
                "branch_id": self.branch_a.pk,
                "cart": {
                    "items": [{"product_id": [self.product_a.pk]}],
                    "checkout_token": "audit-hold-unhashable-product",
                },
            },
        )

        for payload in payloads:
            with self.subTest(payload=payload):
                response = self.client.post(
                    reverse("sales:pos_hold"),
                    data=json.dumps(payload),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400, response.content)
                self.assertFalse(response.json()["ok"])

        self.assertFalse(HeldSale.objects.for_business(self.business_a).exists())

    def test_checkout_reraises_unrelated_integrity_errors(self):
        payload = self.checkout_payload()
        payload["checkout_token"] = "audit-unrelated-integrity"

        with (
            patch(
                "apps.sales.views.services.complete_sale",
                side_effect=IntegrityError("unrelated checkout constraint"),
            ),
            self.assertRaisesMessage(
                IntegrityError,
                "unrelated checkout constraint",
            ),
        ):
            self.post_checkout(payload)

        self.assertFalse(
            Sale.objects.for_business(self.business_a)
            .filter(checkout_token="audit-unrelated-integrity")
            .exists()
        )

    def test_checkout_reraises_unexpected_application_errors(self):
        payload = self.checkout_payload()
        payload["checkout_token"] = "audit-unexpected-error"

        with (
            patch(
                "apps.sales.views.services.complete_sale",
                side_effect=RuntimeError("unexpected checkout defect"),
            ),
            self.assertRaisesMessage(RuntimeError, "unexpected checkout defect"),
        ):
            self.post_checkout(payload)

        self.assertFalse(
            Sale.objects.for_business(self.business_a)
            .filter(checkout_token="audit-unexpected-error")
            .exists()
        )

    def test_checkout_and_hold_reject_invalid_utf8_json(self):
        for route in ("sales:pos_checkout", "sales:pos_hold"):
            with self.subTest(route=route):
                response = self.client.post(
                    reverse(route),
                    data=b"\xff",
                    content_type="application/json",
                )

                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["ok"])

    def test_inventory_transfer_rejects_nan_quantity(self):
        destination = Warehouse.objects.create(
            business=self.business_a,
            branch=self.branch_a,
            name="Audit Destination",
            code="AUDIT-DEST",
        )

        response = self.client.post(
            reverse("inventory:transfer_create"),
            {
                "from_warehouse": self.warehouse_a.pk,
                "to_warehouse": destination.pk,
                "product_id": [self.product_a.pk],
                "variant_id": [""],
                "quantity": ["NaN"],
                "notes": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Quantities must be finite numbers")
        self.assertFalse(StockTransfer.objects.for_business(self.business_a).exists())

    def test_inventory_transfer_rejects_malformed_row_without_partial_write(self):
        destination = Warehouse.objects.create(
            business=self.business_a,
            branch=self.branch_a,
            name="Audit Malformed Destination",
            code="AUDIT-MALFORMED-DEST",
        )

        response = self.client.post(
            reverse("inventory:transfer_create"),
            {
                "from_warehouse": self.warehouse_a.pk,
                "to_warehouse": destination.pk,
                "product_id": [self.product_a.pk, self.product_a.pk],
                "variant_id": ["", ""],
                "quantity": ["1", "not-a-number"],
                "notes": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Quantities must be valid numbers")
        self.assertFalse(StockTransfer.objects.for_business(self.business_a).exists())

    def test_purchase_rejects_malformed_numeric_values_without_partial_write(self):
        supplier = Supplier.objects.create(
            business=self.business_a,
            code="AUDIT-NUMERIC-SUP",
            name="Audit Numeric Supplier",
        )
        base_payload = {
            "supplier_id": supplier.pk,
            "branch_id": self.branch_a.pk,
            "warehouse_id": self.warehouse_a.pk,
            "purchase_date": "2026-08-23",
            "due_date": "",
            "supplier_invoice_number": "AUDIT-NUMERIC-PURCHASE",
            "product_id": [str(self.product_a.pk)],
            "variant_id": [""],
            "quantity": ["1.000"],
            "unit_cost": ["10.000"],
            "discount": "0.000",
            "shipping": "0.000",
            "other": "0.000",
            "notes": "",
        }
        invalid_values = (
            ("quantity", ["not-a-number"]),
            ("unit_cost", ["Infinity"]),
            ("discount", "NaN"),
            ("shipping", "not-a-number"),
            ("other", "Infinity"),
        )

        for field, value in invalid_values:
            with self.subTest(field=field):
                payload = {**base_payload, field: value}
                response = self.client.post(reverse("purchases:create"), payload)

                self.assertEqual(response.status_code, 200)
                self.assertFalse(
                    Purchase.objects.for_business(self.business_a)
                    .filter(supplier_invoice_number="AUDIT-NUMERIC-PURCHASE")
                    .exists()
                )

    def test_stock_count_rejects_nan_without_partial_updates(self):
        count = workflows.start_count(
            business=self.business_a,
            warehouse=self.warehouse_a,
            user=self.owner_a,
        )
        items = list(count.items.order_by("pk"))
        self.assertTrue(items)
        payload = {"action": "save"}
        payload.update({f"counted_{item.pk}": "1" for item in items})
        payload[f"counted_{items[-1].pk}"] = "NaN"

        response = self.client.post(
            reverse("inventory:count_detail", args=[count.public_id]),
            payload,
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Counted quantities must be finite numbers")
        self.assertFalse(count.items.exclude(counted_quantity__isnull=True).exists())

    def test_stock_count_rejects_malformed_value_without_partial_updates(self):
        count = workflows.start_count(
            business=self.business_a,
            warehouse=self.warehouse_a,
            user=self.owner_a,
        )
        items = list(count.items.order_by("pk"))
        self.assertTrue(items)
        payload = {"action": "save"}
        payload.update({f"counted_{item.pk}": "1" for item in items})
        payload[f"counted_{items[-1].pk}"] = "not-a-number"

        response = self.client.post(
            reverse("inventory:count_detail", args=[count.public_id]),
            payload,
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Counted quantities must be valid numbers")
        self.assertFalse(count.items.exclude(counted_quantity__isnull=True).exists())

    def test_register_open_and_close_reject_nan_cash(self):
        open_response = self.client.post(
            reverse("registers:shift_open"),
            {
                "register_id": self.register_a.pk,
                "opening_cash": "NaN",
            },
            follow=True,
        )

        self.assertEqual(open_response.status_code, 200)
        self.assertContains(open_response, "Enter a finite opening cash amount")
        self.assertFalse(
            Shift.objects.for_business(self.business_a).filter(status=Shift.Status.OPEN).exists()
        )

        shift = register_services.open_shift(
            business=self.business_a,
            register=self.register_a,
            cashier=self.owner_a,
            opening_cash=D("10.000"),
        )
        close_response = self.client.post(
            reverse("registers:shift_close", args=[shift.public_id]),
            {"actual_cash": "NaN"},
        )

        self.assertEqual(close_response.status_code, 200)
        self.assertContains(close_response, "Enter a finite actual cash amount")
        shift.refresh_from_db()
        self.assertEqual(shift.status, Shift.Status.OPEN)
        self.assertIsNone(shift.actual_cash)

    def test_sale_return_rejects_nan_quantity(self):
        sale = self.make_sale()
        item = sale.items.get()

        response = self.client.post(
            reverse("sales:return_create", args=[sale.public_id]),
            {
                f"qty_{item.pk}": "NaN",
                "refund_method": SaleReturn.RefundMethod.CASH,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a finite return quantity")
        self.assertFalse(SaleReturn.objects.filter(sale=sale).exists())
        item.refresh_from_db()
        self.assertEqual(item.returned_quantity, D("0"))

    def test_sale_return_reraises_unexpected_application_errors(self):
        sale = self.make_sale()
        item = sale.items.get()

        with (
            patch(
                "apps.sales.views.services.process_return",
                side_effect=RuntimeError("unexpected return defect"),
            ),
            self.assertRaisesMessage(RuntimeError, "unexpected return defect"),
        ):
            self.client.post(
                reverse("sales:return_create", args=[sale.public_id]),
                {
                    f"qty_{item.pk}": "1",
                    "refund_method": SaleReturn.RefundMethod.CASH,
                },
            )

        self.assertFalse(SaleReturn.objects.filter(sale=sale).exists())

    def test_later_sale_payment_rejects_nan_amount(self):
        customer = Customer.objects.create(
            business=self.business_a,
            home_branch=self.branch_a,
            code="AUDIT-CREDIT",
            full_name="Audit Credit Customer",
            credit_limit=D("100.000"),
        )
        sale = self.make_sale(
            customer=customer,
            payments=[{"method": self.credit_a, "amount": D("21.000")}],
        )
        payment_count = sale.payments.count()

        response = self.client.post(
            reverse("sales:payment_add", args=[sale.public_id]),
            {
                "method_id": self.cash_a.pk,
                "amount": "NaN",
                "reference": "",
                "notes": "",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a finite payment amount")
        sale.refresh_from_db()
        self.assertEqual(sale.payments.count(), payment_count)
        self.assertEqual(sale.amount_paid, D("0.000"))
