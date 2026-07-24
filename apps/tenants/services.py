"""Business provisioning — everything a new tenant needs on day one."""
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Membership, Role
from apps.audit import services as audit
from apps.branches.models import Branch, Warehouse
from apps.core.permissions import DEFAULT_ROLES
from apps.subscriptions.models import Plan, Subscription

from .models import Business, BusinessSettings


def get_default_plan():
    plan = Plan.objects.filter(is_active=True).order_by("sort_order", "monthly_price").first()
    if plan is None:
        # Self-heal for fresh installs: a free starter plan the platform
        # admin can edit or replace later. Not tied to any specific business.
        plan = Plan.objects.create(
            name="Starter",
            description="Default starter plan (auto-created).",
            monthly_price=0,
            annual_price=0,
            trial_days=14,
            max_branches=1,
            max_users=2,
            max_warehouses=1,
            feature_transfers=False,
            feature_advanced_reports=False,
        )
    return plan


@transaction.atomic
def provision_business(
    *,
    owner,
    name,
    country="",
    timezone_name="UTC",
    currency_code="USD",
    currency_precision=2,
    business_category="",
    phone="",
    plan=None,
    request=None,
):
    """Create a tenant with the product foundations selected by its plan.

    Legacy plans without WMS retain the established POS provisioning path.
    A WMS-enabled plan without POS Core provisions only shared tenant/login
    records plus the WMS foundation.
    """
    plan = plan or get_default_plan()
    wms_enabled = bool(plan.feature_wms)
    provision_pos = not wms_enabled or bool(plan.feature_sales)

    business = Business.objects.create(
        name=name,
        owner=owner,
        country=country,
        timezone=timezone_name or "UTC",
        currency_code=currency_code or "USD",
        currency_precision=currency_precision,
        business_category=business_category,
        phone=phone,
        email=owner.email,
    )
    BusinessSettings.objects.create(business=business)

    # A shared Membership still requires one platform role. WMS-only tenants
    # receive only the owner identity role; WMS authorization remains separate.
    owner_role = None
    role_templates = (
        DEFAULT_ROLES
        if provision_pos
        else {"Business Owner": DEFAULT_ROLES["Business Owner"]}
    )
    for role_name, spec in role_templates.items():
        role = Role.objects.create(
            business=business,
            name=role_name,
            is_owner=spec.get("is_owner", False),
            is_system=True,
            permissions=spec["permissions"],
        )
        if role.is_owner:
            owner_role = role

    Membership.objects.create(
        business=business,
        user=owner,
        role=owner_role,
    )

    if provision_pos:
        # Existing POS-only and combined-product setup remains unchanged.
        branch = Branch.objects.create(
            business=business,
            name="Head Office",
            code="HO",
            is_head_office=True,
            invoice_prefix="HO",
        )
        Warehouse.objects.create(
            business=business,
            name="Main Warehouse",
            code="MAIN",
            branch=branch,
            is_default=True,
        )

        from apps.catalog.services import create_default_catalog
        from apps.customers.services import ensure_walk_in_customer
        from apps.expenses.services import create_default_expense_categories
        from apps.registers.services import create_default_register
        from apps.sales.services import create_default_payment_methods

        create_default_catalog(business)
        ensure_walk_in_customer(business, branch)
        create_default_payment_methods(business)
        create_default_register(business, branch)
        create_default_expense_categories(business)

    # Subscription: plans can opt out of trial provisioning.
    now = timezone.now()
    status = Subscription.Status.TRIAL if plan.allow_trial else Subscription.Status.ACTIVE
    Subscription.objects.create(
        business=business,
        plan=plan,
        status=status,
        trial_ends_at=(
            now + timezone.timedelta(days=plan.trial_days or 0)
            if plan.allow_trial else None
        ),
        current_period_start=now,
    )

    if wms_enabled:
        from apps.wms_core.services import sync_wms_entitlement

        sync_wms_entitlement(
            business,
            was_enabled=False,
            is_enabled=True,
            request=request,
        )

    audit.log(
        "business.registered",
        business=business,
        user=owner,
        request=request,
        module="tenants",
        obj=business,
        description=f"Business '{business.name}' registered.",
    )
    return business
