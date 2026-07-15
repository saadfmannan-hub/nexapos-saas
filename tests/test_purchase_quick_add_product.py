"""Quick Add Product coverage for the New Purchase workflow."""
from decimal import Decimal

from django.urls import reverse

from apps.accounts.models import Membership, Role, User
from apps.catalog.models import Category, Product, ProductVariant, TaxRate, Unit
from apps.inventory import services as inventory
from apps.inventory.models import StockLevel, StockMovement
from apps.purchases import services as purchase_services
from apps.purchases.models import Purchase
from apps.suppliers.models import Supplier

from .base import TenantTestCase


D = Decimal


class PurchaseQuickAddProductTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)
        self.endpoint = reverse("purchases:quick_add_product")
        self.category_a = Category.objects.create(
            business=self.business_a, name="Quick Add Category",
        )
        self.category_b = Category.objects.create(
            business=self.business_b, name="Other Tenant Category",
        )
        self.unit_a = Unit.objects.for_business(self.business_a).get(name="Piece")
        self.meter_a = Unit.objects.for_business(self.business_a).get(name="Meter")
        self.unit_b = Unit.objects.for_business(self.business_b).get(name="Piece")
        self.tax_b = TaxRate.objects.create(
            business=self.business_b, name="Other VAT", rate=D("5.000"),
        )
        self.supplier = Supplier.objects.create(
            business=self.business_a, code="QUICK-SUP", name="Quick Supplier",
        )

    def payload(self, **overrides):
        data = {
            "name": "Quick Purchase Product",
            "sku": "QUICK-001",
            "category": self.category_a.pk,
            "unit": self.unit_a.pk,
            "purchase_price": "4.125",
            "sale_price": "7.875",
            "tax_rate": self.tax_a.pk,
            "price_includes_tax": "False",
            "track_inventory": "on",
        }
        data.update(overrides)
        return data

    def quick_create(self, **overrides):
        response = self.client.post(self.endpoint, self.payload(**overrides))
        self.assertEqual(response.status_code, 201, response.content)
        return Product.objects.for_business(self.business_a).get(
            pk=response.json()["product"]["product_id"],
        )

    def login_member(self, permissions, suffix):
        user = User.objects.create_user(
            email=f"quick-{suffix}@example.com",
            password="StrongPass123!",
            full_name=f"Quick {suffix}",
        )
        role = Role.objects.create(
            business=self.business_a,
            name=f"Quick {suffix}",
            permissions=permissions,
        )
        Membership.objects.create(
            business=self.business_a, user=user, role=role,
        )
        self.client.force_login(user)
        return user

    def purchase_payload(self, product, **overrides):
        data = {
            "supplier_id": self.supplier.pk,
            "branch_id": self.branch_a.pk,
            "warehouse_id": self.warehouse_a.pk,
            "purchase_date": "2026-07-15",
            "due_date": "",
            "supplier_invoice_number": "SUP-INV-QUICK",
            "product_id": [str(product.pk)],
            "variant_id": [""],
            "quantity": ["2.000"],
            "unit_cost": ["4.125"],
            "discount": "0.250",
            "shipping": "0.500",
            "other": "0.125",
            "notes": "Preserved purchase notes",
        }
        data.update(overrides)
        return data

    def test_authorized_user_can_quick_create_product(self):
        response = self.client.post(self.endpoint, self.payload())
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["product"]["label"], "Quick Purchase Product")
        self.assertEqual(data["product"]["unit_cost"], "4.125")
        product = Product.objects.get(pk=data["product"]["product_id"])
        self.assertEqual(product.product_type, Product.Type.STANDARD)
        self.assertEqual(product.category, self.category_a)
        self.assertEqual(product.unit, self.unit_a)
        self.assertEqual(product.tax_rate, self.tax_a)
        self.assertTrue(product.track_inventory)
        self.assertTrue(product.is_active)

    def test_endpoint_requires_post(self):
        self.assertEqual(self.client.get(self.endpoint).status_code, 405)

    def test_purchase_permission_does_not_bypass_product_permission(self):
        self.login_member(
            ["purchases.view", "purchases.manage", "inventory.view"],
            "purchase-only",
        )
        response = self.client.post(self.endpoint, self.payload())
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Product.objects.filter(sku="QUICK-001").exists())

    def test_product_permission_does_not_bypass_purchase_permission(self):
        self.login_member(
            ["products.view", "products.manage"],
            "product-only",
        )
        response = self.client.post(self.endpoint, self.payload())
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Product.objects.filter(sku="QUICK-001").exists())

    def test_product_is_forced_to_active_request_tenant(self):
        product = self.quick_create(
            business_id=self.business_b.pk,
            tenant_id=self.business_b.pk,
            owner_id=self.owner_b.pk,
        )
        self.assertEqual(product.business, self.business_a)
        self.assertFalse(
            Product.objects.for_business(self.business_b).filter(pk=product.pk).exists(),
        )

    def test_cross_tenant_category_is_rejected(self):
        response = self.client.post(
            self.endpoint, self.payload(category=self.category_b.pk),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("category", response.json()["errors"])

    def test_cross_tenant_unit_is_rejected(self):
        response = self.client.post(
            self.endpoint, self.payload(unit=self.unit_b.pk),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("unit", response.json()["errors"])

    def test_cross_tenant_tax_is_rejected(self):
        response = self.client.post(
            self.endpoint, self.payload(tax_rate=self.tax_b.pk),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("tax_rate", response.json()["errors"])

    def test_missing_name_returns_field_error(self):
        response = self.client.post(self.endpoint, self.payload(name=""))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["errors"]["name"], ["Enter the product name."],
        )

    def test_missing_unit_returns_field_error(self):
        response = self.client.post(self.endpoint, self.payload(unit=""))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["errors"]["unit"], ["Select a product unit."],
        )

    def test_duplicate_product_sku_returns_clear_error(self):
        response = self.client.post(self.endpoint, self.payload(sku=self.product_a.sku))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["errors"]["sku"], ["This SKU is already in use."],
        )

    def test_duplicate_variant_sku_returns_clear_error(self):
        ProductVariant.objects.create(
            business=self.business_a,
            product=self.product_a,
            name="Existing Variant",
            sku="QUICK-VARIANT",
        )
        response = self.client.post(
            self.endpoint, self.payload(sku="QUICK-VARIANT"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["errors"]["sku"], ["This SKU is already in use."],
        )

    def test_decimal_prices_are_preserved(self):
        product = self.quick_create(
            sku="QUICK-DECIMAL",
            purchase_price="12.345",
            sale_price="19.876",
        )
        self.assertEqual(product.purchase_price, D("12.345"))
        self.assertEqual(product.sale_price, D("19.876"))
        self.assertFalse(product.price_includes_tax)

    def test_meter_unit_remains_decimal_compatible(self):
        product = self.quick_create(
            name="Quick Fabric Roll",
            sku="QUICK-METER",
            unit=self.meter_a.pk,
            purchase_price="1.275",
            sale_price="2.625",
        )
        self.assertEqual(product.unit, self.meter_a)
        self.assertTrue(product.unit.allow_decimal)
        self.assertEqual(product.purchase_price, D("1.275"))

    def test_blank_sku_remains_supported(self):
        product = self.quick_create(sku="")
        self.assertEqual(product.sku, "")

    def test_inventory_tracking_choice_is_preserved(self):
        product = self.quick_create(sku="QUICK-NONSTOCK", track_inventory="")
        self.assertFalse(product.track_inventory)
        self.assertFalse(product.is_stocked)

    def test_quick_add_creates_no_opening_stock_or_inventory_rows(self):
        movements_before = StockMovement.objects.count()
        levels_before = StockLevel.objects.count()
        product = self.quick_create(sku="QUICK-NO-STOCK")
        self.assertEqual(StockMovement.objects.count(), movements_before)
        self.assertEqual(StockLevel.objects.count(), levels_before)
        self.assertFalse(StockMovement.objects.filter(product=product).exists())
        self.assertFalse(StockLevel.objects.filter(product=product).exists())

    def test_product_plan_limit_is_enforced(self):
        plan = self.business_a.subscription.plan
        plan.max_products = Product.objects.for_business(self.business_a).count()
        plan.save(update_fields=["max_products"])
        response = self.client.post(self.endpoint, self.payload(sku="QUICK-LIMIT"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("products", response.json()["errors"]["__all__"][0])
        self.assertFalse(Product.objects.filter(sku="QUICK-LIMIT").exists())

    def test_created_product_appears_in_tenant_product_search(self):
        product = self.quick_create(name="Searchable Quick Item", sku="SEARCH-QUICK")
        response = self.client.get(reverse("inventory:item_search"), {"q": "Searchable"})
        self.assertEqual(response.status_code, 200)
        ids = [item["product_id"] for item in response.json()["results"]]
        self.assertIn(product.pk, ids)

    def test_product_search_remains_tenant_isolated(self):
        product = self.quick_create(name="Isolated Quick Item", sku="ISOLATED-QUICK")
        self.client.force_login(self.owner_b)
        response = self.client.get(reverse("inventory:item_search"), {"q": "Isolated"})
        self.assertEqual(response.status_code, 200)
        ids = [item["product_id"] for item in response.json()["results"]]
        self.assertNotIn(product.pk, ids)

    def test_existing_purchase_form_accepts_quick_product_without_posting_stock(self):
        product = self.quick_create(sku="QUICK-PURCHASE")
        movements_before = StockMovement.objects.filter(product=product).count()
        response = self.client.post(
            reverse("purchases:create"), self.purchase_payload(product),
        )
        self.assertEqual(response.status_code, 302)
        purchase = Purchase.objects.for_business(self.business_a).get(
            supplier_invoice_number="SUP-INV-QUICK",
        )
        self.assertEqual(purchase.subtotal, D("8.250"))
        self.assertEqual(purchase.total, D("8.625"))
        self.assertEqual(purchase.tax_amount, D("0.000"))
        self.assertEqual(purchase.items.get().product, product)
        self.assertEqual(
            StockMovement.objects.filter(product=product).count(), movements_before,
        )

    def test_receipt_posts_stock_only_through_existing_purchase_workflow(self):
        product = self.quick_create(sku="QUICK-RECEIVE")
        purchase = purchase_services.create_purchase(
            business=self.business_a,
            supplier=self.supplier,
            branch=self.branch_a,
            warehouse=self.warehouse_a,
            rows=[{
                "product": product,
                "variant": None,
                "quantity": D("2.750"),
                "unit_cost": D("4.125"),
            }],
            user=self.owner_a,
            purchase_date="2026-07-15",
        )
        self.assertFalse(StockMovement.objects.filter(product=product).exists())
        item = purchase.items.get()
        purchase_services.receive_purchase(
            purchase=purchase,
            quantities={item.pk: D("2.750")},
            user=self.owner_a,
        )
        movement = StockMovement.objects.get(product=product)
        self.assertEqual(movement.movement_type, StockMovement.Type.PURCHASE)
        self.assertEqual(movement.quantity, D("2.750"))
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, product),
            D("2.750"),
        )

    def test_purchase_form_shows_quick_add_without_opening_stock(self):
        response = self.client.get(reverse("purchases:create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Quick Add Product", count=2)
        self.assertContains(response, 'id="quickProductModal"')
        self.assertContains(response, "Product Unit")
        self.assertContains(response, "Cost Price")
        self.assertNotContains(response, 'name="opening_stock"')
        self.assertNotContains(response, 'name="opening_warehouse"')

    def test_quick_add_ui_is_hidden_without_product_permission(self):
        self.login_member(
            ["purchases.view", "purchases.manage", "inventory.view"],
            "purchase-ui",
        )
        response = self.client.get(reverse("purchases:create"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Quick Add Product")
        self.assertNotContains(response, 'id="quickProductModal"')

    def test_frontend_contract_selects_created_product_and_preserves_purchase_form(self):
        html = self.client.get(reverse("purchases:create")).content.decode()
        self.assertIn("this.add(data.product);", html)
        self.assertIn("this.rows.push({...r, quantity: 1, unit_cost: unitCost});", html)
        self.assertIn("body: new FormData(form)", html)
        self.assertIn("form.reset();", html)
        self.assertNotIn("window.location", html)

    def test_frontend_contract_keeps_modal_values_on_failure(self):
        html = self.client.get(reverse("purchases:create")).content.decode()
        error_branch = html.split("if (!response.ok || !data.ok)", 1)[1].split(
            "this.add(data.product);", 1,
        )[0]
        self.assertIn("this.quickErrors = data.errors", error_branch)
        self.assertIn("this.focusQuickError();", error_branch)
        self.assertNotIn("form.reset()", error_branch)
        self.assertNotIn(".hide()", error_branch)

    def test_frontend_contract_prevents_double_submit(self):
        html = self.client.get(reverse("purchases:create")).content.decode()
        self.assertIn("if (this.quickSubmitting) return;", html)
        self.assertIn(':disabled="quickSubmitting"', html)
        self.assertIn("Saving", html)
