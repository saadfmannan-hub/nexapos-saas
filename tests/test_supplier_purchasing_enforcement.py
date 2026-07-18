"""Focused Phase 2C enforcement tests for Suppliers and Purchasing."""

import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, Role, User
from apps.branches.models import Branch, Warehouse
from apps.catalog.forms import QuickProductForm
from apps.catalog.models import Unit
from apps.inventory import services as inventory_services
from apps.inventory.models import StockMovement
from apps.purchases import services as purchase_services
from apps.purchases.models import Purchase, PurchaseItem, PurchaseReturnItem
from apps.sales.models import PaymentMethod
from apps.subscriptions import services as subscription_services
from apps.subscriptions.exceptions import DenialCode, ModuleAccessDenied
from apps.subscriptions.models import Plan, Subscription
from apps.suppliers import services as supplier_services
from apps.suppliers.models import Supplier, SupplierPayment

from .base import TenantTestCase

D = Decimal


class SupplierPurchasingEnforcementTests(TenantTestCase):
    password = "StrongPass123!"

    def setUp(self):
        self.subscription = Subscription.objects.select_related("plan").get(
            business=self.business_a
        )
        self.owner_membership = Membership.objects.get(
            business=self.business_a,
            user=self.owner_a,
        )
        self.supplier = Supplier.objects.create(
            business=self.business_a,
            code="PH2C-SUP",
            name="Phase 2C Supplier",
            email="phase2c-supplier@example.com",
        )
        self.foreign_supplier = Supplier.objects.create(
            business=self.business_b,
            code="PH2C-FOREIGN",
            name="Foreign Supplier",
        )
        self.cash_b = PaymentMethod.objects.for_business(self.business_b).get(
            kind=PaymentMethod.Kind.CASH
        )
        self.purchase = self.create_purchase()

    def set_plan(self, **fields):
        Plan.objects.filter(pk=self.subscription.plan_id).update(**fields)

    def set_modules(
        self,
        *,
        suppliers=True,
        purchases=True,
        inventory=True,
        pos_core=True,
    ):
        self.set_plan(
            feature_sales=pos_core,
            feature_inventory=inventory,
            feature_suppliers=suppliers,
            feature_purchases=purchases,
        )

    def set_subscription_status(self, status):
        Subscription.objects.filter(business=self.business_a).update(
            status=status,
            trial_ends_at=None,
            current_period_end=timezone.now() + timedelta(days=30),
        )

    def make_staff(self, permissions, *, suffix, branches=None):
        role = Role.objects.create(
            business=self.business_a,
            name=f"Phase 2C {suffix}",
            permissions=list(permissions),
        )
        user = User.objects.create_user(
            email=f"phase2c-{suffix}@example.com",
            password=self.password,
            full_name=f"Phase 2C {suffix}",
        )
        membership = Membership.objects.create(
            business=self.business_a,
            user=user,
            role=role,
        )
        if branches is not None:
            membership.branches.set(branches)
        return user, membership

    def create_purchase(
        self,
        *,
        supplier=None,
        branch=None,
        warehouse=None,
        product=None,
        user=None,
        membership=None,
    ):
        return purchase_services.create_purchase(
            business=self.business_a,
            supplier=supplier or self.supplier,
            branch=branch or self.branch_a,
            warehouse=warehouse or self.warehouse_a,
            rows=[
                {
                    "product": product or self.product_a,
                    "variant": None,
                    "quantity": D("5"),
                    "unit_cost": D("4"),
                }
            ],
            user=user or self.owner_a,
            membership=membership,
            purchase_date="2026-07-18",
        )

    def assert_service_denied(self, callback, code):
        with self.assertRaises(ModuleAccessDenied) as caught:
            callback()
        self.assertEqual(caught.exception.denial.code, code)

    @staticmethod
    def supplier_values(**overrides):
        values = {
            "name": "Guarded Supplier",
            "code": "GUARDED-SUP",
            "contact_person": "Pat",
            "mobile": "90000000",
            "email": "guarded@example.com",
            "address": "Muscat",
            "tax_number": "VAT-1",
            "payment_terms": "30 days",
            "notes": "Phase 2C",
            "is_active": True,
        }
        values.update(overrides)
        return values

    def test_every_authenticated_supplier_and_purchase_route_is_centrally_guarded(self):
        from apps.purchases.urls import urlpatterns as purchase_patterns
        from apps.suppliers.urls import urlpatterns as supplier_patterns

        self.assertEqual(len(supplier_patterns), 4)
        for pattern in supplier_patterns:
            with self.subTest(route=f"suppliers:{pattern.name}"):
                self.assertTrue(getattr(pattern.callback, "_subscription_module_guarded", False))

        self.assertEqual(len(purchase_patterns), 14)
        for pattern in purchase_patterns:
            with self.subTest(route=f"purchases:{pattern.name}"):
                if pattern.name == "shared":
                    self.assertFalse(
                        getattr(pattern.callback, "_subscription_module_guarded", False)
                    )
                else:
                    self.assertTrue(
                        getattr(pattern.callback, "_subscription_module_guarded", False)
                    )

    def test_enabled_owner_can_open_supplier_and_purchase_reads(self):
        self.set_modules()
        self.client.force_login(self.owner_a)

        for url in (
            reverse("suppliers:list"),
            reverse("suppliers:detail", args=[self.supplier.public_id]),
            reverse("purchases:list"),
            reverse("purchases:detail", args=[self.purchase.public_id]),
            reverse("purchases:print", args=[self.purchase.public_id]),
            reverse("purchases:pdf", args=[self.purchase.public_id]),
            reverse("purchases:share", args=[self.purchase.public_id]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_disabled_suppliers_denies_owner_and_permitted_staff(self):
        staff, _membership = self.make_staff(
            {"suppliers.view", "suppliers.manage"},
            suffix="supplier-disabled",
        )
        self.set_modules(suppliers=False)
        for user in (self.owner_a, staff):
            self.client.force_login(user)
            for url in (
                reverse("suppliers:list"),
                reverse("suppliers:create"),
                reverse("suppliers:detail", args=[self.supplier.public_id]),
                reverse("suppliers:edit", args=[self.supplier.public_id]),
            ):
                with self.subTest(user=user.email, url=url):
                    self.assertEqual(self.client.get(url).status_code, 403)

    def test_supplier_permission_remains_required(self):
        staff, _membership = self.make_staff(set(), suffix="supplier-no-perm")
        self.set_modules()
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse("suppliers:list")).status_code, 403)

    def test_read_only_allows_supplier_history_but_denies_create_and_edit_forms(self):
        self.set_modules()
        self.set_subscription_status(Subscription.Status.PAST_DUE)
        self.client.force_login(self.owner_a)

        self.assertEqual(self.client.get(reverse("suppliers:list")).status_code, 200)
        self.assertEqual(
            self.client.get(
                reverse("suppliers:detail", args=[self.supplier.public_id])
            ).status_code,
            200,
        )
        self.assertEqual(self.client.get(reverse("suppliers:create")).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("suppliers:edit", args=[self.supplier.public_id])).status_code,
            403,
        )

    def test_foreign_supplier_routes_remain_404(self):
        self.set_modules()
        self.client.force_login(self.owner_a)
        for name in ("suppliers:detail", "suppliers:edit"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.client.get(
                        reverse(name, args=[self.foreign_supplier.public_id])
                    ).status_code,
                    404,
                )

    def test_supplier_service_cannot_bypass_disabled_module_or_foreign_tenant(self):
        self.set_modules(suppliers=False)
        before = Supplier.objects.for_business(self.business_a).count()
        self.assert_service_denied(
            lambda: supplier_services.save_supplier(
                business=self.business_a,
                values=self.supplier_values(),
                user=self.owner_a,
            ),
            DenialCode.MODULE_DISABLED,
        )
        self.assertEqual(Supplier.objects.for_business(self.business_a).count(), before)

        self.set_modules()
        with self.assertRaises(Http404):
            supplier_services.save_supplier(
                business=self.business_a,
                supplier=self.foreign_supplier,
                values=self.supplier_values(),
                user=self.owner_a,
            )

    def test_supplier_edit_is_guarded_archive_and_reactivate(self):
        self.set_modules()
        self.client.force_login(self.owner_a)
        url = reverse("suppliers:edit", args=[self.supplier.public_id])
        archived = self.supplier_values(
            name=self.supplier.name,
            code=self.supplier.code,
            is_active=False,
        )
        self.assertEqual(self.client.post(url, archived).status_code, 302)
        self.supplier.refresh_from_db()
        self.assertFalse(self.supplier.is_active)

        reactivated = dict(archived, is_active=True)
        self.assertEqual(self.client.post(url, reactivated).status_code, 302)
        self.supplier.refresh_from_db()
        self.assertTrue(self.supplier.is_active)

    def test_supplier_limit_applies_when_reactivating_not_creating_inactive(self):
        self.set_plan(max_suppliers=1)
        inactive = supplier_services.save_supplier(
            business=self.business_a,
            values=self.supplier_values(
                name="Inactive over-limit supplier",
                code="INACTIVE-LIMIT",
                is_active=False,
            ),
            user=self.owner_a,
        )
        self.assertFalse(inactive.is_active)

        with self.assertRaises(subscription_services.LimitExceeded):
            supplier_services.save_supplier(
                business=self.business_a,
                supplier=inactive,
                values=self.supplier_values(
                    name=inactive.name,
                    code=inactive.code,
                    is_active=True,
                ),
                user=self.owner_a,
            )
        inactive.refresh_from_db()
        self.assertFalse(inactive.is_active)

    def test_supplier_detail_hides_purchasing_data_when_purchases_is_ineffective(self):
        self.set_modules(purchases=False)
        self.client.force_login(self.owner_a)
        response = self.client.get(reverse("suppliers:detail", args=[self.supplier.public_id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Outstanding payable")
        self.assertNotContains(response, "Total purchases")
        self.assertNotContains(response, self.purchase.purchase_number)
        self.assertFalse(response.context["purchases_access"])

    def test_supplier_detail_scopes_embedded_purchases_to_membership_branch(self):
        branch = Branch.objects.create(
            business=self.business_a,
            name="Other Purchase Branch",
            code="PH2C-B2",
        )
        warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=branch,
            name="Other Purchase Warehouse",
            code="PH2C-W2",
        )
        other_purchase = self.create_purchase(branch=branch, warehouse=warehouse)
        Supplier.objects.filter(pk=self.supplier.pk).update(balance=D("9876.543"))
        staff, _membership = self.make_staff(
            {"suppliers.view", "purchases.view"},
            suffix="supplier-branch",
            branches=[self.branch_a],
        )
        self.client.force_login(staff)

        response = self.client.get(reverse("suppliers:detail", args=[self.supplier.public_id]))
        self.assertContains(response, self.purchase.purchase_number)
        self.assertNotContains(response, other_purchase.purchase_number)
        self.assertFalse(response.context["show_business_balance"])
        self.assertContains(response, "Business-wide balance hidden")

        list_response = self.client.get(reverse("suppliers:list"))
        self.assertFalse(list_response.context["show_business_balance"])
        self.assertContains(list_response, "Business-wide balance hidden")
        self.assertNotContains(list_response, "9,876")

    def test_purchase_dependencies_fail_closed_for_owner(self):
        combinations = (
            {"purchases": False},
            {"inventory": False},
            {"suppliers": False},
            {"pos_core": False},
        )
        self.client.force_login(self.owner_a)
        for combination in combinations:
            self.set_modules()
            self.set_modules(**combination)
            with self.subTest(combination=combination):
                self.assertEqual(
                    self.client.get(reverse("purchases:list")).status_code,
                    403,
                )

    def test_purchase_permission_remains_required(self):
        staff, _membership = self.make_staff(set(), suffix="purchase-no-perm")
        self.set_modules()
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse("purchases:list")).status_code, 403)

    def test_grace_subscription_allows_purchase_write(self):
        self.set_modules()
        self.set_subscription_status(Subscription.Status.GRACE)
        before = Purchase.objects.for_business(self.business_a).count()
        created = self.create_purchase()
        self.assertIsNotNone(created.pk)
        self.assertEqual(Purchase.objects.for_business(self.business_a).count(), before + 1)

    def test_read_only_allows_safe_purchase_outputs_and_hides_write_actions(self):
        self.set_modules()
        self.set_subscription_status(Subscription.Status.PAST_DUE)
        self.client.force_login(self.owner_a)

        for name in ("list", "detail", "print", "pdf", "share"):
            args = [] if name == "list" else [self.purchase.public_id]
            with self.subTest(name=name):
                self.assertEqual(
                    self.client.get(reverse(f"purchases:{name}", args=args)).status_code,
                    200,
                )
        detail = self.client.get(reverse("purchases:detail", args=[self.purchase.public_id]))
        self.assertFalse(detail.context["can_manage"])
        self.assertFalse(detail.context["can_email"])
        self.assertNotContains(detail, "Receive goods")
        self.assertNotContains(
            detail,
            reverse("purchases:receive", args=[self.purchase.public_id]),
        )
        self.assertNotContains(detail, "Add supplier payments")
        self.assertNotContains(detail, "Email purchase order")

        self.assertEqual(self.client.get(reverse("purchases:create")).status_code, 403)
        for name in ("receive", "pay", "return", "cancel", "email"):
            self.assertEqual(
                self.client.post(
                    reverse(f"purchases:{name}", args=[self.purchase.public_id]),
                    {},
                ).status_code,
                403,
            )
        self.assertEqual(
            self.client.post(
                reverse("purchases:share", args=[self.purchase.public_id]),
                {},
            ).status_code,
            403,
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_suspended_subscription_denies_supplier_and_purchase_history(self):
        self.set_modules()
        self.set_subscription_status(Subscription.Status.SUSPENDED)
        self.client.force_login(self.owner_a)
        self.assertEqual(self.client.get(reverse("suppliers:list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("purchases:list")).status_code, 403)

    def test_create_purchase_service_denies_before_mutation(self):
        self.set_modules(purchases=False)
        before = Purchase.objects.for_business(self.business_a).count()
        self.assert_service_denied(
            lambda: self.create_purchase(),
            DenialCode.MODULE_DISABLED,
        )
        self.assertEqual(Purchase.objects.for_business(self.business_a).count(), before)

    def test_receive_service_denies_when_purchase_or_inventory_is_disabled(self):
        item = self.purchase.items.get()
        before_stock = inventory_services.get_stock(
            self.business_a,
            self.warehouse_a,
            self.product_a,
        )
        for fields, code in (
            ({"purchases": False}, DenialCode.MODULE_DISABLED),
            ({"inventory": False}, DenialCode.MODULE_DEPENDENCY_MISSING),
        ):
            self.set_modules()
            self.set_modules(**fields)
            with self.subTest(fields=fields):
                self.assert_service_denied(
                    lambda: purchase_services.receive_purchase(
                        purchase=self.purchase,
                        quantities={item.pk: D("1")},
                        user=self.owner_a,
                    ),
                    code,
                )
        self.assertEqual(
            inventory_services.get_stock(
                self.business_a,
                self.warehouse_a,
                self.product_a,
            ),
            before_stock,
        )

    def test_direct_services_reload_inactive_business_state(self):
        self.business_a.__class__.objects.filter(pk=self.business_a.pk).update(is_active=False)
        supplier_count = Supplier.objects.for_business(self.business_a).count()
        purchase_count = Purchase.objects.for_business(self.business_a).count()

        self.assert_service_denied(
            lambda: supplier_services.save_supplier(
                business=self.business_a,
                values=self.supplier_values(code="STALE-BUSINESS"),
                user=self.owner_a,
            ),
            DenialCode.BUSINESS_INACTIVE,
        )
        self.assert_service_denied(
            lambda: self.create_purchase(),
            DenialCode.BUSINESS_INACTIVE,
        )
        self.assertEqual(Supplier.objects.for_business(self.business_a).count(), supplier_count)
        self.assertEqual(Purchase.objects.for_business(self.business_a).count(), purchase_count)

    def test_receive_service_enforces_branch_and_warehouse_scope(self):
        branch = Branch.objects.create(
            business=self.business_a,
            name="Restricted Purchase Branch",
            code="PH2C-RB",
        )
        warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=branch,
            name="Restricted Purchase Warehouse",
            code="PH2C-RW",
        )
        restricted_purchase = self.create_purchase(branch=branch, warehouse=warehouse)
        item = restricted_purchase.items.get()
        staff, membership = self.make_staff(
            {"purchases.view", "purchases.manage"},
            suffix="purchase-scope",
            branches=[self.branch_a],
        )

        with self.assertRaises(Http404):
            purchase_services.receive_purchase(
                purchase=restricted_purchase,
                quantities={item.pk: D("1")},
                user=staff,
                membership=membership,
            )
        item.refresh_from_db()
        self.assertEqual(item.quantity_received, D("0"))

    def test_create_purchase_rejects_foreign_supplier_product_and_payment_method(self):
        self.set_modules()
        with self.assertRaises(Http404):
            self.create_purchase(supplier=self.foreign_supplier)
        with self.assertRaises(Http404):
            self.create_purchase(product=self.product_b)
        with self.assertRaises(Http404):
            purchase_services.pay_purchase(
                purchase=self.purchase,
                amount=D("1"),
                method=self.cash_b,
                user=self.owner_a,
            )

    def test_foreign_purchase_and_cheque_service_objects_are_404(self):
        foreign_purchase = purchase_services.create_purchase(
            business=self.business_b,
            supplier=self.foreign_supplier,
            branch=self.branch_b,
            warehouse=self.warehouse_b,
            rows=[
                {
                    "product": self.product_b,
                    "variant": None,
                    "quantity": D("2"),
                    "unit_cost": D("3"),
                }
            ],
            user=self.owner_b,
            purchase_date="2026-07-18",
        )
        foreign_item = foreign_purchase.items.get()
        with self.assertRaises(Http404):
            purchase_services.receive_purchase(
                purchase=foreign_purchase,
                quantities={foreign_item.pk: D("1")},
                user=self.owner_a,
            )

        foreign_payment = purchase_services.record_purchase_payments(
            purchase=foreign_purchase,
            rows=[
                {
                    "method": SupplierPayment.Method.CHEQUE,
                    "amount": D("1"),
                    "cheque_number": "FOREIGN-CHQ",
                    "bank_name": "Foreign Bank",
                    "cheque_issue_date": timezone.localdate(),
                    "due_date": timezone.localdate() + timedelta(days=2),
                }
            ],
            user=self.owner_b,
        )[0]
        with self.assertRaises(Http404):
            purchase_services.update_cheque_status(
                payment=foreign_payment,
                status=SupplierPayment.ChequeStatus.CLEARED,
                user=self.owner_a,
            )

    def test_purchase_reads_reject_inconsistent_parent_and_child_tenants(self):
        self.client.force_login(self.owner_a)
        share_url = self.client.get(
            reverse("purchases:share", args=[self.purchase.public_id])
        ).context["share_url"]
        PurchaseItem.objects.filter(purchase=self.purchase).update(business=self.business_b)
        corrupt_item = self.purchase.items.get()
        with self.assertRaises(Http404):
            purchase_services.receive_purchase(
                purchase=self.purchase,
                quantities={corrupt_item.pk: D("1")},
                user=self.owner_a,
            )
        with self.assertRaises(Http404):
            purchase_services.cancel_purchase(
                purchase=self.purchase,
                user=self.owner_a,
            )
        for name in ("detail", "print", "pdf"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.client.get(
                        reverse(f"purchases:{name}", args=[self.purchase.public_id])
                    ).status_code,
                    404,
                )
        self.client.logout()
        self.assertEqual(self.client.get(share_url).status_code, 404)

        PurchaseItem.objects.filter(purchase=self.purchase).update(business=self.business_a)
        Purchase.objects.filter(pk=self.purchase.pk).update(supplier=self.foreign_supplier)
        self.client.force_login(self.owner_a)
        self.assertEqual(
            self.client.get(
                reverse("purchases:detail", args=[self.purchase.public_id])
            ).status_code,
            404,
        )
        self.client.logout()
        self.assertEqual(self.client.get(share_url).status_code, 404)

    def test_quick_add_reloads_form_foreign_keys_canonically(self):
        local_unit = Unit.objects.for_business(self.business_a).filter(is_active=True).first()
        foreign_unit = Unit.objects.for_business(self.business_b).filter(is_active=True).first()
        form = QuickProductForm(
            self.business_a,
            data={
                "name": "Guarded quick item",
                "sku": "GUARDED-QUICK",
                "category": "",
                "unit": local_unit.pk,
                "purchase_price": "1.000",
                "sale_price": "2.000",
                "tax_rate": "",
                "price_includes_tax": "",
                "track_inventory": "on",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.instance.unit = foreign_unit
        with self.assertRaises(Http404):
            purchase_services.quick_add_product(
                business=self.business_a,
                form=form,
                user=self.owner_a,
            )

    def test_invalid_negative_purchase_does_not_store_attachment(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                with self.assertRaises(ValidationError):
                    purchase_services.create_purchase(
                        business=self.business_a,
                        supplier=self.supplier,
                        branch=self.branch_a,
                        warehouse=self.warehouse_a,
                        rows=[
                            {
                                "product": self.product_a,
                                "variant": None,
                                "quantity": D("1"),
                                "unit_cost": D("-1"),
                            }
                        ],
                        user=self.owner_a,
                        purchase_date="2026-07-18",
                        attachment=SimpleUploadedFile("unsafe.html", b"<script>alert(1)</script>"),
                    )
                self.assertFalse(any(Path(media_root).rglob("*")))

    def test_non_finite_purchase_inputs_fail_without_mutation(self):
        purchase_count = Purchase.objects.for_business(self.business_a).count()
        with self.assertRaises(ValidationError):
            purchase_services.create_purchase(
                business=self.business_a,
                supplier=self.supplier,
                branch=self.branch_a,
                warehouse=self.warehouse_a,
                rows=[
                    {
                        "product": self.product_a,
                        "variant": None,
                        "quantity": D("1"),
                        "unit_cost": D("Infinity"),
                    }
                ],
                user=self.owner_a,
                purchase_date="2026-07-18",
            )
        self.assertEqual(Purchase.objects.for_business(self.business_a).count(), purchase_count)
        with self.assertRaises(ValidationError):
            purchase_services.create_purchase(
                business=self.business_a,
                supplier=self.supplier,
                branch=self.branch_a,
                warehouse=self.warehouse_a,
                rows=[
                    {
                        "product": self.product_a,
                        "variant": None,
                        "quantity": D("1"),
                        "unit_cost": D("1"),
                    }
                ],
                user=self.owner_a,
                purchase_date="2026-02-30",
            )

        item = self.purchase.items.get()
        stock_before = inventory_services.get_stock(
            self.business_a,
            self.warehouse_a,
            self.product_a,
        )
        with self.assertRaises(ValidationError):
            purchase_services.receive_purchase(
                purchase=self.purchase,
                quantities={item.pk: D("NaN")},
                user=self.owner_a,
            )
        self.assertEqual(
            inventory_services.get_stock(
                self.business_a,
                self.warehouse_a,
                self.product_a,
            ),
            stock_before,
        )

        payment_count = SupplierPayment.objects.for_business(self.business_a).count()
        with self.assertRaises(ValidationError):
            purchase_services.record_purchase_payments(
                purchase=self.purchase,
                rows=[{"method": "cash", "amount": D("NaN")}],
                user=self.owner_a,
            )
        self.assertEqual(
            SupplierPayment.objects.for_business(self.business_a).count(),
            payment_count,
        )

        purchase_services.receive_purchase(
            purchase=self.purchase,
            quantities={item.pk: D("1")},
            user=self.owner_a,
        )
        movement_count = StockMovement.objects.for_business(self.business_a).count()
        with self.assertRaises(ValidationError):
            purchase_services.return_purchase(
                purchase=self.purchase,
                quantities={item.pk: D("Infinity")},
                user=self.owner_a,
            )
        self.assertEqual(
            StockMovement.objects.for_business(self.business_a).count(),
            movement_count,
        )

    def test_payment_and_cheque_services_cannot_bypass_purchase_module(self):
        payment = purchase_services.record_purchase_payments(
            purchase=self.purchase,
            rows=[
                {
                    "method": SupplierPayment.Method.CHEQUE,
                    "amount": D("2"),
                    "cheque_number": "PH2C-CHQ",
                    "bank_name": "Test Bank",
                    "cheque_issue_date": timezone.localdate(),
                    "due_date": timezone.localdate() + timedelta(days=2),
                }
            ],
            user=self.owner_a,
        )[0]
        self.set_modules(purchases=False)
        before_payments = SupplierPayment.objects.for_business(self.business_a).count()
        self.assert_service_denied(
            lambda: purchase_services.record_purchase_payments(
                purchase=self.purchase,
                rows=[{"method": "cash", "amount": D("1")}],
                user=self.owner_a,
            ),
            DenialCode.MODULE_DISABLED,
        )
        self.assert_service_denied(
            lambda: purchase_services.update_cheque_status(
                payment=payment,
                status=SupplierPayment.ChequeStatus.CLEARED,
                user=self.owner_a,
            ),
            DenialCode.MODULE_DISABLED,
        )
        self.assertEqual(
            SupplierPayment.objects.for_business(self.business_a).count(),
            before_payments,
        )

    def test_read_only_denies_direct_cheque_status_service(self):
        payment = purchase_services.record_purchase_payments(
            purchase=self.purchase,
            rows=[
                {
                    "method": SupplierPayment.Method.CHEQUE,
                    "amount": D("2"),
                    "cheque_number": "PH2C-READONLY",
                    "bank_name": "Test Bank",
                    "cheque_issue_date": timezone.localdate(),
                    "due_date": timezone.localdate() + timedelta(days=2),
                }
            ],
            user=self.owner_a,
        )[0]
        self.set_subscription_status(Subscription.Status.CANCELLED)
        self.assert_service_denied(
            lambda: purchase_services.update_cheque_status(
                payment=payment,
                status=SupplierPayment.ChequeStatus.CLEARED,
                user=self.owner_a,
            ),
            DenialCode.SUBSCRIPTION_READ_ONLY,
        )

    def test_purchase_return_inherits_purchases_not_feature_returns(self):
        item = self.purchase.items.get()
        purchase_services.receive_purchase(
            purchase=self.purchase,
            quantities={item.pk: D("2")},
            user=self.owner_a,
        )
        self.set_plan(feature_returns=False)
        movement_count = StockMovement.objects.for_business(self.business_a).count()
        purchase_return = purchase_services.return_purchase(
            purchase=self.purchase,
            quantities={item.pk: D("1")},
            user=self.owner_a,
        )
        self.assertEqual(purchase_return.purchase, self.purchase)
        self.assertEqual(
            StockMovement.objects.for_business(self.business_a).count(),
            movement_count + 1,
        )

    def test_corrupt_existing_return_item_fails_closed(self):
        item = self.purchase.items.get()
        purchase_services.receive_purchase(
            purchase=self.purchase,
            quantities={item.pk: D("2")},
            user=self.owner_a,
        )
        purchase_return = purchase_services.return_purchase(
            purchase=self.purchase,
            quantities={item.pk: D("1")},
            user=self.owner_a,
        )
        PurchaseReturnItem.objects.filter(purchase_return=purchase_return).update(
            business=self.business_b
        )
        stock_before = inventory_services.get_stock(
            self.business_a,
            self.warehouse_a,
            self.product_a,
        )

        with self.assertRaises(Http404):
            purchase_services.return_purchase(
                purchase=self.purchase,
                quantities={item.pk: D("1")},
                user=self.owner_a,
            )
        self.assertEqual(
            inventory_services.get_stock(
                self.business_a,
                self.warehouse_a,
                self.product_a,
            ),
            stock_before,
        )
        self.client.force_login(self.owner_a)
        self.assertEqual(
            self.client.get(
                reverse("purchases:detail", args=[self.purchase.public_id])
            ).status_code,
            404,
        )

    def test_return_and_cancel_services_deny_read_only_and_forged_items(self):
        item = self.purchase.items.get()
        foreign_item = self.create_purchase().items.get()
        with self.assertRaises(Http404):
            purchase_services.receive_purchase(
                purchase=self.purchase,
                quantities={foreign_item.pk: D("1")},
                user=self.owner_a,
            )
        self.set_subscription_status(Subscription.Status.PAST_DUE)
        self.assert_service_denied(
            lambda: purchase_services.return_purchase(
                purchase=self.purchase,
                quantities={item.pk: D("1")},
                user=self.owner_a,
            ),
            DenialCode.SUBSCRIPTION_READ_ONLY,
        )
        self.assert_service_denied(
            lambda: purchase_services.cancel_purchase(
                purchase=self.purchase,
                user=self.owner_a,
            ),
            DenialCode.SUBSCRIPTION_READ_ONLY,
        )

    def test_public_share_stops_when_any_purchase_entitlement_is_lost(self):
        self.set_modules()
        self.client.force_login(self.owner_a)
        response = self.client.get(reverse("purchases:share", args=[self.purchase.public_id]))
        share_url = response.context["share_url"]
        self.client.logout()
        self.assertEqual(self.client.get(share_url).status_code, 200)

        for field in (
            "feature_purchases",
            "feature_inventory",
            "feature_suppliers",
            "feature_sales",
        ):
            self.set_modules()
            self.set_plan(**{field: False})
            with self.subTest(field=field):
                self.assertEqual(self.client.get(share_url).status_code, 404)

    def test_public_share_failures_are_generic_404(self):
        self.set_modules()
        self.client.force_login(self.owner_a)
        share_url = self.client.get(
            reverse("purchases:share", args=[self.purchase.public_id])
        ).context["share_url"]
        self.client.logout()

        self.business_a.is_active = False
        self.business_a.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(share_url).status_code, 404)
        self.business_a.is_active = True
        self.business_a.save(update_fields=["is_active"])

        self.set_plan(is_active=False)
        self.assertEqual(self.client.get(share_url).status_code, 404)
        self.set_plan(is_active=True)

        self.set_subscription_status(Subscription.Status.SUSPENDED)
        self.assertEqual(self.client.get(share_url).status_code, 404)

    def test_expired_public_share_token_is_404(self):
        self.client.force_login(self.owner_a)
        share_url = self.client.get(
            reverse("purchases:share", args=[self.purchase.public_id])
        ).context["share_url"]
        self.client.logout()
        with patch("apps.purchases.views.PO_SHARE_MAX_AGE", -1):
            self.assertEqual(self.client.get(share_url).status_code, 404)

    def test_purchase_attachment_requires_current_module_and_scope(self):
        self.set_modules()
        self.client.force_login(self.owner_a)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                self.purchase.attachment.save(
                    "phase2c.txt",
                    SimpleUploadedFile("phase2c.txt", b"phase2c attachment"),
                    save=True,
                )
                url = self.purchase.attachment.url
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertIn("attachment;", response["Content-Disposition"])
                self.assertEqual(response["X-Content-Type-Options"], "nosniff")
                response.close()

                self.client.force_login(self.owner_b)
                case_alias_url = url.replace("/purchases/", "/PURCHASES/")
                alias_response = self.client.get(case_alias_url)
                self.assertEqual(alias_response.status_code, 404)

                self.client.force_login(self.owner_a)
                self.set_modules(purchases=False)
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_navigation_and_dashboard_follow_effective_modules(self):
        self.client.force_login(self.owner_a)
        self.set_modules(suppliers=True, purchases=False)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, reverse("suppliers:list"))
        self.assertNotContains(response, reverse("purchases:list"))
        self.assertContains(response, reverse("inventory:stock_list"))
        self.assertTrue(response.context["suppliers_access"])
        self.assertFalse(response.context["purchases_access"])

        self.set_modules(suppliers=False, purchases=True)
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, reverse("suppliers:list"))
        self.assertNotContains(response, reverse("purchases:list"))
        self.assertContains(response, reverse("inventory:stock_list"))

    def test_disabled_module_is_not_used_as_a_saved_login_destination(self):
        self.set_modules(suppliers=False, purchases=False)
        self.client.force_login(self.owner_a)
        response = self.client.get(
            reverse("accounts:login"),
            {"next": reverse("purchases:list")},
        )
        self.assertNotEqual(response.get("Location"), reverse("purchases:list"))
