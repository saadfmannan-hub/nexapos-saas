"""Focused adversarial coverage for customer and inventory branch context."""

import json
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import Membership, Role, User
from apps.branches.models import Branch, Warehouse
from apps.customers.models import Customer
from apps.customers.services import ensure_walk_in_customer
from apps.inventory import services as inventory
from apps.subscriptions.models import Plan, Subscription

from .base import TenantTestCase


def csv_upload(text):
    return SimpleUploadedFile(
        "branch-context.csv",
        text.encode("utf-8"),
        content_type="text/csv",
    )


class BranchContextIsolationTests(TenantTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.branch_2 = Branch.objects.create(
            business=cls.business_a,
            name="Mabelah Branch",
            code="MB",
        )
        cls.warehouse_2 = Warehouse.objects.create(
            business=cls.business_a,
            branch=cls.branch_2,
            name="Mabelah Stockroom",
            code="MB-STOCK",
        )
        cls.walk_in_2 = ensure_walk_in_customer(cls.business_a, cls.branch_2)
        cls.walk_in_a = ensure_walk_in_customer(cls.business_a, cls.branch_a)

        cls.customer_a = Customer.objects.create(
            business=cls.business_a,
            home_branch=cls.branch_a,
            code="SAME-001",
            full_name="Al Hail Customer",
            mobile="91000001",
            balance=Decimal("25.000"),
        )
        cls.customer_2 = Customer.objects.create(
            business=cls.business_a,
            home_branch=cls.branch_2,
            code="SAME-001",
            full_name="Mabelah Customer",
            mobile="92000001",
            balance=Decimal("40.000"),
        )
        cls.unassigned = Customer.objects.create(
            business=cls.business_a,
            code="LEGACY-UNASSIGNED",
            full_name="Legacy Unassigned Customer",
        )
        inventory.set_opening_stock(
            business=cls.business_a,
            warehouse=cls.warehouse_2,
            product=cls.product_a,
            quantity=Decimal("30.000"),
            unit_cost=Decimal("4.000"),
            user=cls.owner_a,
        )

        cls.branch_user = User.objects.create_user(
            email="branch-user@example.com",
            password="StrongPass123!",
            full_name="Al Hail Branch User",
        )
        cls.branch_role = Role.objects.create(
            business=cls.business_a,
            name="Branch Operations UAT",
            permissions=[
                "dashboard.view",
                "reports.view",
                "reports.financial",
                "reports.export",
                "sales.view",
                "sales.create",
                "sales.credit",
                "products.view",
                "products.export",
                "products.import",
                "customers.view",
                "customers.manage",
                "customers.export",
                "customers.import",
                "customers.payments",
                "inventory.view",
                "inventory.export",
                "inventory.import",
            ],
        )
        cls.branch_membership = Membership.objects.create(
            business=cls.business_a,
            user=cls.branch_user,
            role=cls.branch_role,
        )
        cls.branch_membership.branches.add(cls.branch_a)

        plan_ids = Subscription.objects.filter(business=cls.business_a).values_list(
            "plan_id", flat=True
        )
        Plan.objects.filter(pk__in=plan_ids).update(feature_api_access=True)

    def setUp(self):
        self.client.force_login(self.branch_user)

    def test_owner_sees_all_customers_and_can_filter_one_branch(self):
        self.client.force_login(self.owner_a)
        response = self.client.get(reverse("customers:list"))
        self.assertContains(response, self.customer_a.full_name)
        self.assertContains(response, self.customer_2.full_name)
        self.assertContains(response, self.unassigned.full_name)

        filtered = self.client.get(
            reverse("customers:list"), {"branch": self.branch_a.id}
        )
        self.assertContains(filtered, self.customer_a.full_name)
        self.assertNotContains(filtered, self.customer_2.full_name)
        self.assertNotContains(filtered, self.unassigned.full_name)

    def test_branch_user_list_and_direct_customer_routes_fail_closed(self):
        response = self.client.get(reverse("customers:list"))
        self.assertContains(response, self.customer_a.full_name)
        self.assertNotContains(response, self.customer_2.full_name)
        self.assertNotContains(response, self.unassigned.full_name)

        for route in ("detail", "edit", "statement"):
            response = self.client.get(
                reverse(f"customers:{route}", args=[self.customer_2.public_id])
            )
            self.assertEqual(response.status_code, 404)
        payment = self.client.post(
            reverse("customers:payment", args=[self.customer_2.public_id]),
            {"amount": "1.000", "payment_method": self.cash_a.id},
        )
        self.assertEqual(payment.status_code, 404)

    def test_branch_user_cannot_create_or_edit_into_another_branch(self):
        create = self.client.post(reverse("customers:create"), {
            "home_branch": self.branch_2.id,
            "full_name": "Forged Branch Customer",
            "mobile": "93000001",
            "is_active": "on",
        })
        self.assertEqual(create.status_code, 404)
        self.assertFalse(Customer.objects.filter(full_name="Forged Branch Customer").exists())

        edit = self.client.post(
            reverse("customers:edit", args=[self.customer_a.public_id]),
            {
                "home_branch": self.branch_2.id,
                "full_name": self.customer_a.full_name,
                "code": self.customer_a.code,
                "mobile": self.customer_a.mobile,
                "is_active": "on",
            },
        )
        self.assertEqual(edit.status_code, 404)
        self.customer_a.refresh_from_db()
        self.assertEqual(self.customer_a.home_branch, self.branch_a)

    def test_pos_lookup_default_walk_in_and_forged_customer_are_branch_scoped(self):
        response = self.client.get(
            reverse("sales:pos_customers"),
            {"branch_id": self.branch_a.id, "q": "Customer"},
        )
        names = {row["name"] for row in response.json()["results"]}
        self.assertIn(self.customer_a.full_name, names)
        self.assertNotIn(self.customer_2.full_name, names)

        foreign_branch = self.client.get(
            reverse("sales:pos_customers"), {"branch_id": self.branch_2.id}
        )
        self.assertEqual(foreign_branch.status_code, 404)

        forged_checkout = self.client.post(
            reverse("sales:pos_checkout"),
            data=json.dumps({
                "branch_id": self.branch_a.id,
                "customer_id": self.customer_2.id,
                "checkout_token": "cross-branch-customer",
                "items": [],
                "payments": [],
            }),
            content_type="application/json",
        )
        self.assertEqual(forged_checkout.status_code, 400)
        self.assertEqual(forged_checkout.json()["error"], "Invalid customer.")

        pos = self.client.get(reverse("sales:pos"))
        self.assertEqual(pos.context["walk_in"], self.walk_in_a)
        self.assertEqual(pos.context["branch"], self.branch_a)
        self.assertEqual(
            Customer.objects.filter(
                business=self.business_a,
                home_branch=self.branch_a,
                is_walk_in=True,
            ).count(),
            1,
        )
        self.assertEqual(
            Customer.objects.filter(
                business=self.business_a,
                home_branch=self.branch_2,
                is_walk_in=True,
            ).count(),
            1,
        )

    def test_customer_import_export_template_are_selected_branch_only(self):
        self.client.force_login(self.owner_a)
        for route in ("export", "import", "import_template"):
            response = self.client.get(reverse(f"customers:{route}"))
            self.assertEqual(response.status_code, 404)

        self.client.force_login(self.branch_user)
        template = self.client.get(reverse("customers:import_template"))
        template_text = template.content.decode()
        self.assertIn("Branch Code", template_text)
        self.assertIn(self.branch_a.code, template_text)

        tampered = self.client.post(reverse("customers:import"), {
            "branch": self.branch_a.id,
            "mode": "skip",
            "file": csv_upload(
                "branch code,branch name,customer code,customer name,mobile\n"
                f"{self.branch_2.code},{self.branch_2.name},TAMPER-1,Tampered,94000001\n"
            ),
        })
        self.assertEqual(tampered.context["results"]["summary"]["failed"], 1)
        self.assertFalse(Customer.objects.filter(code="TAMPER-1").exists())

        foreign_same_code = Customer.objects.create(
            business=self.business_a,
            home_branch=self.branch_2,
            code="BRANCH-DUP",
            full_name="Existing Mabelah Duplicate",
            mobile="95000002",
        )
        same_code = self.client.post(reverse("customers:import"), {
            "branch": self.branch_a.id,
            "mode": "skip",
            "file": csv_upload(
                "customer code,customer name,mobile\n"
                "BRANCH-DUP,Imported Al Hail,94000002\n"
            ),
        })
        self.assertEqual(same_code.context["results"]["summary"]["imported"], 1)
        self.assertTrue(Customer.objects.filter(
            business=self.business_a,
            home_branch=self.branch_a,
            code="BRANCH-DUP",
        ).exists())
        foreign_same_code.refresh_from_db()
        self.assertEqual(foreign_same_code.full_name, "Existing Mabelah Duplicate")

        exported = self.client.get(reverse("customers:export"))
        export_text = exported.content.decode()
        self.assertIn(self.customer_a.full_name, export_text)
        self.assertNotIn(self.customer_2.full_name, export_text)
        self.assertIn("Branch Code", export_text)

    def test_inventory_context_template_import_export_and_tampering(self):
        page = self.client.get(reverse("inventory:stock_list"))
        self.assertEqual(page.context["selected_branch"], self.branch_a)
        self.assertEqual(page.context["selected_warehouse"], self.warehouse_a)
        self.assertNotContains(page, self.warehouse_2.name)

        foreign = self.client.get(reverse("inventory:stock_list"), {
            "branch": self.branch_a.id,
            "warehouse": self.warehouse_2.id,
        })
        self.assertEqual(foreign.status_code, 404)

        template = self.client.get(reverse("inventory:import_template"))
        template_text = template.content.decode()
        for header in (
            "Branch Code", "Branch Name", "Warehouse Code", "Warehouse Name"
        ):
            self.assertIn(header, template_text)

        before_other = inventory.get_stock(
            self.business_a, self.warehouse_2, self.product_a
        )
        imported = self.client.post(reverse("inventory:import"), {
            "branch": self.branch_a.id,
            "warehouse": self.warehouse_a.id,
            "mode": "add",
            "file": csv_upload(
                "branch code,branch name,warehouse code,warehouse name,sku,quantity\n"
                f"{self.branch_a.code},{self.branch_a.name},"
                f"{self.warehouse_a.code},{self.warehouse_a.name},WID-A,5\n"
            ),
        })
        self.assertEqual(imported.context["results"]["summary"]["imported"], 1)
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.product_a),
            Decimal("105.000"),
        )
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_2, self.product_a),
            before_other,
        )

        exported = self.client.get(reverse("inventory:export"))
        export_text = exported.content.decode()
        self.assertIn(self.warehouse_a.name, export_text)
        self.assertNotIn(self.warehouse_2.name, export_text)

        stock_report = self.client.get(
            reverse("reports:view", args=["current_stock"])
        )
        self.assertContains(stock_report, self.warehouse_a.name)
        self.assertNotContains(stock_report, self.warehouse_2.name)
        tampered_report = self.client.get(
            reverse("reports:view", args=["current_stock"]),
            {"warehouse": self.warehouse_2.id},
        )
        self.assertEqual(tampered_report.status_code, 404)

    def test_product_master_is_shared_but_branch_stock_and_master_exports_are_safe(self):
        self.assertEqual(
            self.product_a.__class__.objects.for_business(self.business_a).filter(
                sku=self.product_a.sku
            ).count(),
            1,
        )
        detail = self.client.get(
            reverse("catalog:product_detail", args=[self.product_a.public_id])
        )
        self.assertContains(detail, self.warehouse_a.name)
        self.assertNotContains(detail, self.warehouse_2.name)
        self.assertEqual(
            self.client.get(reverse("catalog:product_export")).status_code,
            403,
        )

    def test_customer_api_and_dashboard_receivables_are_branch_scoped(self):
        api = self.client.get(reverse("api:customer-list"))
        self.assertEqual(api.status_code, 200)
        names = {row["full_name"] for row in api.json()["results"]}
        self.assertIn(self.customer_a.full_name, names)
        self.assertNotIn(self.customer_2.full_name, names)
        self.assertNotIn(self.unassigned.full_name, names)
        foreign = self.client.get(
            reverse("api:customer-detail", args=[self.customer_2.public_id])
        )
        self.assertEqual(foreign.status_code, 404)

        dashboard = self.client.get(reverse("dashboard"))
        receivable_names = {
            customer.full_name
            for customer in dashboard.context["widgets"]["pending_receivables"]
        }
        self.assertIn(self.customer_a.full_name, receivable_names)
        self.assertNotIn(self.customer_2.full_name, receivable_names)
