"""Commercial stabilization coverage for POS tailoring bookings."""
import json
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import FieldDoesNotExist
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, Role, User
from apps.api.serializers import ProductSerializer, SaleSerializer
from apps.branches.models import Branch, Warehouse
from apps.catalog.forms import ProductForm
from apps.catalog.models import ProductVariant
from apps.customers.models import Customer
from apps.inventory import services as inventory
from apps.inventory.models import StockMovement
from apps.registers import services as register_services
from apps.sales import services as sales
from apps.sales.models import HeldSale, PaymentMethod, Sale, SaleItem, SalePayment
from apps.sales.services import SaleError

from .base import TenantTestCase

D = Decimal


class TailoringBookingContractTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()
        self.product_a.is_tailoring_item = True
        self.product_a.save(update_fields=["is_tailoring_item"])
        self.client.force_login(self.owner_a)
        self.checkout_token_counter = 0

    def checkout(self, payload):
        return self.client.post(
            reverse("sales:pos_checkout"),
            json.dumps(payload),
            content_type="application/json",
        )

    def payload(self, **overrides):
        self.checkout_token_counter += 1
        payload = {
            "checkout_token": f"booking-{self.checkout_token_counter}",
            "branch_id": self.branch_a.id,
            "customer_id": self.walk_in_a.id,
            "items": [{
                "product_id": self.product_a.id,
                "variant_id": None,
                "quantity": "1",
                "unit_price": "10.000",
                "discount_amount": "0",
                "garment_classification": "adult",
                "collection_type": "normal",
                "tailoring_details": {},
            }],
            "payments": [{"method_id": self.cash_a.id, "amount": "10.500"}],
            "invoice_discount": "0",
            "delivery_date": str(timezone.localdate()),
            "priority": "normal",
        }
        payload.update(overrides)
        return payload

    def sale_for(self, response):
        data = response.json()
        self.assertTrue(data["ok"], data)
        return Sale.objects.for_business(self.business_a).get(
            public_id=data["sale"]["public_id"]
        )

    def test_adult_is_accepted_and_stored_on_sale_item(self):
        sale = self.sale_for(self.checkout(self.payload()))
        self.assertEqual(
            sale.items.get().garment_classification,
            SaleItem.GarmentClassification.ADULT,
        )

    def test_child_is_accepted_and_stored_on_sale_item(self):
        payload = self.payload()
        payload["items"][0]["garment_classification"] = "child"
        sale = self.sale_for(self.checkout(payload))
        self.assertEqual(
            sale.items.get().garment_classification,
            SaleItem.GarmentClassification.CHILD,
        )

    def test_missing_classification_returns_line_level_error(self):
        payload = self.payload()
        payload["items"][0]["garment_classification"] = ""
        data = self.checkout(payload).json()
        self.assertFalse(data["ok"])
        self.assertEqual(
            data["errors"]["items.0.garment_classification"],
            "Select Adult or Child for every garment.",
        )

    def test_invalid_classification_is_rejected(self):
        payload = self.payload()
        payload["items"][0]["garment_classification"] = "senior"
        data = self.checkout(payload).json()
        self.assertFalse(data["ok"])
        self.assertIn("items.0.garment_classification", data["errors"])

    def test_mixed_adult_and_child_lines_share_one_invoice(self):
        payload = self.payload()
        child = dict(payload["items"][0])
        child["garment_classification"] = "child"
        payload["items"].append(child)
        payload["payments"][0]["amount"] = "21.000"
        sale = self.sale_for(self.checkout(payload))
        self.assertEqual(
            list(sale.items.order_by("id").values_list(
                "garment_classification", flat=True
            )),
            ["adult", "child"],
        )

    def test_quantity_preserves_one_classification_for_the_line(self):
        payload = self.payload()
        payload["items"][0]["quantity"] = "3"
        payload["payments"][0]["amount"] = "31.500"
        sale = self.sale_for(self.checkout(payload))
        item = sale.items.get()
        self.assertEqual(item.quantity, D("3"))
        self.assertEqual(item.garment_classification, "adult")

    def test_direct_service_cannot_bypass_classification(self):
        with self.assertRaisesMessage(
            SaleError, "Select Adult or Child for every garment."
        ):
            self.make_sale(
                items=[{
                    "product": self.product_a,
                    "quantity": D("1"),
                    "unit_price": D("10"),
                }],
                delivery_date=timezone.localdate(),
            )

    def test_retail_item_does_not_require_classification_or_delivery(self):
        self.product_a.is_tailoring_item = False
        self.product_a.save(update_fields=["is_tailoring_item"])
        payload = self.payload(delivery_date=None)
        payload["items"][0]["garment_classification"] = ""
        payload["items"][0]["collection_type"] = ""
        sale = self.sale_for(self.checkout(payload))
        self.assertIsNone(sale.delivery_date)
        self.assertEqual(sale.items.get().garment_classification, "")

    def test_retail_item_rejects_forged_tailoring_metadata(self):
        self.product_a.is_tailoring_item = False
        self.product_a.save(update_fields=["is_tailoring_item"])
        payload = self.payload(delivery_date=None)
        payload["items"][0]["garment_classification"] = "adult"
        data = self.checkout(payload).json()
        self.assertFalse(data["ok"])
        self.assertIn("not configured as a tailoring garment", data["error"])

    def test_historical_blank_classification_remains_readable(self):
        self.product_a.is_tailoring_item = False
        self.product_a.save(update_fields=["is_tailoring_item"])
        sale = self.make_sale()
        item = sale.items.get()
        item.tailoring_details = {
            "design_type": "VIP 3D",
            "priority": "vip",
        }
        item.save(update_fields=["tailoring_details"])
        item.refresh_from_db()
        self.assertEqual(item.garment_classification, "")
        self.assertTrue(item.has_tailoring_details)

    def test_missing_delivery_date_is_rejected_for_tailoring(self):
        data = self.checkout(self.payload(delivery_date=None)).json()
        self.assertFalse(data["ok"])
        self.assertIn("delivery_date", data["errors"])

    def test_invalid_delivery_date_format_is_rejected(self):
        data = self.checkout(self.payload(delivery_date="13/07/2026")).json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "Invalid delivery date.")

    def test_past_delivery_date_is_rejected(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        data = self.checkout(self.payload(delivery_date=str(yesterday))).json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "Delivery date cannot be in the past.")

    def test_same_day_delivery_is_allowed(self):
        sale = self.sale_for(self.checkout(self.payload()))
        self.assertEqual(sale.delivery_date, timezone.localdate())

    def test_delivery_validation_uses_local_date(self):
        local_today = date(2026, 7, 14)
        with patch("apps.sales.views.timezone.localdate", return_value=local_today):
            data = self.checkout(
                self.payload(delivery_date="2026-07-13")
            ).json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "Delivery date cannot be in the past.")

    def test_partial_payment_booking_keeps_delivery_date(self):
        customer = Customer.objects.create(
            business=self.business_a,
            code="TAIL-CREDIT",
            full_name="Tailoring Credit Customer",
            credit_limit=D("100"),
        )
        payload = self.payload(customer_id=customer.id)
        payload["payments"] = [
            {"method_id": self.cash_a.id, "amount": "5.000"},
            {"method_id": self.credit_a.id, "amount": "5.500"},
        ]
        sale = self.sale_for(self.checkout(payload))
        self.assertEqual(sale.status, Sale.Status.PARTIAL)
        self.assertEqual(sale.delivery_date, timezone.localdate())

    def test_later_payment_does_not_rewrite_delivery_date(self):
        customer = Customer.objects.create(
            business=self.business_a,
            code="TAIL-LATER",
            full_name="Tailoring Later Payment",
            credit_limit=D("100"),
        )
        payload = self.payload(customer_id=customer.id)
        payload["payments"] = [
            {"method_id": self.cash_a.id, "amount": "5.000"},
            {"method_id": self.credit_a.id, "amount": "5.500"},
        ]
        sale = self.sale_for(self.checkout(payload))
        original_date = sale.delivery_date
        sales.add_sale_payment(
            sale=sale,
            amount=D("5.500"),
            method=self.card_a,
            user=self.owner_a,
        )
        sale.refresh_from_db()
        self.assertEqual(sale.delivery_date, original_date)
        self.assertEqual(sale.balance, D("0"))

    def test_all_priority_choices_are_stored_on_sale(self):
        for priority in ("normal", "high", "urgent"):
            with self.subTest(priority=priority):
                sale = self.sale_for(
                    self.checkout(self.payload(priority=priority))
                )
                self.assertEqual(sale.priority, priority)

    def test_missing_priority_defaults_to_visible_normal_contract(self):
        payload = self.payload()
        payload.pop("priority")
        sale = self.sale_for(self.checkout(payload))
        self.assertEqual(sale.priority, Sale.Priority.NORMAL)

    def test_invalid_priority_is_rejected(self):
        data = self.checkout(self.payload(priority="vip")).json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["errors"]["priority"], "Select a valid order priority.")

    def test_priority_is_not_stored_on_payment_or_new_item_json(self):
        sale = self.sale_for(self.checkout(self.payload(priority="high")))
        self.assertEqual(sale.priority, "high")
        self.assertNotIn("priority", sale.items.get().tailoring_details)
        with self.assertRaises(FieldDoesNotExist):
            SalePayment._meta.get_field("priority")

    def test_database_constraints_reject_invalid_operational_values(self):
        sale = self.sale_for(self.checkout(self.payload()))
        item = sale.items.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Sale.objects.filter(pk=sale.pk).update(priority="vip")
        with self.assertRaises(IntegrityError), transaction.atomic():
            SaleItem.objects.filter(pk=item.pk).update(
                garment_classification="unknown"
            )

    def test_daraz_text_is_trimmed_and_persisted(self):
        payload = self.payload()
        payload["items"][0]["tailoring_details"] = {
            "design_type": "Daraz",
            "daraz_details": "  Three fine lines  ",
        }
        sale = self.sale_for(self.checkout(payload))
        self.assertEqual(
            sale.items.get().tailoring_details["daraz_details"],
            "Three fine lines",
        )

    def test_excessive_daraz_text_is_rejected_with_field_error(self):
        payload = self.payload()
        payload["items"][0]["tailoring_details"] = {
            "daraz_details": "x" * 201,
        }
        data = self.checkout(payload).json()
        self.assertFalse(data["ok"])
        self.assertIn(
            "items.0.tailoring_details.daraz_details",
            data["errors"],
        )

    def test_daraz_html_is_escaped_in_existing_job_card_template(self):
        payload = self.payload()
        payload["items"][0]["tailoring_details"] = {
            "design_type": "Daraz",
            "daraz_details": "<script>alert(1)</script>",
        }
        sale = self.sale_for(self.checkout(payload))
        item = sale.items.select_related("product__unit", "variant").get()
        request = RequestFactory().get("/")
        request.business = self.business_a
        from apps.sales.views import _job_card_context

        html = render_to_string(
            "invoices/workshop_job_card.html",
            _job_card_context(sale, request, [item], sale_item=item),
        )
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_legacy_daraz_and_vip_design_values_remain_accepted(self):
        payload = self.payload()
        payload["items"][0]["tailoring_details"] = {
            "design_type": "VIP 3D",
            "daraz_details": "Daraz 3 line",
        }
        sale = self.sale_for(self.checkout(payload))
        details = sale.items.get().tailoring_details
        self.assertEqual(details["design_type"], "VIP 3D")
        self.assertEqual(details["daraz_details"], "Daraz 3 line")

    def test_computer_and_vip_3d_design_can_both_persist(self):
        payload = self.payload()
        payload["items"][0]["tailoring_details"] = {
            "design_type": "VIP 3D Design",
            "vip_3d_design": "VIP-22",
            "computer_design": "Sultani-9",
        }
        details = self.sale_for(self.checkout(payload)).items.get().tailoring_details
        self.assertEqual(details["design_type"], "VIP 3D Design")
        self.assertEqual(details["vip_3d_design"], "VIP-22")
        self.assertEqual(details["computer_design"], "Sultani-9")

    def test_duplicate_fabric_payload_is_not_persisted(self):
        payload = self.payload()
        payload["items"][0]["fabric"] = "forged"
        payload["items"][0]["tailoring_details"] = {
            "fabric": "duplicate",
            "daraz_details": "Line 2",
        }
        details = self.sale_for(self.checkout(payload)).items.get().tailoring_details
        self.assertNotIn("fabric", details)
        self.assertEqual(details["daraz_details"], "Line 2")

    def test_tailoring_sale_keeps_tax_and_inventory_rules_unchanged(self):
        before = inventory.get_stock(
            self.business_a, self.warehouse_a, self.product_a
        )
        payload = self.payload()
        payload["items"][0]["quantity"] = "2"
        payload["payments"][0]["amount"] = "21.000"
        sale = self.sale_for(self.checkout(payload))
        self.assertEqual(sale.subtotal, D("20.000"))
        self.assertEqual(sale.tax_amount, D("1.000"))
        self.assertEqual(sale.total, D("21.000"))
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, self.product_a
            ),
            before - D("2"),
        )

    def test_failed_line_validation_is_atomic(self):
        payload = self.payload()
        payload["items"][0]["garment_classification"] = ""
        counts = (
            Sale.objects.count(),
            SaleItem.objects.count(),
            SalePayment.objects.count(),
            StockMovement.objects.count(),
        )
        stock = inventory.get_stock(
            self.business_a, self.warehouse_a, self.product_a
        )
        self.assertFalse(self.checkout(payload).json()["ok"])
        self.assertEqual(
            (
                Sale.objects.count(),
                SaleItem.objects.count(),
                SalePayment.objects.count(),
                StockMovement.objects.count(),
            ),
            counts,
        )
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, self.product_a
            ),
            stock,
        )


class PosSecurityAndOperationsTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()
        self.product_a.is_tailoring_item = True
        self.product_a.save(update_fields=["is_tailoring_item"])
        self.client.force_login(self.owner_a)
        self.checkout_token_counter = 0

    def payload(self, **overrides):
        self.checkout_token_counter += 1
        payload = {
            "checkout_token": f"security-{self.checkout_token_counter}",
            "branch_id": self.branch_a.id,
            "customer_id": self.walk_in_a.id,
            "items": [{
                "product_id": self.product_a.id,
                "quantity": "1",
                "unit_price": "10.000",
                "discount_amount": "0",
                "garment_classification": "adult",
                "collection_type": "normal",
            }],
            "payments": [{"method_id": self.cash_a.id, "amount": "10.500"}],
            "delivery_date": str(timezone.localdate()),
            "priority": "normal",
        }
        payload.update(overrides)
        return payload

    def checkout(self, payload):
        return self.client.post(
            reverse("sales:pos_checkout"),
            json.dumps(payload),
            content_type="application/json",
        )

    def test_sale_completion_requires_sales_create_permission(self):
        viewer = User.objects.create_user(
            email="pos-viewer@example.com", password="StrongPass123!"
        )
        role = Role.objects.for_business(self.business_a).get(
            name="Read-Only Viewer"
        )
        Membership.objects.create(
            business=self.business_a, user=viewer, role=role
        )
        self.client.force_login(viewer)
        self.assertEqual(self.checkout(self.payload()).status_code, 403)

    def test_branch_locked_cashier_cannot_submit_another_branch(self):
        other_branch = Branch.objects.create(
            business=self.business_a, name="Other", code="OTHER"
        )
        self.cashier_membership.branches.add(self.branch_a)
        self.client.force_login(self.cashier_a)
        response = self.checkout(self.payload(branch_id=other_branch.id))
        self.assertEqual(response.status_code, 403)

    def test_branch_locked_cashier_cannot_hold_for_another_branch(self):
        other_branch = Branch.objects.create(
            business=self.business_a, name="Hold Other", code="HOLD-OTHER"
        )
        self.cashier_membership.branches.add(self.branch_a)
        self.client.force_login(self.cashier_a)
        response = self.client.post(
            reverse("sales:pos_hold"),
            json.dumps({
                "branch_id": other_branch.id,
                "cart": {
                    "items": [{"product_id": self.product_a.id}],
                    "checkout_token": "security-hold-other-branch",
                },
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_cross_tenant_customer_and_payment_ids_are_rejected(self):
        cash_b = PaymentMethod.objects.for_business(self.business_b).get(kind="cash")
        for overrides in (
            {"customer_id": self.walk_in_b.id},
            {"payments": [{"method_id": cash_b.id, "amount": "10.500"}]},
        ):
            with self.subTest(overrides=overrides):
                data = self.checkout(self.payload(**overrides)).json()
                self.assertFalse(data["ok"])

    def test_archived_product_and_inactive_variant_are_rejected(self):
        self.product_a.is_archived = True
        self.product_a.save(update_fields=["is_archived"])
        self.assertFalse(self.checkout(self.payload()).json()["ok"])
        self.product_a.is_archived = False
        self.product_a.product_type = self.product_a.Type.VARIANT
        self.product_a.save(update_fields=["is_archived", "product_type"])
        variant = ProductVariant.objects.create(
            business=self.business_a,
            product=self.product_a,
            name="Inactive",
            sale_price=D("10"),
            is_active=False,
        )
        payload = self.payload()
        payload["items"][0]["variant_id"] = variant.id
        self.assertFalse(self.checkout(payload).json()["ok"])

    def test_inactive_register_cannot_be_injected_into_service(self):
        self.register_a.is_active = False
        self.register_a.save(update_fields=["is_active"])
        with self.assertRaisesMessage(SaleError, "Invalid or inactive register."):
            sales.complete_sale(
                business=self.business_a,
                branch=self.branch_a,
                warehouse=self.warehouse_a,
                cashier=self.owner_a,
                customer=self.walk_in_a,
                membership=self.membership_a(),
                register=self.register_a,
                items=[{
                    "product": self.product_a,
                    "quantity": D("1"),
                    "unit_price": D("10"),
                    "garment_classification": "adult",
                }],
                payments=[{"method": self.cash_a, "amount": D("10.5")}],
                delivery_date=timezone.localdate(),
            )

    def test_active_register_metadata_is_allowed_when_shift_is_optional(self):
        sale = sales.complete_sale(
            business=self.business_a,
            branch=self.branch_a,
            warehouse=self.warehouse_a,
            cashier=self.owner_a,
            customer=self.walk_in_a,
            membership=self.membership_a(),
            register=self.register_a,
            items=[{
                "product": self.product_a,
                "quantity": D("1"),
                "unit_price": D("10"),
                "garment_classification": "adult",
            }],
            payments=[{"method": self.cash_a, "amount": D("10.5")}],
            delivery_date=timezone.localdate(),
        )
        self.assertEqual(sale.register, self.register_a)
        self.assertIsNone(sale.shift)

    def test_open_shift_must_match_submitted_branch(self):
        other_branch = Branch.objects.create(
            business=self.business_a, name="Second", code="SECOND"
        )
        Warehouse.objects.create(
            business=self.business_a,
            name="Second Warehouse",
            code="SECOND-WH",
            branch=other_branch,
            is_default=True,
        )
        register_services.open_shift(
            business=self.business_a,
            register=self.register_a,
            cashier=self.owner_a,
            opening_cash=D("0"),
        )
        data = self.checkout(self.payload(branch_id=other_branch.id)).json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "Invalid or inactive register.")

    def test_cashier_without_required_open_shift_creates_nothing(self):
        settings_obj = self.business_a.settings
        settings_obj.allow_sale_without_shift = False
        settings_obj.save(update_fields=["allow_sale_without_shift"])
        self.client.force_login(self.cashier_a)
        before = Sale.objects.count()
        data = self.checkout(self.payload()).json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "An open shift is required before selling.")
        self.assertEqual(Sale.objects.count(), before)

    def test_cashier_with_valid_shift_completes_sale_on_that_register(self):
        settings_obj = self.business_a.settings
        settings_obj.allow_sale_without_shift = False
        settings_obj.save(update_fields=["allow_sale_without_shift"])
        shift = register_services.open_shift(
            business=self.business_a,
            register=self.register_a,
            cashier=self.cashier_a,
            opening_cash=D("25"),
        )
        self.client.force_login(self.cashier_a)
        data = self.checkout(self.payload()).json()
        self.assertTrue(data["ok"], data)
        sale = Sale.objects.get(public_id=data["sale"]["public_id"])
        self.assertEqual(sale.shift, shift)
        self.assertEqual(sale.register, self.register_a)


class PosUiAndContractTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()
        self.client.force_login(self.owner_a)

    def test_pos_page_exposes_one_authoritative_sticky_summary(self):
        response = self.client.get(reverse("sales:pos"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="pos-totals"', count=1)
        self.assertContains(response, 'class="pos-cart-items"', count=1)
        self.assertContains(response, "PAY <span", count=1)

    def test_pos_operational_controls_are_in_global_header_without_shift(self):
        response = self.client.get(reverse("sales:pos"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        header_start = html.index('<header class="np-topbar">')
        header_end = html.index("</header>", header_start)
        header = html[header_start:header_end]

        self.assertIn("POS operational status", header)
        self.assertIn(self.branch_a.name, header)
        self.assertIn("No open shift", header)
        self.assertIn('data-bs-target="#heldModal"', header)
        self.assertIn('@click="toggleFullscreen()"', header)
        self.assertNotIn("pos-header-strip", html)
        self.assertLess(header_end, html.index('class="pos-shell"'))

    def test_open_shift_header_shows_register_status_and_held_count(self):
        HeldSale.objects.create(
            business=self.business_a,
            branch=self.branch_a,
            cashier=self.owner_a,
            label="Header count",
            cart={"items": [{"product_id": self.product_a.id}]},
        )
        register_services.open_shift(
            business=self.business_a,
            register=self.register_a,
            cashier=self.owner_a,
            opening_cash=D("0"),
        )

        html = self.client.get(reverse("sales:pos")).content.decode()
        header_start = html.index('<header class="np-topbar">')
        header = html[header_start:html.index("</header>", header_start)]
        self.assertIn(self.branch_a.name, header)
        self.assertIn(self.register_a.name, header)
        self.assertIn("Shift Open", header)
        self.assertIn('x-text="heldCount">1</span>', header)
        self.assertIn('x-data="pos()"', html[:header_start])

    def test_delivery_control_exists_only_inside_payment_modal(self):
        html = self.client.get(reverse("sales:pos")).content.decode()
        self.assertEqual(html.count('id="deliveryDate"'), 1)
        self.assertLess(html.index('id="payModal"'), html.index('id="deliveryDate"'))
        self.assertNotIn('data-bs-target="#payModal"', html)

    def test_pos_has_explicit_classification_and_priority_controls(self):
        html = self.client.get(reverse("sales:pos")).content.decode()
        self.assertIn("Garment", html)
        self.assertIn('value="adult"', html)
        self.assertIn('value="child"', html)
        self.assertIn('id="orderPriority"', html)
        self.assertIn('<option value="high">High</option>', html)
        self.assertNotIn('x-model="line.tailoring_details.priority"', html)

    def test_pos_uses_free_text_daraz_and_canonical_design_option(self):
        html = self.client.get(reverse("sales:pos")).content.decode()
        self.assertIn("Enter Daraz instructions", html)
        self.assertIn('maxlength="200"', html)
        self.assertIn('value="VIP 3D Design"', html)
        self.assertIn("Computer Design", html)
        self.assertNotIn("Daraz Details", html)

    def test_duplicate_fabric_control_and_submission_are_absent(self):
        html = self.client.get(reverse("sales:pos")).content.decode()
        self.assertNotIn('tailoring_details.fabric', html)
        self.assertNotIn('name="fabric"', html)

    def test_frontend_contract_serializes_new_fields_and_line_errors(self):
        html = self.client.get(reverse("sales:pos")).content.decode()
        self.assertIn(
            "garment_classification: line.is_tailoring_workflow",
            html,
        )
        self.assertIn("fabric_meter_used: line.fabric_meter_used", html)
        self.assertIn("delivery_date: this.deliveryDate", html)
        self.assertIn("priority: this.priority", html)
        self.assertIn("focusInvalidLine", html)
        self.assertIn("line.validation_error", html)
        self.assertIn("canMerge(line, product)", html)

    def test_product_search_and_barcode_contract_include_applicability(self):
        self.product_a.is_tailoring_item = True
        self.product_a.save(update_fields=["is_tailoring_item"])
        products = self.client.get(reverse("sales:pos_products")).json()["items"]
        item = next(row for row in products if row["product_id"] == self.product_a.id)
        self.assertTrue(item["is_tailoring_item"])
        barcode = self.client.get(
            reverse("sales:pos_barcode"), {"code": self.product_a.barcode}
        ).json()
        self.assertTrue(barcode["item"]["is_tailoring_item"])

    def test_product_form_configures_tailoring_applicability(self):
        form = ProductForm(self.business_a, instance=self.product_a)
        self.assertIn("is_tailoring_item", form.fields)
        response = self.client.get(
            reverse("catalog:product_edit", args=[self.product_a.public_id])
        )
        self.assertContains(response, "Tailoring garment")

    def test_held_sale_round_trip_preserves_large_mixed_cart_and_priority(self):
        items = []
        for index in range(25):
            items.append({
                "product_id": self.product_a.id,
                "variant_id": None,
                "name": f"Long garment product name {index} " + ("x" * 80),
                "quantity": 1,
                "unit_price": "10.000",
                "garment_classification": "adult" if index % 2 == 0 else "child",
                "is_tailoring_item": True,
                "tailoring_details": {"daraz_details": "D" * 200},
            })
        response = self.client.post(
            reverse("sales:pos_hold"),
            json.dumps({
                "branch_id": self.branch_a.id,
                "label": "Large tailoring cart",
                "cart": {
                    "items": items,
                    "priority": "urgent",
                    "checkout_token": "ui-large-tailoring-hold",
                },
            }),
            content_type="application/json",
        )
        self.assertTrue(response.json()["ok"])
        held = self.client.get(reverse("sales:pos_held_list")).json()["held"][0]
        self.assertEqual(len(held["cart"]["items"]), 25)
        self.assertEqual(held["cart"]["priority"], "urgent")
        self.assertEqual(
            held["cart"]["items"][1]["garment_classification"], "child"
        )

    def test_api_serializers_publish_operational_fields_read_only(self):
        self.product_a.is_tailoring_item = True
        self.product_a.save(update_fields=["is_tailoring_item"])
        sale = self.make_sale(
            items=[{
                "product": self.product_a,
                "quantity": D("1"),
                "unit_price": D("10"),
                "garment_classification": "child",
            }],
            delivery_date=timezone.localdate(),
            priority=Sale.Priority.HIGH,
        )
        context = {
            "request": SimpleNamespace(
                api_access_context=SimpleNamespace(
                    effective_modules=frozenset({"tailoring"})
                )
            )
        }
        self.assertTrue(
            ProductSerializer(self.product_a, context=context).data[
                "is_tailoring_item"
            ]
        )
        data = SaleSerializer(sale, context=context).data
        self.assertEqual(data["priority"], "high")
        self.assertEqual(data["items"][0]["garment_classification"], "child")
