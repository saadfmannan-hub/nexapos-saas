"""Create a clearly-marked demo business with realistic sample data.

Usage:  python manage.py seed_demo
Safe to run repeatedly — it refuses to run if the demo business exists.
Never run this against a production database with real tenants unless
you intentionally want a demo workspace alongside them.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

D = Decimal

DEMO_EMAIL = "demo-owner@example.com"
DEMO_PASSWORD = "DemoPass123!"


class Command(BaseCommand):
    help = "Seed a demo business with branches, products, sales and more."

    @transaction.atomic
    def handle(self, *args, **options):
        from apps.accounts.models import Membership, Role, User
        from apps.branches.models import Branch, Warehouse
        from apps.catalog.models import Brand, Category, Product, ProductVariant, TaxRate
        from apps.customers.models import Customer
        from apps.expenses.models import Expense, ExpenseCategory
        from apps.inventory import services as inventory
        from apps.inventory import workflows
        from apps.purchases import services as purchases
        from apps.registers import services as registers
        from apps.sales import services as sales
        from apps.sales.models import PaymentMethod, SaleReturn
        from apps.suppliers.models import Supplier
        from apps.tenants.models import Business
        from apps.tenants.services import provision_business

        if Business.objects.filter(name="Demo Business").exists():
            self.stdout.write(self.style.WARNING(
                "Demo business already exists — nothing to do."))
            return

        owner = User.objects.filter(email=DEMO_EMAIL).first()
        if owner is None:
            owner = User.objects.create_user(
                email=DEMO_EMAIL, password=DEMO_PASSWORD,
                full_name="Demo Owner",
            )
        business = provision_business(
            owner=owner, name="Demo Business", country="Demoland",
            currency_code="USD", currency_precision=2,
            business_category="General Trading",
        )
        settings_obj = business.settings
        settings_obj.allow_sale_without_shift = False
        settings_obj.save()

        branch1 = Branch.objects.for_business(business).get(code="HO")
        branch2 = Branch.objects.create(
            business=business, name="City Mall Branch", code="MALL",
            invoice_prefix="ML",
        )
        warehouse = Warehouse.objects.for_business(business).get(code="MAIN")
        register = registers.create_default_register(business, branch1)

        # Staff
        cashier_role = Role.objects.for_business(business).get(name="Cashier")
        manager_role = Role.objects.for_business(business).get(name="Branch Manager")
        cashier = User.objects.create_user(
            email="demo-cashier@example.com", password=DEMO_PASSWORD,
            full_name="Demo Cashier",
        )
        manager = User.objects.create_user(
            email="demo-manager@example.com", password=DEMO_PASSWORD,
            full_name="Demo Manager",
        )
        Membership.objects.create(business=business, user=cashier, role=cashier_role)
        Membership.objects.create(business=business, user=manager, role=manager_role)

        # Catalog
        tax = TaxRate.objects.create(business=business, name="VAT 5%",
                                     rate=D("5"), is_default=True)
        cat_clothing = Category.objects.create(business=business, name="Clothing")
        cat_electronics = Category.objects.create(business=business, name="Electronics")
        cat_accessories = Category.objects.create(business=business, name="Accessories")
        brand = Brand.objects.create(business=business, name="Generic")

        catalog = [
            ("Classic T-Shirt", cat_clothing, "TSH-001", "2000000000016", "4.00", "9.90"),
            ("Slim Jeans", cat_clothing, "JNS-001", "2000000000023", "9.00", "19.50"),
            ("Wireless Earbuds", cat_electronics, "EAR-001", "2000000000030", "12.00", "29.90"),
            ("Phone Charger", cat_electronics, "CHG-001", "2000000000047", "2.50", "7.50"),
            ("Leather Wallet", cat_accessories, "WAL-001", "2000000000054", "5.00", "14.00"),
            ("Sunglasses", cat_accessories, "SUN-001", "2000000000061", "3.00", "11.00"),
        ]
        products = []
        for name, category, sku, barcode, cost, price in catalog:
            product = Product.objects.create(
                business=business, name=name, category=category, brand=brand,
                sku=sku, barcode=barcode, purchase_price=D(cost),
                sale_price=D(price), tax_rate=tax, reorder_level=D("10"),
            )
            inventory.set_opening_stock(
                business=business, warehouse=warehouse, product=product,
                quantity=D("60"), unit_cost=D(cost), user=owner,
            )
            products.append(product)

        # Variant product
        polo = Product.objects.create(
            business=business, name="Polo Shirt", category=cat_clothing,
            brand=brand, product_type=Product.Type.VARIANT,
            purchase_price=D("6.00"), sale_price=D("15.00"), tax_rate=tax,
        )
        for size, barcode in (("S", "2000000000078"), ("M", "2000000000085"),
                              ("L", "2000000000092")):
            variant = ProductVariant.objects.create(
                business=business, product=polo, name=f"Size {size}",
                attributes={"Size": size}, sku=f"POLO-{size}",
                barcode=barcode, purchase_price=D("6.00"), sale_price=D("15.00"),
            )
            inventory.record_movement(
                business=business, warehouse=warehouse, product=polo,
                variant=variant, movement_type="opening", quantity=D("20"),
                unit_cost=D("6.00"), reference_type="Opening", user=owner,
            )

        # Customers
        walk_in = Customer.objects.for_business(business).get(is_walk_in=True)
        customer_credit = Customer.objects.create(
            business=business, code="CUST-00001", full_name="Aisha Trading LLC",
            mobile="90000001", credit_limit=D("500"),
        )
        customer_cash = Customer.objects.create(
            business=business, code="CUST-00002", full_name="John Smith",
            mobile="90000002",
        )

        # Supplier + received purchase
        supplier = Supplier.objects.create(
            business=business, code="SUP-0001", name="Global Imports Co",
            contact_person="Sam Lee", mobile="91000001",
        )
        purchase = purchases.create_purchase(
            business=business, supplier=supplier, branch=branch1,
            warehouse=warehouse,
            rows=[{"product": products[0], "variant": None,
                   "quantity": D("40"), "unit_cost": D("3.80")},
                  {"product": products[2], "variant": None,
                   "quantity": D("20"), "unit_cost": D("11.50")}],
            user=owner, purchase_date=timezone.localdate() - timedelta(days=7),
        )
        purchases.receive_purchase(
            purchase=purchase,
            quantities={item.pk: item.quantity_ordered
                        for item in purchase.items.all()},
            user=owner,
        )
        purchases.pay_purchase(
            purchase=purchase, amount=D("100.00"),
            method=PaymentMethod.objects.for_business(business).get(kind="bank"),
            user=owner, reference="TRF-100",
        )

        # Shift + sales
        shift = registers.open_shift(
            business=business, register=register, cashier=cashier,
            opening_cash=D("100.00"),
        )
        cash = PaymentMethod.objects.for_business(business).get(kind="cash")
        card = PaymentMethod.objects.for_business(business).get(kind="card")
        credit = PaymentMethod.objects.for_business(business).get(
            kind="customer_credit")
        owner_membership = business.memberships.get(user=owner)

        def sell(items, payments, customer=walk_in):
            return sales.complete_sale(
                business=business, branch=branch1, warehouse=warehouse,
                cashier=cashier, customer=customer, items=items,
                payments=payments, membership=owner_membership,
                register=register, shift=shift,
            )

        s1 = sell([{"product": products[0], "quantity": D("2"),
                    "unit_price": products[0].sale_price},
                   {"product": products[4], "quantity": D("1"),
                    "unit_price": products[4].sale_price}],
                  [{"method": cash, "amount": D("35.49")}])
        sell([{"product": products[2], "quantity": D("1"),
               "unit_price": products[2].sale_price}],
             [{"method": card, "amount": D("31.395"), "reference": "AUTH-1234"}],
             customer=customer_cash)
        sell([{"product": products[1], "quantity": D("3"),
               "unit_price": products[1].sale_price}],
             [{"method": credit, "amount": D("61.425")}],
             customer=customer_credit)

        # A return against the first sale
        item = s1.items.first()
        sales.process_return(
            sale=s1, items=[{"sale_item": item, "quantity": D("1")}],
            refund_method=SaleReturn.RefundMethod.CASH, user=manager,
            reason="Customer changed mind", shift=shift,
        )

        # Transfer to second branch's shelf warehouse
        warehouse2 = Warehouse.objects.create(
            business=business, name="Mall Stockroom", code="MALLWH",
            branch=branch2,
        )
        transfer = workflows.create_transfer(
            business=business, from_warehouse=warehouse, to_warehouse=warehouse2,
            rows=[{"product": products[5], "variant": None, "quantity": D("10")}],
            user=manager,
        )
        workflows.dispatch_transfer(transfer=transfer, user=manager)
        workflows.receive_transfer(transfer=transfer, user=manager)

        # Expense
        rent = ExpenseCategory.objects.for_business(business).get(name="Rent")
        Expense.objects.create(
            business=business, expense_number="EXP-000001",
            expense_date=timezone.localdate(), branch=branch1, category=rent,
            payee="Demo Landlord", amount=D("250.00"),
            payment_method=PaymentMethod.objects.for_business(business).get(kind="bank"),
            status="approved", created_by=owner,
            approved_by=owner, shift=shift,
        )

        registers.close_shift(shift=shift, actual_cash=D("125.00"),
                              user=cashier)

        self.stdout.write(self.style.SUCCESS(
            "Demo business created.\n"
            f"  Owner:   {DEMO_EMAIL} / {DEMO_PASSWORD}\n"
            f"  Manager: demo-manager@example.com / {DEMO_PASSWORD}\n"
            f"  Cashier: demo-cashier@example.com / {DEMO_PASSWORD}"
        ))
