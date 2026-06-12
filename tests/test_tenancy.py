"""Cross-tenant isolation tests — the most important security tests."""
from django.urls import reverse

from apps.catalog.models import Product

from .base import TenantTestCase


class TenantIsolationTests(TenantTestCase):
    def login_a(self):
        self.client.force_login(self.owner_a)

    def test_product_list_only_shows_own_products(self):
        self.login_a()
        response = self.client.get(reverse("catalog:product_list"))
        self.assertContains(response, "Widget A")
        self.assertNotContains(response, "Widget B")

    def test_cross_tenant_product_detail_is_404(self):
        self.login_a()
        response = self.client.get(
            reverse("catalog:product_detail", args=[self.product_b.public_id])
        )
        self.assertEqual(response.status_code, 404)

    def test_cross_tenant_product_edit_is_404(self):
        self.login_a()
        response = self.client.post(
            reverse("catalog:product_edit", args=[self.product_b.public_id]),
            {"name": "Hacked"},
        )
        self.assertEqual(response.status_code, 404)
        self.product_b.refresh_from_db()
        self.assertEqual(self.product_b.name, "Widget B")

    def test_cross_tenant_customer_is_404(self):
        self.login_a()
        response = self.client.get(
            reverse("customers:detail", args=[self.walk_in_b.public_id])
        )
        self.assertEqual(response.status_code, 404)

    def test_cross_tenant_sale_is_404(self):
        self.allow_no_shift()
        sale = self.make_sale()
        self.client.force_login(self.owner_b)
        for name in ("sales:detail", "sales:invoice", "sales:receipt",
                     "sales:invoice_pdf"):
            response = self.client.get(reverse(name, args=[sale.public_id]))
            self.assertEqual(response.status_code, 404, name)

    def test_tenant_manager_filters(self):
        qs = Product.objects.for_business(self.business_a)
        self.assertEqual(list(qs.values_list("name", flat=True)), ["Widget A"])

    def test_pos_search_endpoints_are_tenant_scoped(self):
        self.login_a()
        response = self.client.get(reverse("sales:pos_barcode"),
                                   {"code": "WID-B"})
        self.assertFalse(response.json()["found"])
        response = self.client.get(reverse("sales:pos_products"), {"q": "Widget"})
        names = [i["name"] for i in response.json()["items"]]
        self.assertEqual(names, ["Widget A"])

    def test_report_data_is_tenant_scoped(self):
        self.allow_no_shift()
        self.make_sale()
        self.client.force_login(self.owner_b)
        response = self.client.get(reverse("reports:view", args=["sales_detailed"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["data"]["rows"]), 0)

    def test_export_is_tenant_scoped(self):
        self.allow_no_shift()
        self.make_sale()
        self.client.force_login(self.owner_b)
        response = self.client.get(
            reverse("reports:view", args=["sales_detailed"]) + "?export=csv"
        )
        content = response.content.decode()
        self.assertNotIn("Alpha", content)
        self.assertNotIn("Walk-In", content.split("\n")[1] if "\n" in content else "")

    def test_audit_log_is_tenant_scoped(self):
        self.login_a()
        response = self.client.get(reverse("audit:list"))
        for log in response.context["page_obj"]:
            self.assertEqual(log.business_id, self.business_a.id)

    def test_user_without_membership_redirected(self):
        from apps.accounts.models import User

        loner = User.objects.create_user(
            email="loner@example.com", password="StrongPass123!",
            full_name="No Business",
        )
        self.client.force_login(loner)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("no-business", response.url)

    def test_cashier_cannot_access_admin_pages(self):
        self.client.force_login(self.cashier_a)
        for name in ("accounts:user_list", "tenants:settings", "audit:list"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 403, name)

    def test_api_requires_plan_feature(self):
        self.login_a()
        response = self.client.get("/api/v1/products/")
        # Default starter plan has no API access
        self.assertEqual(response.status_code, 403)
