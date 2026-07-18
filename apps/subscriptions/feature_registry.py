"""Immutable commercial-module registry for subscription authorization.

The registry is deliberately declarative.  It contains string metadata only,
so importing it never imports views, models, reports, or service modules.
"""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping


@dataclass(frozen=True, slots=True)
class ModuleDefinition:
    """One commercial module and its integration metadata."""

    key: str
    label: str
    category: str
    plan_field: str | None = None
    derived_from: str | None = None
    dependencies: tuple[str, ...] = ()
    implemented: bool = True
    denial_behavior: str = "forbidden"
    permissions: tuple[str, ...] = ()
    url_namespaces: tuple[str, ...] = ()
    route_names: tuple[str, ...] = ()
    reports: tuple[str, ...] = ()
    api_resources: tuple[str, ...] = ()
    documents: tuple[str, ...] = ()
    service_operations: tuple[str, ...] = ()
    navigation_entries: tuple[str, ...] = ()


_DEFINITIONS = (
    ModuleDefinition(
        key="pos_core",
        label="POS Core",
        category="NexaPOS Modules",
        plan_field="feature_sales",
    ),
    ModuleDefinition(
        key="customers",
        label="Customers",
        category="Included with POS Core",
        derived_from="pos_core",
        dependencies=("pos_core",),
    ),
    ModuleDefinition(
        key="users_staff",
        label="Users & Staff",
        category="Included with POS Core",
        derived_from="pos_core",
        dependencies=("pos_core",),
    ),
    ModuleDefinition(
        key="inventory",
        label="Inventory Management",
        category="NexaPOS Modules",
        plan_field="feature_inventory",
        dependencies=("pos_core",),
    ),
    ModuleDefinition(
        key="suppliers",
        label="Suppliers",
        category="NexaPOS Modules",
        plan_field="feature_suppliers",
        dependencies=("pos_core",),
    ),
    ModuleDefinition(
        key="purchases",
        label="Purchasing",
        category="NexaPOS Modules",
        plan_field="feature_purchases",
        dependencies=("inventory", "suppliers"),
    ),
    ModuleDefinition(
        key="stock_transfers",
        label="Stock Transfers",
        category="NexaPOS Modules",
        plan_field="feature_transfers",
        dependencies=("inventory",),
    ),
    ModuleDefinition(
        key="expenses",
        label="Expenses",
        category="NexaPOS Modules",
        plan_field="feature_expenses",
        dependencies=("pos_core",),
    ),
    ModuleDefinition(
        key="tailoring",
        label="Tailoring Operations",
        category="NexaPOS Modules",
        plan_field="feature_tailoring_module",
        dependencies=("pos_core", "inventory"),
    ),
    ModuleDefinition(
        key="customer_credit",
        label="Customer Credit",
        category="NexaPOS Modules",
        plan_field="feature_customer_credit",
        dependencies=("pos_core",),
    ),
    ModuleDefinition(
        key="advanced_reports",
        label="Advanced Reports",
        category="NexaPOS Modules",
        plan_field="feature_advanced_reports",
        dependencies=("pos_core",),
    ),
    ModuleDefinition(
        key="audit_logs",
        label="Audit Logs",
        category="NexaPOS Modules",
        plan_field="feature_audit_logs",
        dependencies=("pos_core",),
    ),
    ModuleDefinition(
        key="barcode_printing",
        label="Barcode Printing",
        category="NexaPOS Modules",
        plan_field="feature_barcode_printing",
        dependencies=("pos_core",),
    ),
    ModuleDefinition(
        key="custom_roles",
        label="Custom Roles",
        category="NexaPOS Modules",
        plan_field="feature_custom_roles",
        dependencies=("users_staff",),
    ),
    ModuleDefinition(
        key="api_access",
        label="API Access",
        category="NexaPOS Modules",
        plan_field="feature_api_access",
    ),
)

FEATURE_REGISTRY: Final[Mapping[str, ModuleDefinition]] = MappingProxyType(
    {definition.key: definition for definition in _DEFINITIONS}
)
ACTIVE_MODULE_KEYS: Final[tuple[str, ...]] = tuple(FEATURE_REGISTRY)

# These existing Plan fields are intentionally not registry modules in Phase 1.
# Keeping the list explicit makes accidental activation visible in tests/audits.
FUTURE_PLAN_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "feature_executive_dashboard",
        "feature_attendance",
        "feature_payroll",
        "feature_manufacturing",
        "feature_crm",
        "feature_loyalty_program",
        "feature_gift_cards",
        "feature_whatsapp_integration",
        "feature_kitchen_display",
        "feature_multi_currency",
        "feature_offline_mode",
        "feature_mobile_app",
        "feature_owner_dashboard_app",
        "feature_ai_reports",
        "feature_ai_forecast",
        "feature_ai_sales_prediction",
        "feature_ai_assistant",
        "feature_daily_backup",
        "feature_weekly_backup",
        "feature_priority_restore",
        "feature_email_integration",
        "feature_sms_integration",
        "feature_payment_gateway",
        "feature_white_label",
        "feature_custom_domain",
        "feature_logo_replacement",
        "feature_email_branding",
        "feature_receipt_branding",
        "feature_invoice_branding",
    }
)


def get_module_definition(module_key: str) -> ModuleDefinition | None:
    """Return a registered module, or ``None`` for an unknown key."""

    return FEATURE_REGISTRY.get(module_key)
