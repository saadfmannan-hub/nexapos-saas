"""Focused Phase 2D/E/F enforcement tests for reports, audit, and API."""

from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.audit import services as audit
from apps.audit.models import AuditLog
from apps.branches.models import Branch, Warehouse
from apps.catalog.models import Product
from apps.inventory.models import StockLevel
from apps.registers.models import CashRegister, Shift
from apps.reports.queries import REPORT_REQUIRED_MODULES, REPORTS
from apps.sales.models import Sale, SaleItem, SaleReturn, SaleReturnItem
from apps.subscriptions.models import Subscription

from .base import TenantTestCase


class ReportsAuditAPIEnforcementTests(TenantTestCase):
    def setUp(self):
        self.subscription = Subscription.objects.select_related("plan").get(
            business=self.business_a,
        )
        self.plan = self.subscription.plan
        self.client.force_login(self.owner_a)

    def set_features(self, **features):
        for field, value in features.items():
            setattr(self.plan, field, value)
        self.plan.save(update_fields=list(features))

    def test_every_registered_report_declares_modules(self):
        self.assertEqual(set(REPORT_REQUIRED_MODULES), set(REPORTS))

    def test_standard_and_advanced_reports_inherit_source_modules(self):
        self.set_features(
            feature_sales=True,
            feature_expenses=False,
            feature_advanced_reports=False,
        )

        self.assertEqual(
            self.client.get(reverse("reports:view", args=["sales_summary"])).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse("reports:view", args=["expenses"])).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("reports:view", args=["product_sales"])).status_code,
            403,
        )

        self.set_features(feature_advanced_reports=True)
        self.assertEqual(
            self.client.get(reverse("reports:view", args=["product_sales"])).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse("reports:view", args=["profit_loss"])).status_code,
            403,
        )

        self.set_features(feature_expenses=True)
        self.assertEqual(
            self.client.get(reverse("reports:view", args=["profit_loss"])).status_code,
            200,
        )

    def test_report_index_hides_unavailable_reports(self):
        self.set_features(
            feature_sales=True,
            feature_expenses=False,
            feature_advanced_reports=False,
            feature_customer_credit=False,
        )
        response = self.client.get(reverse("reports:index"))
        self.assertEqual(response.status_code, 200)
        keys = {
            item["key"]
            for group in response.context["groups"]
            for item in group["items"]
        }
        self.assertIn("sales_summary", keys)
        self.assertNotIn("expenses", keys)
        self.assertNotIn("receivables", keys)
        self.assertNotIn("product_sales", keys)

    def test_report_exports_use_the_same_module_intersection(self):
        self.set_features(
            feature_sales=True,
            feature_expenses=False,
            feature_advanced_reports=True,
        )
        url = reverse("reports:view", args=["expense_analysis"])
        for export in ("csv", "xlsx", "pdf"):
            with self.subTest(export=export):
                self.assertEqual(
                    self.client.get(f"{url}?export={export}").status_code,
                    403,
                )

        self.set_features(feature_expenses=True)
        for export in ("csv", "xlsx", "pdf"):
            with self.subTest(export=export):
                self.assertEqual(
                    self.client.get(f"{url}?export={export}").status_code,
                    200,
                )

    def test_branch_restricted_report_screens_and_exports_do_not_leak(self):
        hidden_branch = Branch.objects.create(
            business=self.business_a,
            name="Restricted Branch",
            code="RESTRICTED",
        )
        hidden_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=hidden_branch,
            name="Restricted Warehouse",
            code="RESTRICTED",
        )
        central_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=None,
            name="Central Shared Warehouse",
            code="CENTRAL-SHARED",
        )
        membership = self.membership_a()
        membership.branches.add(self.branch_a)

        allowed_product = Product.objects.create(
            business=self.business_a,
            name="Allowed Stock Marker",
            sku="ALLOWED-STOCK",
            purchase_price=Decimal("2.000"),
            sale_price=Decimal("4.000"),
            reorder_level=Decimal("5.000"),
        )
        hidden_product = Product.objects.create(
            business=self.business_a,
            name="Hidden Stock Marker",
            sku="HIDDEN-STOCK",
            purchase_price=Decimal("2.000"),
            sale_price=Decimal("4.000"),
            reorder_level=Decimal("5.000"),
        )
        central_product = Product.objects.create(
            business=self.business_a,
            name="Central Stock Marker",
            sku="CENTRAL-STOCK",
            purchase_price=Decimal("2.000"),
            sale_price=Decimal("4.000"),
            reorder_level=Decimal("5.000"),
        )
        for warehouse, product in (
            (self.warehouse_a, allowed_product),
            (hidden_warehouse, hidden_product),
            (central_warehouse, central_product),
        ):
            StockLevel.objects.create(
                business=self.business_a,
                warehouse=warehouse,
                product=product,
                quantity=Decimal("1.000"),
            )

        now = timezone.now()

        def create_return(branch, warehouse, marker, suffix):
            sale = Sale.objects.create(
                business=self.business_a,
                branch=branch,
                warehouse=warehouse,
                cashier=self.owner_a,
                customer=self.walk_in_a,
                invoice_number=f"SCOPE-{suffix}",
                status=Sale.Status.PART_RETURNED,
                sale_date=now,
                subtotal=Decimal("10.000"),
                total=Decimal("10.000"),
                amount_paid=Decimal("10.000"),
            )
            sale_item = SaleItem.objects.create(
                business=self.business_a,
                sale=sale,
                product=allowed_product,
                product_name=marker,
                sku=f"RETURN-{suffix}",
                quantity=Decimal("1.000"),
                unit_price=Decimal("10.000"),
                line_total=Decimal("10.000"),
                returned_quantity=Decimal("1.000"),
            )
            sale_return = SaleReturn.objects.create(
                business=self.business_a,
                return_number=f"RETURN-{suffix}",
                sale=sale,
                customer=self.walk_in_a,
                branch=branch,
                warehouse=warehouse,
                refund_method=SaleReturn.RefundMethod.CASH,
                refund_amount=Decimal("10.000"),
                processed_by=self.owner_a,
            )
            SaleReturnItem.objects.create(
                business=self.business_a,
                sale_return=sale_return,
                sale_item=sale_item,
                quantity=Decimal("1.000"),
                refund_per_unit=Decimal("10.000"),
                line_refund=Decimal("10.000"),
            )

        create_return(
            self.branch_a,
            central_warehouse,
            "Allowed Return Marker",
            "ALLOWED",
        )
        create_return(
            hidden_branch,
            hidden_warehouse,
            "Hidden Return Marker",
            "HIDDEN",
        )
        create_return(
            self.branch_a,
            hidden_warehouse,
            "Hidden Return Marker - Warehouse Mismatch",
            "HIDDEN-WAREHOUSE",
        )

        allowed_register = CashRegister.objects.create(
            business=self.business_a,
            branch=self.branch_a,
            name="Allowed Shift Marker",
            code="ALLOWED-SHIFT",
        )
        hidden_register = CashRegister.objects.create(
            business=self.business_a,
            branch=hidden_branch,
            name="Hidden Shift Marker",
            code="HIDDEN-SHIFT",
        )
        for register, branch in (
            (allowed_register, self.branch_a),
            (hidden_register, hidden_branch),
        ):
            Shift.objects.create(
                business=self.business_a,
                register=register,
                branch=branch,
                cashier=self.owner_a,
                status=Shift.Status.CLOSED,
                opened_at=now - timedelta(hours=1),
                closed_at=now,
                expected_cash=Decimal("10.000"),
                actual_cash=Decimal("10.000"),
            )

        report_markers = {
            "returns": ("Allowed Return Marker", "Hidden Return Marker"),
            "current_stock": ("Allowed Stock Marker", "Hidden Stock Marker"),
            "low_stock": ("Allowed Stock Marker", "Hidden Stock Marker"),
            "shifts": ("Allowed Shift Marker", "Hidden Shift Marker"),
        }
        for key, (allowed_marker, hidden_marker) in report_markers.items():
            with self.subTest(key=key, surface="screen"):
                response = self.client.get(reverse("reports:view", args=[key]))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, allowed_marker)
                self.assertNotContains(response, hidden_marker)
            with self.subTest(key=key, surface="csv"):
                response = self.client.get(
                    reverse("reports:view", args=[key]),
                    {"export": "csv"},
                )
                self.assertEqual(response.status_code, 200)
                csv_text = response.content.decode("utf-8")
                self.assertIn(allowed_marker, csv_text)
                self.assertNotIn(hidden_marker, csv_text)

        for key in ("current_stock", "low_stock"):
            with self.subTest(key=key, warehouse="central"):
                response = self.client.get(reverse("reports:view", args=[key]))
                self.assertNotContains(response, "Central Stock Marker")

    def test_fabric_history_requires_tailoring_and_inventory(self):
        self.set_features(
            feature_sales=True,
            feature_inventory=True,
            feature_tailoring_module=False,
        )
        url = reverse("reports:view", args=["fabric_history"])
        self.assertEqual(self.client.get(url).status_code, 403)

        self.set_features(feature_tailoring_module=True)
        self.assertEqual(self.client.get(url).status_code, 200)

        self.set_features(feature_inventory=False)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_detailed_sales_stays_pos_core_but_hides_add_on_outputs(self):
        tailoring_product = Product.objects.create(
            business=self.business_a,
            name="Detailed Tailoring Filter Marker",
            sku="DETAIL-TAILOR",
            sale_price=Decimal("12.000"),
            is_tailoring_item=True,
        )
        self.set_features(
            feature_sales=True,
            feature_tailoring_module=False,
            feature_customer_credit=False,
        )
        url = reverse("reports:view", args=["sales_detailed"])

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        columns = response.context["data"]["columns"]
        self.assertNotIn("Garment Classification", columns)
        self.assertNotIn("Legacy Workshop Actual", columns)
        self.assertNotIn("Balance", columns)
        self.assertNotContains(response, tailoring_product.name)

        csv_response = self.client.get(url, {"export": "csv"})
        self.assertEqual(csv_response.status_code, 200)
        csv_header = csv_response.content.decode("utf-8").splitlines()[0]
        self.assertNotIn("Garment Classification", csv_header)
        self.assertNotIn("Balance", csv_header)

        self.set_features(
            feature_tailoring_module=True,
            feature_customer_credit=True,
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        columns = response.context["data"]["columns"]
        self.assertIn("Garment Classification", columns)
        self.assertIn("Balance", columns)
        self.assertContains(response, tailoring_product.name)

    def test_audit_viewer_is_commercial_but_recording_continues(self):
        self.set_features(feature_audit_logs=False)
        before = AuditLog.objects.filter(business=self.business_a).count()
        audit.log(
            "phase2.audit.recorded",
            business=self.business_a,
            user=self.owner_a,
            module="tests",
            description="Recording remains active while viewer is disabled.",
        )
        self.assertEqual(
            AuditLog.objects.filter(business=self.business_a).count(),
            before + 1,
        )
        self.assertEqual(self.client.get(reverse("audit:list")).status_code, 403)

        self.set_features(feature_audit_logs=True)
        response = self.client.get(reverse("audit:list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "phase2.audit.recorded")

    def test_api_requires_explicit_context_api_access_and_pos_core(self):
        token = Token.objects.create(user=self.owner_a)
        api_client = APIClient()
        api_client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        url = reverse("api:product-list")
        headers = {"HTTP_X_BUSINESS_ID": str(self.business_a.public_id)}

        self.set_features(feature_sales=True, feature_api_access=True)
        self.assertEqual(api_client.get(url).status_code, 403)
        self.assertEqual(api_client.get(reverse("api:api-root")).status_code, 403)
        self.assertEqual(api_client.get(url, **headers).status_code, 200)
        self.assertEqual(
            api_client.get(reverse("api:api-root"), **headers).status_code,
            200,
        )

        self.set_features(feature_api_access=False)
        self.assertEqual(api_client.get(url, **headers).status_code, 403)

        self.set_features(feature_api_access=True, feature_sales=False)
        self.assertEqual(api_client.get(url, **headers).status_code, 403)

    def test_api_hides_module_owned_fields_without_blocking_core_resources(self):
        tailoring_product = Product.objects.create(
            business=self.business_a,
            name="API Tailoring Product",
            sku="API-TAILOR",
            sale_price="12.000",
            is_tailoring_item=True,
        )
        token = Token.objects.create(user=self.owner_a)
        api_client = APIClient()
        api_client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        headers = {"HTTP_X_BUSINESS_ID": str(self.business_a.public_id)}
        self.set_features(
            feature_sales=True,
            feature_inventory=True,
            feature_api_access=True,
            feature_tailoring_module=False,
            feature_customer_credit=False,
        )

        product_response = api_client.get(reverse("api:product-list"), **headers)
        self.assertEqual(product_response.status_code, 200)
        product_results = product_response.json()["results"]
        self.assertNotIn(
            str(tailoring_product.public_id),
            {item["public_id"] for item in product_results},
        )
        product = product_results[0]
        self.assertNotIn("is_tailoring_item", product)
        self.assertNotIn("estimated_adult_fabric", product)

        customer_response = api_client.get(reverse("api:customer-list"), **headers)
        self.assertEqual(customer_response.status_code, 200)
        customer = customer_response.json()["results"][0]
        self.assertNotIn("balance", customer)
        self.assertNotIn("store_credit", customer)

        self.set_features(
            feature_tailoring_module=True,
            feature_customer_credit=True,
        )
        product_results = api_client.get(
            reverse("api:product-list"), **headers
        ).json()["results"]
        self.assertIn(
            str(tailoring_product.public_id),
            {item["public_id"] for item in product_results},
        )
        product = product_results[0]
        customer = api_client.get(reverse("api:customer-list"), **headers).json()[
            "results"
        ][0]
        self.assertIn("is_tailoring_item", product)
        self.assertIn("balance", customer)

        self.membership_a().branches.add(self.branch_a)
        scoped_customer = api_client.get(
            reverse("api:customer-list"), **headers
        ).json()["results"][0]
        self.assertNotIn("balance", scoped_customer)
        self.assertNotIn("store_credit", scoped_customer)

    def test_api_cross_tenant_object_is_still_404(self):
        token = Token.objects.create(user=self.owner_a)
        api_client = APIClient()
        api_client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.set_features(feature_sales=True, feature_api_access=True)
        response = api_client.get(
            reverse("api:product-detail", args=[self.product_b.public_id]),
            HTTP_X_BUSINESS_ID=str(self.business_a.public_id),
        )
        self.assertEqual(response.status_code, 404)
