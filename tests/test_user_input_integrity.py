"""Regression coverage for expected user-input integrity failures."""

import json
from unittest import mock

from django.db import IntegrityError
from django.urls import reverse

from apps.accounts.forms import RoleForm
from apps.accounts.models import Role
from apps.branches.forms import BranchForm, WarehouseForm
from apps.branches.models import Branch, Warehouse
from apps.catalog import services as catalog_services
from apps.catalog.forms import QuickProductForm
from apps.catalog.models import (
    Brand,
    Category,
    Product,
    ProductVariant,
    TaxRate,
    Unit,
)
from apps.customers.forms import CustomerForm
from apps.customers.models import Customer
from apps.registers.models import CashRegister
from apps.sales.models import Sale
from apps.subscriptions.models import Plan

from .base import TenantTestCase


class CatalogSetupIntegrityTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)

    def test_duplicate_brand_create_is_a_controlled_form_error(self):
        existing = Brand.objects.create(
            business=self.business_a,
            name="Toyobo",
        )
        before = Brand.objects.for_business(self.business_a).count()

        response = self.client.post(
            reverse("catalog:brand_list"),
            {"name": "Toyobo", "is_active": "on"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.status_code, 500)
        self.assertIn("name", response.context["form"].errors)
        self.assertContains(response, "A brand with this name already exists.")
        self.assertEqual(
            Brand.objects.for_business(self.business_a).count(),
            before,
        )
        existing.refresh_from_db()
        self.assertEqual(existing.name, "Toyobo")

    def test_brand_case_duplicate_is_rejected(self):
        Brand.objects.create(business=self.business_a, name="Toyobo")
        before = Brand.objects.for_business(self.business_a).count()

        response = self.client.post(
            reverse("catalog:brand_list"),
            {"name": "toyobo", "is_active": "on"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)
        self.assertEqual(Brand.objects.for_business(self.business_a).count(), before)

    def test_brand_whitespace_duplicate_is_rejected(self):
        Brand.objects.create(business=self.business_a, name="Toyobo")
        before = Brand.objects.for_business(self.business_a).count()

        response = self.client.post(
            reverse("catalog:brand_list"),
            {"name": "  TOYOBO  ", "is_active": "on"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)
        self.assertEqual(Brand.objects.for_business(self.business_a).count(), before)

    def test_brand_edit_collision_is_rejected_without_mutation(self):
        toyobo = Brand.objects.create(business=self.business_a, name="Toyobo")
        loro_piana = Brand.objects.create(
            business=self.business_a,
            name="Loro Piana",
        )
        before = Brand.objects.for_business(self.business_a).count()

        response = self.client.post(
            f'{reverse("catalog:brand_list")}?edit={loro_piana.public_id}',
            {"name": " Toyobo ", "is_active": "on"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)
        self.assertEqual(Brand.objects.for_business(self.business_a).count(), before)
        toyobo.refresh_from_db()
        loro_piana.refresh_from_db()
        self.assertEqual(toyobo.name, "Toyobo")
        self.assertEqual(loro_piana.name, "Loro Piana")

    def test_same_brand_name_is_allowed_in_another_business(self):
        Brand.objects.create(business=self.business_a, name="Toyobo")
        self.client.force_login(self.owner_b)

        response = self.client.post(
            reverse("catalog:brand_list"),
            {"name": "  TOYOBO  ", "is_active": "on"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            list(
                Brand.objects.for_business(self.business_b).values_list(
                    "name", flat=True
                )
            ),
            ["TOYOBO"],
        )
        self.assertEqual(
            Brand.objects.for_business(self.business_a).get().name,
            "Toyobo",
        )

    def test_root_category_case_and_whitespace_collision_is_rejected(self):
        Category.objects.create(business=self.business_a, name="Fabrics")
        before = Category.objects.for_business(self.business_a).count()

        response = self.client.post(
            reverse("catalog:category_list"),
            {"name": "  FABRICS  ", "parent": "", "is_active": "on"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)
        self.assertEqual(Category.objects.for_business(self.business_a).count(), before)

    def test_category_name_is_scoped_to_its_parent(self):
        parent_a = Category.objects.create(
            business=self.business_a,
            name="Parent A",
        )
        parent_b = Category.objects.create(
            business=self.business_a,
            name="Parent B",
        )
        Category.objects.create(
            business=self.business_a,
            parent=parent_a,
            name="Fabric",
        )

        response = self.client.post(
            reverse("catalog:category_list"),
            {"name": " Fabric ", "parent": parent_b.pk, "is_active": "on"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            Category.objects.for_business(self.business_a).filter(
                parent=parent_b,
                name="Fabric",
            ).exists()
        )

    def test_unit_case_and_whitespace_collision_is_rejected(self):
        before = Unit.objects.for_business(self.business_a).count()

        response = self.client.post(
            reverse("catalog:unit_list"),
            {
                "name": "  PIECE  ",
                "abbreviation": "pc",
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)
        self.assertEqual(Unit.objects.for_business(self.business_a).count(), before)

    def test_tax_rate_case_and_whitespace_collision_is_rejected(self):
        before = TaxRate.objects.for_business(self.business_a).count()

        response = self.client.post(
            reverse("catalog:tax_list"),
            {
                "name": "  vat  ",
                "rate": "5.000",
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)
        self.assertEqual(TaxRate.objects.for_business(self.business_a).count(), before)

    def test_shared_crud_integrity_race_becomes_the_same_form_error(self):
        Brand.objects.create(business=self.business_a, name="Toyobo")
        before = Brand.objects.for_business(self.business_a).count()

        with mock.patch(
            "apps.catalog.forms.BrandForm.name_conflict_exists",
            return_value=False,
        ) as conflict_check:
            response = self.client.post(
                reverse("catalog:brand_list"),
                {"name": "Toyobo", "is_active": "on"},
            )

        self.assertEqual(response.status_code, 200)
        conflict_check.assert_called_once_with()
        self.assertIn("name", response.context["form"].errors)
        self.assertEqual(Brand.objects.for_business(self.business_a).count(), before)

    def test_shared_crud_reraises_unrelated_integrity_errors(self):
        with (
            mock.patch(
                "apps.catalog.forms.BrandForm.name_conflict_exists",
                return_value=False,
            ),
            mock.patch(
                "apps.catalog.models.Brand.save",
                side_effect=IntegrityError("unrelated database failure"),
            ),
            self.assertRaises(IntegrityError),
        ):
            self.client.post(
                reverse("catalog:brand_list"),
                {"name": "New Brand", "is_active": "on"},
            )


class CatalogIdentifierRaceTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)

    def product_payload(self, **overrides):
        payload = {
            "name": "Concurrent Product",
            "product_type": Product.Type.STANDARD,
            "purchase_price": "1.000",
            "sale_price": "2.000",
            "wholesale_price": "0",
            "minimum_sale_price": "0",
            "reorder_level": "0",
            "opening_stock": "0",
            "opening_warehouse": self.warehouse_a.pk,
            "track_inventory": "on",
            "is_active": "on",
        }
        payload.update(overrides)
        return payload

    def test_product_database_identifier_race_is_a_form_error(self):
        Product.objects.create(
            business=self.business_a,
            name="Race Winner",
            sku="RACE-SKU",
        )
        before = Product.objects.for_business(self.business_a).count()

        with mock.patch(
            "apps.catalog.services.find_reusable_product",
            return_value=None,
        ):
            response = self.client.post(
                reverse("catalog:product_create"),
                self.product_payload(sku="RACE-SKU"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("sku", response.context["form"].errors)
        self.assertEqual(Product.objects.for_business(self.business_a).count(), before)

    def test_variant_database_identifier_race_is_a_form_error_without_partial_write(self):
        existing_parent = Product.objects.create(
            business=self.business_a,
            name="Existing Variant Parent",
            product_type=Product.Type.VARIANT,
        )
        ProductVariant.objects.create(
            business=self.business_a,
            product=existing_parent,
            name="Winner",
            sku="RACE-VARIANT",
        )
        before = ProductVariant.objects.for_business(self.business_a).count()

        with mock.patch(
            "apps.catalog.forms.VariantForm._unique_check",
            autospec=True,
            side_effect=lambda _form, _field, value: value,
        ):
            response = self.client.post(
                reverse(
                    "catalog:variant_create",
                    args=[self.product_a.public_id],
                ),
                {
                    "name": "Losing Variant",
                    "sku": "RACE-VARIANT",
                    "barcode": "",
                    "purchase_price": "1.000",
                    "sale_price": "2.000",
                    "is_active": "on",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("sku", response.context["form"].errors)
        self.assertEqual(
            ProductVariant.objects.for_business(self.business_a).count(),
            before,
        )
        self.product_a.refresh_from_db()
        self.assertEqual(self.product_a.product_type, Product.Type.STANDARD)

    def test_product_service_reraises_unrelated_integrity_errors(self):
        product = Product(
            business=self.business_a,
            name="Unrelated Failure",
            sku="UNIQUE-SKU",
        )

        with (
            mock.patch.object(
                product,
                "save",
                side_effect=IntegrityError("unrelated database failure"),
            ),
            self.assertRaises(IntegrityError),
        ):
            catalog_services.save_product(
                product=product,
                business=self.business_a,
                user=self.owner_a,
                membership=self.membership_a(),
            )


class APIWriteSurfaceTests(TenantTestCase):
    def test_model_api_routes_are_read_only_and_do_not_mutate(self):
        Plan.objects.filter(pk=self.business_a.subscription.plan_id).update(
            feature_api_access=True,
            feature_sales=True,
        )
        self.client.force_login(self.owner_a)
        before = {
            "products": Product.objects.for_business(self.business_a).count(),
            "categories": Category.objects.for_business(self.business_a).count(),
            "customers": Customer.objects.for_business(self.business_a).count(),
            "sales": Sale.objects.for_business(self.business_a).count(),
        }

        for route_name in (
            "api:product-list",
            "api:category-list",
            "api:customer-list",
            "api:sale-list",
        ):
            with self.subTest(route=route_name):
                response = self.client.post(
                    reverse(route_name),
                    data=json.dumps({"name": "Must Not Write"}),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 405)

        self.assertEqual(
            {
                "products": Product.objects.for_business(self.business_a).count(),
                "categories": Category.objects.for_business(self.business_a).count(),
                "customers": Customer.objects.for_business(self.business_a).count(),
                "sales": Sale.objects.for_business(self.business_a).count(),
            },
            before,
        )


class TenantCodeRaceFallbackTests(TenantTestCase):
    def setUp(self):
        plan = self.business_a.subscription.plan
        plan.max_branches = 20
        plan.max_warehouses = 20
        plan.max_pos_terminals = 20
        plan.feature_custom_roles = True
        plan.save(
            update_fields=[
                "max_branches",
                "max_warehouses",
                "max_pos_terminals",
                "feature_custom_roles",
            ]
        )
        self.client.force_login(self.owner_a)

    def test_branch_and_warehouse_code_races_are_controlled(self):
        for label, route, form_class, payload, model in (
            (
                "branch",
                "branches:branch_create",
                BranchForm,
                {
                    "name": "Concurrent Branch",
                    "code": self.branch_a.code,
                    "usage_type": Branch.UsageType.WORKSHOP_STOCK,
                    "address": "",
                    "phone": "",
                    "email": "",
                    "invoice_prefix": "",
                    "receipt_footer": "",
                    "is_active": "on",
                },
                Branch,
            ),
            (
                "warehouse",
                "branches:warehouse_create",
                WarehouseForm,
                {
                    "name": "Concurrent Warehouse",
                    "code": self.warehouse_a.code,
                    "branch": self.branch_a.pk,
                    "address": "",
                    "is_active": "on",
                },
                Warehouse,
            ),
        ):
            with self.subTest(label=label):
                before = model.objects.for_business(self.business_a).count()
                with mock.patch.object(
                    form_class,
                    "clean_code",
                    return_value=payload["code"],
                ):
                    response = self.client.post(reverse(route), payload)

                self.assertEqual(response.status_code, 200)
                self.assertIn("code", response.context["form"].errors)
                self.assertEqual(
                    model.objects.for_business(self.business_a).count(),
                    before,
                )

    def test_role_name_race_is_controlled(self):
        existing = Role.objects.for_business(self.business_a).get(name="Cashier")

        with mock.patch.object(
            RoleForm,
            "clean_name",
            return_value=existing.name,
        ):
            response = self.client.post(
                reverse("accounts:role_create"),
                {"name": existing.name, "permissions": ["sales.view"]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)
        self.assertEqual(
            Role.objects.for_business(self.business_a)
            .filter(name=existing.name)
            .count(),
            1,
        )

    def test_register_code_race_is_controlled(self):
        before = CashRegister.objects.for_business(self.business_a).count()

        with mock.patch(
            "apps.registers.forms.RegisterForm.clean_code",
            return_value=self.register_a.code,
        ):
            response = self.client.post(
                reverse("registers:register_create"),
                {
                    "name": "Concurrent Register",
                    "code": self.register_a.code,
                    "branch": self.branch_a.pk,
                    "receipt_printer": "80mm",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("code", response.context["form"].errors)
        self.assertEqual(
            CashRegister.objects.for_business(self.business_a).count(),
            before,
        )

    def test_customer_code_create_and_edit_races_are_controlled(self):
        existing = Customer.objects.create(
            business=self.business_a,
            home_branch=self.branch_a,
            code="AUDIT-CUSTOMER-RACE",
            full_name="Audit Existing Customer",
        )
        edited = Customer.objects.create(
            business=self.business_a,
            home_branch=self.branch_a,
            code="AUDIT-CUSTOMER-EDIT",
            full_name="Audit Edited Customer",
        )
        payload = {
            "home_branch": self.branch_a.pk,
            "full_name": "Audit Concurrent Customer",
            "code": existing.code,
            "mobile": "",
            "whatsapp": "",
            "email": "",
            "address": "",
            "city": "",
            "country": "",
            "group": "",
            "tax_number": "",
            "credit_limit": "0",
            "notes": "",
            "is_active": "on",
        }

        with mock.patch.object(
            CustomerForm,
            "clean_code",
            return_value=existing.code,
        ):
            create_response = self.client.post(
                reverse("customers:create"),
                payload,
            )
            edit_response = self.client.post(
                reverse("customers:edit", args=[edited.public_id]),
                payload,
            )

        self.assertEqual(create_response.status_code, 200)
        self.assertIn("code", create_response.context["form"].errors)
        self.assertEqual(edit_response.status_code, 200)
        self.assertIn("code", edit_response.context["form"].errors)
        edited.refresh_from_db()
        self.assertEqual(edited.code, "AUDIT-CUSTOMER-EDIT")
        self.assertEqual(
            Customer.objects.for_business(self.business_a)
            .filter(code=existing.code)
            .count(),
            1,
        )

    def test_pos_quick_customer_code_race_is_controlled(self):
        existing = Customer.objects.create(
            business=self.business_a,
            home_branch=self.branch_a,
            code="AUDIT-QUICK-CUSTOMER-RACE",
            full_name="Audit Existing Quick Customer",
        )

        with mock.patch(
            "apps.customers.services.next_customer_code",
            return_value=existing.code,
        ):
            response = self.client.post(
                reverse("sales:pos_quick_customer"),
                {
                    "branch_id": self.branch_a.pk,
                    "name": "Audit Concurrent Quick Customer",
                    "mobile": "",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(
            Customer.objects.for_business(self.business_a)
            .filter(code=existing.code)
            .count(),
            1,
        )

    def test_purchase_quick_add_uses_narrow_identifier_race_handling(self):
        piece = Unit.objects.for_business(self.business_a).get(name="Piece")
        endpoint = reverse("purchases:quick_add_product")
        payload = {
            "name": "Concurrent Quick Product",
            "sku": self.product_a.sku,
            "category": "",
            "unit": piece.pk,
            "purchase_price": "1.000",
            "sale_price": "2.000",
            "tax_rate": self.tax_a.pk,
            "price_includes_tax": "False",
            "track_inventory": "on",
        }

        with mock.patch.object(
            QuickProductForm,
            "_unique_check",
            autospec=True,
            side_effect=lambda _form, _field, value: value,
        ):
            response = self.client.post(endpoint, payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["errors"]["sku"],
            ["This SKU is already in use."],
        )

        payload["sku"] = "UNIQUE-QUICK-SKU"
        with (
            mock.patch.object(
                Product,
                "save",
                side_effect=IntegrityError("unrelated quick product constraint"),
            ),
            self.assertRaisesMessage(
                IntegrityError,
                "unrelated quick product constraint",
            ),
        ):
            self.client.post(endpoint, payload)

    def test_tenant_code_fallbacks_reraise_unrelated_integrity_errors(self):
        with (
            mock.patch.object(
                Customer,
                "save",
                side_effect=IntegrityError("unrelated customer constraint"),
            ),
            self.assertRaisesMessage(
                IntegrityError,
                "unrelated customer constraint",
            ),
        ):
            self.client.post(
                reverse("customers:create"),
                {
                    "home_branch": self.branch_a.pk,
                    "full_name": "Unique Customer",
                    "code": "UNIQUE-CUSTOMER",
                    "mobile": "",
                    "whatsapp": "",
                    "email": "",
                    "address": "",
                    "city": "",
                    "country": "",
                    "group": "",
                    "tax_number": "",
                    "credit_limit": "0",
                    "notes": "",
                    "is_active": "on",
                },
            )

        with (
            mock.patch.object(
                Branch,
                "save",
                side_effect=IntegrityError("unrelated branch constraint"),
            ),
            self.assertRaisesMessage(
                IntegrityError,
                "unrelated branch constraint",
            ),
        ):
            self.client.post(
                reverse("branches:branch_create"),
                {
                    "name": "Unique Branch",
                    "code": "UNIQUE-BRANCH",
                    "usage_type": Branch.UsageType.WORKSHOP_STOCK,
                    "address": "",
                    "phone": "",
                    "email": "",
                    "invoice_prefix": "",
                    "receipt_footer": "",
                    "is_active": "on",
                },
            )

        with (
            mock.patch.object(
                CashRegister,
                "save",
                side_effect=IntegrityError("unrelated register constraint"),
            ),
            self.assertRaisesMessage(
                IntegrityError,
                "unrelated register constraint",
            ),
        ):
            self.client.post(
                reverse("registers:register_create"),
                {
                    "name": "Unique Register",
                    "code": "UNIQUE-REGISTER",
                    "branch": self.branch_a.pk,
                    "receipt_printer": "80mm",
                },
            )

        with (
            mock.patch.object(
                Role,
                "save",
                side_effect=IntegrityError("unrelated role constraint"),
            ),
            self.assertRaisesMessage(
                IntegrityError,
                "unrelated role constraint",
            ),
        ):
            self.client.post(
                reverse("accounts:role_create"),
                {"name": "Unique Role", "permissions": ["sales.view"]},
            )
