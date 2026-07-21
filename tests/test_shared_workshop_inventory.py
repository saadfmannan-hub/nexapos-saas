from decimal import Decimal
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.branches.models import Branch, Warehouse
from apps.catalog.models import Product, Unit
from apps.customers import services as customer_services
from apps.customers.models import Customer
from apps.inventory import services as inventory
from apps.registers import services as register_services
from apps.registers.models import CashRegister
from apps.sales import services as sales_services
from apps.sales.models import Sale, SaleReturn
from apps.tenants.forms import BusinessSettingsForm

from .base import TenantTestCase

D = Decimal


class SharedWorkshopInventoryTests(TenantTestCase):
    def setUp(self):
        settings_obj = self.business_a.settings
        settings_obj.vat_enabled = False
        settings_obj.allow_sale_without_shift = True
        settings_obj.save(update_fields=["vat_enabled", "allow_sale_without_shift"])

        self.branch_two = Branch.objects.create(
            business=self.business_a,
            name="Mabelah",
            code="MB",
        )
        self.warehouse_two = Warehouse.objects.create(
            business=self.business_a,
            branch=self.branch_two,
            name="Mabelah Stock",
            code="MB-STOCK",
            is_default=True,
        )
        self.customer_two = customer_services.ensure_walk_in_customer(
            self.business_a, self.branch_two
        )
        self.workshop = Branch.objects.create(
            business=self.business_a,
            name="Production Location",
            code="WS",
            usage_type=Branch.UsageType.WORKSHOP_STOCK,
        )
        self.workshop_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=self.workshop,
            name="Shared Fabric Stock",
            code="WS-FABRIC",
            is_default=True,
        )
        self.meter = Unit.objects.create(
            business=self.business_a,
            name="Workshop Meter",
            abbreviation="M",
            allow_decimal=True,
            is_meter=True,
        )
        self.pcs = Unit.objects.create(
            business=self.business_a,
            name="Workshop PCS",
            abbreviation="PCS",
        )
        self.fabric = Product.objects.create(
            business=self.business_a,
            name="Shared Test Fabric",
            sku="SHARED-FABRIC",
            unit=self.meter,
            sale_price=D("20.00"),
            purchase_price=D("4.00"),
            is_tailoring_item=True,
            track_inventory=True,
        )
        self.retail_a = Product.objects.create(
            business=self.business_a,
            name="Al Hail Retail",
            sku="AH-RETAIL",
            unit=self.pcs,
            sale_price=D("5.00"),
            track_inventory=True,
        )
        self.retail_two = Product.objects.create(
            business=self.business_a,
            name="Mabelah Retail",
            sku="MB-RETAIL",
            unit=self.pcs,
            sale_price=D("6.00"),
            track_inventory=True,
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=self.workshop_warehouse,
            product=self.fabric,
            quantity=D("100"),
            unit_cost=D("4"),
            user=self.owner_a,
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=self.retail_a,
            quantity=D("10"),
            unit_cost=D("2"),
            user=self.owner_a,
        )
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=self.warehouse_two,
            product=self.retail_two,
            quantity=D("8"),
            unit_cost=D("3"),
            user=self.owner_a,
        )
        settings_obj.shared_fabric_warehouse = self.workshop_warehouse
        settings_obj.full_clean()
        settings_obj.save(update_fields=["shared_fabric_warehouse"])

    def fabric_line(self, meters="3"):
        return {
            "product": self.fabric,
            "quantity": D("1"),
            "unit_price": D("20"),
            "fabric_meter_used": D(meters),
            "garment_classification": "adult",
            "collection_type": "normal",
            "tailoring_details": {},
        }

    def complete(self, *, branch, warehouse, customer, items, token=None):
        total = sum(D(str(item["quantity"])) * D(str(item["unit_price"])) for item in items)
        return sales_services.complete_sale(
            business=self.business_a,
            branch=branch,
            warehouse=warehouse,
            cashier=self.owner_a,
            customer=customer,
            items=items,
            payments=[{"method": self.cash_a, "amount": total}],
            membership=self.membership_a(),
            delivery_date=timezone.localdate()
            if any(item["product"].is_meter_tailoring for item in items)
            else None,
            checkout_token=token,
        )

    def test_existing_branch_defaults_to_sales_branch(self):
        branch = Branch.objects.create(
            business=self.business_a,
            name="Default Type",
            code="DEFAULT-TYPE",
        )
        self.assertEqual(branch.usage_type, Branch.UsageType.SALES_BRANCH)

    def test_workshop_cannot_open_register_or_create_sale(self):
        register = CashRegister.objects.create(
            business=self.business_a,
            branch=self.workshop,
            name="Invalid Workshop Register",
            code="WS-REG",
        )
        with self.assertRaises(register_services.ShiftError):
            register_services.open_shift(
                business=self.business_a,
                register=register,
                cashier=self.owner_a,
                opening_cash=D("0"),
                membership=self.membership_a(),
            )
        workshop_customer = Customer.objects.create(
            business=self.business_a,
            home_branch=self.workshop,
            code="WS-CUSTOMER",
            full_name="Invalid Workshop Customer",
        )
        with self.assertRaises(sales_services.SaleError):
            self.complete(
                branch=self.workshop,
                warehouse=self.workshop_warehouse,
                customer=workshop_customer,
                items=[self.fabric_line()],
            )

    def test_both_sales_branches_deduct_same_workshop_balance(self):
        first = self.complete(
            branch=self.branch_a,
            warehouse=self.warehouse_a,
            customer=self.walk_in_a,
            items=[self.fabric_line("3")],
        )
        second = self.complete(
            branch=self.branch_two,
            warehouse=self.warehouse_two,
            customer=self.customer_two,
            items=[self.fabric_line("2")],
        )
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.workshop_warehouse, self.fabric
            ),
            D("95"),
        )
        self.assertEqual(first.branch, self.branch_a)
        self.assertEqual(second.branch, self.branch_two)
        self.assertEqual(first.items.get().stock_warehouse, self.workshop_warehouse)
        self.assertEqual(second.items.get().stock_warehouse, self.workshop_warehouse)

    def test_retail_deducts_only_from_originating_branch(self):
        self.complete(
            branch=self.branch_a,
            warehouse=self.warehouse_a,
            customer=self.walk_in_a,
            items=[{
                "product": self.retail_a,
                "quantity": D("2"),
                "unit_price": D("5"),
            }],
        )
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.retail_a),
            D("8"),
        )
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.warehouse_two, self.retail_two
            ),
            D("8"),
        )

    def test_void_restores_original_workshop_stock(self):
        sale = self.complete(
            branch=self.branch_a,
            warehouse=self.warehouse_a,
            customer=self.walk_in_a,
            items=[self.fabric_line("4")],
        )
        sales_services.void_sale(
            sale=sale,
            user=self.owner_a,
            reason="Focused reversal test",
            membership=self.membership_a(),
        )
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.workshop_warehouse, self.fabric
            ),
            D("100"),
        )
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.fabric),
            D("0"),
        )

    def test_return_restores_original_workshop_stock(self):
        sale = self.complete(
            branch=self.branch_two,
            warehouse=self.warehouse_two,
            customer=self.customer_two,
            items=[self.fabric_line("2.5")],
        )
        item = sale.items.get()
        sales_services.process_return(
            sale=sale,
            items=[{
                "sale_item": item,
                "quantity": item.quantity,
                "restock": True,
            }],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=self.owner_a,
            reason="Focused Workshop return",
            restock=True,
            membership=self.membership_a(),
        )
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.workshop_warehouse, self.fabric
            ),
            D("100"),
        )
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_two, self.fabric),
            D("0"),
        )

    def test_shared_location_rejects_cross_tenant_warehouse(self):
        settings_obj = self.business_a.settings
        settings_obj.shared_fabric_warehouse = self.warehouse_b
        with self.assertRaises(ValidationError):
            settings_obj.full_clean()

    def test_no_configuration_retains_sale_branch_stock_behavior(self):
        settings_obj = self.business_a.settings
        settings_obj.shared_fabric_warehouse = None
        settings_obj.save(update_fields=["shared_fabric_warehouse"])
        inventory.set_opening_stock(
            business=self.business_a,
            warehouse=self.warehouse_a,
            product=self.fabric,
            quantity=D("20"),
            unit_cost=D("4"),
            user=self.owner_a,
        )
        sale = self.complete(
            branch=self.branch_a,
            warehouse=self.warehouse_a,
            customer=self.walk_in_a,
            items=[self.fabric_line("3")],
        )
        self.assertEqual(
            inventory.get_stock(self.business_a, self.warehouse_a, self.fabric),
            D("17"),
        )
        self.assertEqual(sale.items.get().stock_warehouse, self.warehouse_a)

    def test_pos_combines_workshop_fabric_with_current_branch_retail_only(self):
        self.client.force_login(self.owner_a)
        response = self.client.get(
            reverse("sales:pos_products"),
            {"warehouse_id": self.warehouse_a.pk},
        )
        self.assertEqual(response.status_code, 200)
        items = {item["product_id"]: item for item in response.json()["items"]}
        self.assertEqual(items[self.fabric.pk]["stock"], 100.0)
        self.assertEqual(items[self.retail_a.pk]["stock"], 10.0)
        self.assertNotIn(self.retail_two.pk, items)

    def test_catalog_and_settings_use_linked_workshop_warehouse(self):
        settings_form = BusinessSettingsForm(instance=self.business_a.settings)
        self.assertEqual(
            list(
                settings_form.fields["shared_fabric_warehouse"]
                .queryset.values_list("pk", flat=True)
            ),
            [self.workshop_warehouse.pk],
        )

        self.client.force_login(self.owner_a)
        mabelah = self.client.get(
            reverse("catalog:product_list"),
            {"branch": self.branch_two.pk, "warehouse": self.warehouse_two.pk},
        )
        self.assertEqual(mabelah.status_code, 200)
        mabelah_products = {product.pk: product for product in mabelah.context["page_obj"]}
        self.assertIn(self.fabric.pk, mabelah_products)
        self.assertIn(self.retail_two.pk, mabelah_products)
        self.assertNotIn(self.retail_a.pk, mabelah_products)
        self.assertEqual(mabelah_products[self.fabric.pk].total_stock, D("100"))

        workshop = self.client.get(
            reverse("catalog:product_list"),
            {
                "branch": self.workshop.pk,
                "warehouse": self.workshop_warehouse.pk,
            },
        )
        self.assertEqual(workshop.status_code, 200)
        workshop_ids = {
            product.pk for product in workshop.context["page_obj"]
        }
        self.assertIn(self.fabric.pk, workshop_ids)
        self.assertNotIn(self.retail_a.pk, workshop_ids)
        self.assertNotIn(self.retail_two.pk, workshop_ids)

    def test_held_and_replayed_checkout_deducts_workshop_once(self):
        token = str(uuid4())
        sales_services.hold_sale(
            business=self.business_a,
            branch=self.branch_a,
            cashier=self.owner_a,
            cart={"items": [{"product_id": self.fabric.pk}]},
            membership=self.membership_a(),
        )
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.workshop_warehouse, self.fabric
            ),
            D("100"),
        )
        first = self.complete(
            branch=self.branch_a,
            warehouse=self.warehouse_a,
            customer=self.walk_in_a,
            items=[self.fabric_line("3")],
            token=token,
        )
        replay = self.complete(
            branch=self.branch_a,
            warehouse=self.warehouse_a,
            customer=self.walk_in_a,
            items=[self.fabric_line("3")],
            token=token,
        )
        self.assertEqual(first.pk, replay.pk)
        self.assertEqual(
            inventory.get_stock(
                self.business_a, self.workshop_warehouse, self.fabric
            ),
            D("97"),
        )
        self.assertEqual(
            Sale.objects.for_business(self.business_a)
            .filter(checkout_token=token)
            .count(),
            1,
        )
