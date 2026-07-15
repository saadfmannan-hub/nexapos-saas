from datetime import UTC, date, datetime
from unittest.mock import patch

from django.http import QueryDict
from django.test import override_settings
from django.urls import reverse

from apps.core.date_ranges import (
    business_localdate,
    current_month_date_range,
    date_range_querystring,
    resolve_date_range,
)
from apps.reports.queries import REPORTS
from apps.sales.models import Sale
from apps.suppliers.models import Supplier

from .base import TenantTestCase


class GlobalDateFilterDefaultTests(TenantTestCase):
    today = date(2026, 7, 16)
    month_start = date(2026, 7, 1)

    def setUp(self):
        self.business_a.timezone = "Asia/Muscat"
        self.business_a.save(update_fields=["timezone", "updated_at"])
        self.now_patch = patch(
            "apps.core.date_ranges.timezone.now",
            return_value=datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
        )
        self.now_patch.start()
        self.addCleanup(self.now_patch.stop)
        self.allow_no_shift()
        self.client.force_login(self.owner_a)

    def assert_default_range(self, response, *, context_key=None):
        self.assertEqual(response.status_code, 200)
        context = response.context[context_key] if context_key else response.context
        self.assertEqual(str(context["date_from"]), self.month_start.isoformat())
        self.assertEqual(str(context["date_to"]), self.today.isoformat())

    def test_shared_default_is_current_business_month_through_local_today(self):
        self.assertEqual(business_localdate(self.business_a), self.today)
        self.assertEqual(
            current_month_date_range(self.business_a),
            (self.month_start, self.today),
        )
        self.assertEqual(
            resolve_date_range(QueryDict(""), self.business_a),
            (self.month_start.isoformat(), self.today.isoformat()),
        )

    def test_business_timezone_prevents_utc_date_drift(self):
        self.business_a.timezone = "Asia/Muscat"
        self.assertEqual(business_localdate(self.business_a), date(2026, 7, 16))
        self.assertEqual(current_month_date_range(self.business_a), (
            date(2026, 7, 1),
            date(2026, 7, 16),
        ))
        self.business_a.timezone = "UTC"
        self.assertEqual(business_localdate(self.business_a), date(2026, 7, 15))
        self.assertEqual(
            current_month_date_range(self.business_a),
            (date(2026, 7, 1), date(2026, 7, 15)),
        )

    def test_blank_or_invalid_business_timezone_uses_configured_then_utc(self):
        with override_settings(TIME_ZONE="Asia/Muscat"):
            for timezone_name in ("", "Not/A-Timezone"):
                with self.subTest(timezone_name=timezone_name):
                    self.business_a.timezone = timezone_name
                    self.assertEqual(
                        business_localdate(self.business_a),
                        date(2026, 7, 16),
                    )

        self.business_a.timezone = "Not/A-Timezone"
        with override_settings(TIME_ZONE="Also/Invalid"):
            self.assertEqual(
                business_localdate(self.business_a),
                date(2026, 7, 15),
            )

    def test_explicit_and_single_boundary_dates_are_preserved(self):
        explicit = QueryDict("from=2025-02-03&to=2025-02-19")
        self.assertEqual(
            resolve_date_range(explicit, self.business_a),
            ("2025-02-03", "2025-02-19"),
        )
        self.assertEqual(
            resolve_date_range(QueryDict("from=2025-02-03"), self.business_a),
            ("2025-02-03", self.today.isoformat()),
        )
        self.assertEqual(
            resolve_date_range(QueryDict("to=2025-02-19"), self.business_a),
            (self.month_start.isoformat(), "2025-02-19"),
        )

    def test_querystring_preserves_filters_and_removes_page_and_export(self):
        params = QueryDict(
            "q=needle&status=completed&branch=4&sort=-total&page=3&export=csv"
        )
        encoded = date_range_querystring(
            params,
            "2025-02-03",
            "2025-02-19",
        )
        preserved = QueryDict(encoded)
        self.assertEqual(preserved["from"], "2025-02-03")
        self.assertEqual(preserved["to"], "2025-02-19")
        self.assertEqual(preserved["q"], "needle")
        self.assertEqual(preserved["status"], "completed")
        self.assertEqual(preserved["branch"], "4")
        self.assertEqual(preserved["sort"], "-total")
        self.assertNotIn("page", preserved)
        self.assertNotIn("export", preserved)

    def test_history_and_ledger_screens_use_global_defaults(self):
        supplier = Supplier.objects.create(
            business=self.business_a,
            code="DATE-SUP",
            name="Date Filter Supplier",
        )
        urls = {
            "sales": reverse("sales:list"),
            "purchases": reverse("purchases:list"),
            "expenses": reverse("expenses:list"),
            "returns": reverse("sales:return_list"),
            "shifts": reverse("registers:shift_list"),
            "stock movements": reverse("inventory:movement_list"),
            "audit": reverse("audit:list"),
            "dashboard": reverse("dashboard"),
            "customer statement": reverse(
                "customers:statement",
                args=[self.walk_in_a.public_id],
            ),
            "supplier history": reverse(
                "suppliers:detail",
                args=[supplier.public_id],
            ),
        }
        for label, url in urls.items():
            with self.subTest(screen=label):
                self.assert_default_range(self.client.get(url))

    def test_all_report_screens_use_global_defaults(self):
        for key in REPORTS:
            with self.subTest(report=key):
                response = self.client.get(reverse("reports:view", args=[key]))
                self.assert_default_range(response, context_key="filters")

    def test_required_financial_reports_share_the_same_defaults(self):
        required = ("expenses", "expense_analysis", "profit", "profit_loss")
        for key in required:
            with self.subTest(report=key):
                response = self.client.get(reverse("reports:view", args=[key]))
                self.assert_default_range(response, context_key="filters")

    def test_rollover_boundary_applies_to_dashboard_and_required_reports(self):
        self.assert_default_range(self.client.get(reverse("dashboard")))
        for key in ("sales_summary", "expenses"):
            with self.subTest(report=key):
                response = self.client.get(reverse("reports:view", args=[key]))
                self.assert_default_range(response, context_key="filters")

    def test_views_preserve_explicit_dates_exactly(self):
        selected = {"from": "2025-02-03", "to": "2025-02-19"}
        sales_response = self.client.get(reverse("sales:list"), selected)
        self.assertEqual(sales_response.context["date_from"], selected["from"])
        self.assertEqual(sales_response.context["date_to"], selected["to"])

        report_response = self.client.get(
            reverse("reports:view", args=["profit_loss"]),
            selected,
        )
        self.assertEqual(report_response.context["filters"]["date_from"], selected["from"])
        self.assertEqual(report_response.context["filters"]["date_to"], selected["to"])

    def test_reset_and_clear_urls_reapply_dynamic_defaults(self):
        selected = {"from": "2025-02-03", "to": "2025-02-19"}
        report_url = reverse("reports:view", args=["expenses"])
        selected_response = self.client.get(report_url, selected)
        self.assertContains(selected_response, f'href="{report_url}"')
        self.assert_default_range(
            self.client.get(report_url),
            context_key="filters",
        )

        sales_url = reverse("sales:list")
        selected_response = self.client.get(sales_url, selected)
        self.assertContains(selected_response, f'href="{sales_url}"')
        self.assert_default_range(self.client.get(sales_url))

    def test_pagination_and_sorting_querystring_preserve_selected_dates(self):
        params = {
            "from": "2025-02-03",
            "to": "2025-02-19",
            "q": "needle",
            "status": Sale.Status.COMPLETED,
            "sort": "-total",
            "page": "3",
        }
        response = self.client.get(reverse("sales:list"), params)
        preserved = QueryDict(response.context["querystring"].rstrip("&"))
        self.assertEqual(preserved["from"], params["from"])
        self.assertEqual(preserved["to"], params["to"])
        self.assertEqual(preserved["q"], params["q"])
        self.assertEqual(preserved["status"], params["status"])
        self.assertEqual(preserved["sort"], params["sort"])
        self.assertNotIn("page", preserved)

    def test_report_export_links_and_print_keep_the_selected_range(self):
        sale = self.make_sale()
        params = {
            "from": self.month_start.isoformat(),
            "to": self.today.isoformat(),
            "branch": str(self.branch_a.pk),
        }
        url = reverse("reports:view", args=["sales_summary"])
        screen = self.client.get(url, params)
        active = QueryDict(screen.context["filter_querystring"])
        self.assertEqual(active["from"], params["from"])
        self.assertEqual(active["to"], params["to"])
        self.assertEqual(active["branch"], params["branch"])
        self.assertContains(screen, "export=csv")
        self.assertContains(screen, "export=xlsx")
        self.assertContains(screen, "export=pdf")
        self.assertContains(screen, 'onclick="window.print()"')

        csv_response = self.client.get(url, {**params, "export": "csv"})
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("text/csv", csv_response["Content-Type"])
        self.assertIn(sale.invoice_number, csv_response.content.decode("utf-8-sig"))

        xlsx_response = self.client.get(url, {**params, "export": "xlsx"})
        self.assertEqual(xlsx_response.status_code, 200)
        self.assertIn("spreadsheetml", xlsx_response["Content-Type"])

        pdf_response = self.client.get(url, {**params, "export": "pdf"})
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response["Content-Type"], "application/pdf")

    def test_customer_statement_exports_and_print_keep_selected_dates(self):
        params = {
            "from": "2025-02-03",
            "to": "2025-02-19",
            "branch": str(self.branch_a.pk),
        }
        response = self.client.get(
            reverse("customers:statement", args=[self.walk_in_a.public_id]),
            params,
        )
        active = QueryDict(response.context["filter_querystring"])
        self.assertEqual(active["from"], params["from"])
        self.assertEqual(active["to"], params["to"])
        self.assertEqual(active["branch"], params["branch"])
        self.assertContains(response, "export=csv")
        self.assertContains(response, "export=pdf")
        self.assertContains(response, 'onclick="window.print()"')

    def test_default_filter_is_applied_to_results_and_existing_search_still_works(self):
        current_sale = self.make_sale()
        previous_sale = self.make_sale()
        Sale.objects.filter(pk=previous_sale.pk).update(
            sale_date=datetime(2026, 6, 20, 12, tzinfo=UTC),
        )

        response = self.client.get(reverse("sales:list"))
        self.assertContains(response, current_sale.invoice_number)
        self.assertNotContains(response, previous_sale.invoice_number)

        response = self.client.get(reverse("sales:list"), {"q": "not-found"})
        self.assertEqual(response.context["q"], "not-found")
        self.assertEqual(response.context["page_obj"].paginator.count, 0)
        self.assert_default_range(response)

    def test_single_transaction_and_recurring_template_dates_are_unchanged(self):
        expense_response = self.client.get(reverse("expenses:create"))
        self.assertEqual(expense_response.status_code, 200)
        self.assertIsNone(expense_response.context["form"]["expense_date"].value())

        recurring_response = self.client.get(reverse("expenses:recurring_create"))
        self.assertEqual(recurring_response.status_code, 200)
        recurring_form = recurring_response.context["form"]
        self.assertIsNone(recurring_form["start_date"].value())
        self.assertIsNone(recurring_form["end_date"].value())

        purchase_response = self.client.get(reverse("purchases:create"))
        self.assertEqual(purchase_response.status_code, 200)
        self.assertEqual(purchase_response.context["today"], date(2026, 7, 15))
        self.assertContains(purchase_response, 'name="purchase_date"')
