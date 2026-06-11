"""
Central registry of business-level permission codes.

Roles store a list of these codes. Backend views enforce them through
apps.core.mixins.PermissionRequiredMixin / the `require_permission`
decorator; templates only use them to hide menus (never as the only
control).
"""
from django.utils.translation import gettext_lazy as _

PERMISSIONS = {
    # Dashboard & reports
    "dashboard.view": _("View dashboard"),
    "reports.view": _("View reports"),
    "reports.financial": _("View financial reports"),
    "reports.export": _("Export reports"),
    "profit.view": _("View profit figures"),
    "cost.view": _("View purchase costs"),
    # Sales / POS
    "sales.view": _("View sales"),
    "sales.create": _("Create sale"),
    "sales.void": _("Void sale"),
    "sales.refund": _("Refund / return sale"),
    "sales.discount": _("Apply discount"),
    "sales.price_override": _("Override price"),
    "sales.credit": _("Create credit sale"),
    # Catalog
    "products.view": _("View products"),
    "products.manage": _("Manage products"),
    "products.import": _("Import products"),
    # Inventory
    "inventory.view": _("View inventory"),
    "inventory.adjust": _("Adjust stock"),
    "inventory.adjust_approve": _("Approve stock adjustments"),
    "inventory.transfer": _("Transfer stock"),
    "inventory.transfer_approve": _("Approve stock transfers"),
    "inventory.count": _("Run physical stock counts"),
    # Purchases / suppliers
    "purchases.view": _("View purchases"),
    "purchases.manage": _("Manage purchases"),
    "purchases.approve": _("Approve purchases"),
    "suppliers.view": _("View suppliers"),
    "suppliers.manage": _("Manage suppliers"),
    # Customers
    "customers.view": _("View customers"),
    "customers.manage": _("Manage customers"),
    "customers.payments": _("Record customer payments"),
    "credit.approve": _("Approve sales over credit limit"),
    # Expenses
    "expenses.view": _("View expenses"),
    "expenses.manage": _("Record expenses"),
    "expenses.approve": _("Approve expenses"),
    # Registers / shifts
    "registers.manage": _("Manage cash registers"),
    "shifts.open": _("Open shifts"),
    "shifts.close": _("Close shifts"),
    "shifts.approve": _("Approve shift closings / cash differences"),
    "shifts.reopen": _("Reopen closed shifts"),
    # Administration
    "users.manage": _("Manage users and roles"),
    "branches.manage": _("Manage branches and warehouses"),
    "settings.manage": _("Manage business settings"),
    "audit.view": _("View audit logs"),
    "notifications.view": _("View notifications"),
}

ALL_PERMISSION_CODES = list(PERMISSIONS.keys())

# Default role templates created for every new business.
# Owner role is flagged is_owner and implicitly holds every permission.
DEFAULT_ROLES = {
    "Business Owner": {"is_owner": True, "permissions": ALL_PERMISSION_CODES},
    "Business Administrator": {
        "permissions": [c for c in ALL_PERMISSION_CODES if c != "settings.manage"]
        + ["settings.manage"],
    },
    "Branch Manager": {
        "permissions": [
            "dashboard.view", "reports.view", "reports.export", "profit.view",
            "sales.view", "sales.create", "sales.void", "sales.refund",
            "sales.discount", "sales.credit", "products.view", "inventory.view",
            "inventory.adjust", "inventory.transfer", "inventory.count",
            "customers.view", "customers.manage", "customers.payments",
            "expenses.view", "expenses.manage", "registers.manage",
            "shifts.open", "shifts.close", "shifts.approve",
            "notifications.view", "credit.approve",
        ],
    },
    "Cashier": {
        "permissions": [
            "sales.view", "sales.create", "sales.discount",
            "products.view", "customers.view", "customers.manage",
            "shifts.open", "shifts.close", "notifications.view",
        ],
    },
    "Salesperson": {
        "permissions": [
            "sales.view", "sales.create", "products.view",
            "customers.view", "customers.manage", "notifications.view",
        ],
    },
    "Accountant": {
        "permissions": [
            "dashboard.view", "reports.view", "reports.financial",
            "reports.export", "profit.view", "cost.view", "sales.view",
            "purchases.view", "suppliers.view", "customers.view",
            "customers.payments", "expenses.view", "expenses.manage",
            "expenses.approve", "notifications.view",
        ],
    },
    "Storekeeper": {
        "permissions": [
            "products.view", "inventory.view", "inventory.adjust",
            "inventory.transfer", "inventory.count", "purchases.view",
            "notifications.view",
        ],
    },
    "Purchase Manager": {
        "permissions": [
            "products.view", "inventory.view", "purchases.view",
            "purchases.manage", "purchases.approve", "suppliers.view",
            "suppliers.manage", "cost.view", "notifications.view",
        ],
    },
    "Auditor": {
        "permissions": [
            "dashboard.view", "reports.view", "reports.financial",
            "reports.export", "profit.view", "cost.view", "sales.view",
            "purchases.view", "suppliers.view", "customers.view",
            "expenses.view", "inventory.view", "audit.view",
            "notifications.view",
        ],
    },
    "Read-Only Viewer": {
        "permissions": [
            "dashboard.view", "sales.view", "products.view",
            "inventory.view", "customers.view", "notifications.view",
        ],
    },
}
