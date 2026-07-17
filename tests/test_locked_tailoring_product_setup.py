import json
from decimal import Decimal
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.branches.models import Branch, Warehouse
from apps.catalog import services as catalog_services
from apps.catalog.forms import ProductForm, QuickProductForm, UnitForm, VariantForm
from apps.catalog.models import Product, ProductVariant, TaxRate, Unit
from apps.inventory import services as inventory
from apps.inventory.models import StockMovement

from .base import TenantTestCase

D = Decimal


class LockedTailoringProductSetupTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)
        self.pcs = Unit.objects.for_business(self.business_a).get(name="Piece")
        self.meter = Unit.objects.for_business(self.business_a).get(name="Meter")

    def pcs_payload(self, **overrides):
        data = {
            "name": "Retail Piece",
            "product_type": Product.Type.STANDARD,
            "unit": self.pcs.pk,
            "sku": "LOCK-PCS",
            "purchase_price": "4.125",
            "sale_price": "9.875",
            "wholesale_price": "8.000",
            "minimum_sale_price": "7.500",
            "tax_rate": self.tax_a.pk,
            "price_includes_tax": "False",
            "reorder_level": "2.000",
            "opening_stock": "12.500",
            "opening_warehouse": self.warehouse_a.pk,
            "track_inventory": "on",
            "allow_discount": "on",
            "is_active": "on",
        }
        data.update(overrides)
        return data

    def meter_payload(self, **overrides):
        data = {
            "name": "Locked Fabric",
            "product_type": Product.Type.STANDARD,
            "unit": self.meter.pk,
            "sku": "LOCK-METER",
            "purchase_price": "1.250",
            "is_active": "on",
        }
        data.update(overrides)
        return data

    @staticmethod
    def variant_rows(**overrides):
        row = {
            "name": "Color 4",
            "attributes": {"Color": "4"},
            "sku": "LOCK-METER-C4",
            "barcode": "",
            "purchase_price": "1.250",
            "sale_price": "25.000",
            "opening_stock": "22.125",
        }
        row.update(overrides)
        return json.dumps([row])

    def test_unit_options_expose_stable_meter_semantics(self):
        kilogram = Unit.objects.for_business(self.business_a).get(name="Kilogram")
        self.assertTrue(self.meter.is_meter)
        self.assertFalse(kilogram.is_meter)
        html = str(ProductForm(self.business_a)["unit"])
        self.assertIn(
            f'value="{self.meter.pk}" data-unit-label="m" data-is-meter="true"',
            html,
        )
        self.assertIn(f'value="{kilogram.pk}"', html)
        self.assertIn('data-is-meter="false"', html)

    def test_new_meter_unit_is_canonical_and_rename_preserves_semantics(self):
        create_form = UnitForm(
            self.business_a,
            data={
                "name": "Fabric Length",
                "abbreviation": "m",
                "allow_decimal": "on",
                "is_active": "on",
            },
        )
        self.assertTrue(create_form.is_valid(), create_form.errors)
        unit = create_form.save(commit=False)
        unit.business = self.business_a
        unit.save()
        self.assertTrue(unit.is_meter)

        rename_form = UnitForm(
            self.business_a,
            data={
                "name": "Fabric Roll Length",
                "abbreviation": "roll",
                "allow_decimal": "on",
                "is_active": "on",
            },
            instance=unit,
        )
        self.assertTrue(rename_form.is_valid(), rename_form.errors)
        renamed = rename_form.save()
        self.assertTrue(renamed.is_meter)

    def test_existing_non_meter_unit_cannot_be_reclassified_as_meter(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Linked Piece Product",
            sku="LINKED-PIECE",
            unit=self.pcs,
            sale_price=D("10.000"),
        )
        form = UnitForm(
            self.business_a,
            data={
                "name": "Piece Length",
                "abbreviation": "m",
                "allow_decimal": "on",
                "is_active": "on",
            },
            instance=self.pcs,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("Create a new Meter unit", str(form.errors))
        self.pcs.refresh_from_db()
        product.refresh_from_db()
        self.assertFalse(self.pcs.is_meter)
        self.assertFalse(product.is_meter_tailoring)

    def test_historical_meter_retail_edit_preserves_retail_classification(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Historical Meter Retail",
            sku="HIST-METER-RETAIL",
            unit=self.meter,
            sale_price=D("5.000"),
            is_tailoring_item=False,
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=product,
            quantity=D("5.000"),
            unit_cost=D("1.000"),
            user=self.owner_a,
        )
        form = ProductForm(
            self.business_a,
            data=self.pcs_payload(
                name="Historical Meter Retail Edited",
                sku=product.sku,
                unit=self.meter.pk,
                sale_price="5.000",
            ),
            instance=product,
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(form.cleaned_data["is_tailoring_item"])
        saved = form.save()
        self.assertFalse(saved.is_tailoring_item)
        self.assertFalse(saved.is_meter_tailoring)
        self.assertEqual(saved.sale_price, D("5.000"))

    def test_historical_nonstock_meter_retail_remains_fully_editable(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Historical Nonstock Meter Retail",
            sku="HIST-METER-NONSTOCK",
            unit=self.meter,
            product_type=Product.Type.NON_STOCK,
            track_inventory=False,
            sale_price=D("6.500"),
            reorder_level=D("2.000"),
            is_tailoring_item=False,
        )
        data = self.pcs_payload(
            name="Historical Nonstock Meter Retail Edited",
            sku=product.sku,
            unit=self.meter.pk,
            product_type=Product.Type.NON_STOCK,
            track_inventory="",
            sale_price="7.250",
            reorder_level="3.000",
        )
        form = ProductForm(self.business_a, data=data, instance=product)

        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.product_type, Product.Type.NON_STOCK)
        self.assertFalse(saved.track_inventory)
        self.assertFalse(saved.is_tailoring_item)
        self.assertEqual(saved.sale_price, D("7.250"))
        self.assertEqual(saved.reorder_level, D("3.000"))

    def test_historical_meter_retail_variant_keeps_sale_price(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Historical Variant Meter Retail",
            sku="HIST-METER-VARIANT",
            unit=self.meter,
            product_type=Product.Type.VARIANT,
            is_tailoring_item=False,
        )
        form = VariantForm(
            self.business_a,
            data={
                "name": "Retail Length",
                "sku": "HIST-METER-VARIANT-1",
                "purchase_price": "1.000",
                "sale_price": "8.500",
                "is_active": "on",
            },
            product=product,
        )

        self.assertTrue(form.is_valid(), form.errors)
        variant = form.save(commit=False)
        variant.business = self.business_a
        variant.product = product
        variant.save()
        self.assertEqual(variant.sale_price, D("8.500"))

    def test_historical_meter_retail_variant_builder_keeps_sale_price(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Historical Builder Meter Retail",
            sku="HIST-METER-BUILDER",
            unit=self.meter,
            product_type=Product.Type.VARIANT,
            track_inventory=True,
            is_tailoring_item=False,
        )
        response = self.client.post(
            reverse("catalog:product_edit", args=[product.public_id]),
            self.pcs_payload(
                name=product.name,
                sku=product.sku,
                unit=self.meter.pk,
                product_type=Product.Type.VARIANT,
                opening_warehouse=self.warehouse_a.pk,
                variants_json=self.variant_rows(
                    sku="HIST-METER-BUILDER-V1",
                    sale_price="8.500",
                    opening_stock="0",
                ),
            ),
        )

        self.assertEqual(response.status_code, 302, response.context)
        variant = product.variants.get(sku="HIST-METER-BUILDER-V1")
        self.assertEqual(variant.sale_price, D("8.500"))

    def test_locked_meter_variant_creation_rejects_parent_stock(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Meter Parent Stock",
            sku="METER-PARENT-STOCK",
            unit=self.meter,
            product_type=Product.Type.STANDARD,
            track_inventory=True,
            is_tailoring_item=True,
        )
        inventory.record_movement(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=product,
            movement_type=StockMovement.Type.PURCHASE,
            quantity=D("2.000"),
            unit_cost=D("1.000"),
            user=self.owner_a,
        )
        Product.objects.filter(pk=product.pk).update(
            product_type=Product.Type.VARIANT
        )
        product.refresh_from_db()

        response = self.client.post(
            reverse("catalog:variant_create", args=[product.public_id]),
            {
                "name": "Color blocked",
                "sku": "METER-PARENT-STOCK-C1",
                "purchase_price": "1.000",
                "sale_price": "0",
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "parent stock to zero")
        self.assertFalse(product.variants.exists())

    def test_inactive_historical_meter_unit_remains_editable(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Inactive Unit Fabric",
            sku="INACTIVE-METER",
            product_type=Product.Type.STANDARD,
            unit=self.meter,
            track_inventory=True,
            is_tailoring_item=True,
        )
        self.meter.is_active = False
        self.meter.save(update_fields=["is_active"])

        form = ProductForm(self.business_a, instance=product)

        self.assertIn(self.meter, form.fields["unit"].queryset)
        self.assertTrue(form._meter_selected)

    def test_stocked_meter_product_cannot_change_inventory_shape(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Stocked Meter Fabric",
            sku="STOCKED-METER",
            product_type=Product.Type.STANDARD,
            unit=self.meter,
            track_inventory=True,
            is_tailoring_item=True,
        )
        inventory.record_movement(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=product,
            movement_type=StockMovement.Type.PURCHASE,
            quantity=D("5.000"),
            unit_cost=D("1.000"),
            user=self.owner_a,
        )

        form = ProductForm(
            self.business_a,
            data=self.meter_payload(
                name=product.name,
                sku=product.sku,
                product_type=Product.Type.VARIANT,
            ),
            instance=product,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("product_type", form.errors)

    def test_variant_import_cannot_bypass_historical_meter_shape_lock(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Historical Import Fabric",
            sku="IMPORT-HISTORY-METER",
            product_type=Product.Type.STANDARD,
            unit=self.meter,
            track_inventory=True,
            is_tailoring_item=True,
        )
        inventory.record_movement(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=product,
            movement_type=StockMovement.Type.PURCHASE,
            quantity=D("1.000"),
            unit_cost=D("1.000"),
            user=self.owner_a,
        )

        summary, errors = catalog_services.import_products(
            business=self.business_a,
            rows=[{
                "Product Name": product.name,
                "Variant Parent": product.sku,
                "Variant Name": "Color Locked",
                "Variant SKU": "IMPORT-HISTORY-METER-C1",
                "Unit": "Meter",
                "Purchase Price": "1.000",
            }],
            match_by="sku",
            user=self.owner_a,
        )

        product.refresh_from_db()
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(product.product_type, Product.Type.STANDARD)
        self.assertFalse(product.variants.exists())

    def test_variant_import_cannot_activate_historical_meter_retail(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Historical Retail Meter Import",
            sku="IMPORT-RETAIL-METER",
            product_type=Product.Type.STANDARD,
            unit=self.meter,
            track_inventory=True,
            is_tailoring_item=False,
        )
        inventory.record_movement(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=product,
            movement_type=StockMovement.Type.PURCHASE,
            quantity=D("1.000"),
            unit_cost=D("1.000"),
            user=self.owner_a,
        )

        summary, errors = catalog_services.import_products(
            business=self.business_a,
            rows=[{
                "Product Name": product.name,
                "Variant Parent": product.sku,
                "Variant Name": "Color Locked",
                "Variant SKU": "IMPORT-RETAIL-METER-C1",
                "Unit": "Meter",
                "Purchase Price": "1.000",
            }],
            match_by="sku",
            user=self.owner_a,
        )

        product.refresh_from_db()
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(len(errors), 1)
        self.assertFalse(product.is_tailoring_item)
        self.assertFalse(product.variants.exists())

    def test_variant_import_preserves_historical_meter_retail_price(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Historical Retail Meter Variants",
            sku="IMPORT-RETAIL-METER-V",
            product_type=Product.Type.VARIANT,
            unit=self.meter,
            track_inventory=True,
            is_tailoring_item=False,
        )

        summary, errors = catalog_services.import_products(
            business=self.business_a,
            rows=[{
                "Product Name": product.name,
                "Variant Parent": product.sku,
                "Variant Name": "Retail Color",
                "Variant SKU": "IMPORT-RETAIL-METER-V1",
                "Unit": "Meter",
                "Purchase Price": "1.000",
                "Sale Price": "8.500",
            }],
            match_by="sku",
            user=self.owner_a,
        )

        self.assertEqual(errors, [])
        self.assertEqual(summary["created"], 1)
        product.refresh_from_db()
        variant = product.variants.get(sku="IMPORT-RETAIL-METER-V1")
        self.assertFalse(product.is_tailoring_item)
        self.assertEqual(variant.sale_price, D("8.500"))

    def test_meter_import_accepts_multiple_colors_with_shared_parent_identifier(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Shared Parent Import",
            sku="SHARED-PARENT-IMPORT",
            product_type=Product.Type.VARIANT,
            unit=self.meter,
            track_inventory=True,
            is_tailoring_item=True,
        )
        rows = [
            {
                "Product Name": product.name,
                "SKU": product.sku,
                "Variant Parent": product.sku,
                "Variant Name": name,
                "Unit": "Meter",
                "Purchase Price": "1.000",
            }
            for name in ("Color 1", "Color 2")
        ]

        summary, errors = catalog_services.import_products(
            business=self.business_a,
            rows=rows,
            match_by="sku",
            user=self.owner_a,
        )

        self.assertEqual(errors, [])
        self.assertEqual(summary["created"], 2)
        self.assertEqual(product.variants.count(), 2)
        self.assertEqual(
            list(product.variants.order_by("name").values_list("sku", flat=True)),
            ["", ""],
        )

    def test_meter_variant_import_rejects_parent_identifier_collision(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Collision Parent Import",
            sku="COLLISION-PARENT",
            product_type=Product.Type.VARIANT,
            unit=self.meter,
            track_inventory=True,
            is_tailoring_item=True,
        )

        summary, errors = catalog_services.import_products(
            business=self.business_a,
            rows=[{
                "Product Name": product.name,
                "SKU": product.sku,
                "Variant Parent": product.sku,
                "Variant Name": "Color collision",
                "Variant SKU": product.sku,
                "Unit": "Meter",
                "Purchase Price": "1.000",
            }],
            match_by="sku",
            user=self.owner_a,
        )

        self.assertEqual(summary["failed"], 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("Duplicate SKU", errors[0][1])
        self.assertFalse(product.variants.exists())

    def test_meter_import_does_not_create_unused_tax_configuration(self):
        tax_name = "VAT 12%"
        self.assertFalse(
            TaxRate.objects.for_business(self.business_a).filter(name=tax_name).exists()
        )

        summary, errors = catalog_services.import_products(
            business=self.business_a,
            rows=[{
                "Product Name": "Imported Locked Fabric",
                "SKU": "IMPORT-METER-LOCKED",
                "Product Type": "standard",
                "Unit": "Meter",
                "Purchase Price": "1.250",
                "Sale Price": "99.000",
                "Tax/VAT Rate": "12",
            }],
            match_by="sku",
            user=self.owner_a,
        )

        self.assertEqual(errors, [])
        self.assertEqual(summary["created"], 1)
        product = Product.objects.get(sku="IMPORT-METER-LOCKED")
        self.assertIsNone(product.tax_rate)
        self.assertEqual(product.sale_price, D("0"))
        self.assertFalse(product.allow_discount)
        self.assertFalse(
            TaxRate.objects.for_business(self.business_a).filter(name=tax_name).exists()
        )

    def test_pcs_pricing_inventory_and_opening_stock_are_unchanged(self):
        response = self.client.post(reverse("catalog:product_create"), self.pcs_payload())
        self.assertEqual(response.status_code, 302)
        product = Product.objects.for_business(self.business_a).get(sku="LOCK-PCS")
        self.assertEqual(product.sale_price, D("9.875"))
        self.assertEqual(product.wholesale_price, D("8.000"))
        self.assertEqual(product.minimum_sale_price, D("7.500"))
        self.assertEqual(product.reorder_level, D("2.000"))
        self.assertTrue(product.allow_discount)
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, product),
            D("12.500"),
        )

    def test_pcs_customer_prices_remain_required(self):
        data = self.pcs_payload()
        data.pop("sale_price")
        form = ProductForm(self.business_a, data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("sale_price", form.errors)
        self.assertTrue(form.fields["opening_stock"].required)
        self.assertTrue(form.fields["reorder_level"].required)

    def test_meter_create_needs_no_fake_customer_pricing_or_parent_stock(self):
        response = self.client.post(
            reverse("catalog:product_create"), self.meter_payload()
        )
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        product = Product.objects.for_business(self.business_a).get(sku="LOCK-METER")
        self.assertTrue(product.is_tailoring_item)
        self.assertTrue(product.track_inventory)
        self.assertTrue(product.is_meter_tailoring)
        self.assertEqual(product.sale_price, D("0"))
        self.assertEqual(product.wholesale_price, D("0"))
        self.assertEqual(product.minimum_sale_price, D("0"))
        self.assertEqual(product.reorder_level, D("0"))
        self.assertIsNone(product.tax_rate)
        self.assertIsNone(product.price_includes_tax)
        self.assertFalse(product.allow_discount)
        self.assertIsNone(product.estimated_adult_fabric)
        self.assertIsNone(product.estimated_child_fabric)
        self.assertFalse(
            StockMovement.objects.for_business(self.business_a).filter(
                product=product, movement_type=StockMovement.Type.OPENING,
            ).exists()
        )

    def test_meter_is_restricted_to_stocked_standard_or_variant_products(self):
        for product_type in (Product.Type.SERVICE, Product.Type.NON_STOCK):
            with self.subTest(product_type=product_type):
                form = ProductForm(
                    self.business_a,
                    data=self.meter_payload(product_type=product_type),
                )
                self.assertFalse(form.is_valid())
                self.assertIn("product_type", form.errors)
                self.assertTrue(form.cleaned_data["track_inventory"])
                self.assertTrue(form.cleaned_data["is_tailoring_item"])

    def test_meter_parent_opening_stock_is_rejected_server_side(self):
        response = self.client.post(
            reverse("catalog:product_create"),
            self.meter_payload(
                opening_stock="50.000",
                opening_warehouse=self.warehouse_a.pk,
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["form"],
            "opening_stock",
            "Parent opening stock is not allowed for Meter products. "
            "Enter stock for each variant instead.",
        )
        self.assertFalse(Product.objects.filter(sku="LOCK-METER").exists())

    def test_meter_variant_opening_stock_is_exact_and_parent_stock_is_absent(self):
        response = self.client.post(
            reverse("catalog:product_create"),
            self.meter_payload(
                product_type=Product.Type.VARIANT,
                opening_warehouse=self.warehouse_a.pk,
                variants_json=self.variant_rows(),
            ),
        )
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        product = Product.objects.for_business(self.business_a).get(sku="LOCK-METER")
        variant = product.variants.get(sku="LOCK-METER-C4")
        self.assertEqual(variant.purchase_price, D("1.250"))
        self.assertEqual(variant.sale_price, D("0"))
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, product, variant=variant,
            ),
            D("22.125"),
        )
        self.assertFalse(
            StockMovement.objects.for_business(self.business_a).filter(
                product=product,
                variant__isnull=True,
                movement_type=StockMovement.Type.OPENING,
            ).exists()
        )
        movement = StockMovement.objects.for_business(self.business_a).get(
            product=product,
            variant=variant,
            movement_type=StockMovement.Type.OPENING,
        )
        self.assertEqual(movement.quantity, D("22.125"))

    def test_positive_variant_opening_requires_tenant_warehouse(self):
        response = self.client.post(
            reverse("catalog:product_create"),
            self.meter_payload(
                product_type=Product.Type.VARIANT,
                variants_json=self.variant_rows(),
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["form"],
            "opening_warehouse",
            "Select a warehouse for the variant opening stock.",
        )
        self.assertFalse(Product.objects.filter(sku="LOCK-METER").exists())

    def test_variant_decimal_payload_is_strictly_validated(self):
        cases = (
            ("purchase_price", "invalid"),
            ("sale_price", "Infinity"),
            ("opening_stock", "-0.001"),
            ("opening_stock", "1.2345"),
        )
        for index, (field, value) in enumerate(cases):
            with self.subTest(field=field, value=value):
                sku = f"LOCK-BAD-{index}"
                response = self.client.post(
                    reverse("catalog:product_create"),
                    self.meter_payload(
                        name=f"Invalid Variant {index}",
                        sku=sku,
                        product_type=Product.Type.VARIANT,
                        opening_warehouse=self.warehouse_a.pk,
                        variants_json=self.variant_rows(
                            sku=f"{sku}-V", **{field: value},
                        ),
                    ),
                )
                self.assertEqual(response.status_code, 200)
                self.assertFalse(Product.objects.filter(sku=sku).exists())

    def test_variant_edit_keeps_warehouse_for_new_color_opening_stock(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Existing Fabric",
            sku="EXIST-METER",
            unit=self.meter,
            product_type=Product.Type.VARIANT,
            purchase_price=D("1.100"),
            sale_price=D("30.000"),
            is_tailoring_item=True,
        )
        form = ProductForm(self.business_a, instance=product)
        self.assertNotIn("opening_stock", form.fields)
        self.assertIn("opening_warehouse", form.fields)

        response = self.client.post(
            reverse("catalog:product_edit", args=[product.public_id]),
            self.meter_payload(
                name=product.name,
                sku=product.sku,
                product_type=Product.Type.VARIANT,
                purchase_price="1.100",
                opening_warehouse=self.warehouse_a.pk,
                variants_json=self.variant_rows(
                    sku="EXIST-METER-C4", opening_stock="5.750",
                ),
            ),
        )
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        variant = ProductVariant.objects.get(sku="EXIST-METER-C4")
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, product, variant=variant,
            ),
            D("5.750"),
        )

    def test_product_edit_preserves_concurrent_ledger_cost_update(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Concurrent Cost Product",
            sku="CONCURRENT-COST-P",
            unit=self.pcs,
            purchase_price=D("1.000"),
            sale_price=D("2.000"),
            average_cost=D("1.000"),
        )
        original_is_valid = ProductForm.is_valid

        def validate_then_update_cost(form):
            result = original_is_valid(form)
            Product.objects.filter(pk=product.pk).update(average_cost=D("7.875"))
            return result

        with patch.object(ProductForm, "is_valid", new=validate_then_update_cost):
            response = self.client.post(
                reverse("catalog:product_edit", args=[product.public_id]),
                self.pcs_payload(
                    name="Concurrent Cost Product Edited",
                    sku=product.sku,
                    opening_stock="0",
                ),
            )

        self.assertEqual(response.status_code, 302)
        product.refresh_from_db()
        self.assertEqual(product.name, "Concurrent Cost Product Edited")
        self.assertEqual(product.average_cost, D("7.875"))

    def test_variant_edit_preserves_concurrent_ledger_cost_update(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Concurrent Variant Fabric",
            sku="CONCURRENT-COST-V",
            unit=self.meter,
            product_type=Product.Type.VARIANT,
            is_tailoring_item=True,
        )
        variant = ProductVariant.objects.create(
            business=self.business_a,
            product=product,
            name="Color 1",
            sku="CONCURRENT-COST-V1",
            purchase_price=D("1.000"),
            average_cost=D("1.000"),
        )
        original_is_valid = VariantForm.is_valid

        def validate_then_update_cost(form):
            result = original_is_valid(form)
            ProductVariant.objects.filter(pk=variant.pk).update(
                average_cost=D("6.625")
            )
            return result

        with patch.object(VariantForm, "is_valid", new=validate_then_update_cost):
            response = self.client.post(
                reverse(
                    "catalog:variant_edit",
                    args=[product.public_id, variant.public_id],
                ),
                {
                    "name": "Color 1 Edited",
                    "sku": variant.sku,
                    "purchase_price": "1.250",
                    "sale_price": "99.000",
                    "is_active": "on",
                },
            )

        self.assertEqual(response.status_code, 302)
        variant.refresh_from_db()
        self.assertEqual(variant.name, "Color 1 Edited")
        self.assertEqual(variant.average_cost, D("6.625"))

    def test_restricted_member_meter_opening_warehouses_are_scoped(self):
        other_branch = Branch.objects.create(
            business=self.business_a,
            name="Other Catalog Branch",
            code="CAT-B2",
        )
        other_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=other_branch,
            name="Other Catalog Warehouse",
            code="CAT-W2",
        )
        central = Warehouse.objects.create(
            business=self.business_a,
            branch=None,
            name="Central Catalog Warehouse",
            code="CAT-CENTRAL",
        )
        self.membership_a().branches.set([self.branch_a])

        response = self.client.post(
            reverse("catalog:product_create"),
            self.meter_payload(
                product_type=Product.Type.VARIANT,
                opening_warehouse=other_warehouse.pk,
                variants_json=self.variant_rows(sku="SCOPED-METER-C4"),
            ),
        )

        self.assertEqual(response.status_code, 200)
        warehouse_ids = set(
            response.context["form"].fields["opening_warehouse"].queryset.values_list(
                "id", flat=True
            )
        )
        self.assertIn(self.warehouse_a.id, warehouse_ids)
        self.assertIn(central.id, warehouse_ids)
        self.assertNotIn(other_warehouse.id, warehouse_ids)
        self.assertFalse(Product.objects.filter(sku="LOCK-METER").exists())

    def test_restricted_member_catalog_import_allows_central_not_other_branch(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Scoped Import Fabric",
            sku="SCOPED-IMPORT-METER",
            unit=self.meter,
            product_type=Product.Type.VARIANT,
            is_tailoring_item=True,
        )
        other_branch = Branch.objects.create(
            business=self.business_a,
            name="Other Import Branch",
            code="CAT-IMP-B2",
        )
        other_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=other_branch,
            name="Other Import Warehouse",
            code="CAT-IMP-W2",
        )
        central = Warehouse.objects.create(
            business=self.business_a,
            branch=None,
            name="Central Import Warehouse",
            code="CAT-IMP-CENTRAL",
        )
        self.membership_a().branches.set([self.branch_a])
        csv_file = SimpleUploadedFile(
            "scoped-meter.csv",
            (
                "product name,variant parent,variant name,variant sku,unit,"
                "purchase price,opening stock,branch,warehouse\n"
                f"{product.name},{product.sku},Other Color,SCOPED-OTHER,"
                f"Meter,1.000,3.000,{other_branch.name},{other_warehouse.name}\n"
                f"{product.name},{product.sku},Central Color,SCOPED-CENTRAL,"
                f"Meter,1.000,2.250,,{central.name}\n"
            ).encode(),
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("catalog:product_import"),
            {"file": csv_file, "match_by": "sku"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["results"]["summary"]["failed"], 1)
        self.assertEqual(response.context["results"]["summary"]["created"], 1)
        self.assertFalse(product.variants.filter(sku="SCOPED-OTHER").exists())
        central_variant = product.variants.get(sku="SCOPED-CENTRAL")
        self.assertEqual(
            inventory.get_stock(
                self.business_a, central, product, variant=central_variant
            ),
            D("2.250"),
        )

    def test_standalone_meter_variant_hides_and_neutralizes_sale_price(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Standalone Variant Fabric",
            sku="STANDALONE-METER",
            unit=self.meter,
            product_type=Product.Type.VARIANT,
            purchase_price=D("1.100"),
            is_tailoring_item=True,
        )
        create_url = reverse("catalog:variant_create", args=[product.public_id])
        response = self.client.get(create_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="sale_price"')
        self.assertContains(response, "garment charge is entered at POS")

        response = self.client.post(
            create_url,
            {
                "name": "Color 9",
                "sku": "STANDALONE-METER-C9",
                "purchase_price": "1.250",
                "sale_price": "99.000",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        variant = ProductVariant.objects.get(sku="STANDALONE-METER-C9")
        self.assertEqual(variant.purchase_price, D("1.250"))
        self.assertEqual(variant.sale_price, D("0"))

        variant.sale_price = D("45.000")
        variant.save(update_fields=["sale_price"])
        response = self.client.post(
            reverse(
                "catalog:variant_edit",
                args=[product.public_id, variant.public_id],
            ),
            {
                "name": "Color 9 Edited",
                "sku": variant.sku,
                "purchase_price": "1.500",
                "sale_price": "88.000",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        variant.refresh_from_db()
        self.assertEqual(variant.name, "Color 9 Edited")
        self.assertEqual(variant.purchase_price, D("1.500"))
        self.assertEqual(variant.sale_price, D("45.000"))

    def test_meter_edit_preserves_all_hidden_historical_values(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Historical Fabric",
            sku="HIST-METER",
            unit=self.meter,
            product_type=Product.Type.STANDARD,
            purchase_price=D("2.000"),
            sale_price=D("40.000"),
            wholesale_price=D("35.000"),
            minimum_sale_price=D("30.000"),
            tax_rate=self.tax_a,
            price_includes_tax=True,
            allow_discount=True,
            reorder_level=D("9.500"),
            is_tailoring_item=True,
            estimated_adult_fabric=D("3.500"),
            estimated_child_fabric=D("2.250"),
        )
        response = self.client.post(
            reverse("catalog:product_edit", args=[product.public_id]),
            self.meter_payload(
                name="Historical Fabric Edited",
                sku=product.sku,
                purchase_price="2.500",
            ),
        )
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        product.refresh_from_db()
        self.assertEqual(product.name, "Historical Fabric Edited")
        self.assertEqual(product.purchase_price, D("2.500"))
        self.assertEqual(product.sale_price, D("40.000"))
        self.assertEqual(product.wholesale_price, D("35.000"))
        self.assertEqual(product.minimum_sale_price, D("30.000"))
        self.assertEqual(product.tax_rate, self.tax_a)
        self.assertTrue(product.price_includes_tax)
        self.assertTrue(product.allow_discount)
        self.assertEqual(product.reorder_level, D("9.500"))
        self.assertEqual(product.estimated_adult_fabric, D("3.500"))
        self.assertEqual(product.estimated_child_fabric, D("2.250"))

    def test_legacy_null_unit_tailoring_product_remains_valid(self):
        self.product_a.is_tailoring_item = True
        self.product_a.save(update_fields=["is_tailoring_item"])
        form = ProductForm(
            self.business_a,
            data={
                "name": self.product_a.name,
                "product_type": Product.Type.STANDARD,
                "unit": "",
                "sku": self.product_a.sku,
                "barcode": self.product_a.barcode,
                "purchase_price": "4.000",
                "sale_price": "10.000",
                "wholesale_price": "0.000",
                "minimum_sale_price": "0.000",
                "reorder_level": "0.000",
                "track_inventory": "on",
                "allow_discount": "on",
                "is_tailoring_item": "on",
                "estimated_adult_fabric": "3.500",
                "estimated_child_fabric": "2.250",
                "is_active": "on",
            },
            instance=self.product_a,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(self.product_a.is_meter_tailoring)
        self.assertTrue(self.product_a.is_legacy_tailoring)

    def test_product_form_contains_meter_ui_contract_and_variant_opening(self):
        response = self.client.get(reverse("catalog:product_create"))
        self.assertContains(response, 'data-is-meter="true"')
        self.assertContains(response, 'x-show="!isMeterUnit()"')
        self.assertContains(response, 'x-bind:disabled="isMeterUnit()"')
        self.assertContains(response, 'x-model="v.opening_stock"')
        self.assertContains(response, "Meter products are tracked tailoring fabric")

    def test_meter_product_detail_is_internal_inventory_not_customer_pricing(self):
        product = Product.objects.create(
            business=self.business_a,
            name="Detail Fabric",
            sku="DETAIL-METER",
            unit=self.meter,
            product_type=Product.Type.VARIANT,
            purchase_price=D("1.500"),
            sale_price=D("45.000"),
            tax_rate=self.tax_a,
            reorder_level=D("8.000"),
            is_tailoring_item=True,
            estimated_adult_fabric=D("3.500"),
            estimated_child_fabric=D("2.250"),
        )
        response = self.client.get(
            reverse("catalog:product_detail", args=[product.public_id])
        )
        self.assertContains(response, "Tailoring fabric / Meter")
        self.assertContains(response, "<th>Purchase price</th>", html=True)
        self.assertNotContains(response, "<th>Sale price</th>", html=True)
        self.assertNotContains(response, "<th>Tax</th>", html=True)
        self.assertNotContains(response, "Estimated Adult Fabric")
        self.assertNotContains(response, "<th>Reorder level</th>", html=True)

    def test_legacy_null_unit_detail_retains_historical_estimates(self):
        self.product_a.is_tailoring_item = True
        self.product_a.save(update_fields=["is_tailoring_item"])
        response = self.client.get(
            reverse("catalog:product_detail", args=[self.product_a.public_id])
        )
        self.assertContains(response, "Legacy tailoring item")
        self.assertContains(response, "Estimated Adult Fabric")
        self.assertContains(response, "Estimated Child Fabric")

    def test_quick_meter_product_needs_cost_but_no_fake_sale_price(self):
        form = QuickProductForm(
            self.business_a,
            data={
                "name": "Quick Locked Fabric",
                "sku": "QUICK-LOCK-M",
                "unit": self.meter.pk,
                "purchase_price": "1.375",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["sale_price"], D("0"))
        self.assertTrue(form.cleaned_data["track_inventory"])

        response = self.client.post(
            reverse("purchases:quick_add_product"),
            {
                "name": "Quick Locked Fabric",
                "sku": "QUICK-LOCK-M",
                "unit": self.meter.pk,
                "purchase_price": "1.375",
                "sale_price": "99.000",
                "tax_rate": self.tax_a.pk,
                "price_includes_tax": "True",
            },
        )
        self.assertEqual(response.status_code, 201, response.content)
        product = Product.objects.get(pk=response.json()["product"]["product_id"])
        self.assertEqual(product.product_type, Product.Type.STANDARD)
        self.assertTrue(product.track_inventory)
        self.assertTrue(product.is_tailoring_item)
        self.assertTrue(product.is_meter_tailoring)
        self.assertEqual(product.sale_price, D("0"))
        self.assertIsNone(product.tax_rate)
        self.assertIsNone(product.price_includes_tax)

    def test_quick_pcs_product_still_requires_sale_price(self):
        form = QuickProductForm(
            self.business_a,
            data={
                "name": "Quick PCS",
                "sku": "QUICK-LOCK-PCS",
                "unit": self.pcs.pk,
                "purchase_price": "1.000",
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("sale_price", form.errors)

    def test_purchase_quick_modal_contains_meter_ui_contract(self):
        response = self.client.get(reverse("purchases:create"))
        self.assertContains(response, "quickIsMeterUnit()")
        self.assertContains(response, 'x-model="quickUnitId"')
        self.assertContains(response, "the garment charge is entered later at POS")
