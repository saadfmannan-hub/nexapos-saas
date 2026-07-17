import json
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.branches.models import Branch, Warehouse
from apps.catalog.models import Product, ProductVariant, Unit
from apps.customers.models import Customer
from apps.inventory import services as inventory
from apps.sales.models import HeldSale, Sale
from apps.sales.views import _invoice_context

from .base import TenantTestCase

D = Decimal


class LockedTailoringPosJobCardTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()
        self.client.force_login(self.owner_a)

        self.meter_unit = Unit.objects.create(
            business=self.business_a,
            name="Locked Meter",
            abbreviation="M",
            allow_decimal=True,
            is_meter=True,
        )
        self.pcs_unit = Unit.objects.create(
            business=self.business_a,
            name="Locked PCS",
            abbreviation="PCS",
            allow_decimal=False,
            is_meter=False,
        )
        self.fabric = Product.objects.create(
            business=self.business_a,
            name="Golden City Fabric",
            sku="LOCK-FABRIC",
            product_type=Product.Type.VARIANT,
            unit=self.meter_unit,
            purchase_price=D("4.000"),
            sale_price=D("25.000"),
            track_inventory=True,
            is_tailoring_item=True,
        )
        self.color = ProductVariant.objects.create(
            business=self.business_a,
            product=self.fabric,
            name="Color 7",
            sku="LOCK-FABRIC-C7",
            barcode="6299990000007",
            purchase_price=D("4.000"),
            sale_price=D("25.000"),
        )
        self.retail = Product.objects.create(
            business=self.business_a,
            name="Kumma Finished Good",
            sku="LOCK-KUMMA",
            barcode="6299990000014",
            unit=self.pcs_unit,
            purchase_price=D("5.000"),
            sale_price=D("15.000"),
            track_inventory=True,
            is_tailoring_item=False,
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=self.fabric,
            variant=self.color,
            quantity=D("250.000"),
            unit_cost=D("4.000"),
            user=self.owner_a,
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=self.retail,
            quantity=D("100.000"),
            unit_cost=D("5.000"),
            user=self.owner_a,
        )

        self.second_branch = Branch.objects.create(
            business=self.business_a,
            name="Locked Second Branch",
            code="LOCK-B2",
        )
        self.second_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=self.second_branch,
            name="Locked Fabric Warehouse",
            code="LOCK-W2",
            is_default=True,
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=self.second_warehouse,
            product=self.fabric,
            variant=self.color,
            quantity=D("100.000"),
            unit_cost=D("4.000"),
            user=self.owner_a,
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=self.second_warehouse,
            product=self.retail,
            quantity=D("50.000"),
            unit_cost=D("5.000"),
            user=self.owner_a,
        )

    def meter_line(
        self,
        meter="3.500",
        *,
        quantity="1",
        garment="adult",
        collection="premium",
        design="Design A",
    ):
        return {
            "product_id": self.fabric.id,
            "variant_id": self.color.id,
            "quantity": quantity,
            "unit_price": "25.000",
            "fabric_meter_used": meter,
            "garment_classification": garment,
            "collection_type": collection,
            "tailoring_details": {
                "design_type": "Daraz",
                "daraz_details": design,
            },
        }

    def retail_line(self, *, quantity="1"):
        return {
            "product_id": self.retail.id,
            "variant_id": None,
            "quantity": quantity,
            "unit_price": "15.000",
            "garment_classification": "",
            "collection_type": "",
            "tailoring_details": {},
        }

    def checkout(
        self,
        items,
        *,
        branch=None,
        customer=None,
        held_id=None,
        token=None,
    ):
        branch = branch or self.branch_a
        customer = customer or self.walk_in_a
        total = sum(
            D(str(item["quantity"])) * D(str(item["unit_price"]))
            for item in items
        )
        payload = {
            "branch_id": branch.id,
            "customer_id": customer.id,
            "items": items,
            "payments": [{"method_id": self.cash_a.id, "amount": str(total)}],
            "invoice_discount": "0",
            "priority": "normal",
            "delivery_date": (
                str(timezone.localdate())
                if any(item["product_id"] == self.fabric.id for item in items)
                else None
            ),
            "checkout_token": token or f"locked-{uuid4().hex}",
        }
        if held_id is not None:
            payload["held_id"] = held_id
        return self.client.post(
            reverse("sales:pos_checkout"),
            json.dumps(payload),
            content_type="application/json",
        )

    def sale_from_response(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["ok"], response.json())
        return Sale.objects.for_business(self.business_a).get(
            public_id=response.json()["sale"]["public_id"]
        )

    def test_pos_product_and_barcode_flags_distinguish_meter_from_pcs(self):
        products = self.client.get(
            reverse("sales:pos_products"),
            {"warehouse_id": self.warehouse_a.id},
        ).json()["items"]
        fabric = next(
            item
            for item in products
            if item["product_id"] == self.fabric.id
            and item["variant_id"] == self.color.id
        )
        retail = next(
            item for item in products if item["product_id"] == self.retail.id
        )

        self.assertTrue(fabric["is_tailoring_item"])
        self.assertTrue(fabric["is_meter_tailoring"])
        self.assertEqual(fabric["unit"], "M")
        self.assertFalse(retail["is_tailoring_item"])
        self.assertFalse(retail["is_meter_tailoring"])
        self.assertEqual(retail["unit"], "PCS")

        fabric_scan = self.client.get(
            reverse("sales:pos_barcode"),
            {"code": self.color.barcode},
        ).json()["item"]
        retail_scan = self.client.get(
            reverse("sales:pos_barcode"),
            {"code": self.retail.barcode},
        ).json()["item"]
        self.assertTrue(fabric_scan["is_meter_tailoring"])
        self.assertFalse(retail_scan["is_meter_tailoring"])

    def test_pos_ui_contract_locks_meter_qty_and_never_merges_duplicate_fabric(self):
        html = self.client.get(reverse("sales:pos")).content.decode()

        self.assertIn('x-show="!line.is_meter_tailoring"', html)
        self.assertIn('x-show="line.is_meter_tailoring"', html)
        self.assertIn("Qty <strong>1</strong>", html)
        self.assertIn('<span class="input-group-text">Meter</span>', html)
        self.assertIn('x-model="line.fabric_meter_used"', html)
        self.assertIn("if (product.is_meter_tailoring) return false;", html)
        self.assertIn("if (product.is_legacy_tailoring)", html)
        self.assertIn("duplicateLine(index)", html)
        self.assertIn("quantity: 1,", html)
        self.assertIn("fabric_meter_used: '',", html)
        self.assertIn("line.quantity = 1;", html)
        self.assertIn("field.endsWith('.fabric_meter_used')", html)

    def test_checkout_stores_exact_meter_and_deducts_meter_not_qty(self):
        before = inventory.get_stock(
            self.business_a,
            self.warehouse_a,
            self.fabric,
            self.color,
        )
        sale = self.sale_from_response(
            self.checkout([self.meter_line("3.625")])
        )
        item = sale.items.get()

        self.assertEqual(item.quantity, D("1.000"))
        self.assertEqual(item.fabric_meter_used, D("3.625"))
        self.assertEqual(item.unit_price, D("25.000"))
        self.assertEqual(item.line_total, D("25.000"))
        self.assertEqual(
            inventory.get_stock(
                self.business_a,
                self.warehouse_a,
                self.fabric,
                self.color,
            ),
            before - D("3.625"),
        )

    def test_checkout_token_replay_cannot_cross_customer_context(self):
        token = "locked-pos-context-token"
        response = self.checkout(
            [self.meter_line("3.100")],
            token=token,
        )
        self.assertEqual(response.status_code, 200)
        sale_count = Sale.objects.for_business(self.business_a).count()

        other_customer = Customer.objects.create(
            business=self.business_a,
            code="LOCK-CUSTOMER-2",
            full_name="Other Customer",
        )
        response = self.checkout(
            [self.meter_line("2.000")],
            token=token,
            customer=other_customer,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Invalid checkout token.")
        self.assertEqual(
            Sale.objects.for_business(self.business_a).count(),
            sale_count,
        )

    def test_checkout_rejects_meter_qty_override_and_maps_meter_errors(self):
        count_before = Sale.objects.for_business(self.business_a).count()
        quantity_response = self.checkout(
            [self.meter_line("3.500", quantity="2")]
        )
        self.assertEqual(quantity_response.status_code, 400)
        self.assertEqual(
            quantity_response.json()["errors"]["items.0.quantity"],
            "Quantity must be 1 for meter tailoring garments.",
        )

        meter_response = self.checkout([self.meter_line("")])
        self.assertEqual(meter_response.status_code, 400)
        self.assertEqual(
            meter_response.json()["errors"]["items.0.fabric_meter_used"],
            "Enter Meter for every tailoring garment.",
        )
        precision_response = self.checkout([self.meter_line("3.5001")])
        self.assertEqual(precision_response.status_code, 400)
        self.assertIn(
            "at most 3 decimal places",
            precision_response.json()["errors"]["items.0.fabric_meter_used"],
        )
        self.assertEqual(
            Sale.objects.for_business(self.business_a).count(),
            count_before,
        )

    def test_pcs_checkout_keeps_editable_quantity_and_no_tailoring_data(self):
        sale = self.sale_from_response(
            self.checkout([self.retail_line(quantity="2")])
        )
        item = sale.items.get()

        self.assertEqual(item.quantity, D("2.000"))
        self.assertIsNone(item.fabric_meter_used)
        self.assertEqual(item.garment_classification, "")
        self.assertEqual(item.collection_type, "")
        self.assertEqual(item.tailoring_details, {})
        self.assertFalse(item.is_tailoring_line)

    def test_held_cart_preserves_duplicates_blank_legacy_and_original_branch(self):
        first = self.meter_line("3.100", design="Design A")
        second = self.meter_line(
            "3.200",
            garment="child",
            collection="normal",
            design="Design B",
        )
        legacy = self.meter_line("3.300", design="Legacy blank meter")
        legacy.pop("fabric_meter_used")
        hold_response = self.client.post(
            reverse("sales:pos_hold"),
            json.dumps({
                "branch_id": self.branch_a.id,
                "label": "Three separate garments",
                "cart": {
                    "items": [first, second, legacy],
                    "checkout_token": "held-three-garments",
                },
            }),
            content_type="application/json",
        )
        self.assertEqual(hold_response.status_code, 200)
        held_id = hold_response.json()["held_id"]

        held = next(
            row
            for row in self.client.get(reverse("sales:pos_held_list")).json()["held"]
            if row["id"] == held_id
        )
        lines = held["cart"]["items"]
        self.assertEqual(len(lines), 3)
        self.assertEqual(
            [line.get("fabric_meter_used") for line in lines],
            ["3.100", "3.200", None],
        )
        self.assertEqual(
            [line["tailoring_details"]["daraz_details"] for line in lines],
            ["Design A", "Design B", "Legacy blank meter"],
        )
        self.assertTrue(all(line["is_meter_tailoring"] for line in lines))

        blank_line = dict(lines[2])
        blank_line["fabric_meter_used"] = ""
        blank_response = self.checkout(
            [blank_line],
            held_id=held_id,
            token="held-three-garments",
        )
        self.assertEqual(blank_response.status_code, 400)
        self.assertIn(
            "items.0.fabric_meter_used",
            blank_response.json()["errors"],
        )
        self.assertTrue(
            HeldSale.objects.for_business(self.business_a).filter(pk=held_id).exists()
        )

        branch_response = self.checkout(
            [self.meter_line("3.000")],
            branch=self.second_branch,
            held_id=held_id,
            token="held-three-garments",
        )
        self.assertEqual(branch_response.status_code, 400)
        self.assertEqual(
            branch_response.json()["error"],
            "Resume this held sale from its original branch.",
        )

    def test_meter_is_internal_only_across_customer_documents(self):
        sale = self.sale_from_response(
            self.checkout([self.meter_line("7.321")])
        )

        detail = self.client.get(reverse("sales:detail", args=[sale.public_id]))
        self.assertContains(detail, "Meter:")
        self.assertContains(detail, "7.321 m")
        self.assertNotContains(detail, 'name="actual_fabric_used"')

        a4 = self.client.get(reverse("sales:invoice", args=[sale.public_id]))
        receipt_80 = self.client.get(reverse("sales:receipt", args=[sale.public_id]))
        receipt_58 = render_to_string(
            "invoices/receipt_58mm.html",
            _invoice_context(sale),
        )
        for html in (
            a4.content.decode(),
            receipt_80.content.decode(),
            receipt_58,
        ):
            self.assertNotIn("7.321", html)
            self.assertNotIn("Fabric Meter", html)

        with patch("apps.reports.pdf.render_pdf", return_value=b"%PDF locked") as pdf:
            response = self.client.get(
                reverse("sales:invoice_pdf", args=[sale.public_id])
            )
        self.assertEqual(response.status_code, 200)
        template, context = pdf.call_args.args
        pdf_html = render_to_string(template, context)
        self.assertNotIn("7.321", pdf_html)
        self.assertNotIn("Fabric Meter", pdf_html)

    def test_retail_has_no_job_link_and_job_routes_reject_it(self):
        sale = self.sale_from_response(
            self.checkout([self.retail_line(quantity="2")])
        )
        item = sale.items.get()
        detail = self.client.get(reverse("sales:detail", args=[sale.public_id]))
        self.assertNotContains(detail, "Download All Job Cards")
        self.assertNotContains(detail, 'target="_blank">Job Card</a>')

        with patch("apps.reports.pdf.render_pdf", return_value=b"%PDF locked") as pdf:
            bulk = self.client.get(
                reverse("sales:workshop_job_card_pdf", args=[sale.public_id])
            )
            single = self.client.get(
                reverse(
                    "sales:sale_item_workshop_job_card_pdf",
                    args=[sale.public_id, item.id],
                )
            )
        self.assertEqual(bulk.status_code, 404)
        self.assertEqual(single.status_code, 404)
        pdf.assert_not_called()

    def test_later_product_toggle_does_not_turn_historical_retail_into_job(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Historical Retail Candidate",
            sku="LOCK-HIST-RETAIL",
            product_type=Product.Type.NON_STOCK,
            unit=None,
            track_inventory=False,
            is_tailoring_item=False,
            sale_price=D("12.000"),
        )
        sale = self.sale_from_response(
            self.checkout([{
                "product_id": product.id,
                "variant_id": None,
                "quantity": "1",
                "unit_price": "12.000",
                "garment_classification": "",
                "collection_type": "",
                "tailoring_details": {},
            }])
        )
        item = sale.items.get()
        product.is_tailoring_item = True
        product.save(update_fields=["is_tailoring_item"])
        item.refresh_from_db()

        self.assertFalse(item.is_tailoring_line)
        detail = self.client.get(reverse("sales:detail", args=[sale.public_id]))
        self.assertNotContains(detail, "Download All Job Cards")
        with patch("apps.reports.pdf.render_pdf", return_value=b"%PDF locked") as pdf:
            response = self.client.get(
                reverse("sales:workshop_job_card_pdf", args=[sale.public_id])
            )
        self.assertEqual(response.status_code, 404)
        pdf.assert_not_called()

    def test_bulk_job_cards_support_one_and_five_tailoring_lines(self):
        one_sale = self.sale_from_response(
            self.checkout([self.meter_line("3.000")])
        )
        five_sale = self.sale_from_response(
            self.checkout(
                [
                    self.meter_line(
                        f"3.00{index}",
                        design=f"Five-{index}",
                    )
                    for index in range(1, 6)
                ]
            )
        )

        with patch("apps.reports.pdf.render_pdf", return_value=b"%PDF locked") as pdf:
            response = self.client.get(
                reverse("sales:workshop_job_card_pdf", args=[one_sale.public_id])
            )
        self.assertEqual(response.status_code, 200)
        one_cards = pdf.call_args.args[1]["job_cards"]
        self.assertEqual(len(one_cards), 1)
        self.assertEqual(one_cards[0]["job_card_sequence_label"], "1/1")
        self.assertEqual(one_cards[0]["items"], [one_cards[0]["job_item"]])

        with patch("apps.reports.pdf.render_pdf", return_value=b"%PDF locked") as pdf:
            response = self.client.get(
                reverse("sales:workshop_job_card_pdf", args=[five_sale.public_id])
            )
        self.assertEqual(response.status_code, 200)
        five_cards = pdf.call_args.args[1]["job_cards"]
        self.assertEqual(len(five_cards), 5)
        self.assertEqual(
            [card["job_card_sequence_label"] for card in five_cards],
            ["1/5", "2/5", "3/5", "4/5", "5/5"],
        )
        self.assertTrue(
            all(card["items"] == [card["job_item"]] for card in five_cards)
        )

    def test_mixed_sale_sequences_only_three_tailoring_lines(self):
        items = []
        for index in range(1, 4):
            items.extend([
                self.meter_line(
                    f"3.10{index}",
                    design=f"Mixed-{index}",
                ),
                self.retail_line(),
            ])
        sale = self.sale_from_response(self.checkout(items))
        self.assertEqual(sale.items.count(), 6)

        with patch("apps.reports.pdf.render_pdf", return_value=b"%PDF locked") as pdf:
            response = self.client.get(
                reverse("sales:workshop_job_card_pdf", args=[sale.public_id])
            )
        self.assertEqual(response.status_code, 200)
        cards = pdf.call_args.args[1]["job_cards"]
        self.assertEqual(len(cards), 3)
        self.assertEqual(
            [card["job_card_sequence_label"] for card in cards],
            ["1/3", "2/3", "3/3"],
        )
        self.assertEqual(
            [card["job_item"].product_id for card in cards],
            [self.fabric.id, self.fabric.id, self.fabric.id],
        )
        self.assertEqual(
            [card["job_card_number"].rsplit("-", 1)[1] for card in cards],
            ["01", "02", "03"],
        )

    def test_same_fabric_lines_keep_separate_cards_and_workshop_meter(self):
        sale = self.sale_from_response(
            self.checkout([
                self.meter_line("3.111", design="Design A"),
                self.meter_line(
                    "3.222",
                    garment="child",
                    collection="normal",
                    design="Design B",
                ),
            ])
        )
        items = list(sale.items.order_by("id"))
        self.assertEqual(len(items), 2)
        self.assertNotEqual(items[0].id, items[1].id)

        with patch("apps.reports.pdf.render_pdf", return_value=b"%PDF locked") as pdf:
            response = self.client.get(
                reverse("sales:workshop_job_card_pdf", args=[sale.public_id])
            )
        self.assertEqual(response.status_code, 200)
        context = pdf.call_args.args[1]
        cards = context["job_cards"]
        self.assertEqual(
            [card["job_item"].fabric_meter_used for card in cards],
            [D("3.111"), D("3.222")],
        )
        self.assertEqual(
            [card["tailoring"]["daraz_details"] for card in cards],
            ["Design A", "Design B"],
        )
        html = render_to_string("invoices/workshop_job_card.html", context)
        self.assertIn("Job Card 1/2", html)
        self.assertIn("Job Card 2/2", html)
        self.assertIn('<div class="label">Meter</div>', html)
        self.assertIn("3.111 m", html)
        self.assertIn("3.222 m", html)
        self.assertIn("POS-entered fabric consumption", html)

        with patch("apps.reports.pdf.render_pdf", return_value=b"%PDF locked") as pdf:
            response = self.client.get(reverse(
                "sales:sale_item_workshop_job_card_pdf",
                args=[sale.public_id, items[1].id],
            ))
        self.assertEqual(response.status_code, 200)
        single_card = pdf.call_args.args[1]["job_cards"][0]
        self.assertEqual(single_card["job_card_sequence_label"], "2/2")
        self.assertEqual(single_card["job_item"].id, items[1].id)

    def test_existing_workshop_copy_labels_and_numbering_are_preserved(self):
        sale = self.sale_from_response(
            self.checkout([self.meter_line("3.500")])
        )
        url = reverse("sales:workshop_job_card_pdf", args=[sale.public_id])

        for query, expected_label in (
            ("", "Original"),
            ("?copy=copy", "Copy"),
            ("?copy=reprint", "Reprint"),
        ):
            with self.subTest(query=query):
                with patch(
                    "apps.reports.pdf.render_pdf", return_value=b"%PDF locked"
                ) as pdf:
                    response = self.client.get(url + query)
                self.assertEqual(response.status_code, 200)
                card = pdf.call_args.args[1]["job_cards"][0]
                self.assertEqual(card["copy_type"], expected_label)
                self.assertEqual(card["workshop_copy_number"], 1)

        sale.reprint_count = 2
        sale.save(update_fields=["reprint_count"])
        with patch(
            "apps.reports.pdf.render_pdf", return_value=b"%PDF locked"
        ) as pdf:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        card = pdf.call_args.args[1]["job_cards"][0]
        self.assertEqual(card["copy_type"], "Reprint")
        self.assertEqual(card["workshop_copy_number"], 3)

    def test_job_card_routes_enforce_branch_and_tenant_access(self):
        sale = self.sale_from_response(
            self.checkout(
                [self.meter_line("3.500")],
                branch=self.second_branch,
            )
        )
        item = sale.items.get()

        self.cashier_membership.branches.set([self.branch_a])
        self.client.force_login(self.cashier_a)
        with patch("apps.reports.pdf.render_pdf", return_value=b"%PDF locked") as pdf:
            branch_response = self.client.get(
                reverse("sales:workshop_job_card_pdf", args=[sale.public_id])
            )
        self.assertEqual(branch_response.status_code, 404)
        pdf.assert_not_called()

        self.client.force_login(self.owner_b)
        with patch("apps.reports.pdf.render_pdf", return_value=b"%PDF locked") as pdf:
            tenant_bulk = self.client.get(
                reverse("sales:workshop_job_card_pdf", args=[sale.public_id])
            )
            tenant_single = self.client.get(reverse(
                "sales:sale_item_workshop_job_card_pdf",
                args=[sale.public_id, item.id],
            ))
        self.assertEqual(tenant_bulk.status_code, 404)
        self.assertEqual(tenant_single.status_code, 404)
        pdf.assert_not_called()
