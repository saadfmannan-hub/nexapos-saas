from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.purchases.models import Purchase
from apps.suppliers.models import Supplier

from .base import TenantTestCase

NAV_MARKER = "data-global-back-navigation"


class GlobalBackNavigationTests(TenantTestCase):
    def setUp(self):
        self.client.force_login(self.owner_a)
        self.supplier = Supplier.objects.create(
            business=self.business_a,
            code="NAV-SUP",
            name="Navigation Supplier",
        )
        self.purchase = Purchase.objects.create(
            business=self.business_a,
            purchase_number="NAV-PO",
            supplier=self.supplier,
            branch=self.branch_a,
            warehouse=self.warehouse_a,
            purchase_date=timezone.localdate(),
            created_by=self.owner_a,
        )

    def source(self, relative_path):
        return Path(settings.BASE_DIR, relative_path).read_text(encoding="utf-8")

    def navigation_html(self, response):
        html = response.content.decode()
        start = html.index('<nav class="global-back-nav')
        return html[start : html.index("</nav>", start) + len("</nav>")]

    def assert_back_disabled(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, NAV_MARKER, count=1)
        navigation = self.navigation_html(response)
        self.assertIn("disabled", navigation)
        self.assertIn('aria-disabled="true"', navigation)
        self.assertNotIn(" action=", navigation)
        self.assertFalse(response.context["back_enabled"])
        self.assertEqual(response.context["back_url"], "")

    def assert_back_to(self, response, parent_view_name):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, NAV_MARKER, count=1)
        parent_url = reverse(parent_view_name)
        navigation = self.navigation_html(response)
        self.assertNotIn(" disabled", navigation)
        self.assertIn('aria-disabled="false"', navigation)
        self.assertIn(f'action="{parent_url}"', navigation)
        self.assertTrue(response.context["back_enabled"])
        self.assertEqual(response.context["back_url"], parent_url)

    def test_01_next_button_is_completely_removed(self):
        component = self.source("templates/components/back_navigation.html")
        response = self.client.get(reverse("dashboard"))
        self.assertNotIn("Next", component)
        self.assertNotContains(response, ">Next<", html=False)
        self.assertNotContains(response, 'aria-label="Next"')

    def test_02_reports_center_shows_back_disabled(self):
        self.assert_back_disabled(self.client.get(reverse("reports:index")))

    def test_03_individual_report_returns_to_reports_center(self):
        response = self.client.get(reverse("reports:view", args=["sales_summary"]))
        self.assert_back_to(response, "reports:index")

    def test_04_products_list_shows_back_disabled(self):
        self.assert_back_disabled(self.client.get(reverse("catalog:product_list")))

    def test_05_product_edit_returns_to_products_list(self):
        response = self.client.get(
            reverse("catalog:product_edit", args=[self.product_a.public_id])
        )
        self.assert_back_to(response, "catalog:product_list")

    def test_06_customers_list_shows_back_disabled(self):
        self.assert_back_disabled(self.client.get(reverse("customers:list")))

    def test_07_customer_detail_returns_to_customers_list(self):
        response = self.client.get(
            reverse("customers:detail", args=[self.walk_in_a.public_id])
        )
        self.assert_back_to(response, "customers:list")

    def test_08_suppliers_list_shows_back_disabled(self):
        self.assert_back_disabled(self.client.get(reverse("suppliers:list")))

    def test_09_supplier_detail_returns_to_suppliers_list(self):
        response = self.client.get(
            reverse("suppliers:detail", args=[self.supplier.public_id])
        )
        self.assert_back_to(response, "suppliers:list")

    def test_10_purchases_list_shows_back_disabled(self):
        self.assert_back_disabled(self.client.get(reverse("purchases:list")))

    def test_11_purchase_detail_returns_to_purchases_list(self):
        response = self.client.get(
            reverse("purchases:detail", args=[self.purchase.public_id])
        )
        self.assert_back_to(response, "purchases:list")

    def test_12_sales_list_shows_back_disabled(self):
        self.assert_back_disabled(self.client.get(reverse("sales:list")))

    def test_13_sale_detail_returns_to_sales_list(self):
        self.allow_no_shift()
        sale = self.make_sale()
        response = self.client.get(reverse("sales:detail", args=[sale.public_id]))
        self.assert_back_to(response, "sales:list")

    def test_14_returns_list_shows_back_disabled(self):
        self.assert_back_disabled(self.client.get(reverse("sales:return_list")))

    def test_15_return_form_returns_to_returns_list(self):
        self.allow_no_shift()
        sale = self.make_sale()
        response = self.client.get(
            reverse("sales:return_create", args=[sale.public_id])
        )
        self.assert_back_to(response, "sales:return_list")

    def test_16_expenses_list_shows_back_disabled(self):
        self.assert_back_disabled(self.client.get(reverse("expenses:list")))

    def test_17_expense_form_returns_to_expenses_list(self):
        self.assert_back_to(
            self.client.get(reverse("expenses:create")),
            "expenses:list",
        )

    def test_18_registers_and_shifts_main_shows_back_disabled(self):
        self.assert_back_disabled(self.client.get(reverse("registers:shift_list")))

    def test_19_register_subpage_returns_to_registers_and_shifts(self):
        response = self.client.get(
            reverse("registers:register_edit", args=[self.register_a.public_id])
        )
        self.assert_back_to(response, "registers:shift_list")

    def test_20_branches_and_warehouses_main_shows_back_disabled(self):
        self.assert_back_disabled(self.client.get(reverse("branches:list")))

    def test_21_branch_subpage_returns_to_branches_and_warehouses(self):
        response = self.client.get(
            reverse("branches:branch_edit", args=[self.branch_a.public_id])
        )
        self.assert_back_to(response, "branches:list")

    def test_22_pos_sell_shows_back_disabled(self):
        self.allow_no_shift()
        response = self.client.get(reverse("sales:pos"))
        self.assert_back_disabled(response)
        self.assertContains(response, "PAY <span", html=False)

    def test_23_no_browser_history_javascript_remains(self):
        self.assertFalse(
            Path(settings.BASE_DIR, "static/js/history-navigation.js").exists()
        )
        sources = "\n".join(
            (
                self.source("templates/layouts/base.html"),
                self.source("templates/platformadmin/_base.html"),
                self.source("templates/components/back_navigation.html"),
                self.source("apps/core/context_processors.py"),
            )
        )
        for forbidden in (
            "window.history",
            "document.referrer",
            "sessionStorage",
            "localStorage",
            "data-history-direction",
        ):
            self.assertNotIn(forbidden, sources)

    def test_24_disabled_back_cannot_navigate(self):
        for view_name in (
            "dashboard",
            "inventory:stock_list",
            "catalog:category_list",
        ):
            with self.subTest(view_name=view_name):
                response = self.client.get(reverse(view_name))
                self.assert_back_disabled(response)
                navigation = self.navigation_html(response)
                self.assertIn('type="submit"', navigation)
        self.assertIn("pointer-events: none", self.source("static/css/app.css"))

    def test_25_login_print_and_export_pages_do_not_show_back(self):
        self.allow_no_shift()
        sale = self.make_sale()
        invoice = self.client.get(reverse("sales:invoice", args=[sale.public_id]))
        export = self.client.get(reverse("catalog:product_export"))
        self.assertNotContains(invoice, NAV_MARKER)
        self.assertNotIn(NAV_MARKER.encode(), export.content)

        self.client.logout()
        login = self.client.get(reverse("accounts:login"))
        self.assertNotContains(login, NAV_MARKER)

    def test_26_navigation_feature_requires_no_migration(self):
        output = StringIO()
        call_command(
            "makemigrations",
            check=True,
            dry_run=True,
            stdout=output,
            verbosity=1,
        )
        self.assertIn("No changes detected", output.getvalue())
