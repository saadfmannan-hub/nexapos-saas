"""Focused Product operational branch-context and isolation coverage."""

import csv
import io
import json
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import Membership, Role, User
from apps.branches.models import Branch, Warehouse
from apps.catalog.models import Product, ProductVariant, Unit
from apps.inventory import services as inventory
from apps.subscriptions.models import Plan, Subscription

from .base import TenantTestCase


def csv_upload(text):
    return SimpleUploadedFile(
        "product-branch-context.csv",
        text.encode("utf-8"),
        content_type="text/csv",
    )


class ProductBranchContextTests(TenantTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.branch_mb = Branch.objects.create(
            business=cls.business_a,
            name="Mabelah Branch",
            code="MB",
        )
        cls.warehouse_mb = Warehouse.objects.create(
            business=cls.business_a,
            branch=cls.branch_mb,
            name="Mabelah Stockroom",
            code="MB-STOCK",
        )
        cls.product_ah = Product.objects.create(
            business=cls.business_a,
            name="Al Hail Only Product",
            sku="AH-ONLY",
            barcode="6299000000001",
            sale_price=Decimal("10.000"),
            reorder_level=Decimal("20.000"),
        )
        cls.product_mb = Product.objects.create(
            business=cls.business_a,
            name="Mabelah Only Product",
            sku="MB-ONLY",
            barcode="6299000000002",
            sale_price=Decimal("12.000"),
            reorder_level=Decimal("20.000"),
        )
        cls.product_both = Product.objects.create(
            business=cls.business_a,
            name="Shared Branch Product",
            sku="BOTH-001",
            barcode="6299000000003",
            purchase_price=Decimal("4.000"),
            sale_price=Decimal("9.000"),
            reorder_level=Decimal("20.000"),
        )
        inventory.set_opening_stock(
            business=cls.business_a,
            warehouse=cls.warehouse_a,
            product=cls.product_ah,
            quantity=Decimal("5.000"),
            unit_cost=Decimal("4.000"),
            user=cls.owner_a,
        )
        inventory.set_opening_stock(
            business=cls.business_a,
            warehouse=cls.warehouse_mb,
            product=cls.product_mb,
            quantity=Decimal("6.000"),
            unit_cost=Decimal("4.000"),
            user=cls.owner_a,
        )
        for warehouse, quantity in (
            (cls.warehouse_a, "7.000"),
            (cls.warehouse_mb, "11.000"),
        ):
            inventory.set_opening_stock(
                business=cls.business_a,
                warehouse=warehouse,
                product=cls.product_both,
                quantity=Decimal(quantity),
                unit_cost=Decimal("4.000"),
                user=cls.owner_a,
            )

        cls.branch_role = Role.objects.create(
            business=cls.business_a,
            name="Product Branch Operator",
            permissions=[
                "dashboard.view",
                "inventory.view",
                "inventory.adjust",
                "products.view",
                "products.manage",
                "products.export",
                "products.import",
                "sales.create",
            ],
        )
        cls.user_ah = User.objects.create_user(
            email="products-ah@example.com",
            password="StrongPass123!",
            full_name="Al Hail Product User",
        )
        cls.membership_ah = Membership.objects.create(
            business=cls.business_a,
            user=cls.user_ah,
            role=cls.branch_role,
        )
        cls.membership_ah.branches.add(cls.branch_a)
        cls.user_mb = User.objects.create_user(
            email="products-mb@example.com",
            password="StrongPass123!",
            full_name="Mabelah Product User",
        )
        cls.membership_mb = Membership.objects.create(
            business=cls.business_a,
            user=cls.user_mb,
            role=cls.branch_role,
        )
        cls.membership_mb.branches.add(cls.branch_mb)

        plan_ids = Subscription.objects.filter(business=cls.business_a).values_list(
            "plan_id", flat=True
        )
        Plan.objects.filter(pk__in=plan_ids).update(feature_api_access=True)

    def export_rows(self, response):
        self.assertEqual(response.status_code, 200)
        return list(csv.DictReader(io.StringIO(response.content.decode("utf-8"))))

    def product_payload(self, **overrides):
        piece = Unit.objects.for_business(self.business_a).get(name="Piece")
        data = {
            "branch": self.branch_a.id,
            "name": "Branch Onboarded Product",
            "product_type": Product.Type.STANDARD,
            "unit": piece.id,
            "sku": "BRANCH-ONBOARD-1",
            "purchase_price": "2.000",
            "sale_price": "5.000",
            "wholesale_price": "0",
            "minimum_sale_price": "0",
            "reorder_level": "1",
            "opening_stock": "10",
            "opening_warehouse": self.warehouse_a.id,
            "track_inventory": "on",
            "allow_discount": "on",
            "is_active": "on",
        }
        data.update(overrides)
        return data

    def import_csv(self, branch, warehouse, records):
        columns = [
            "product name", "sku", "barcode", "category", "brand",
            "product type", "unit", "purchase price", "sale price",
            "cost price", "tax/vat rate", "tax inclusive",
            "track inventory", "opening stock", "minimum stock",
            "branch code", "branch name", "warehouse code",
            "warehouse name", "variant option name", "variant option value",
            "variant sku", "variant barcode", "active", "archived",
        ]
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        for record in records:
            row = {
                "branch code": branch.code,
                "branch name": branch.name,
                "warehouse code": warehouse.code,
                "warehouse name": warehouse.name,
                "product type": "standard",
                "track inventory": "Yes",
                "opening stock": "0",
                "active": "Yes",
                "archived": "No",
            }
            row.update(record)
            writer.writerow(row)
        return output.getvalue()

    def test_owner_all_and_selected_branch_product_visibility_and_counts(self):
        self.client.force_login(self.owner_a)
        all_products = self.client.get(reverse("catalog:product_list"))
        self.assertContains(all_products, "Select a Branch and Warehouse")
        self.assertNotContains(all_products, "Product Master")
        self.assertNotContains(all_products, self.product_ah.name)

        al_hail = self.client.get(
            reverse("catalog:product_list"), {"branch": self.branch_a.id}
        )
        self.assertContains(al_hail, self.product_ah.name)
        self.assertContains(al_hail, self.product_both.name)
        self.assertNotContains(al_hail, self.product_mb.name)
        self.assertEqual(al_hail.context["product_count"], 3)

        mabelah = self.client.get(
            reverse("catalog:product_list"), {"branch": self.branch_mb.id}
        )
        self.assertContains(mabelah, self.product_mb.name)
        self.assertContains(mabelah, self.product_both.name)
        self.assertNotContains(mabelah, self.product_ah.name)
        self.assertNotContains(mabelah, self.product_a.name)
        self.assertEqual(mabelah.context["product_count"], 2)

    def test_branch_users_are_locked_and_cannot_tamper_or_open_foreign_detail(self):
        self.client.force_login(self.user_ah)
        page = self.client.get(reverse("catalog:product_list"))
        self.assertEqual(page.context["selected_branch"], self.branch_a)
        self.assertTrue(page.context["branch_locked"])
        self.assertContains(page, self.product_ah.name)
        self.assertNotContains(page, self.product_mb.name)
        self.assertContains(page, "readonly")

        tampered = self.client.get(
            reverse("catalog:product_list"), {"branch": self.branch_mb.id}
        )
        self.assertEqual(tampered.status_code, 404)
        foreign_detail = self.client.get(
            reverse("catalog:product_detail", args=[self.product_mb.public_id])
        )
        self.assertEqual(foreign_detail.status_code, 404)
        foreign_edit = self.client.get(
            reverse("catalog:product_edit", args=[self.product_mb.public_id])
        )
        self.assertEqual(foreign_edit.status_code, 404)

        self.client.force_login(self.user_mb)
        mabelah = self.client.get(reverse("catalog:product_list"))
        self.assertContains(mabelah, self.product_mb.name)
        self.assertNotContains(mabelah, self.product_ah.name)

    def test_owner_selects_context_and_branch_user_is_locked_for_new_product(self):
        self.client.force_login(self.owner_a)
        direct = self.client.get(reverse("catalog:product_create"))
        self.assertRedirects(direct, reverse("catalog:product_list"))

        selected = self.client.get(
            reverse("catalog:product_list"),
            {"branch": self.branch_a.id, "warehouse": self.warehouse_a.id},
        )
        self.assertContains(selected, "New Product")
        self.assertContains(selected, "Import Products")
        self.assertContains(selected, "Export Products")
        self.assertNotContains(selected, "Product Master")

        self.client.force_login(self.user_ah)
        form_page = self.client.get(reverse("catalog:product_create"))
        self.assertEqual(form_page.status_code, 200)
        self.assertEqual(form_page.context["selected_branch"], self.branch_a)
        self.assertEqual(
            set(
                form_page.context["form"]
                .fields["opening_warehouse"]
                .queryset.values_list("id", flat=True)
            ),
            {self.warehouse_a.id},
        )
        self.assertContains(form_page, "Assigned branch")

        tampered = self.client.get(
            reverse("catalog:product_create"),
            {"branch": self.branch_mb.id, "warehouse": self.warehouse_mb.id},
        )
        self.assertEqual(tampered.status_code, 404)

    def test_standard_product_is_created_and_reused_across_branches(self):
        self.client.force_login(self.owner_a)
        created = self.client.post(
            reverse("catalog:product_create"), self.product_payload()
        )
        self.assertEqual(created.status_code, 302, created.content)
        product = Product.objects.for_business(self.business_a).get(
            sku="BRANCH-ONBOARD-1"
        )
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, product),
            Decimal("10"),
        )
        before_count = Product.objects.for_business(self.business_a).count()

        reused = self.client.post(
            reverse("catalog:product_create"),
            self.product_payload(
                branch=self.branch_mb.id,
                opening_warehouse=self.warehouse_mb.id,
                opening_stock="4",
            ),
        )
        self.assertEqual(reused.status_code, 302, reused.content)
        self.assertEqual(
            Product.objects.for_business(self.business_a).count(), before_count
        )
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, product),
            Decimal("10"),
        )
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_mb, product),
            Decimal("4"),
        )

    def test_fabric_colors_and_new_color_use_meter_stock_in_selected_warehouse(self):
        self.client.force_login(self.owner_a)
        meter = Unit.objects.for_business(self.business_a).get(name="Meter")
        variants = [
            {
                "name": f"Color {number}",
                "attributes": {"Color Code": f"Color {number}"},
                "sku": f"HI-SOFY-C{number}",
                "barcode": f"62998100000{number}0",
                "purchase_price": "2.500",
                "sale_price": "0",
                "opening_stock": quantity,
            }
            for number, quantity in ((1, "80"), (2, "60"), (3, "95"))
        ]
        response = self.client.post(
            reverse("catalog:product_create"),
            self.product_payload(
                name="Hi Sofy",
                sku="HI-SOFY",
                product_type=Product.Type.VARIANT,
                unit=meter.id,
                opening_stock="0",
                variants_json=json.dumps(variants),
            ),
        )
        self.assertEqual(response.status_code, 302, response.content)
        product = Product.objects.for_business(self.business_a).get(sku="HI-SOFY")
        self.assertTrue(product.is_meter_tailoring)
        self.assertEqual(product.variants.count(), 3)
        color_2 = product.variants.get(sku="HI-SOFY-C2")
        self.assertEqual(color_2.attributes, {"Color Code": "Color 2"})
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, product, color_2
            ),
            Decimal("60"),
        )
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_mb, product, color_2
            ),
            Decimal("0"),
        )

        new_color = [{
            "name": "Color 25",
            "attributes": {"Color Code": "Color 25"},
            "sku": "HI-SOFY-C25",
            "barcode": "6299810000251",
            "purchase_price": "2.500",
            "sale_price": "0",
            "opening_stock": "100",
        }]
        added = self.client.post(
            reverse("catalog:product_edit", args=[product.public_id]),
            self.product_payload(
                name=product.name,
                sku=product.sku,
                product_type=Product.Type.VARIANT,
                unit=meter.id,
                opening_stock="0",
                variants_json=json.dumps(new_color),
            ),
        )
        self.assertEqual(added.status_code, 302, added.content)
        color_25 = product.variants.get(sku="HI-SOFY-C25")
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, product, color_25
            ),
            Decimal("100"),
        )

    def test_piece_size_products_create_variants_and_stock(self):
        self.client.force_login(self.owner_a)
        piece = Unit.objects.for_business(self.business_a).get(name="Piece")
        cases = (
            ("Premium Kumma", "KUMMA-SIMPLE", ("10", "10.5", "11")),
            ("Royal Khanjar", "KHANJAR-SIMPLE", ("Small", "Medium", "Large")),
            ("Classic Assa", "ASSA-SIMPLE", ("Small", "Medium", "Large")),
        )
        for name, sku, sizes in cases:
            with self.subTest(product=name):
                rows = [
                    {
                        "name": size,
                        "attributes": {"Size": size},
                        "sku": f"{sku}-{index}",
                        "barcode": "",
                        "purchase_price": "2",
                        "sale_price": "5",
                        "opening_stock": str(index + 5),
                    }
                    for index, size in enumerate(sizes, start=1)
                ]
                response = self.client.post(
                    reverse("catalog:product_create"),
                    self.product_payload(
                        name=name,
                        sku=sku,
                        product_type=Product.Type.VARIANT,
                        unit=piece.id,
                        opening_stock="0",
                        variants_json=json.dumps(rows),
                    ),
                )
                self.assertEqual(response.status_code, 302, response.content)
                product = Product.objects.for_business(self.business_a).get(
                    sku=sku
                )
                self.assertEqual(product.variants.count(), 3)
                self.assertTrue(
                    all(
                        inventory.get_stock(
                            self.business_a,
                            self.warehouse_a,
                            product,
                            variant,
                        ) > 0
                        for variant in product.variants.all()
                    )
                )

    def test_selected_warehouse_export_is_scoped_and_reimportable(self):
        self.client.force_login(self.owner_a)
        self.assertEqual(
            self.client.get(reverse("catalog:product_export")).status_code,
            404,
        )

        branch_rows = self.export_rows(
            self.client.get(
                reverse("catalog:product_export"),
                {
                    "branch": self.branch_a.id,
                    "warehouse": self.warehouse_a.id,
                    "format": "csv",
                },
            )
        )
        branch_skus = {row["SKU"] for row in branch_rows}
        self.assertIn(self.product_ah.sku, branch_skus)
        self.assertIn(self.product_both.sku, branch_skus)
        self.assertNotIn(self.product_mb.sku, branch_skus)
        shared = next(row for row in branch_rows if row["SKU"] == self.product_both.sku)
        self.assertEqual(Decimal(shared["Opening Stock"]), Decimal("7"))
        self.assertEqual(shared["Branch Code"], self.branch_a.code)
        self.assertEqual(shared["Warehouse Code"], self.warehouse_a.code)
        self.assertIn("Variant Option Name", shared)

        self.client.force_login(self.user_ah)
        restricted_rows = self.export_rows(
            self.client.get(
                reverse("catalog:product_export"),
                {"warehouse": self.warehouse_a.id, "format": "csv"},
            )
        )
        self.assertNotIn(self.product_mb.sku, {row["SKU"] for row in restricted_rows})

    def test_branch_import_validates_metadata_and_never_mutates_other_warehouse(self):
        self.client.force_login(self.user_ah)
        before_product_count = Product.objects.for_business(self.business_a).count()
        mismatch = self.client.post(
            reverse("catalog:product_import"),
            {
                "branch": self.branch_a.id,
                "warehouse": self.warehouse_a.id,
                "match_by": "sku",
                "file": csv_upload(
                    "product name,sku,product type,track inventory,opening stock,"
                    "branch code,branch name,warehouse code,warehouse name\n"
                    f"Forged Product,FORGED-1,standard,yes,5,{self.branch_mb.code},"
                    f"{self.branch_mb.name},{self.warehouse_mb.code},"
                    f"{self.warehouse_mb.name}\n"
                ),
            },
        )
        self.assertEqual(mismatch.context["results"]["summary"]["failed"], 1)
        self.assertEqual(
            Product.objects.for_business(self.business_a).count(), before_product_count
        )

        before_other = inventory.get_stock(
            self.business_a, self.warehouse_mb, self.product_both
        )
        imported = self.client.post(
            reverse("catalog:product_import"),
            {
                "branch": self.branch_a.id,
                "warehouse": self.warehouse_a.id,
                "match_by": "sku",
                "file": csv_upload(
                    "product name,sku,product type,track inventory,purchase price,"
                    "opening stock,branch code,branch name,warehouse code,warehouse name\n"
                    f"{self.product_both.name},{self.product_both.sku},standard,yes,4,3,"
                    f"{self.branch_a.code},{self.branch_a.name},{self.warehouse_a.code},"
                    f"{self.warehouse_a.name}\n"
                ),
            },
        )
        self.assertEqual(imported.context["results"]["summary"]["updated"], 1)
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_both),
            Decimal("7.000"),
        )
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_mb, self.product_both),
            before_other,
        )
        self.assertEqual(
            Product.objects.for_business(self.business_a).filter(
                sku=self.product_both.sku
            ).count(),
            1,
        )

        template = self.client.get(
            reverse("catalog:import_template"),
            {"branch": self.branch_a.id, "warehouse": self.warehouse_a.id},
        )
        template_text = template.content.decode()
        for value in (
            "Branch Code", "Branch Name", "Warehouse Code", "Warehouse Name",
            self.branch_a.code, self.warehouse_a.code,
        ):
            self.assertIn(value, template_text)

    def test_one_file_import_reuses_parent_variants_and_branch_stock(self):
        self.client.force_login(self.owner_a)
        records = [
            {
                "product name": "Imported Hi Sofy",
                "sku": "IMPORT-HI-SOFY",
                "category": "Fabrics",
                "brand": "Imported Sofy",
                "product type": "variant",
                "unit": "Meter",
                "purchase price": "2.500",
                "opening stock": quantity,
                "variant option name": "Color Code",
                "variant option value": f"Color {number}",
                "variant sku": f"IMPORT-HI-SOFY-C{number}",
                "variant barcode": f"62998200000{number}0",
            }
            for number, quantity in ((1, "80"), (2, "60"), (3, "95"))
        ]
        records.append({
            "product name": "Imported Standard Product",
            "sku": "IMPORT-STANDARD",
            "category": "Retail",
            "brand": "Imported Brand",
            "product type": "standard",
            "unit": "Piece",
            "purchase price": "1.500",
            "sale price": "4.000",
            "opening stock": "10",
        })
        ah_csv = self.import_csv(self.branch_a, self.warehouse_a, records)
        response = self.client.post(
            reverse("catalog:product_import"),
            {
                "branch": self.branch_a.id,
                "warehouse": self.warehouse_a.id,
                "match_by": "sku",
                "file": csv_upload(ah_csv),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["results"]["errors"])
        fabric = Product.objects.for_business(self.business_a).get(
            sku="IMPORT-HI-SOFY"
        )
        self.assertEqual(fabric.variants.count(), 3)
        color_1 = fabric.variants.get(sku="IMPORT-HI-SOFY-C1")
        self.assertEqual(color_1.attributes, {"Color Code": "Color 1"})
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, fabric, color_1
            ),
            Decimal("80"),
        )
        product_count = Product.objects.for_business(self.business_a).count()
        variant_count = ProductVariant.objects.for_business(
            self.business_a
        ).count()

        repeated = self.client.post(
            reverse("catalog:product_import"),
            {
                "branch": self.branch_a.id,
                "warehouse": self.warehouse_a.id,
                "match_by": "sku",
                "file": csv_upload(ah_csv),
            },
        )
        self.assertFalse(repeated.context["results"]["errors"])
        self.assertEqual(
            Product.objects.for_business(self.business_a).count(), product_count
        )
        self.assertEqual(
            ProductVariant.objects.for_business(self.business_a).count(),
            variant_count,
        )
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, fabric, color_1
            ),
            Decimal("80"),
        )

        mb_csv = self.import_csv(self.branch_mb, self.warehouse_mb, records)
        mabelah = self.client.post(
            reverse("catalog:product_import"),
            {
                "branch": self.branch_mb.id,
                "warehouse": self.warehouse_mb.id,
                "match_by": "sku",
                "file": csv_upload(mb_csv),
            },
        )
        self.assertFalse(mabelah.context["results"]["errors"])
        self.assertEqual(
            Product.objects.for_business(self.business_a).filter(
                sku="IMPORT-HI-SOFY"
            ).count(),
            1,
        )
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_mb, fabric, color_1
            ),
            Decimal("80"),
        )
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_a, fabric, color_1
            ),
            Decimal("80"),
        )

        exported = self.export_rows(
            self.client.get(
                reverse("catalog:product_export"),
                {
                    "branch": self.branch_a.id,
                    "warehouse": self.warehouse_a.id,
                    "format": "csv",
                },
            )
        )
        fabric_rows = [
            row for row in exported if row["SKU"] == "IMPORT-HI-SOFY"
        ]
        self.assertEqual(len(fabric_rows), 3)
        self.assertEqual(
            {row["Variant Option Value"] for row in fabric_rows},
            {"Color 1", "Color 2", "Color 3"},
        )
        self.assertEqual(
            {row["Warehouse Code"] for row in fabric_rows},
            {self.warehouse_a.code},
        )

    def test_import_conflict_rolls_back_entire_file(self):
        self.client.force_login(self.owner_a)
        records = [
            {
                "product name": "Atomic Standard",
                "sku": "ATOMIC-STANDARD",
                "unit": "Piece",
                "opening stock": "3",
            },
            {
                "product name": "Atomic Fabric",
                "sku": "ATOMIC-FABRIC",
                "product type": "variant",
                "unit": "Meter",
                "variant option name": "Color Code",
                "variant option value": "Color 1",
                "variant sku": "ATOMIC-FABRIC-C1",
                "variant barcode": self.product_a.barcode,
                "opening stock": "5",
            },
        ]
        response = self.client.post(
            reverse("catalog:product_import"),
            {
                "branch": self.branch_a.id,
                "warehouse": self.warehouse_a.id,
                "match_by": "sku",
                "file": csv_upload(
                    self.import_csv(self.branch_a, self.warehouse_a, records)
                ),
            },
        )
        self.assertTrue(response.context["results"]["errors"])
        self.assertFalse(
            Product.objects.for_business(self.business_a).filter(
                sku__in=("ATOMIC-STANDARD", "ATOMIC-FABRIC")
            ).exists()
        )

    def test_pos_barcode_and_checkout_reject_foreign_branch_products(self):
        self.client.force_login(self.user_ah)
        products = self.client.get(
            reverse("sales:pos_products"),
            {"warehouse_id": self.warehouse_a.id, "q": "Product"},
        )
        names = {row["name"] for row in products.json()["items"]}
        self.assertIn(self.product_ah.name, names)
        self.assertNotIn(self.product_mb.name, names)

        barcode = self.client.get(
            reverse("sales:pos_barcode"),
            {
                "warehouse_id": self.warehouse_a.id,
                "code": self.product_mb.barcode,
            },
        )
        self.assertFalse(barcode.json()["found"])

        self.allow_no_shift()
        checkout = self.client.post(
            reverse("sales:pos_checkout"),
            data=json.dumps({
                "branch_id": self.branch_a.id,
                "customer_id": self.walk_in_a.id,
                "checkout_token": "foreign-product-branch-test",
                "items": [{
                    "product_id": self.product_mb.id,
                    "quantity": "1",
                    "unit_price": "12",
                    "discount_amount": "0",
                }],
                "payments": [{"method_id": self.cash_a.id, "amount": "12"}],
            }),
            content_type="application/json",
        )
        self.assertEqual(checkout.status_code, 400)
        self.assertEqual(checkout.json()["error"], "Invalid product in cart.")

    def test_dashboard_and_api_product_scope(self):
        self.client.force_login(self.user_ah)
        dashboard = self.client.get(reverse("dashboard"))
        low_stock_names = {
            level.product.name
            for level in dashboard.context["widgets"]["low_stock_items"]
        }
        self.assertIn(self.product_ah.name, low_stock_names)
        self.assertNotIn(self.product_mb.name, low_stock_names)

        api = self.client.get(reverse("api:product-list"))
        self.assertEqual(api.status_code, 200)
        api_names = {row["name"] for row in api.json()["results"]}
        self.assertIn(self.product_ah.name, api_names)
        self.assertNotIn(self.product_mb.name, api_names)
        foreign = self.client.get(
            reverse("api:product-detail", args=[self.product_mb.public_id])
        )
        self.assertEqual(foreign.status_code, 404)
        tampered = self.client.get(
            reverse("api:product-list"), {"branch": self.branch_mb.id}
        )
        self.assertEqual(tampered.status_code, 404)

        self.client.force_login(self.owner_a)
        owner_branch = self.client.get(
            reverse("api:product-list"), {"branch": self.branch_mb.id}
        )
        owner_names = {row["name"] for row in owner_branch.json()["results"]}
        self.assertIn(self.product_mb.name, owner_names)
        self.assertNotIn(self.product_ah.name, owner_names)
