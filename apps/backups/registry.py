"""Explicit, versioned, fail-closed backup component registry.

The registry contains metadata only.  It never discovers a model and silently
adds it to a backup.  Completeness helpers perform the opposite operation:
they report tenant-related models that have not received an explicit
classification.
"""

from dataclasses import dataclass
from types import MappingProxyType

from django.apps import apps as django_apps
from django.core.checks import Error, Tags, register

from .enums import BackupScope, ProductOwner, RestoreBehavior
from .versioning import DEFAULT_COMPONENT_VERSION


class ComponentRegistryError(ValueError):
    pass


class UnknownComponentError(ComponentRegistryError, LookupError):
    pass


class UnclassifiedTenantModelsError(ComponentRegistryError):
    def __init__(self, model_labels):
        self.model_labels = tuple(sorted(model_labels))
        super().__init__(
            "Unclassified tenant models: " + ", ".join(self.model_labels)
        )


class ScopeResolutionError(ComponentRegistryError):
    pass


@dataclass(frozen=True, slots=True)
class ComponentDefinition:
    key: str
    product_owner: str
    included_model_labels: tuple[str, ...]
    required_component_keys: tuple[str, ...] = ()
    export_order: int = 100
    import_order: int = 100
    restore_behavior: str = RestoreBehavior.REPLACEABLE
    media_fields: tuple[str, ...] = ()
    component_version: str = DEFAULT_COMPONENT_VERSION
    validator_hooks: tuple[str, ...] = ()
    scope_eligibility: tuple[str, ...] = (
        BackupScope.POS,
        BackupScope.WMS,
        BackupScope.ALL_ENABLED,
    )

    def __post_init__(self):
        if not self.key or self.key.strip() != self.key:
            raise ComponentRegistryError("Component keys must be non-empty and normalized.")
        if self.product_owner not in ProductOwner.values:
            raise ComponentRegistryError(
                f"Invalid product owner for component '{self.key}'."
            )
        if self.restore_behavior not in RestoreBehavior.values:
            raise ComponentRegistryError(
                f"Invalid restore behavior for component '{self.key}'."
            )
        invalid_scopes = set(self.scope_eligibility).difference(BackupScope.values)
        if invalid_scopes:
            raise ComponentRegistryError(
                f"Invalid scope eligibility for component '{self.key}'."
            )
        if len(set(self.included_model_labels)) != len(self.included_model_labels):
            raise ComponentRegistryError(
                f"Component '{self.key}' contains duplicate model labels."
            )
        if any(
            not label
            or label.strip() != label
            or label.count(".") != 1
            for label in self.included_model_labels
        ):
            raise ComponentRegistryError(
                f"Component '{self.key}' contains a non-canonical model label."
            )
        if (
            not self.component_version
            or self.component_version.strip() != self.component_version
        ):
            raise ComponentRegistryError(
                f"Component '{self.key}' has an invalid component version."
            )
        if len(set(self.required_component_keys)) != len(
            self.required_component_keys
        ):
            raise ComponentRegistryError(
                f"Component '{self.key}' contains duplicate dependencies."
            )
        if self.key in self.required_component_keys:
            raise ComponentRegistryError(
                f"Component '{self.key}' cannot depend on itself."
            )

    @property
    def model_labels(self):
        return self.included_model_labels


class ComponentRegistry:
    """Immutable collection of explicit component definitions."""

    def __init__(self, definitions=()):
        definitions_by_key = {}
        model_owners = {}
        for definition in definitions:
            if definition.key in definitions_by_key:
                raise ComponentRegistryError(
                    f"Duplicate component key '{definition.key}'."
                )
            for model_label in definition.included_model_labels:
                if model_label in model_owners:
                    raise ComponentRegistryError(
                        f"Model '{model_label}' is classified by both "
                        f"'{model_owners[model_label]}' and '{definition.key}'."
                    )
                model_owners[model_label] = definition.key
            definitions_by_key[definition.key] = definition

        unknown_dependencies = {
            dependency
            for definition in definitions_by_key.values()
            for dependency in definition.required_component_keys
            if dependency not in definitions_by_key
        }
        if unknown_dependencies:
            raise ComponentRegistryError(
                "Unknown component dependencies: "
                + ", ".join(sorted(unknown_dependencies))
            )
        self._assert_acyclic(definitions_by_key)

        self._definitions = MappingProxyType(definitions_by_key)
        self._model_owners = MappingProxyType(model_owners)

    @staticmethod
    def _assert_acyclic(definitions_by_key):
        visiting = set()
        visited = set()

        def visit(component_key):
            if component_key in visited:
                return
            if component_key in visiting:
                raise ComponentRegistryError(
                    "Backup component dependencies contain a cycle."
                )
            visiting.add(component_key)
            for dependency_key in definitions_by_key[
                component_key
            ].required_component_keys:
                visit(dependency_key)
            visiting.remove(component_key)
            visited.add(component_key)

        for component_key in definitions_by_key:
            visit(component_key)

    @property
    def definitions(self):
        return self._definitions

    def get(self, component_key):
        try:
            return self._definitions[component_key]
        except KeyError as exc:
            raise UnknownComponentError(
                f"Unknown backup component '{component_key}'."
            ) from exc

    def maybe_get(self, component_key):
        return self._definitions.get(component_key)

    def owner_for_model(self, model_or_label):
        label = _model_label(model_or_label)
        try:
            component_key = self._model_owners[label]
        except KeyError as exc:
            raise UnclassifiedTenantModelsError((label,)) from exc
        return self.get(component_key)

    def classified_model_labels(self):
        return frozenset(self._model_owners)

    def assert_models_classified(self, models_or_labels):
        unknown = {
            _model_label(model_or_label)
            for model_or_label in models_or_labels
            if _model_label(model_or_label) not in self._model_owners
        }
        if unknown:
            raise UnclassifiedTenantModelsError(unknown)
        return True

    def resolve(self, scope, enabled_products):
        try:
            normalized_scope = BackupScope(scope)
        except (TypeError, ValueError) as exc:
            raise ScopeResolutionError("Unknown backup scope.") from exc

        normalized_products = _normalize_products(enabled_products)
        if normalized_scope == BackupScope.POS:
            if ProductOwner.POS not in normalized_products:
                raise ScopeResolutionError("POS is not enabled for this business.")
            selected_products = frozenset({ProductOwner.POS})
        elif normalized_scope == BackupScope.WMS:
            if ProductOwner.WMS not in normalized_products:
                raise ScopeResolutionError("WMS is not enabled for this business.")
            selected_products = frozenset({ProductOwner.WMS})
        else:
            if not normalized_products:
                raise ScopeResolutionError(
                    "No backup-enabled product is available for this business."
                )
            selected_products = normalized_products

        resolved = {
            definition.key: definition
            for definition in self._definitions.values()
            if normalized_scope in definition.scope_eligibility
            and (
                definition.product_owner == ProductOwner.SHARED
                or definition.product_owner in selected_products
            )
        }
        for definition in tuple(resolved.values()):
            missing = set(definition.required_component_keys).difference(resolved)
            if missing:
                raise ComponentRegistryError(
                    f"Component '{definition.key}' has unavailable dependencies: "
                    + ", ".join(sorted(missing))
                )
        return tuple(
            sorted(
                resolved.values(),
                key=lambda item: (item.export_order, item.key),
            )
        )


def _model_label(model_or_label):
    if isinstance(model_or_label, str):
        return model_or_label
    meta = getattr(model_or_label, "_meta", None)
    if meta is None:
        raise TypeError("Expected a Django model or canonical model label.")
    return meta.label


def _normalize_products(products):
    try:
        normalized = frozenset(
            ProductOwner(getattr(product, "value", product)) for product in products
        )
    except (TypeError, ValueError) as exc:
        raise ScopeResolutionError("Unknown enabled product.") from exc
    invalid = normalized.difference({ProductOwner.POS, ProductOwner.WMS})
    if invalid:
        raise ScopeResolutionError("Only POS and WMS are backup product entitlements.")
    return normalized


SHARED_SCOPES = (
    BackupScope.POS,
    BackupScope.WMS,
    BackupScope.ALL_ENABLED,
)
POS_SCOPES = (BackupScope.POS, BackupScope.ALL_ENABLED)
WMS_SCOPES = (BackupScope.WMS, BackupScope.ALL_ENABLED)


_DEFINITIONS = (
    ComponentDefinition(
        key="shared.tenant_identity",
        product_owner=ProductOwner.SHARED,
        included_model_labels=("tenants.Business",),
        export_order=10,
        import_order=10,
        restore_behavior=RestoreBehavior.REFERENCE_ONLY,
        media_fields=("tenants.Business.logo",),
        scope_eligibility=SHARED_SCOPES,
    ),
    ComponentDefinition(
        key="shared.locations",
        product_owner=ProductOwner.SHARED,
        included_model_labels=("branches.Branch", "branches.Warehouse"),
        required_component_keys=("shared.tenant_identity",),
        export_order=20,
        import_order=20,
        restore_behavior=RestoreBehavior.DEPENDENCY_ONLY,
        scope_eligibility=SHARED_SCOPES,
    ),
    ComponentDefinition(
        key="shared.tenant_settings",
        product_owner=ProductOwner.SHARED,
        included_model_labels=("tenants.BusinessSettings",),
        required_component_keys=(
            "shared.tenant_identity",
            "shared.locations",
        ),
        export_order=30,
        import_order=30,
        restore_behavior=RestoreBehavior.DEPENDENCY_ONLY,
        scope_eligibility=SHARED_SCOPES,
    ),
    ComponentDefinition(
        key="shared.access_control",
        product_owner=ProductOwner.SHARED,
        included_model_labels=("accounts.Role", "accounts.Membership"),
        required_component_keys=(
            "shared.tenant_identity",
            "shared.locations",
        ),
        export_order=40,
        import_order=40,
        restore_behavior=RestoreBehavior.REFERENCE_ONLY,
        scope_eligibility=SHARED_SCOPES,
    ),
    ComponentDefinition(
        key="shared.subscription_control",
        product_owner=ProductOwner.SHARED,
        included_model_labels=(
            "subscriptions.Subscription",
            "subscriptions.SubscriptionPayment",
        ),
        restore_behavior=RestoreBehavior.NON_RESTORABLE,
        scope_eligibility=(),
    ),
    ComponentDefinition(
        key="shared.audit_evidence",
        product_owner=ProductOwner.SHARED,
        included_model_labels=(
            "audit.AuditLog",
            "platformadmin.SupportAccessGrant",
            "backups.BackupRecord",
            "backups.BackupSchedule",
            "backups.RestoreOperation",
            "backups.TenantOperationLock",
            "backups.BackupActivity",
            "backups.DownloadGrant",
            "backups.BackupComponent",
        ),
        restore_behavior=RestoreBehavior.NON_RESTORABLE,
        scope_eligibility=(),
    ),
    ComponentDefinition(
        key="shared.notifications",
        product_owner=ProductOwner.SHARED,
        included_model_labels=("notifications.Notification",),
        restore_behavior=RestoreBehavior.NON_RESTORABLE,
        scope_eligibility=(),
    ),
    ComponentDefinition(
        key="pos.catalog",
        product_owner=ProductOwner.POS,
        included_model_labels=(
            "catalog.Category",
            "catalog.Brand",
            "catalog.Unit",
            "catalog.TaxRate",
            "catalog.Product",
            "catalog.ProductVariant",
        ),
        required_component_keys=("shared.tenant_identity",),
        export_order=100,
        import_order=100,
        media_fields=(
            "catalog.Product.image",
            "catalog.ProductVariant.image",
        ),
        scope_eligibility=POS_SCOPES,
    ),
    ComponentDefinition(
        key="pos.customers",
        product_owner=ProductOwner.POS,
        included_model_labels=(
            "customers.CustomerGroup",
            "customers.Customer",
            "customers.CustomerPayment",
        ),
        required_component_keys=("shared.tenant_identity", "shared.locations"),
        export_order=110,
        import_order=110,
        scope_eligibility=POS_SCOPES,
    ),
    ComponentDefinition(
        key="pos.suppliers",
        product_owner=ProductOwner.POS,
        included_model_labels=("suppliers.Supplier", "suppliers.SupplierPayment"),
        required_component_keys=("shared.tenant_identity",),
        export_order=120,
        import_order=120,
        scope_eligibility=POS_SCOPES,
    ),
    ComponentDefinition(
        key="pos.inventory",
        product_owner=ProductOwner.POS,
        included_model_labels=(
            "inventory.StockLevel",
            "inventory.StockMovement",
            "inventory.StockTransfer",
            "inventory.StockTransferItem",
            "inventory.StockAdjustment",
            "inventory.StockAdjustmentItem",
            "inventory.StockCount",
            "inventory.StockCountItem",
        ),
        required_component_keys=("shared.locations", "pos.catalog"),
        export_order=130,
        import_order=130,
        scope_eligibility=POS_SCOPES,
    ),
    ComponentDefinition(
        key="pos.purchases",
        product_owner=ProductOwner.POS,
        included_model_labels=(
            "purchases.Purchase",
            "purchases.PurchaseItem",
            "purchases.PurchaseReturn",
            "purchases.PurchaseReturnItem",
        ),
        required_component_keys=("pos.catalog", "pos.inventory", "pos.suppliers"),
        export_order=140,
        import_order=140,
        media_fields=("purchases.Purchase.attachment",),
        scope_eligibility=POS_SCOPES,
    ),
    ComponentDefinition(
        key="pos.registers",
        product_owner=ProductOwner.POS,
        included_model_labels=("registers.CashRegister", "registers.Shift"),
        required_component_keys=("shared.locations",),
        export_order=150,
        import_order=150,
        scope_eligibility=POS_SCOPES,
    ),
    ComponentDefinition(
        key="pos.sales",
        product_owner=ProductOwner.POS,
        included_model_labels=(
            "sales.PaymentMethod",
            "sales.InvoiceSequence",
            "sales.Sale",
            "sales.SaleItem",
            "sales.SalePayment",
            "sales.SaleReturn",
            "sales.SaleReturnItem",
        ),
        required_component_keys=(
            "shared.locations",
            "pos.catalog",
            "pos.customers",
            "pos.inventory",
            "pos.registers",
        ),
        export_order=160,
        import_order=160,
        scope_eligibility=POS_SCOPES,
    ),
    ComponentDefinition(
        key="pos.transient_sales",
        product_owner=ProductOwner.POS,
        included_model_labels=("sales.HeldSale",),
        restore_behavior=RestoreBehavior.NON_RESTORABLE,
        scope_eligibility=(),
    ),
    ComponentDefinition(
        key="pos.expenses",
        product_owner=ProductOwner.POS,
        included_model_labels=(
            "expenses.ExpenseCategory",
            "expenses.RecurringExpenseTemplate",
            "expenses.Expense",
        ),
        required_component_keys=("shared.locations",),
        export_order=170,
        import_order=170,
        media_fields=("expenses.Expense.attachment",),
        scope_eligibility=POS_SCOPES,
    ),
    ComponentDefinition(
        key="wms.core",
        product_owner=ProductOwner.WMS,
        included_model_labels=(
            "wms_core.WmsLocation",
            "wms_core.WmsSettings",
            "wms_core.WmsRole",
            "wms_core.WmsUserAccess",
        ),
        required_component_keys=("shared.access_control", "shared.locations"),
        export_order=200,
        import_order=200,
        scope_eligibility=WMS_SCOPES,
    ),
    ComponentDefinition(
        key="wms.workforce",
        product_owner=ProductOwner.WMS,
        included_model_labels=(
            "wms_workforce.WmsEmployee",
            "wms_workforce.WmsProductionCategory",
            "wms_workforce.WmsEmployeeCategoryAssignment",
        ),
        required_component_keys=("wms.core",),
        export_order=210,
        import_order=210,
        scope_eligibility=WMS_SCOPES,
    ),
    ComponentDefinition(
        key="wms.attendance",
        product_owner=ProductOwner.WMS,
        included_model_labels=("wms_attendance.WmsAttendance",),
        required_component_keys=("wms.workforce",),
        export_order=220,
        import_order=220,
        scope_eligibility=WMS_SCOPES,
    ),
    ComponentDefinition(
        key="wms.production",
        product_owner=ProductOwner.WMS,
        included_model_labels=(
            "wms_production.WmsProductionEntry",
            "wms_production.WmsProductionEntryLine",
        ),
        required_component_keys=("wms.workforce",),
        export_order=230,
        import_order=230,
        scope_eligibility=WMS_SCOPES,
    ),
    ComponentDefinition(
        key="wms.orders",
        product_owner=ProductOwner.WMS,
        included_model_labels=(
            "wms_orders.WmsWorkshopOrder",
            "wms_orders.WmsWorkshopOrderStatusHistory",
        ),
        required_component_keys=("wms.core", "wms.workforce"),
        export_order=240,
        import_order=240,
        scope_eligibility=WMS_SCOPES,
    ),
    ComponentDefinition(
        key="wms.alterations",
        product_owner=ProductOwner.WMS,
        included_model_labels=("wms_alterations.WmsAlteration",),
        required_component_keys=("wms.orders",),
        export_order=250,
        import_order=250,
        scope_eligibility=WMS_SCOPES,
    ),
    ComponentDefinition(
        key="wms.salary",
        product_owner=ProductOwner.WMS,
        included_model_labels=(
            "wms_salary.WmsSalary",
            "wms_salary.WmsSalaryLocationSnapshot",
            "wms_salary.WmsSalaryDay",
            "wms_salary.WmsSalaryPieceLine",
        ),
        required_component_keys=(
            "wms.workforce",
            "wms.attendance",
            "wms.production",
        ),
        export_order=260,
        import_order=260,
        scope_eligibility=WMS_SCOPES,
    ),
)


COMPONENT_REGISTRY = ComponentRegistry(_DEFINITIONS)


def get_component_definition(component_key):
    return COMPONENT_REGISTRY.get(component_key)


def resolve_components(scope, enabled_products):
    return COMPONENT_REGISTRY.resolve(scope, enabled_products)


def tenant_related_model_labels(apps_registry=None):
    """Return direct and child models rooted by a tenant-owned relation.

    This is classification discovery only.  It is never used to decide what a
    backup contains.  Traversal follows forward concrete relations from a
    potential child to an already tenant-owned parent, so global objects such
    as ``accounts.User`` are not incorrectly claimed through reverse links.
    """

    registry = apps_registry or django_apps
    all_models = tuple(registry.get_models())
    labels = {
        model._meta.label
        for model in all_models
        if model._meta.label == "tenants.Business"
        or any(field.name == "business" for field in model._meta.fields)
    }
    changed = True
    while changed:
        changed = False
        for model in all_models:
            if model._meta.label in labels:
                continue
            related_labels = {
                field.related_model._meta.label
                for field in model._meta.fields
                if field.is_relation
                and field.many_to_one
                and field.related_model is not None
            }
            if related_labels.intersection(labels):
                labels.add(model._meta.label)
                changed = True
    return frozenset(labels)


def assert_models_classified(models_or_labels=None, apps_registry=None):
    candidates = (
        tenant_related_model_labels(apps_registry)
        if models_or_labels is None
        else models_or_labels
    )
    return COMPONENT_REGISTRY.assert_models_classified(candidates)


def unclassified_tenant_model_labels(apps_registry=None):
    return (
        tenant_related_model_labels(apps_registry)
        - COMPONENT_REGISTRY.classified_model_labels()
    )


@register(Tags.models)
def check_component_registry_completeness(app_configs, **kwargs):
    """Django system check that fails closed for unclassified tenant models."""

    unknown = unclassified_tenant_model_labels()
    if not unknown:
        return []
    return [
        Error(
            "Tenant-related models are missing backup registry classification: "
            + ", ".join(sorted(unknown)),
            hint=(
                "Classify every model explicitly. Do not add automatic backup "
                "inclusion."
            ),
            id="backups.E001",
        )
    ]
