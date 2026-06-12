"""Export and report tests."""
from decimal import Decimal

from django.urls import reverse

from .base import TenantTestCase

D = Decimal


class ExportTests(TenantTestCase):
    def setUp(self):
        self.allow_no_shift()
        self.sale = self.make_sale()
        self.client.force_login(self.owner_a)

    def test_csv_export(self):
        response = self.client.get(
            reverse("reports:view", args=["sales_detailed"]) + "?export=csv"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn(self.sale.invoice_number, response.content.decode())

    def test_xlsx_export(self):
        response = self.client.get(
            reverse("reports:view", args=["sales_summary"]) + "?export=xlsx"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response["Content-Type"])
        self.assertTrue(response.content.startswith(b"PK"))  # zip magic

    def test_pdf_export(self):
        response = self.client.get(
            reverse("reports:view", args=["sales_summary"]) + "?export=pdf"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_export_respects_date_filter(self):
        response = self.client.get(
            reverse("reports:view", args=["sales_detailed"])
            + "?from=1990-01-01&to=1990-01-02&export=csv"
        )
        self.assertNotIn(self.sale.invoice_number, response.content.decode())

    def test_export_totals_match_screen(self):
        screen = self.client.get(reverse("reports:view", args=["sales_summary"]))
        csv_resp = self.client.get(
            reverse("reports:view", args=["sales_summary"]) + "?export=csv"
        )
        totals = screen.context["data"]["totals"]
        self.assertIn(str(totals[2]), csv_resp.content.decode())

    def test_export_requires_permission(self):
        from apps.accounts.models import Membership, Role, User

        viewer = User.objects.create_user(email="noexport@example.com",
                                          password="x" * 10, full_name="NoExp")
        role = Role.objects.for_business(self.business_a).get(
            name="Read-Only Viewer")
        Membership.objects.create(business=self.business_a, user=viewer, role=role)
        self.client.force_login(viewer)
        response = self.client.get(
            reverse("reports:view", args=["sales_detailed"]) + "?export=csv"
        )
        self.assertEqual(response.status_code, 403)

    def test_dashboard_renders_with_real_data(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["kpis"]["invoices"], 1)
        self.assertEqual(response.context["kpis"]["period_sales"], D("21.000"))
