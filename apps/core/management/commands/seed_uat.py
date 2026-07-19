"""Create the isolated Shumukh Al Khaleej commercial UAT tenant.

Usage: python manage.py seed_uat

The command is deliberately local-only and non-destructive. It requires DEBUG,
requires SQLite, and idempotently upgrades only its dedicated UAT business.
"""

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction
from django.utils import timezone

D = Decimal

BUSINESS_NAME = "Shumukh Al Khaleej (UAT)"
PLAN_NAME = "Shumukh UAT Full Access"
OWNER_EMAIL = "owner.uat@shumukh.example"
DEFAULT_PASSWORD = "UATReview2026!"


class Command(BaseCommand):
    help = "Create one isolated, realistic Shumukh commercial UAT tenant."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            default=DEFAULT_PASSWORD,
            help="Password assigned to newly-created UAT users.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        self._assert_safe_database()

        from apps.accounts.models import Membership, Role, User
        from apps.branches.models import Branch, Warehouse
        from apps.catalog.models import Brand, Category, Product, ProductVariant, Unit
        from apps.customers.models import Customer
        from apps.expenses import services as expense_services
        from apps.expenses.models import (
            Expense,
            ExpenseCategory,
            RecurringExpenseTemplate,
        )
        from apps.inventory import services as inventory
        from apps.inventory.models import StockMovement
        from apps.purchases import services as purchases
        from apps.registers import services as registers
        from apps.registers.models import CashRegister, Shift
        from apps.sales import services as sales
        from apps.sales.models import PaymentMethod, Sale, SalePayment, SaleReturn
        from apps.subscriptions.models import Plan, Subscription
        from apps.suppliers.models import Supplier, SupplierPayment
        from apps.tenants.models import Business
        from apps.tenants.services import provision_business

        existing = Business.objects.filter(
            name=BUSINESS_NAME,
            owner__email=OWNER_EMAIL,
        ).first()
        if existing is not None:
            changed = self._upgrade_existing_branch_customers(existing)
            self.stdout.write(
                self.style.WARNING(
                    "The UAT business already exists; branch customer data was "
                    + ("upgraded." if changed else "already current.")
                )
            )
            self._print_counts(existing)
            return

        if User.objects.filter(email=OWNER_EMAIL).exists():
            raise CommandError(
                f"User {OWNER_EMAIL} already exists without the UAT business; "
                "refusing to reuse or overwrite it."
            )

        password = options["password"]
        plan, created = Plan.objects.get_or_create(
            name=PLAN_NAME,
            defaults={
                "description": "Local-only full-access plan for controlled UAT.",
                "currency_code": "OMR",
                "monthly_price": D("0.000"),
                "annual_price": D("0.000"),
                "allow_trial": False,
                "trial_days": 0,
                "max_branches": 0,
                "max_users": 0,
                "max_warehouses": 0,
                "max_products": 0,
                "max_customers": 0,
                "max_monthly_invoices": 0,
                "max_suppliers": 0,
                "max_active_orders": 0,
                "max_pos_terminals": 0,
                "feature_sales": True,
                "feature_inventory": True,
                "feature_suppliers": True,
                "feature_purchases": True,
                "feature_expenses": True,
                "feature_returns": True,
                "feature_transfers": True,
                "feature_tailoring_module": True,
                "feature_customer_credit": True,
                "feature_advanced_reports": True,
                "feature_audit_logs": True,
                "feature_barcode_printing": True,
                "feature_custom_roles": True,
                "feature_api_access": True,
            },
        )
        required_features = (
            "feature_sales",
            "feature_inventory",
            "feature_suppliers",
            "feature_purchases",
            "feature_expenses",
            "feature_returns",
            "feature_tailoring_module",
            "feature_customer_credit",
            "feature_advanced_reports",
        )
        if not created and any(not getattr(plan, field) for field in required_features):
            raise CommandError(
                f"Existing plan {PLAN_NAME!r} is not full access; refusing to modify it."
            )

        owner = User.objects.create_user(
            email=OWNER_EMAIL,
            password=password,
            full_name="Shumukh UAT Owner",
        )
        business = provision_business(
            owner=owner,
            name=BUSINESS_NAME,
            country="Oman",
            timezone_name="Asia/Muscat",
            currency_code="OMR",
            currency_precision=3,
            business_category="Tailoring and Retail",
            phone="+968 2450 0000",
            plan=plan,
        )
        Business.objects.filter(pk=business.pk).update(
            legal_name="Shumukh Al Khaleej Trading (UAT)",
            address="Al Hail, Seeb",
            city="Muscat",
            currency_symbol="OMR",
            onboarding_completed=True,
        )
        business.refresh_from_db()
        Subscription.objects.filter(business=business).update(
            status=Subscription.Status.ACTIVE,
            trial_ends_at=None,
            current_period_start=timezone.now(),
            current_period_end=timezone.now() + timedelta(days=365),
            notes="Controlled local commercial UAT subscription.",
        )
        business.settings.allow_sale_without_shift = False
        business.settings.invoice_prefix = "SHK"
        business.settings.invoice_include_branch_code = True
        business.settings.require_customer_for_credit = True
        business.settings.return_window_days = 0
        measurement_labels = (
            "Toul", "Shoulders", "Chest", "Side",
            "Sleeves", "Open", "Front", "Button",
        )
        for index, label in enumerate(measurement_labels, start=1):
            setattr(business.settings, f"more_option_label_{index}", label)
        business.settings.save()

        # Locations: the provisioned head office becomes Al Hail. Workshop is
        # represented by the branch-linked warehouse supported by this schema.
        al_hail = Branch.objects.for_business(business).get(code="HO")
        al_hail.name = "Al Hail Branch"
        al_hail.code = "AH"
        al_hail.address = "Al Hail North, Seeb, Muscat"
        al_hail.phone = "+968 2450 1001"
        al_hail.invoice_prefix = "AH"
        al_hail.save()

        from apps.customers.services import ensure_walk_in_customer

        walk_in_al_hail = ensure_walk_in_customer(business, al_hail)

        al_hail_warehouse = Warehouse.objects.for_business(business).get(code="MAIN")
        al_hail_warehouse.name = "Al Hail Stockroom"
        al_hail_warehouse.code = "AH-STOCK"
        al_hail_warehouse.branch = al_hail
        al_hail_warehouse.save()

        mabelah = Branch.objects.create(
            business=business,
            name="Mabelah Branch",
            code="MB",
            address="Al Mabelah South, Seeb, Muscat",
            phone="+968 2450 1002",
            invoice_prefix="MB",
        )
        workshop = Warehouse.objects.create(
            business=business,
            name="Workshop",
            code="MB-WORKSHOP",
            branch=mabelah,
            address="Mabelah Branch tailoring workshop",
            is_default=True,
        )
        walk_in_mabelah = ensure_walk_in_customer(business, mabelah)

        al_hail_register = CashRegister.objects.for_business(business).get(code="REG1")
        al_hail_register.name = "Al Hail Register"
        al_hail_register.code = "AH-REG1"
        al_hail_register.branch = al_hail
        al_hail_register.save()
        mabelah_register = CashRegister.objects.create(
            business=business,
            name="Mabelah Register",
            code="MB-REG1",
            branch=mabelah,
        )

        # Users and branch-scoped roles.
        cashier_permissions = [
            "sales.view",
            "sales.create",
            "sales.discount",
            "sales.credit",
            "sales.refund",
            "products.view",
            "products.manage",
            "products.import",
            "products.export",
            "customers.view",
            "customers.manage",
            "customers.export",
            "customers.import",
            "inventory.view",
            "inventory.adjust",
            "inventory.export",
            "inventory.import",
            "shifts.open",
            "shifts.close",
            "notifications.view",
        ]
        salesman_role = Role.objects.create(
            business=business,
            name="Salesman",
            is_system=False,
            permissions=cashier_permissions,
        )
        workshop_role = Role.objects.for_business(business).get(name="Workshop Manager")

        def create_staff(email, name, role, branch):
            user = User.objects.create_user(
                email=email,
                password=password,
                full_name=name,
            )
            membership = Membership.objects.create(
                business=business,
                user=user,
                role=role,
            )
            membership.branches.add(branch)
            return user, membership

        salesman_al_hail, membership_al_hail = create_staff(
            "sales.alhail.uat@shumukh.example",
            "Salesman Al Hail",
            salesman_role,
            al_hail,
        )
        salesman_mabelah, membership_mabelah = create_staff(
            "sales.mabelah.uat@shumukh.example",
            "Salesman Mabelah",
            salesman_role,
            mabelah,
        )
        workshop_manager, workshop_membership = create_staff(
            "workshop.uat@shumukh.example",
            "Workshop Manager",
            workshop_role,
            mabelah,
        )

        # Catalog: named tailoring services, meter-based fabric/color stock,
        # and finished retail products measured in PCS.
        tailoring_category = Category.objects.create(
            business=business, name="Tailoring Services"
        )
        fabric_category = Category.objects.create(business=business, name="Fabrics")
        retail_category = Category.objects.create(
            business=business, name="Retail Accessories"
        )
        piece = Unit.objects.for_business(business).get(name="Piece")
        meter = Unit.objects.for_business(business).get(is_meter=True)

        tailoring_specs = (
            ("Dishdasha", "TAIL-DISHDASHA", "18.000", "3.500", "2.300"),
            ("Shirt", "TAIL-SHIRT", "9.000", "2.100", "1.500"),
            ("Pant", "TAIL-PANT", "8.000", "1.800", "1.300"),
            ("Kids Dishdasha", "TAIL-KIDS", "12.000", "2.800", "2.100"),
            ("Premium Dishdasha", "TAIL-PREMIUM", "35.000", "4.000", "2.700"),
        )
        tailoring_products = []
        for name, sku, price, adult_meter, child_meter in tailoring_specs:
            tailoring_products.append(
                Product.objects.create(
                    business=business,
                    name=name,
                    sku=sku,
                    category=tailoring_category,
                    product_type=Product.Type.SERVICE,
                    unit=None,
                    purchase_price=D("0.000"),
                    sale_price=D(price),
                    track_inventory=False,
                    is_tailoring_item=True,
                    estimated_adult_fabric=D(adult_meter),
                    estimated_child_fabric=D(child_meter),
                )
            )

        fabric_specs = (
            ("Al Safwa", "FAB-SAFWA", "2.600", ("White", "Cream", "Silver")),
            ("Royal Loom", "FAB-ROYAL", "3.200", ("White", "Beige", "Sky Blue")),
            ("Nizwa Premium", "FAB-NIZWA", "4.400", ("Pearl", "Ivory", "Navy")),
        )
        fabric_variants = []
        for brand_name, sku, cost, colors in fabric_specs:
            brand = Brand.objects.create(business=business, name=brand_name)
            product = Product.objects.create(
                business=business,
                name=f"{brand_name} Fabric",
                sku=sku,
                category=fabric_category,
                brand=brand,
                unit=meter,
                product_type=Product.Type.VARIANT,
                purchase_price=D(cost),
                sale_price=D("0.000"),
                track_inventory=True,
                allow_discount=False,
                is_tailoring_item=True,
            )
            for index, color in enumerate(colors, start=1):
                variant = ProductVariant.objects.create(
                    business=business,
                    product=product,
                    name=color,
                    attributes={"Color": color},
                    sku=f"{sku}-{index:02d}",
                    barcode=f"629110{len(fabric_variants) + 1:07d}",
                    purchase_price=D(cost),
                    sale_price=D("0.000"),
                )
                fabric_variants.append(variant)
                inventory.set_opening_stock(
                    business=business,
                    warehouse=al_hail_warehouse,
                    product=product,
                    variant=variant,
                    quantity=D("90.000"),
                    unit_cost=D(cost),
                    user=owner,
                )
                inventory.set_opening_stock(
                    business=business,
                    warehouse=workshop,
                    product=product,
                    variant=variant,
                    quantity=D("60.000"),
                    unit_cost=D(cost),
                    user=owner,
                )

        retail_specs = (
            ("Kumma", "RET-KUMMA", "4.500", "12.000"),
            ("Musar", "RET-MUSAR", "6.000", "16.000"),
            ("Assa", "RET-ASSA", "5.500", "14.000"),
            ("Perfume", "RET-PERFUME", "3.000", "8.500"),
            ("Buttons", "RET-BUTTONS", "0.700", "2.000"),
            ("Accessories", "RET-ACCESS", "1.500", "5.000"),
        )
        retail_products = []
        retail_brand = Brand.objects.create(business=business, name="Shumukh Select")
        for index, (name, sku, cost, price) in enumerate(retail_specs, start=1):
            product = Product.objects.create(
                business=business,
                name=name,
                sku=sku,
                barcode=f"629120{index:07d}",
                category=retail_category,
                brand=retail_brand,
                unit=piece,
                purchase_price=D(cost),
                sale_price=D(price),
                reorder_level=D("8.000"),
            )
            retail_products.append(product)
            inventory.set_opening_stock(
                business=business,
                warehouse=al_hail_warehouse,
                product=product,
                quantity=D("35.000"),
                unit_cost=D(cost),
                user=owner,
            )
            inventory.set_opening_stock(
                business=business,
                warehouse=workshop,
                product=product,
                quantity=D("24.000"),
                unit_cost=D(cost),
                user=owner,
            )

        self._ensure_branch_only_products(
            business=business,
            owner=owner,
            al_hail=al_hail,
            mabelah=mabelah,
            al_hail_warehouse=al_hail_warehouse,
            mabelah_warehouse=workshop,
        )
        self._ensure_simple_product_examples(
            business=business,
            owner=owner,
            warehouse=al_hail_warehouse,
        )

        # Named customers are evenly branch-owned; walk-ins remain separate.
        customer_specs = (
            ("Ahmed Al Balushi", "أحمد البلوشي"),
            ("Mohammed Al Harthy", "محمد الحارثي"),
            ("Said Al Maawali", "سعيد المعولي"),
            ("Khalid Al Rawahi", "خالد الرواحي"),
            ("Yousuf Al Hinai", "يوسف الهنائي"),
            ("Ali Al Siyabi", "علي السيابي"),
            ("Hamad Al Busaidi", "حمد البوسعيدي"),
            ("Nasser Al Wahaibi", "ناصر الوهيبي"),
            ("Salim Al Amri", "سالم العامري"),
            ("Abdullah Al Shukaili", "عبدالله الشكيلي"),
            ("Fahad Al Jabri", "فهد الجابري"),
            ("Mazin Al Kindi", "مازن الكندي"),
            ("Ibrahim Al Farsi", "إبراهيم الفارسي"),
            ("Rashid Al Maskari", "راشد المسكري"),
            ("Talal Al Lawati", "طلال اللواتي"),
            ("Omar Al Riyami", "عمر الريامي"),
            ("Zahir Al Nabhani", "ظاهر النبهاني"),
            ("Bader Al Mahrouqi", "بدر المحروقي"),
            ("Hilal Al Sarmi", "هلال الصارمي"),
            ("Mubarak Al Yaqoubi", "مبارك اليعقوبي"),
        )
        customers = []
        for index in range(1, 151):
            if index <= len(customer_specs):
                english, arabic = customer_specs[index - 1]
                full_name = f"{english} / {arabic}"
            else:
                full_name = f"UAT Customer {index:03d}"
            branch = al_hail if index % 2 else mabelah
            customers.append(
                Customer.objects.create(
                    business=business,
                    home_branch=branch,
                    code=f"{branch.code}-CUST-{index:04d}",
                    full_name=full_name,
                    mobile=f"+968 9{2100000 + index:07d}",
                    whatsapp=f"+968 9{2100000 + index:07d}",
                    city="Muscat" if index % 3 else "Seeb",
                    country="Oman",
                    credit_limit=D("500.000") if index <= 8 else D("150.000"),
                    notes="Controlled UAT customer record.",
                    more_options={
                        "1": str(54 + (index % 8)),
                        "2": str(17 + (index % 4)),
                        "3": str(36 + (index % 8)),
                        "4": str(20 + (index % 5)),
                        "5": str(22 + (index % 6)),
                        "6": str(11 + (index % 4)),
                        "7": str(16 + (index % 5)),
                        "8": str(8 + (index % 3)),
                    },
                )
            )

        supplier_specs = (
            ("Muscat Textile House", "Fabric", "30 days"),
            ("Sohar Fabrics LLC", "Fabric", "45 days"),
            ("Nizwa Premium Textiles", "Fabric", "Cheque 60 days"),
            ("Al Khoud Accessories", "Accessories", "Cash"),
            ("Oman Buttons Trading", "Accessories", "30 days"),
            ("Muttrah Perfumes", "Retail", "Cash"),
            ("Gulf General Supplies", "General", "30 days"),
            ("Seeb Packaging Centre", "General", "Cash"),
        )
        suppliers = []
        for index, (name, supplier_type, terms) in enumerate(supplier_specs, start=1):
            suppliers.append(
                Supplier.objects.create(
                    business=business,
                    code=f"SUP-{index:04d}",
                    name=name,
                    contact_person=f"UAT Contact {index}",
                    mobile=f"+968 24{600000 + index:06d}",
                    email=f"supplier{index}.uat@example.com",
                    address="Muscat, Oman",
                    payment_terms=terms,
                    notes=f"{supplier_type} supplier for controlled UAT.",
                )
            )

        cash = PaymentMethod.objects.for_business(business).get(kind="cash")
        card = PaymentMethod.objects.for_business(business).get(kind="card")
        bank = PaymentMethod.objects.for_business(business).get(kind="bank")
        credit = PaymentMethod.objects.for_business(business).get(
            kind="customer_credit"
        )
        today = timezone.localdate()

        # Purchases cover cash, partial, post-dated cheque and multiple cheques.
        purchase_specs = (
            (0, 135, al_hail, al_hail_warehouse, [(0, "80", "2.500"), (1, "70", "2.500")]),
            (1, 105, mabelah, workshop, [(3, "90", "3.050"), (4, "80", "3.050")]),
            (2, 75, mabelah, workshop, [(6, "65", "4.200"), (7, "55", "4.200")]),
            (3, 48, al_hail, al_hail_warehouse, [(9, "25", "4.200"), (10, "20", "5.700")]),
            (4, 32, mabelah, workshop, [(13, "120", "0.650"), (14, "45", "1.350")]),
            (5, 18, al_hail, al_hail_warehouse, [(12, "24", "2.850"), (11, "20", "5.200")]),
        )
        purchase_records = []
        purchase_items_for_return = None
        for index, (supplier_index, days_ago, branch, warehouse, rows) in enumerate(
            purchase_specs
        ):
            purchase_rows = []
            for product_index, quantity, cost in rows:
                if product_index < len(fabric_variants):
                    variant = fabric_variants[product_index]
                    product = variant.product
                else:
                    product = retail_products[product_index - len(fabric_variants)]
                    variant = None
                purchase_rows.append(
                    {
                        "product": product,
                        "variant": variant,
                        "quantity": D(quantity),
                        "unit_cost": D(cost),
                    }
                )
            purchase = purchases.create_purchase(
                business=business,
                supplier=suppliers[supplier_index],
                branch=branch,
                warehouse=warehouse,
                rows=purchase_rows,
                user=owner,
                purchase_date=today - timedelta(days=days_ago),
                due_date=today - timedelta(days=max(days_ago - 30, 0)),
                supplier_invoice_number=f"UAT-SINV-{index + 1:04d}",
                notes="Historical controlled UAT purchase.",
            )
            purchases.receive_purchase(
                purchase=purchase,
                quantities={item.pk: item.quantity_ordered for item in purchase.items.all()},
                user=owner,
            )
            purchase.refresh_from_db()
            purchase_records.append(purchase)
            if index == 0:
                purchases.pay_purchase(
                    purchase=purchase,
                    amount=purchase.total,
                    method=cash,
                    user=owner,
                    reference="UAT-CASH-FULL",
                )
            elif index == 1:
                purchases.pay_purchase(
                    purchase=purchase,
                    amount=(purchase.total * D("0.400")).quantize(D("0.001")),
                    method=bank,
                    user=owner,
                    reference="UAT-PARTIAL-BANK",
                )
            elif index == 2:
                purchases.record_purchase_payments(
                    purchase=purchase,
                    rows=[
                        {
                            "method": SupplierPayment.Method.CHEQUE,
                            "amount": (purchase.total * D("0.500")).quantize(D("0.001")),
                            "cheque_number": "UAT-PDC-1001",
                            "bank_name": "Bank Muscat",
                            "cheque_issue_date": today,
                            "due_date": today + timedelta(days=30),
                            "notes": "Post-dated cheque example.",
                        }
                    ],
                    user=owner,
                )
            elif index == 3:
                cheque_amount = (purchase.total * D("0.300")).quantize(D("0.001"))
                purchases.record_purchase_payments(
                    purchase=purchase,
                    rows=[
                        {
                            "method": SupplierPayment.Method.CHEQUE,
                            "amount": cheque_amount,
                            "cheque_number": "UAT-MULTI-2001",
                            "bank_name": "National Bank of Oman",
                            "cheque_issue_date": today,
                            "due_date": today + timedelta(days=45),
                        },
                        {
                            "method": SupplierPayment.Method.CHEQUE,
                            "amount": cheque_amount,
                            "cheque_number": "UAT-MULTI-2002",
                            "bank_name": "National Bank of Oman",
                            "cheque_issue_date": today,
                            "due_date": today + timedelta(days=60),
                        },
                    ],
                    user=owner,
                )
            elif index == 4:
                purchases.pay_purchase(
                    purchase=purchase,
                    amount=purchase.total,
                    method=card,
                    user=owner,
                    reference="UAT-CARD-FULL",
                )
            if index == 1:
                purchase_items_for_return = purchase.items.order_by("id").first()

        purchase_return = purchases.return_purchase(
            purchase=purchase_records[1],
            quantities={purchase_items_for_return.pk: D("5.000")},
            user=owner,
            reason="Five meters failed quality inspection.",
        )

        # Fixed monthly and day-to-day operating expenses.
        expense_categories = ExpenseCategory.objects.for_business(business)
        rent_category = expense_categories.get(name="Rent")
        salaries_category = expense_categories.get(name="Salaries")
        utilities_category = expense_categories.get(name="Utilities")
        transport_category = expense_categories.get(name="Transport")
        maintenance_category = expense_categories.get(name="Maintenance")
        recurring_templates = [
            RecurringExpenseTemplate.objects.create(
                business=business,
                name="Al Hail Monthly Rent",
                category=rent_category,
                default_amount=D("950.000"),
                due_day=1,
                start_date=(today - timedelta(days=150)).replace(day=1),
                notes="Fixed monthly Al Hail rent.",
            ),
            RecurringExpenseTemplate.objects.create(
                business=business,
                name="Monthly Staff Salaries",
                category=salaries_category,
                default_amount=D("2400.000"),
                due_day=25,
                start_date=(today - timedelta(days=150)).replace(day=1),
                notes="Fixed monthly UAT payroll expense.",
            ),
        ]
        expense_services.ensure_recurring_expenses_for_range(
            business,
            today - timedelta(days=150),
            today,
        )
        variable_expense_specs = (
            (4, al_hail, utilities_category, "Electricity top-up", "28.500", cash),
            (9, mabelah, transport_category, "Customer delivery fuel", "12.000", cash),
            (16, al_hail, maintenance_category, "Sewing machine service", "45.000", bank),
            (25, mabelah, utilities_category, "Workshop electricity", "31.750", bank),
            (38, al_hail, transport_category, "Fabric collection transport", "18.000", cash),
            (52, mabelah, maintenance_category, "Steam iron repair", "22.500", card),
            (67, al_hail, utilities_category, "Internet and telephone", "36.000", bank),
            (83, mabelah, transport_category, "Courier charges", "14.250", cash),
            (101, al_hail, maintenance_category, "Register printer service", "19.000", cash),
            (119, mabelah, utilities_category, "Water charges", "11.500", bank),
        )
        for index, (days_ago, branch, category, description, amount, method) in enumerate(
            variable_expense_specs, start=1
        ):
            Expense.objects.create(
                business=business,
                expense_number=f"UAT-EXP-{index:04d}",
                expense_date=today - timedelta(days=days_ago),
                branch=branch,
                category=category,
                payee=description,
                amount=D(amount),
                payment_method=method,
                reference=f"UAT-EXP-REF-{index:04d}",
                description=description,
                status=Expense.Status.APPROVED,
                created_by=owner,
                approved_by=owner,
            )

        # Sales history is created through the canonical POS service, then its
        # business dates are backdated to represent several months of trading.
        shift_al_hail = registers.open_shift(
            business=business,
            register=al_hail_register,
            cashier=salesman_al_hail,
            opening_cash=D("100.000"),
            membership=membership_al_hail,
            notes="UAT historical Al Hail shift.",
        )
        shift_mabelah = registers.open_shift(
            business=business,
            register=mabelah_register,
            cashier=salesman_mabelah,
            opening_cash=D("100.000"),
            membership=membership_mabelah,
            notes="UAT historical Mabelah shift.",
        )

        def retail_line(index, quantity=1):
            product = retail_products[index]
            return {
                "product": product,
                "variant": None,
                "quantity": D(str(quantity)),
                "unit_price": product.sale_price,
            }

        def service_line(index, classification="adult", collection="normal"):
            product = tailoring_products[index]
            return {
                "product": product,
                "variant": None,
                "quantity": D("1.000"),
                "unit_price": product.sale_price,
                "garment_classification": classification,
                "collection_type": collection,
                "tailoring_details": {
                    "design_type": "Daraz",
                    "daraz_details": "Classic Omani traditional finish",
                    "workshop_notes": "Controlled UAT order",
                },
            }

        def fabric_line(index, meters, classification="adult", collection="normal"):
            variant = fabric_variants[index]
            charge = D("26.000") if collection == "normal" else D("42.000")
            return {
                "product": variant.product,
                "variant": variant,
                "quantity": D("1.000"),
                "unit_price": charge,
                "fabric_meter_used": D(meters),
                "garment_classification": classification,
                "collection_type": collection,
                "tailoring_details": {
                    "design_type": "Daraz",
                    "daraz_details": "UAT sample embroidery",
                },
            }

        al_hail_customers = customers[0::2]
        mabelah_customers = customers[1::2]
        sale_specs = (
            (125, al_hail, al_hail_customers[0], [service_line(0)], "cash", "delivered"),
            (112, mabelah, mabelah_customers[0], [fabric_line(0, "3.600")], "card", "delivered"),
            (101, al_hail, al_hail_customers[1], [retail_line(0, 2), retail_line(3)], "bank", ""),
            (91, mabelah, mabelah_customers[1], [service_line(4, collection="premium")], "credit", "ready"),
            (82, al_hail, al_hail_customers[2], [fabric_line(4, "3.800"), retail_line(4, 2)], "split", "delivered"),
            (73, mabelah, mabelah_customers[2], [service_line(3, "child"), retail_line(0)], "cash_card", "ready"),
            (65, al_hail, walk_in_al_hail, [retail_line(1), retail_line(2)], "cash", ""),
            (57, mabelah, mabelah_customers[3], [fabric_line(7, "2.500", "child", "premium")], "credit", "pending"),
            (49, al_hail, al_hail_customers[3], [service_line(1), retail_line(5, 2)], "card", "delivered"),
            (41, mabelah, mabelah_customers[4], [retail_line(3, 2), retail_line(4, 3)], "bank", ""),
            (34, al_hail, al_hail_customers[4], [fabric_line(2, "3.450")], "cash", "ready"),
            (28, mabelah, mabelah_customers[5], [service_line(2), retail_line(1)], "split", "pending"),
            (22, al_hail, walk_in_al_hail, [retail_line(0), retail_line(5)], "cash_card", ""),
            (17, mabelah, mabelah_customers[6], [fabric_line(5, "3.700", collection="premium")], "card", "ready"),
            (12, al_hail, al_hail_customers[5], [service_line(0), retail_line(4, 4)], "credit", "pending"),
            (8, mabelah, walk_in_mabelah, [retail_line(2), retail_line(3)], "cash", ""),
            (5, al_hail, al_hail_customers[6], [fabric_line(1, "2.350", "child"), retail_line(0)], "bank", "pending"),
            (2, mabelah, mabelah_customers[7], [service_line(4, collection="premium"), retail_line(1)], "split", "pending"),
        )
        created_sales = []
        legacy_mabelah_sale = None
        return_candidate = None
        for index, (days_ago, branch, customer, lines, payment_kind, delivery_status) in enumerate(
            sale_specs, start=1
        ):
            if branch == al_hail:
                warehouse = al_hail_warehouse
                register = al_hail_register
                shift = shift_al_hail
                cashier_user = salesman_al_hail
                membership = membership_al_hail
            else:
                warehouse = workshop
                register = mabelah_register
                shift = shift_mabelah
                cashier_user = salesman_mabelah
                membership = membership_mabelah
            total = sum(
                (D(str(line["quantity"])) * D(str(line["unit_price"])) for line in lines),
                D("0.000"),
            )
            if payment_kind == "cash":
                payments = [{"method": cash, "amount": total}]
            elif payment_kind == "card":
                payments = [{"method": card, "amount": total, "reference": f"UAT-CARD-{index:04d}"}]
            elif payment_kind == "bank":
                payments = [{"method": bank, "amount": total, "reference": f"UAT-BANK-{index:04d}"}]
            elif payment_kind == "credit":
                payments = [{"method": credit, "amount": total}]
            elif payment_kind == "split":
                paid = (total * D("0.400")).quantize(D("0.001"))
                payments = [
                    {"method": cash, "amount": paid},
                    {"method": credit, "amount": total - paid},
                ]
            else:
                cash_amount = (total * D("0.500")).quantize(D("0.001"))
                payments = [
                    {"method": cash, "amount": cash_amount},
                    {"method": card, "amount": total - cash_amount, "reference": f"UAT-MIX-{index:04d}"},
                ]
            is_tailoring = any(line["product"].is_tailoring_item for line in lines)
            sale = sales.complete_sale(
                business=business,
                branch=branch,
                warehouse=warehouse,
                cashier=cashier_user,
                customer=customer,
                items=lines,
                payments=payments,
                membership=membership,
                register=register,
                shift=shift,
                salesperson=cashier_user,
                delivery_date=(today + timedelta(days=10) if is_tailoring else None),
                priority=(Sale.Priority.HIGH if index % 5 == 0 else Sale.Priority.NORMAL),
                notes=f"Historical controlled UAT sale {index}.",
                checkout_token=f"shumukh-uat-{index:04d}",
            )
            historical_date = today - timedelta(days=days_ago)
            historical_datetime = timezone.make_aware(
                datetime.combine(historical_date, time(hour=10 + (index % 8))),
                timezone=timezone.get_current_timezone(),
            )
            Sale.objects.filter(pk=sale.pk).update(sale_date=historical_datetime)
            SalePayment.objects.filter(sale=sale).update(payment_date=historical_date)
            StockMovement.objects.filter(
                business=business,
                reference_type="Sale",
                reference_id=sale.invoice_number,
            ).update(created_at=historical_datetime)
            sale.refresh_from_db()
            if delivery_status:
                sales.set_delivery_status(
                    sale=sale,
                    status=delivery_status,
                    user=cashier_user,
                    membership=membership,
                )
            created_sales.append(sale)
            if branch == mabelah and any(
                line["product"] in tailoring_products for line in lines
            ):
                legacy_mabelah_sale = sale
            if return_candidate is None and not is_tailoring and branch == al_hail:
                return_candidate = sale

        # Workshop actual fabric is meaningful for a legacy/service tailoring line.
        legacy_item = legacy_mabelah_sale.items.filter(
            product__in=tailoring_products
        ).first()
        sales.update_actual_fabric(
            sale_item=legacy_item,
            actual_fabric_used=D("3.650"),
            user=workshop_manager,
            membership=workshop_membership,
        )

        returned_item = return_candidate.items.first()
        sale_return = sales.process_return(
            sale=return_candidate,
            items=[{"sale_item": returned_item, "quantity": D("1.000")}],
            refund_method=SaleReturn.RefundMethod.CASH,
            user=salesman_al_hail,
            reason="Retail size exchanged during UAT history.",
            restock=True,
            shift=shift_al_hail,
            membership=membership_al_hail,
        )

        # Backdate opening and purchase stock movements for meaningful history.
        historical_opening = timezone.now() - timedelta(days=180)
        StockMovement.objects.for_business(business).filter(
            movement_type="opening"
        ).update(created_at=historical_opening)
        for purchase in purchase_records:
            movement_datetime = timezone.make_aware(
                datetime.combine(purchase.purchase_date, time(hour=9)),
                timezone=timezone.get_current_timezone(),
            )
            StockMovement.objects.for_business(business).filter(
                reference_type="Purchase",
                reference_id=purchase.purchase_number,
            ).update(created_at=movement_datetime)

        totals_al_hail = registers.shift_totals(shift_al_hail)
        totals_mabelah = registers.shift_totals(shift_mabelah)
        registers.close_shift(
            shift=shift_al_hail,
            actual_cash=totals_al_hail["expected_cash"],
            user=salesman_al_hail,
            membership=membership_al_hail,
            notes="Controlled UAT shift reconciled.",
        )
        registers.close_shift(
            shift=shift_mabelah,
            actual_cash=totals_mabelah["expected_cash"],
            user=salesman_mabelah,
            membership=membership_mabelah,
            notes="Controlled UAT shift reconciled.",
        )
        Shift.objects.filter(pk=shift_al_hail.pk).update(
            opened_at=timezone.now() - timedelta(days=126),
            closed_at=timezone.now() - timedelta(days=1),
        )
        Shift.objects.filter(pk=shift_mabelah.pk).update(
            opened_at=timezone.now() - timedelta(days=113),
            closed_at=timezone.now() - timedelta(days=1),
        )

        self._verify_seed(
            business,
            workshop=workshop,
            purchase_return=purchase_return,
            sale_return=sale_return,
            recurring_templates=recurring_templates,
        )
        self.stdout.write(self.style.SUCCESS("Shumukh commercial UAT tenant created."))
        self.stdout.write(f"Owner: {OWNER_EMAIL}")
        self.stdout.write("Salesman Al Hail: sales.alhail.uat@shumukh.example")
        self.stdout.write("Salesman Mabelah: sales.mabelah.uat@shumukh.example")
        self.stdout.write("Workshop Manager: workshop.uat@shumukh.example")
        self.stdout.write(f"Password: {password}")
        self._print_counts(business)

    def _assert_safe_database(self):
        engine = settings.DATABASES["default"]["ENGINE"]
        if not settings.DEBUG:
            raise CommandError("seed_uat is disabled unless DEBUG=True.")
        if not engine.endswith("sqlite3"):
            raise CommandError(
                "seed_uat is restricted to the local SQLite database and will not "
                "run against shared or production databases."
            )

    def _ensure_branch_only_products(
        self,
        *,
        business,
        owner,
        al_hail,
        mabelah,
        al_hail_warehouse,
        mabelah_warehouse,
    ):
        """Idempotently retain one clear visibility fixture per UAT branch."""
        from apps.catalog.models import Brand, Category, Product, Unit
        from apps.inventory import services as inventory
        from apps.inventory.models import StockLevel

        category, _ = Category.objects.get_or_create(
            business=business,
            name="Retail",
            parent=None,
        )
        brand, _ = Brand.objects.get_or_create(
            business=business,
            name="Shumukh Select",
        )
        unit = Unit.objects.for_business(business).filter(name="Piece").first()
        fixtures = (
            ("Al Hail Exclusive Gift Set", "UAT-AH-ONLY", al_hail, al_hail_warehouse),
            ("Mabelah Exclusive Gift Set", "UAT-MB-ONLY", mabelah, mabelah_warehouse),
        )
        changed = False
        for name, sku, branch, warehouse in fixtures:
            product, created = Product.objects.get_or_create(
                business=business,
                sku=sku,
                defaults={
                    "name": name,
                    "category": category,
                    "brand": brand,
                    "unit": unit,
                    "purchase_price": D("4.000"),
                    "sale_price": D("10.000"),
                    "reorder_level": D("3.000"),
                    "track_inventory": True,
                },
            )
            changed = changed or created
            if not StockLevel.objects.for_business(business).filter(
                product=product,
                warehouse__branch=branch,
            ).exists():
                inventory.set_opening_stock(
                    business=business,
                    warehouse=warehouse,
                    product=product,
                    quantity=D("12.000"),
                    unit_cost=D("4.000"),
                    user=owner,
                )
                changed = True
        return changed

    def _ensure_simple_product_examples(self, *, business, owner, warehouse):
        """Create idempotent branch-onboarding examples for local UAT."""
        from apps.catalog.models import (
            Brand,
            Category,
            Product,
            ProductVariant,
            Unit,
        )
        from apps.inventory import services as inventory
        from apps.inventory.models import StockLevel

        fabrics, _ = Category.objects.get_or_create(
            business=business, name="Fabrics", parent=None
        )
        retail, _ = Category.objects.get_or_create(
            business=business, name="Retail", parent=None
        )
        meter = Unit.objects.for_business(business).get(is_meter=True)
        piece = Unit.objects.for_business(business).get(name="Piece")
        examples = (
            (
                "Hi Sofy", "UAT-HI-SOFY", fabrics, "Hi Sofy", meter,
                "Color Code", (("Color 1", "80"), ("Color 2", "60"), ("Color 3", "95")),
            ),
            (
                "Premium Kumma", "UAT-PREMIUM-KUMMA", retail, "Shumukh Select",
                piece, "Size", (("10", "8"), ("10.5", "10"), ("11", "6")),
            ),
            (
                "Royal Khanjar", "UAT-ROYAL-KHANJAR", retail, "Shumukh Select",
                piece, "Size", (("Small", "5"), ("Medium", "7"), ("Large", "4")),
            ),
            (
                "Classic Assa", "UAT-CLASSIC-ASSA", retail, "Shumukh Select",
                piece, "Size", (("Small", "9"), ("Medium", "11"), ("Large", "8")),
            ),
        )
        changed = False
        for name, sku, category, brand_name, unit, option_name, values in examples:
            brand, _ = Brand.objects.get_or_create(
                business=business, name=brand_name
            )
            product, created = Product.objects.get_or_create(
                business=business,
                sku=sku,
                defaults={
                    "name": name,
                    "category": category,
                    "brand": brand,
                    "unit": unit,
                    "product_type": Product.Type.VARIANT,
                    "purchase_price": D("2.500"),
                    "sale_price": D("0") if unit.is_meter else D("8.000"),
                    "track_inventory": True,
                    "allow_discount": not unit.is_meter,
                    "is_tailoring_item": unit.is_meter,
                },
            )
            changed = changed or created
            for index, (value, quantity) in enumerate(values, start=1):
                variant, variant_created = ProductVariant.objects.get_or_create(
                    business=business,
                    sku=f"{sku}-{index}",
                    defaults={
                        "product": product,
                        "name": value,
                        "attributes": {option_name: value},
                        "purchase_price": D("2.500"),
                        "sale_price": D("0") if unit.is_meter else D("8.000"),
                    },
                )
                if variant.product_id != product.id:
                    raise CommandError(
                        f"UAT variant SKU collision: {variant.sku}"
                    )
                changed = changed or variant_created
                if not StockLevel.objects.for_business(business).filter(
                    warehouse=warehouse,
                    product=product,
                    variant=variant,
                ).exists():
                    inventory.set_opening_stock(
                        business=business,
                        warehouse=warehouse,
                        product=product,
                        variant=variant,
                        quantity=D(quantity),
                        unit_cost=D("2.500"),
                        user=owner,
                    )
                    changed = True
        return changed

    def _upgrade_existing_branch_customers(self, business):
        """Idempotently upgrade only the command's known local UAT fixture."""
        from apps.accounts.models import Role
        from apps.branches.models import Branch
        from apps.customers.models import Customer
        from apps.customers.services import ensure_walk_in_customer
        from apps.sales.models import Sale

        try:
            al_hail = Branch.objects.for_business(business).get(code="AH")
            mabelah = Branch.objects.for_business(business).get(code="MB")
        except Branch.DoesNotExist as exc:
            raise CommandError(
                "Existing UAT business does not have the expected AH/MB branches; "
                "refusing to guess its structure."
            ) from exc

        changed = False
        al_hail_warehouse = al_hail.warehouses.filter(is_active=True).first()
        mabelah_warehouse = mabelah.warehouses.filter(is_active=True).first()
        if al_hail_warehouse is None or mabelah_warehouse is None:
            raise CommandError("Existing UAT branches require active warehouses.")
        changed = self._ensure_branch_only_products(
            business=business,
            owner=business.owner,
            al_hail=al_hail,
            mabelah=mabelah,
            al_hail_warehouse=al_hail_warehouse,
            mabelah_warehouse=mabelah_warehouse,
        ) or changed
        changed = self._ensure_simple_product_examples(
            business=business,
            owner=business.owner,
            warehouse=al_hail_warehouse,
        ) or changed
        salesman_role = Role.objects.for_business(business).filter(
            name="Salesman"
        ).first()
        if salesman_role is None:
            raise CommandError("Existing UAT business has no Salesman role.")
        required_permissions = {
            "customers.view", "customers.manage", "customers.export",
            "customers.import", "inventory.view", "inventory.export",
            "inventory.import", "inventory.adjust", "products.view",
            "products.manage", "products.import", "products.export",
            "sales.view", "sales.create",
        }
        updated_permissions = set(salesman_role.permissions or []) | required_permissions
        if updated_permissions != set(salesman_role.permissions or []):
            salesman_role.permissions = sorted(updated_permissions)
            salesman_role.save(update_fields=["permissions"])
            changed = True
        labels = (
            "Toul", "Shoulders", "Chest", "Side",
            "Sleeves", "Open", "Front", "Button",
        )
        settings_obj = business.settings
        settings_updates = []
        for index, label in enumerate(labels, start=1):
            field = f"more_option_label_{index}"
            if getattr(settings_obj, field) != label:
                setattr(settings_obj, field, label)
                settings_updates.append(field)
        if settings_updates:
            settings_obj.save(update_fields=settings_updates)
            changed = True

        ensure_walk_in_customer(business, al_hail)
        ensure_walk_in_customer(business, mabelah)

        # Preserve legacy records and their sale links. The old global walk-in
        # was used only by Al Hail in this controlled fixture; retain it as a
        # normal historical Al Hail customer now that protected branch walk-ins
        # exist.
        for customer in Customer.objects.for_business(business).filter(
            home_branch__isnull=True
        ).order_by("id"):
            sale_branches = set(
                Sale.objects.for_business(business)
                .filter(customer=customer)
                .values_list("branch_id", flat=True)
                .distinct()
            )
            if len(sale_branches) > 1:
                raise CommandError(
                    f"UAT customer {customer.code} has sales in multiple branches; "
                    "refusing to guess ownership."
                )
            if sale_branches:
                target_branch = (
                    al_hail if al_hail.id in sale_branches else mabelah
                )
            else:
                digits = "".join(character for character in customer.code if character.isdigit())
                sequence = int(digits or customer.id)
                target_branch = al_hail if sequence % 2 else mabelah
            if customer.is_walk_in:
                customer.is_walk_in = False
                customer.notes = " ".join(filter(None, (
                    customer.notes.strip(),
                    "Legacy global walk-in retained for historical UAT sales.",
                )))
            customer.home_branch = target_branch
            customer.save(update_fields=[
                "home_branch", "is_walk_in", "notes", "updated_at",
            ])
            changed = True

        def measurement_values(sequence):
            return {
                "1": str(54 + (sequence % 8)),
                "2": str(17 + (sequence % 4)),
                "3": str(36 + (sequence % 8)),
                "4": str(20 + (sequence % 5)),
                "5": str(22 + (sequence % 6)),
                "6": str(11 + (sequence % 4)),
                "7": str(16 + (sequence % 5)),
                "8": str(8 + (sequence % 3)),
            }

        for customer in Customer.objects.for_business(business).filter(
            is_walk_in=False,
            home_branch__in=(al_hail, mabelah),
        ):
            values = dict(customer.more_options or {})
            original = dict(values)
            for key, value in measurement_values(customer.id).items():
                values.setdefault(key, value)
            if values != original:
                customer.more_options = values
                customer.save(update_fields=["more_options", "updated_at"])
                changed = True

        for branch in (al_hail, mabelah):
            count = Customer.objects.for_business(business).filter(
                home_branch=branch,
                is_walk_in=False,
            ).count()
            while count < 75:
                sequence = count + 1
                code = f"{branch.code}-UAT-{sequence:04d}"
                if Customer.objects.for_business(business).filter(
                    home_branch=branch,
                    code=code,
                ).exists():
                    count += 1
                    continue
                Customer.objects.create(
                    business=business,
                    home_branch=branch,
                    code=code,
                    full_name=f"{branch.code} UAT Customer {sequence:03d}",
                    mobile=f"+968 97{branch.id % 100:02d}{sequence:04d}",
                    whatsapp=f"+968 97{branch.id % 100:02d}{sequence:04d}",
                    city="Seeb",
                    country="Oman",
                    credit_limit=D("150.000"),
                    notes="Controlled UAT branch customer record.",
                    more_options=measurement_values(sequence),
                )
                count += 1
                changed = True

        invalid_sales = Sale.objects.for_business(business).exclude(
            customer__home_branch_id=models.F("branch_id")
        )
        if invalid_sales.exists():
            raise CommandError(
                "UAT upgrade left cross-branch customer-sale relationships."
            )
        return changed

    def _verify_seed(
        self,
        business,
        *,
        workshop,
        purchase_return,
        sale_return,
        recurring_templates,
    ):
        from apps.accounts.models import Membership
        from apps.branches.models import Branch
        from apps.catalog.models import Product
        from apps.customers.models import Customer
        from apps.expenses.models import Expense
        from apps.inventory.models import StockMovement
        from apps.purchases.models import Purchase
        from apps.registers.models import CashRegister
        from apps.sales.models import Sale
        from apps.suppliers.models import Supplier, SupplierPayment

        checks = {
            "two branches": Branch.objects.for_business(business).count() == 2,
            "workshop linked to Mabelah": workshop.branch.name == "Mabelah Branch",
            "one register per branch": (
                CashRegister.objects.for_business(business).count() == 2
                and all(
                    branch.registers.count() == 1
                    for branch in Branch.objects.for_business(business)
                )
            ),
            "four users": Membership.objects.for_business(business).count() == 4,
            "required products": Product.objects.for_business(business).count() >= 14,
            "75 Al Hail customers": Customer.objects.for_business(business).filter(
                is_walk_in=False,
                home_branch__code="AH",
            ).count() == 75,
            "75 Mabelah customers": Customer.objects.for_business(business).filter(
                is_walk_in=False,
                home_branch__code="MB",
            ).count() == 75,
            "one walk-in per branch": all(
                Customer.objects.for_business(business).filter(
                    is_walk_in=True,
                    home_branch=branch,
                ).count() == 1
                for branch in Branch.objects.for_business(business).filter(
                    is_active=True
                )
            ),
            "eight suppliers": Supplier.objects.for_business(business).count() == 8,
            "purchase history": Purchase.objects.for_business(business).count() >= 6,
            "post-dated cheques": SupplierPayment.objects.for_business(business).filter(
                method="cheque", cheque_status="pending"
            ).count() >= 3,
            "purchase return": purchase_return.pk is not None,
            "daily and fixed expenses": (
                Expense.objects.for_business(business).filter(
                    recurring_template__isnull=True
                ).exists()
                and Expense.objects.for_business(business).filter(
                    recurring_template__in=recurring_templates
                ).exists()
            ),
            "historical sales": Sale.objects.for_business(business).count() >= 18,
            "branch-consistent historical sales": all(
                sale.customer.home_branch_id == sale.branch_id
                for sale in Sale.objects.for_business(business).select_related(
                    "customer"
                )
            ),
            "tailoring sales": Sale.objects.for_business(business).filter(
                items__product__is_tailoring_item=True
            ).distinct().exists(),
            "retail sales": Sale.objects.for_business(business).filter(
                items__product__is_tailoring_item=False
            ).distinct().exists(),
            "customer credit": Customer.objects.for_business(business).filter(
                balance__gt=0
            ).count() >= 4,
            "fabric history": StockMovement.objects.for_business(business).filter(
                product__unit__is_meter=True
            ).exists(),
            "sale return": sale_return.pk is not None,
        }
        failures = [label for label, passed in checks.items() if not passed]
        if failures:
            raise CommandError("UAT seed verification failed: " + ", ".join(failures))

    def _print_counts(self, business):
        from apps.accounts.models import Membership
        from apps.branches.models import Branch, Warehouse
        from apps.catalog.models import Product, ProductVariant
        from apps.customers.models import Customer
        from apps.expenses.models import Expense, RecurringExpenseTemplate
        from apps.inventory.models import StockLevel, StockMovement
        from apps.purchases.models import Purchase, PurchaseReturn
        from apps.registers.models import CashRegister, Shift
        from apps.sales.models import Sale, SaleReturn
        from apps.suppliers.models import Supplier, SupplierPayment

        counts = (
            ("Businesses", 1),
            ("Branches", Branch.objects.for_business(business).count()),
            ("Warehouses/workshop", Warehouse.objects.for_business(business).count()),
            ("Registers", CashRegister.objects.for_business(business).count()),
            ("Users/memberships", Membership.objects.for_business(business).count()),
            ("Products", Product.objects.for_business(business).count()),
            ("Product variants/colors", ProductVariant.objects.for_business(business).count()),
            ("Named customers", Customer.objects.for_business(business).filter(is_walk_in=False).count()),
            ("Suppliers", Supplier.objects.for_business(business).count()),
            ("Purchases", Purchase.objects.for_business(business).count()),
            ("Supplier payments", SupplierPayment.objects.for_business(business).count()),
            ("Purchase returns", PurchaseReturn.objects.for_business(business).count()),
            ("Recurring expense templates", RecurringExpenseTemplate.objects.for_business(business).count()),
            ("Expenses", Expense.objects.for_business(business).count()),
            ("Sales", Sale.objects.for_business(business).count()),
            ("Sales returns", SaleReturn.objects.for_business(business).count()),
            ("Open customer balances", Customer.objects.for_business(business).filter(balance__gt=0).count()),
            ("Stock levels", StockLevel.objects.for_business(business).count()),
            ("Stock movements", StockMovement.objects.for_business(business).count()),
            ("Shifts", Shift.objects.for_business(business).count()),
        )
        self.stdout.write("Created entity counts:")
        for label, count in counts:
            self.stdout.write(f"  {label}: {count}")
