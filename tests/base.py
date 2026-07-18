"""Shared test fixtures: two isolated businesses with users and products."""
from decimal import Decimal

from django.test import TestCase

from apps.accounts.models import Role, User
from apps.branches.models import Branch, Warehouse
from apps.catalog.models import Product, TaxRate
from apps.customers.models import Customer
from apps.inventory import services as inventory
from apps.registers.models import CashRegister
from apps.sales.models import PaymentMethod
from apps.subscriptions.models import Plan, Subscription
from apps.tenants.services import provision_business


class TenantTestCase(TestCase):
    """Creates Business A and Business B with owners, a cashier for A,
    one taxed product each, and opening stock of 100 in A / 50 in B."""

    @classmethod
    def setUpTestData(cls):
        cls.owner_a = User.objects.create_user(
            email="owner-a@example.com", password="StrongPass123!",
            full_name="Owner A",
        )
        cls.business_a = provision_business(
            owner=cls.owner_a, name="Alpha Retail", currency_code="USD",
            currency_precision=2,
        )
        cls.owner_b = User.objects.create_user(
            email="owner-b@example.com", password="StrongPass123!",
            full_name="Owner B",
        )
        cls.business_b = provision_business(
            owner=cls.owner_b, name="Beta Trading", currency_code="OMR",
            currency_precision=3,
        )

        # These are controlled test tenants.  Production seed-plan data stays
        # untouched while legacy POS/catalog/customer/register suites exercise
        # the explicitly enabled POS Core capability.
        plan_ids = Subscription.objects.filter(
            business__in=(cls.business_a, cls.business_b)
        ).values_list("plan_id", flat=True)
        Plan.objects.filter(pk__in=plan_ids).update(feature_sales=True)
        cls.business_a.refresh_from_db()
        cls.business_b.refresh_from_db()

        cls.branch_a = Branch.objects.for_business(cls.business_a).get(code="HO")
        cls.warehouse_a = Warehouse.objects.for_business(cls.business_a).get(code="MAIN")
        cls.branch_b = Branch.objects.for_business(cls.business_b).get(code="HO")
        cls.warehouse_b = Warehouse.objects.for_business(cls.business_b).get(code="MAIN")
        cls.register_a = CashRegister.objects.for_business(cls.business_a).first()

        # Cashier in business A
        cls.cashier_a = User.objects.create_user(
            email="cashier-a@example.com", password="StrongPass123!",
            full_name="Cashier A",
        )
        cashier_role = Role.objects.for_business(cls.business_a).get(name="Cashier")
        from apps.accounts.models import Membership

        cls.cashier_membership = Membership.objects.create(
            business=cls.business_a, user=cls.cashier_a, role=cashier_role,
        )

        cls.tax_a = TaxRate.objects.create(
            business=cls.business_a, name="VAT", rate=Decimal("5"),
        )
        cls.product_a = Product.objects.create(
            business=cls.business_a, name="Widget A", sku="WID-A",
            barcode="1000000000017", purchase_price=Decimal("4.000"),
            sale_price=Decimal("10.000"), tax_rate=cls.tax_a,
            estimated_adult_fabric=Decimal("3.500"),
            estimated_child_fabric=Decimal("2.250"),
        )
        inventory.set_opening_stock(
            business=cls.business_a, warehouse=cls.warehouse_a,
            product=cls.product_a, quantity=Decimal("100"),
            unit_cost=Decimal("4.000"), user=cls.owner_a,
        )
        cls.product_b = Product.objects.create(
            business=cls.business_b, name="Widget B", sku="WID-B",
            purchase_price=Decimal("2.000"), sale_price=Decimal("5.000"),
            estimated_adult_fabric=Decimal("3.500"),
            estimated_child_fabric=Decimal("2.250"),
        )
        inventory.set_opening_stock(
            business=cls.business_b, warehouse=cls.warehouse_b,
            product=cls.product_b, quantity=Decimal("50"),
            unit_cost=Decimal("2.000"), user=cls.owner_b,
        )

        cls.walk_in_a = Customer.objects.for_business(cls.business_a).get(is_walk_in=True)
        cls.walk_in_b = Customer.objects.for_business(cls.business_b).get(is_walk_in=True)
        cls.cash_a = PaymentMethod.objects.for_business(cls.business_a).get(kind="cash")
        cls.card_a = PaymentMethod.objects.for_business(cls.business_a).get(kind="card")
        cls.credit_a = PaymentMethod.objects.for_business(cls.business_a).get(
            kind="customer_credit")
        cls.store_credit_a = PaymentMethod.objects.for_business(cls.business_a).get(
            kind="store_credit")

    def membership_a(self):
        return self.business_a.memberships.get(user=self.owner_a)

    def allow_no_shift(self, business=None):
        settings_obj = (business or self.business_a).settings
        settings_obj.allow_sale_without_shift = True
        settings_obj.save()

    def make_sale(self, items=None, payments=None, customer=None, **kwargs):
        """Helper: complete a simple sale in business A as the owner."""
        from apps.sales import services as sales

        items = items or [{"product": self.product_a, "quantity": Decimal("2"),
                           "unit_price": Decimal("10.000")}]
        total = kwargs.pop("expect_total", None)
        if payments is None:
            from apps.sales.services import compute_line

            amount = Decimal("0")
            for line in items:
                parts = compute_line(
                    line["product"], line.get("variant"), line["quantity"],
                    line.get("unit_price", line["product"].sale_price),
                    line.get("discount_amount", Decimal("0")),
                    self.business_a.settings.prices_include_tax,
                )
                amount += parts["total"]
            payments = [{"method": self.cash_a, "amount": amount}]
        return sales.complete_sale(
            business=self.business_a, branch=self.branch_a,
            warehouse=self.warehouse_a, cashier=self.owner_a,
            customer=customer or self.walk_in_a, items=items,
            payments=payments, membership=self.membership_a(), **kwargs,
        )
