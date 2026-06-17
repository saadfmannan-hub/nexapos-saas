"""Tests for the product variants builder and auto-SKU generation."""
import json
from decimal import Decimal

from django.urls import reverse

from apps.catalog import services as catalog_services
from apps.catalog.models import Product, ProductVariant
from apps.inventory import services as inventory

from .base import TenantTestCase

D = Decimal


class GenerateSkuServiceTests(TenantTestCase):
    def test_prefix_from_business_name(self):
        # business_a is "Alpha Retail"
        self.assertEqual(catalog_services.sku_prefix_for(self.business_a), "ALP")

    def test_prefix_fallback_when_no_letters(self):
        self.business_a.name = "!!!"
        self.assertEqual(catalog_services.sku_prefix_for(self.business_a), "SKU")

    def test_sequential_and_padded(self):
        first = catalog_services.generate_sku(self.business_a)
        self.assertEqual(first, "ALP-000001")
        Product.objects.create(business=self.business_a, name="P1", sku=first)
        self.assertEqual(catalog_services.generate_sku(self.business_a), "ALP-000002")

    def test_taken_set_avoids_collision(self):
        first = catalog_services.generate_sku(self.business_a)
        second = catalog_services.generate_sku(self.business_a, taken={first})
        self.assertNotEqual(first, second)
        self.assertEqual(second, "ALP-000002")

    def test_scans_variants_too(self):
        p = Product.objects.create(business=self.business_a, name="VarP",
                                   product_type=Product.Type.VARIANT)
        ProductVariant.objects.create(business=self.business_a, product=p,
                                      name="V", sku="ALP-000005")
        self.assertEqual(catalog_services.generate_sku(self.business_a), "ALP-000006")


class ProductFormBaseTest(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)

    def _base_payload(self, **overrides):
        data = {
            "name": "New Product",
            "product_type": "standard",
            "purchase_price": "4.000",
            "sale_price": "10.000",
            "wholesale_price": "0",
            "minimum_sale_price": "0",
            "reorder_level": "0",
            "track_inventory": "on",
            "is_active": "on",
        }
        data.update(overrides)
        return data


class SimpleProductTests(ProductFormBaseTest):
    def test_simple_product_still_saves(self):
        before = Product.objects.for_business(self.business_a).count()
        r = self.client.post(reverse("catalog:product_create"),
                             self._base_payload(name="Simple Widget", sku="MAN-1"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, reverse("catalog:product_list"))
        p = Product.objects.for_business(self.business_a).get(name="Simple Widget")
        self.assertEqual(p.sku, "MAN-1")
        self.assertEqual(p.product_type, "standard")
        self.assertEqual(
            Product.objects.for_business(self.business_a).count(), before + 1)
        # no variants created for a simple product
        self.assertFalse(p.variants.exists())

    def test_simple_product_ignores_variants_payload(self):
        # Even if a stray payload is posted, a standard product makes none.
        r = self.client.post(reverse("catalog:product_create"), self._base_payload(
            name="Standard Only", sku="STD-1",
            variants_json=json.dumps([{"name": "X", "attributes": {"Size": "M"}}]),
        ))
        self.assertEqual(r.status_code, 302)
        p = Product.objects.for_business(self.business_a).get(name="Standard Only")
        self.assertFalse(p.variants.exists())

    def test_manual_duplicate_sku_rejected(self):
        # product_a in the fixture already uses SKU "WID-A"
        before = Product.objects.for_business(self.business_a).count()
        r = self.client.post(reverse("catalog:product_create"),
                             self._base_payload(name="Dup", sku="WID-A"))
        self.assertEqual(r.status_code, 200)  # re-render with field error
        self.assertEqual(
            Product.objects.for_business(self.business_a).count(), before)


class VariantProductTests(ProductFormBaseTest):
    def _variants_json(self):
        return json.dumps([
            {"name": "M / Black", "attributes": {"Size": "M", "Color": "Black"},
             "sku": "TS-MB", "barcode": "", "purchase_price": "4.000",
             "sale_price": "10.000", "opening_stock": "5"},
            {"name": "M / White", "attributes": {"Size": "M", "Color": "White"},
             "sku": "TS-MW", "barcode": "", "purchase_price": "4.000",
             "sale_price": "10.000", "opening_stock": "3"},
        ])

    def test_variant_product_creates_variants_and_stock(self):
        r = self.client.post(reverse("catalog:product_create"), self._base_payload(
            name="T-Shirt", product_type="variant", sku="TSHIRT",
            opening_warehouse=self.warehouse_a.pk,
            variants_json=self._variants_json(),
        ))
        self.assertEqual(r.status_code, 302)
        product = Product.objects.for_business(self.business_a).get(name="T-Shirt")
        self.assertEqual(r.url, reverse("catalog:product_detail",
                                        args=[product.public_id]))
        variants = product.variants.all()
        self.assertEqual(variants.count(), 2)
        mb = variants.get(sku="TS-MB")
        self.assertEqual(mb.attributes, {"Size": "M", "Color": "Black"})
        self.assertEqual(mb.sale_price, D("10.000"))
        # opening stock recorded for the variant via the ledger
        self.assertEqual(
            inventory.total_stock(self.business_a, product, variant=mb), D("5"))

    def test_duplicate_variant_sku_rejected_no_partial_save(self):
        before = Product.objects.for_business(self.business_a).count()
        bad = json.dumps([
            {"name": "A", "attributes": {"Size": "S"}, "sku": "DUPE"},
            {"name": "B", "attributes": {"Size": "M"}, "sku": "DUPE"},
        ])
        r = self.client.post(reverse("catalog:product_create"), self._base_payload(
            name="BadVariants", product_type="variant",
            variants_json=bad,
        ))
        self.assertEqual(r.status_code, 200)
        # product not created, no variants leaked
        self.assertEqual(
            Product.objects.for_business(self.business_a).count(), before)
        self.assertFalse(
            Product.objects.for_business(self.business_a).filter(
                name="BadVariants").exists())

    def test_variant_sku_collision_with_existing_rejected(self):
        before = Product.objects.for_business(self.business_a).count()
        bad = json.dumps([{"name": "A", "attributes": {"Size": "S"}, "sku": "WID-A"}])
        r = self.client.post(reverse("catalog:product_create"), self._base_payload(
            name="ClashVariant", product_type="variant", variants_json=bad))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            Product.objects.for_business(self.business_a).count(), before)


class AutoSkuTests(ProductFormBaseTest):
    def test_auto_sku_on_product(self):
        r = self.client.post(reverse("catalog:product_create"), self._base_payload(
            name="Auto Simple", auto_generate_sku="on", sku=""))
        self.assertEqual(r.status_code, 302)
        p = Product.objects.for_business(self.business_a).get(name="Auto Simple")
        self.assertEqual(p.sku, "ALP-000001")

    def test_auto_sku_generates_unique_variant_skus(self):
        variants = json.dumps([
            {"name": "S", "attributes": {"Size": "S"}, "sku": "",
             "sale_price": "5.000"},
            {"name": "M", "attributes": {"Size": "M"}, "sku": "",
             "sale_price": "5.000"},
        ])
        r = self.client.post(reverse("catalog:product_create"), self._base_payload(
            name="Auto Variants", product_type="variant",
            auto_generate_sku="on", sku="",
            variants_json=variants,
        ))
        self.assertEqual(r.status_code, 302)
        product = Product.objects.for_business(self.business_a).get(name="Auto Variants")
        skus = set(product.variants.values_list("sku", flat=True))
        self.assertEqual(len(skus), 2)  # all unique
        # product SKU + 2 variant SKUs share the ALP- sequence, no collisions
        self.assertTrue(product.sku.startswith("ALP-"))
        for s in skus:
            self.assertTrue(s.startswith("ALP-"))
        all_skus = skus | {product.sku}
        self.assertEqual(len(all_skus), 3)
