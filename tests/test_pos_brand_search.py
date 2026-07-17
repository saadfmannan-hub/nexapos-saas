from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.branches.models import Warehouse
from apps.catalog.models import Brand, Category, Product
from apps.inventory import services as inventory

from .base import TenantTestCase

D = Decimal


class PosBrandSearchTests(TenantTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.tailoring = Category.objects.create(
            business=cls.business_a,
            name="Tailoring",
        )
        cls.tailoring_colors = Category.objects.create(
            business=cls.business_a,
            name="Tailoring Colors",
            parent=cls.tailoring,
        )
        cls.retail = Category.objects.create(
            business=cls.business_a,
            name="Retail",
        )
        cls.hi_sofy = Brand.objects.create(
            business=cls.business_a,
            name="Hi sofy",
        )
        cls.hindam = Brand.objects.create(
            business=cls.business_a,
            name="Hindam",
        )

        cls.beige = Product.objects.create(
            business=cls.business_a,
            name="Beige",
            internal_code="Hi sofy",
            sku="SOFY-BEI",
            barcode="BC-SOFY-BEI",
            category=cls.tailoring,
            brand=cls.hi_sofy,
            purchase_price=D("10"),
            sale_price=D("25"),
            is_tailoring_item=True,
            estimated_adult_fabric=D("3.500"),
            estimated_child_fabric=D("2.250"),
        )
        cls.black = Product.objects.create(
            business=cls.business_a,
            name="Black",
            sku="SOFY-BLA",
            barcode="BC-SOFY-BLA",
            category=cls.tailoring_colors,
            brand=cls.hi_sofy,
            purchase_price=D("10"),
            sale_price=D("25"),
            is_tailoring_item=True,
        )
        cls.brown = Product.objects.create(
            business=cls.business_a,
            name="Brown",
            sku="SOFY-BRO",
            barcode="BC-SOFY-BRO",
            category=cls.tailoring_colors,
            brand=cls.hi_sofy,
            purchase_price=D("10"),
            sale_price=D("25"),
            is_tailoring_item=True,
        )
        cls.color_2 = Product.objects.create(
            business=cls.business_a,
            name="Color 2",
            sku="HIN-C2",
            barcode="BC-HIN-C2",
            category=cls.tailoring,
            brand=cls.hindam,
            purchase_price=D("10"),
            sale_price=D("25"),
            is_tailoring_item=True,
        )
        cls.inactive = Product.objects.create(
            business=cls.business_a,
            name="Hidden Color",
            sku="SOFY-HIDDEN",
            category=cls.tailoring,
            brand=cls.hi_sofy,
            sale_price=D("25"),
            is_active=False,
        )
        cls.retail_item = Product.objects.create(
            business=cls.business_a,
            name="Needle Kit",
            sku="NEEDLE-KIT",
            barcode="BC-NEEDLE",
            category=cls.retail,
            purchase_price=D("1"),
            sale_price=D("3"),
        )

        cls.product_a.category = cls.retail
        cls.product_a.save(update_fields=["category"])
        cls.brand_b = Brand.objects.create(
            business=cls.business_b,
            name="Hi sofy",
        )
        cls.product_b.brand = cls.brand_b
        cls.product_b.save(update_fields=["brand"])

        cls.overflow_warehouse = Warehouse.objects.create(
            business=cls.business_a,
            name="Overflow Warehouse",
            code="OVERFLOW",
            branch=cls.branch_a,
        )
        for product, quantity in (
            (cls.beige, D("23")),
            (cls.black, D("21")),
            (cls.brown, D("19")),
            (cls.color_2, D("25")),
            (cls.retail_item, D("7")),
        ):
            inventory.set_opening_stock(
                business=cls.business_a,
                warehouse=cls.warehouse_a,
                product=product,
                quantity=quantity,
                unit_cost=product.purchase_price,
                user=cls.owner_a,
            )
        inventory.set_opening_stock(
            business=cls.business_a,
            warehouse=cls.overflow_warehouse,
            product=cls.beige,
            quantity=D("5"),
            unit_cost=cls.beige.purchase_price,
            user=cls.owner_a,
        )

    def setUp(self):
        self.allow_no_shift()
        self.client.force_login(self.owner_a)

    def source(self, relative_path):
        return Path(settings.BASE_DIR, relative_path).read_text(encoding="utf-8")

    def products(self, **params):
        query = {"warehouse_id": self.warehouse_a.pk, **params}
        response = self.client.get(reverse("sales:pos_products"), query)
        self.assertEqual(response.status_code, 200)
        return response.json()["items"]

    def names(self, **params):
        return [item["name"] for item in self.products(**params)]

    def product_item(self, name, **params):
        return next(item for item in self.products(**params) if item["name"] == name)

    def test_01_search_by_exact_product_name(self):
        self.assertEqual(self.names(q="Color 2"), ["Color 2"])

    def test_02_search_by_partial_product_name(self):
        self.assertEqual(self.names(q="colo"), ["Color 2"])

    def test_03_search_by_exact_brand_name(self):
        self.assertEqual(self.names(q="HINDAM"), ["Color 2"])

    def test_04_search_by_partial_brand_name(self):
        self.assertEqual(self.names(q="sOf"), ["Beige", "Black", "Brown"])

    def test_05_brand_search_returns_every_matching_product(self):
        self.assertEqual(
            self.names(q="Hi sofy"),
            ["Beige", "Black", "Brown"],
        )

    def test_06_brand_search_remains_tenant_isolated(self):
        self.product_a.brand = self.brand_b
        self.product_a.save(update_fields=["brand"])
        items = self.products(q="Hi sofy")
        self.assertNotIn(self.product_b.pk, [item["product_id"] for item in items])
        self.assertNotIn("Widget B", [item["name"] for item in items])
        self.assertNotIn("Widget A", [item["name"] for item in items])
        self.assertEqual(self.product_item("Widget A", q="Widget A")["brand"], "")

    def test_07_existing_product_name_search_is_unchanged(self):
        self.assertEqual(self.names(q="Widget A"), ["Widget A"])

    def test_08_existing_sku_search_is_unchanged(self):
        self.assertEqual(self.names(q="SOFY-BEI"), ["Beige"])

    def test_09_existing_barcode_search_is_unchanged(self):
        self.assertEqual(self.names(q="BC-SOFY-BEI"), ["Beige"])
        response = self.client.get(
            reverse("sales:pos_barcode"),
            {"code": "BC-SOFY-BEI"},
        )
        self.assertTrue(response.json()["found"])
        self.assertEqual(response.json()["item"]["name"], "Beige")

    def test_10_f2_barcode_shortcut_is_unchanged(self):
        html = self.source("templates/pos/pos.html")
        self.assertIn('x-ref="barcode"', html)
        self.assertIn('@keydown.enter.prevent="scanBarcode()"', html)
        self.assertIn("e.key === 'F2'", html)
        self.assertIn("this.$refs.barcode.focus()", html)

    def test_11_f4_normal_search_shortcut_is_unchanged(self):
        html = self.source("templates/pos/pos.html")
        self.assertIn('x-ref="search"', html)
        self.assertIn('x-model="search"', html)
        self.assertIn("e.key === 'F4'", html)
        self.assertIn("this.$refs.search.focus()", html)

    def test_12_all_and_tailoring_filters_remain_correct(self):
        page = self.client.get(reverse("sales:pos"))
        self.assertContains(page, ">All<", html=False)
        self.assertContains(page, ">Tailoring<", html=False)
        all_names = self.names()
        self.assertIn("Needle Kit", all_names)
        tailoring_names = self.names(category=self.tailoring.pk)
        self.assertEqual(
            tailoring_names,
            ["Beige", "Black", "Brown", "Color 2"],
        )

    def test_13_category_filtering_remains_correct(self):
        retail_names = self.names(category=self.retail.pk)
        self.assertIn("Needle Kit", retail_names)
        self.assertIn("Widget A", retail_names)
        self.assertNotIn("Beige", retail_names)

    def test_14_warehouse_stock_filtering_remains_correct(self):
        primary = self.product_item("Beige", q="Beige")
        overflow = self.product_item(
            "Beige",
            q="Beige",
            warehouse_id=self.overflow_warehouse.pk,
        )
        self.assertEqual(primary["stock"], 23.0)
        self.assertEqual(overflow["stock"], 5.0)

    def test_15_inactive_products_remain_hidden(self):
        names = self.names(q="Hi sofy")
        self.assertNotIn("Hidden Color", names)

    def test_16_product_card_shows_brand_name(self):
        item = self.product_item("Color 2", q="Hindam")
        self.assertEqual(item["brand"], "Hindam")
        html = self.source("templates/pos/pos.html")
        name_at = html.index('class="pp-name"')
        brand_at = html.index('class="pp-brand"')
        price_at = html.index('class="pp-price"')
        self.assertLess(name_at, brand_at)
        self.assertLess(brand_at, price_at)

    def test_17_product_card_hides_brand_line_when_missing(self):
        item = self.product_item("Widget A", q="Widget A")
        self.assertEqual(item["brand"], "")
        html = self.source("templates/pos/pos.html")
        self.assertIn('class="pp-brand" x-show="p.brand"', html)
        self.assertNotIn("No Brand", html)

    def test_18_brand_search_has_no_duplicate_product_cards(self):
        items = self.products(q="Hi sofy")
        keys = [(item["product_id"], item["variant_id"]) for item in items]
        self.assertEqual(len(keys), len(set(keys)))

    def test_19_brand_loading_does_not_add_n_plus_one_queries(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                reverse("sales:pos_products"),
                {"q": "Hi sofy", "warehouse_id": self.warehouse_a.pk},
            )
        self.assertEqual(response.status_code, 200)
        sql = [query["sql"].lower() for query in queries.captured_queries]
        self.assertEqual(
            sum('join "catalog_brand"' in statement for statement in sql),
            1,
        )
        self.assertFalse(
            any('from "catalog_brand"' in statement for statement in sql)
        )

    def test_20_product_add_to_cart_contract_is_unchanged(self):
        item = self.product_item("Beige", q="Hi sofy")
        for field in (
            "product_id",
            "variant_id",
            "name",
            "price",
            "tax_rate",
            "stocked",
            "allow_discount",
            "is_tailoring_item",
        ):
            self.assertIn(field, item)
        html = self.source("templates/pos/pos.html")
        self.assertIn('@click="addToCart(p)"', html)
        self.assertIn("addToCart(p) {", html)

    def test_21_adult_child_classification_is_unchanged(self):
        html = self.source("templates/pos/pos.html")
        self.assertIn('value="adult"', html)
        self.assertIn('value="child"', html)
        self.assertIn('x-model="line.garment_classification"', html)

    def test_22_normal_premium_collection_selector_is_unchanged(self):
        html = self.source("templates/pos/pos.html")
        self.assertIn('value="normal"', html)
        self.assertIn('value="premium"', html)
        self.assertIn('x-model="line.collection_type"', html)

    def test_23_fabric_consumption_behavior_is_unchanged(self):
        sale = self.make_sale(
            items=[{
                "product": self.beige,
                "quantity": D("1"),
                "unit_price": D("25"),
                "garment_classification": "adult",
                "collection_type": "normal",
            }],
            delivery_date=timezone.localdate(),
        )
        item = sale.items.get()
        self.assertEqual(item.garment_classification, "adult")
        self.assertEqual(item.collection_type, "normal")
        self.assertEqual(item.estimated_fabric, D("3.500"))

    def test_24_pos_brand_markup_remains_compact_and_responsive(self):
        html = self.source("templates/pos/pos.html")
        css = self.source("static/css/app.css")
        self.assertIn('<div class="pp-brand"', html)
        self.assertIn(".pos-product-card .pp-brand {", css)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertIn("-webkit-line-clamp: 2", css)
        self.assertIn("minmax(132px, 1fr)", css)
        self.assertIn("repeat(2, minmax(0, 1fr))", css)
