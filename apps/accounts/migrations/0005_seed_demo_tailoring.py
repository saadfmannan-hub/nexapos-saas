"""Temporarily seed a demo tenant for client demos.

This is additive and idempotent. It creates Demo Tailoring only when that
business is missing, and rollback does not delete or alter any data.
"""
from decimal import Decimal

from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, migrations
from django.utils import timezone


BUSINESS_NAME = "Demo Tailoring"
OWNER_NAME = "Demo Owner"
OWNER_EMAIL = "demo@tailoring.com"
OWNER_PASSWORD = "Demo@2026"

ALL_PERMISSION_CODES = [
    "dashboard.view",
    "reports.view",
    "reports.financial",
    "reports.export",
    "profit.view",
    "cost.view",
    "sales.view",
    "sales.create",
    "sales.void",
    "sales.delete",
    "sales.refund",
    "sales.discount",
    "sales.price_override",
    "sales.credit",
    "products.view",
    "products.manage",
    "products.import",
    "products.archive",
    "products.delete",
    "products.export",
    "inventory.view",
    "inventory.export",
    "inventory.import",
    "inventory.adjust",
    "inventory.adjust_approve",
    "inventory.transfer",
    "inventory.transfer_approve",
    "inventory.count",
    "purchases.view",
    "purchases.manage",
    "purchases.approve",
    "suppliers.view",
    "suppliers.manage",
    "customers.view",
    "customers.manage",
    "customers.payments",
    "customers.export",
    "customers.import",
    "credit.approve",
    "expenses.view",
    "expenses.manage",
    "expenses.approve",
    "registers.manage",
    "shifts.open",
    "shifts.close",
    "shifts.approve",
    "shifts.reopen",
    "users.manage",
    "branches.manage",
    "settings.manage",
    "audit.view",
    "notifications.view",
]

DEFAULT_UNITS = [
    ("Piece", "pc", False),
    ("Box", "box", False),
    ("Pack", "pack", False),
    ("Set", "set", False),
    ("Kilogram", "kg", True),
    ("Gram", "g", True),
    ("Liter", "L", True),
    ("Milliliter", "ml", True),
    ("Meter", "m", True),
    ("Hour", "hr", True),
    ("Service", "svc", False),
]

DEFAULT_PAYMENT_METHODS = [
    ("Cash", "cash"),
    ("Card", "card"),
    ("Bank Transfer", "bank"),
    ("Customer Credit", "customer_credit"),
    ("Store Credit", "store_credit"),
]

DEFAULT_EXPENSE_CATEGORIES = [
    "Rent",
    "Salaries",
    "Utilities",
    "Transport",
    "Maintenance",
    "Marketing",
    "Office Supplies",
    "Other",
]


def seed_demo_tailoring(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    Business = apps.get_model("tenants", "Business")
    BusinessSettings = apps.get_model("tenants", "BusinessSettings")
    Role = apps.get_model("accounts", "Role")
    Membership = apps.get_model("accounts", "Membership")
    Plan = apps.get_model("subscriptions", "Plan")
    Subscription = apps.get_model("subscriptions", "Subscription")
    Branch = apps.get_model("branches", "Branch")
    Warehouse = apps.get_model("branches", "Warehouse")
    Unit = apps.get_model("catalog", "Unit")
    Customer = apps.get_model("customers", "Customer")
    PaymentMethod = apps.get_model("sales", "PaymentMethod")
    CashRegister = apps.get_model("registers", "CashRegister")
    ExpenseCategory = apps.get_model("expenses", "ExpenseCategory")

    db_alias = schema_editor.connection.alias

    if Business.objects.using(db_alias).filter(name=BUSINESS_NAME).exists():
        return

    owner = User.objects.using(db_alias).filter(email__iexact=OWNER_EMAIL).first()
    if owner is None:
        owner = User(
            email=OWNER_EMAIL,
            full_name=OWNER_NAME,
            password=make_password(OWNER_PASSWORD),
            is_active=True,
            is_staff=False,
            is_superuser=False,
            is_platform_admin=False,
            email_verified=True,
        )
        try:
            owner.save(using=db_alias)
        except IntegrityError:
            owner = User.objects.using(db_alias).filter(email__iexact=OWNER_EMAIL).first()
            if owner is None:
                raise

    plan, _ = Plan.objects.using(db_alias).get_or_create(
        name="Demo Full Access",
        defaults={
            "description": "Temporary full-access plan for client demos.",
            "monthly_price": Decimal("0"),
            "annual_price": Decimal("0"),
            "currency_code": "USD",
            "trial_days": 0,
            "is_active": True,
            "sort_order": 0,
            "max_branches": 0,
            "max_users": 0,
            "max_warehouses": 0,
            "max_products": 0,
            "max_customers": 0,
            "max_monthly_invoices": 0,
            "storage_limit_mb": 0,
            "feature_purchases": True,
            "feature_expenses": True,
            "feature_returns": True,
            "feature_transfers": True,
            "feature_advanced_reports": True,
            "feature_customer_credit": True,
            "feature_api_access": True,
            "feature_white_label": True,
            "feature_custom_roles": True,
            "feature_audit_logs": True,
            "support_level": "demo",
        },
    )

    now = timezone.now()
    business = Business.objects.using(db_alias).create(
        name=BUSINESS_NAME,
        legal_name=BUSINESS_NAME,
        owner=owner,
        email=OWNER_EMAIL,
        country="United Arab Emirates",
        timezone="Asia/Dubai",
        currency_code="AED",
        currency_symbol="AED",
        currency_precision=2,
        business_category="Tailoring",
        is_active=True,
        onboarding_completed=True,
    )

    BusinessSettings.objects.using(db_alias).create(
        business=business,
        invoice_prefix="DT",
        allow_sale_without_shift=True,
    )

    owner_role = Role.objects.using(db_alias).create(
        business=business,
        name="Business Owner",
        is_owner=True,
        is_system=True,
        permissions=ALL_PERMISSION_CODES,
    )
    Membership.objects.using(db_alias).create(
        business=business,
        user=owner,
        role=owner_role,
        is_active=True,
    )

    branch = Branch.objects.using(db_alias).create(
        business=business,
        name="Main Branch",
        code="MAIN",
        email=OWNER_EMAIL,
        manager=owner,
        invoice_prefix="DT",
        is_head_office=True,
        is_active=True,
    )
    Warehouse.objects.using(db_alias).create(
        business=business,
        name="Main Warehouse",
        code="MAIN",
        branch=branch,
        manager=owner,
        is_default=True,
        is_active=True,
    )
    CashRegister.objects.using(db_alias).create(
        business=business,
        name="Main Register",
        code="REG1",
        branch=branch,
        is_active=True,
    )

    for name, abbreviation, allow_decimal in DEFAULT_UNITS:
        Unit.objects.using(db_alias).get_or_create(
            business=business,
            name=name,
            defaults={"abbreviation": abbreviation, "allow_decimal": allow_decimal},
        )

    Customer.objects.using(db_alias).get_or_create(
        business=business,
        is_walk_in=True,
        defaults={"code": "WALK-IN", "full_name": "Walk-In Customer"},
    )

    for name, kind in DEFAULT_PAYMENT_METHODS:
        PaymentMethod.objects.using(db_alias).get_or_create(
            business=business,
            name=name,
            defaults={"kind": kind, "is_system": True, "is_active": True},
        )

    for name in DEFAULT_EXPENSE_CATEGORIES:
        ExpenseCategory.objects.using(db_alias).get_or_create(
            business=business,
            name=name,
            parent=None,
            defaults={"is_active": True},
        )

    Subscription.objects.using(db_alias).create(
        business=business,
        plan=plan,
        status="active",
        billing_cycle="monthly",
        current_period_start=now,
        current_period_end=now + timezone.timedelta(days=365),
        notes="Temporary client-demo subscription seeded automatically.",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_seed_render_admin"),
        ("branches", "0001_initial"),
        ("catalog", "0002_initial"),
        ("customers", "0002_initial"),
        ("expenses", "0002_initial"),
        ("registers", "0001_initial"),
        ("sales", "0003_remove_invoicesequence_uniq_invoice_sequence_and_more"),
        ("subscriptions", "0001_initial"),
        ("tenants", "0003_business_reactivated_at_business_reactivated_by_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_demo_tailoring, migrations.RunPython.noop),
    ]
