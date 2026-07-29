"""Explicit, immutable model and field policies for tenant logical export."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from django.apps import apps as django_apps
from django.db import models

from apps.backups.enums import BackupScope, ProductOwner, RestoreBehavior
from apps.backups.registry import COMPONENT_REGISTRY, ComponentRegistry

from .exceptions import (
    ComponentExportValidationError,
    LogicalExportRegistryError,
    UnknownLogicalExportModel,
)

LOGICAL_MODEL_VERSION = "1.0.0"

_SUSPICIOUS_FIELD_TERMS = (
    "password",
    "token",
    "secret",
    "credential",
    "api_key",
    "private_key",
    "encryption_key",
    "hash",
    "signature",
    "session",
    "cookie",
    "checkout_token",
    "presigned",
    "authorization",
)


class IdentityKind(StrEnum):
    PUBLIC_UUID = "PUBLIC_UUID"
    TENANT_SINGLETON = "TENANT_SINGLETON"


class JsonPolicy(StrEnum):
    CANONICAL = "CANONICAL"
    SORTED_STRING_SET = "SORTED_STRING_SET"
    FLAT_STRING_MAP = "FLAT_STRING_MAP"
    INDEXED_STRING_MAP = "INDEXED_STRING_MAP"
    DENOMINATION_MAP = "DENOMINATION_MAP"


class ScalarPolicy(StrEnum):
    CANONICAL = "CANONICAL"
    OPAQUE_BUSINESS_REFERENCE = "OPAQUE_BUSINESS_REFERENCE"
    VALIDATED_UUID_SNAPSHOT = "VALIDATED_UUID_SNAPSHOT"


class OmissionReason(StrEnum):
    INTERNAL_PRIMARY_KEY = "INTERNAL_PRIMARY_KEY"
    DESTINATION_LOCAL_TOKEN = "DESTINATION_LOCAL_TOKEN"


_VALIDATOR_REFERENCE_MODEL_LABELS = {
    "stock_movement_business_reference_matches": (
        "inventory.StockTransfer",
        "inventory.StockAdjustment",
        "inventory.StockCount",
        "sales.Sale",
        "sales.SaleReturn",
        "purchases.Purchase",
        "purchases.PurchaseReturn",
    ),
    "wms_salary_assignment_snapshot_matches": (
        "wms_production.WmsProductionEntryLine",
        "wms_workforce.WmsEmployeeCategoryAssignment",
    ),
}

_VALIDATOR_MODEL_LABELS = {
    "stock_movement_business_reference_matches": "inventory.StockMovement",
    "wms_salary_assignment_snapshot_matches": "wms_salary.WmsSalaryPieceLine",
}


@dataclass(frozen=True, slots=True)
class RelationExportSpec:
    field_name: str
    target_model_label: str
    nullable: bool = False
    global_reference: bool = False


@dataclass(frozen=True, slots=True)
class ManyToManyExportSpec:
    field_name: str
    target_model_label: str


@dataclass(frozen=True, slots=True)
class JsonFieldExportSpec:
    field_name: str
    policy: JsonPolicy = JsonPolicy.CANONICAL
    allowed_values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OmittedFieldSpec:
    field_name: str
    reason: OmissionReason


def relation(
    field_name,
    target_model_label,
    *,
    nullable=False,
    global_reference=None,
):
    normalized_target = str(target_model_label)
    if global_reference is None:
        global_reference = normalized_target == "accounts.User"
    return RelationExportSpec(
        field_name=str(field_name),
        target_model_label=normalized_target,
        nullable=bool(nullable),
        global_reference=bool(global_reference),
    )


def many_to_many(field_name, target_model_label):
    return ManyToManyExportSpec(
        field_name=str(field_name),
        target_model_label=str(target_model_label),
    )


def json_field(
    field_name,
    *,
    policy=JsonPolicy.CANONICAL,
    allowed_values=(),
):
    return JsonFieldExportSpec(
        field_name=str(field_name),
        policy=JsonPolicy(policy),
        allowed_values=tuple(str(value) for value in allowed_values),
    )


def omit(
    field_name,
    reason=OmissionReason.INTERNAL_PRIMARY_KEY,
):
    return OmittedFieldSpec(
        field_name=str(field_name),
        reason=OmissionReason(reason),
    )


@dataclass(frozen=True, slots=True)
class ModelExportSpec:
    model_label: str
    component_key: str
    identity_kind: IdentityKind
    identity_field: str | None
    ownership_field: str | None
    scalar_fields: tuple[str, ...] = ()
    relation_fields: tuple[RelationExportSpec, ...] = ()
    many_to_many_fields: tuple[ManyToManyExportSpec, ...] = ()
    json_fields: tuple[JsonFieldExportSpec, ...] = ()
    media_fields: tuple[str, ...] = ()
    omitted_fields: tuple[OmittedFieldSpec, ...] = ()
    scalar_policies: tuple[tuple[str, ScalarPolicy], ...] = ()
    validators: tuple[str, ...] = ()
    model_version: str = LOGICAL_MODEL_VERSION
    export_eligible: bool = True

    def __post_init__(self):
        object.__setattr__(self, "model_label", str(self.model_label))
        object.__setattr__(self, "component_key", str(self.component_key))
        object.__setattr__(self, "identity_kind", IdentityKind(self.identity_kind))
        object.__setattr__(
            self,
            "scalar_fields",
            tuple(str(value) for value in self.scalar_fields),
        )
        object.__setattr__(
            self,
            "relation_fields",
            tuple(self.relation_fields),
        )
        object.__setattr__(
            self,
            "many_to_many_fields",
            tuple(self.many_to_many_fields),
        )
        object.__setattr__(self, "json_fields", tuple(self.json_fields))
        object.__setattr__(
            self,
            "media_fields",
            tuple(str(value) for value in self.media_fields),
        )
        object.__setattr__(self, "omitted_fields", tuple(self.omitted_fields))
        object.__setattr__(
            self,
            "scalar_policies",
            tuple((str(name), ScalarPolicy(policy)) for name, policy in self.scalar_policies),
        )
        object.__setattr__(
            self,
            "validators",
            tuple(str(value) for value in self.validators),
        )
        object.__setattr__(self, "model_version", str(self.model_version))

    @property
    def scalar_policy_map(self):
        return MappingProxyType(dict(self.scalar_policies))

    @property
    def classified_concrete_field_names(self):
        values = set(self.scalar_fields)
        values.update(item.field_name for item in self.relation_fields)
        values.update(item.field_name for item in self.json_fields)
        values.update(self.media_fields)
        values.update(item.field_name for item in self.omitted_fields)
        if self.identity_field:
            values.add(self.identity_field)
        if self.ownership_field:
            values.add(self.ownership_field)
        return frozenset(values)

    @property
    def classified_many_to_many_field_names(self):
        return frozenset(item.field_name for item in self.many_to_many_fields)


class LogicalExportRegistry:
    """Fail-closed logical schema independent from operational ORM row reads."""

    def __init__(self, specs=(), *, component_registry=COMPONENT_REGISTRY):
        if type(component_registry) is not ComponentRegistry:
            raise LogicalExportRegistryError()
        self.component_registry = component_registry
        by_model = {}
        by_component = {}
        for spec in specs:
            if type(spec) is not ModelExportSpec:
                raise LogicalExportRegistryError()
            if spec.model_label in by_model:
                raise LogicalExportRegistryError()
            if not spec.model_label or spec.model_label.strip() != spec.model_label:
                raise LogicalExportRegistryError()
            if not spec.model_version or spec.model_version.strip() != spec.model_version:
                raise LogicalExportRegistryError()
            definition = component_registry.maybe_get(spec.component_key)
            if definition is None:
                raise LogicalExportRegistryError()
            if (
                definition.restore_behavior == RestoreBehavior.NON_RESTORABLE
                or not definition.scope_eligibility
                or not spec.export_eligible
            ):
                raise LogicalExportRegistryError()
            by_model[spec.model_label] = spec
            by_component.setdefault(spec.component_key, []).append(spec)

        ordered_by_component = {}
        for component_key, component_specs in by_component.items():
            definition = component_registry.get(component_key)
            positions = {
                label: index for index, label in enumerate(definition.included_model_labels)
            }
            if any(spec.model_label not in positions for spec in component_specs):
                raise LogicalExportRegistryError()
            ordered_by_component[component_key] = tuple(
                sorted(
                    component_specs,
                    key=lambda spec: positions[spec.model_label],
                )
            )
        self._by_model = MappingProxyType(by_model)
        self._by_component = MappingProxyType(ordered_by_component)

    @property
    def specs_by_model(self):
        return self._by_model

    @property
    def specs_by_component(self):
        return self._by_component

    @property
    def specs(self):
        return tuple(self._by_model.values())

    def get(self, model_label):
        try:
            return self._by_model[str(model_label)]
        except (KeyError, TypeError, ValueError):
            raise UnknownLogicalExportModel() from None

    def maybe_get(self, model_label):
        return self._by_model.get(str(model_label))

    def for_component(self, component_key):
        try:
            return self._by_component[str(component_key)]
        except (KeyError, TypeError, ValueError):
            raise ComponentExportValidationError() from None

    def validate_complete(self, *, apps_registry=None):
        registry = apps_registry or django_apps
        expected = []
        for definition in self.component_registry.definitions.values():
            if (
                definition.restore_behavior == RestoreBehavior.NON_RESTORABLE
                or not definition.scope_eligibility
            ):
                continue
            expected.extend(definition.included_model_labels)
        if set(expected) != set(self._by_model) or len(expected) != len(self._by_model):
            raise LogicalExportRegistryError()

        declared_media = set()
        for spec in self.specs:
            self._validate_model_spec(spec, registry)
            declared_media.update(
                f"{spec.model_label}.{field_name}" for field_name in spec.media_fields
            )
        component_media = {
            field_name
            for definition in self.component_registry.definitions.values()
            if definition.scope_eligibility
            for field_name in definition.media_fields
        }
        if declared_media != component_media:
            raise LogicalExportRegistryError()
        return True

    @staticmethod
    def _validate_model_spec(spec, registry):
        if (
            type(spec) is not ModelExportSpec
            or type(spec.export_eligible) is not bool
            or any(type(item) is not RelationExportSpec for item in spec.relation_fields)
            or any(type(item) is not ManyToManyExportSpec for item in spec.many_to_many_fields)
            or any(type(item) is not JsonFieldExportSpec for item in spec.json_fields)
            or any(type(item) is not OmittedFieldSpec for item in spec.omitted_fields)
        ):
            raise LogicalExportRegistryError()
        try:
            model = registry.get_model(spec.model_label)
        except (LookupError, ValueError):
            raise LogicalExportRegistryError() from None
        concrete = tuple(model._meta.concrete_fields)
        concrete_names = tuple(field.name for field in concrete)
        if set(concrete_names) != set(spec.classified_concrete_field_names):
            raise LogicalExportRegistryError()
        if len(concrete_names) != len(spec.classified_concrete_field_names):
            raise LogicalExportRegistryError()

        buckets = (
            tuple(spec.scalar_fields),
            tuple(item.field_name for item in spec.relation_fields),
            tuple(item.field_name for item in spec.json_fields),
            tuple(spec.media_fields),
            tuple(item.field_name for item in spec.omitted_fields),
            (() if spec.identity_field is None else (spec.identity_field,)),
            (() if spec.ownership_field is None else (spec.ownership_field,)),
        )
        flattened = tuple(name for bucket in buckets for name in bucket)
        if len(flattened) != len(set(flattened)):
            raise LogicalExportRegistryError()
        primary_key_name = model._meta.pk.name
        omitted = {item.field_name: item.reason for item in spec.omitted_fields}
        if omitted.get(primary_key_name) != OmissionReason.INTERNAL_PRIMARY_KEY:
            raise LogicalExportRegistryError()
        if any(
            reason == OmissionReason.INTERNAL_PRIMARY_KEY and field_name != primary_key_name
            for field_name, reason in omitted.items()
        ):
            raise LogicalExportRegistryError()
        if primary_key_name in {
            *spec.scalar_fields,
            *(item.field_name for item in spec.relation_fields),
            *(item.field_name for item in spec.json_fields),
            *spec.media_fields,
        }:
            raise LogicalExportRegistryError()
        if spec.identity_kind == IdentityKind.PUBLIC_UUID:
            if spec.identity_field != "public_id":
                raise LogicalExportRegistryError()
            identity = model._meta.get_field(spec.identity_field)
            if not isinstance(identity, models.UUIDField) or not identity.unique:
                raise LogicalExportRegistryError()
        else:
            if spec.identity_field is not None or spec.ownership_field is None:
                raise LogicalExportRegistryError()

        if spec.model_label == "tenants.Business":
            if spec.ownership_field is not None:
                raise LogicalExportRegistryError()
        else:
            if spec.ownership_field != "business":
                raise LogicalExportRegistryError()
            ownership = model._meta.get_field(spec.ownership_field)
            if (
                not ownership.is_relation
                or ownership.related_model._meta.label != "tenants.Business"
            ):
                raise LogicalExportRegistryError()
            if spec.identity_kind == IdentityKind.TENANT_SINGLETON and not (
                ownership.one_to_one or ownership.unique
            ):
                raise LogicalExportRegistryError()

        relation_by_name = {item.field_name: item for item in spec.relation_fields}
        for field_name, relation_spec in relation_by_name.items():
            field = model._meta.get_field(field_name)
            if (
                not field.is_relation
                or not (field.many_to_one or field.one_to_one)
                or field.related_model._meta.label != relation_spec.target_model_label
                or bool(field.null) != relation_spec.nullable
            ):
                raise LogicalExportRegistryError()
            if relation_spec.global_reference != (
                relation_spec.target_model_label == "accounts.User"
            ):
                raise LogicalExportRegistryError()

        json_by_name = {item.field_name: item for item in spec.json_fields}
        if len(json_by_name) != len(spec.json_fields):
            raise LogicalExportRegistryError()
        for item in spec.json_fields:
            if not isinstance(model._meta.get_field(item.field_name), models.JSONField):
                raise LogicalExportRegistryError()
            if len(item.allowed_values) != len(set(item.allowed_values)):
                raise LogicalExportRegistryError()
            if (
                item.policy
                in {
                    JsonPolicy.SORTED_STRING_SET,
                    JsonPolicy.INDEXED_STRING_MAP,
                }
                and not item.allowed_values
            ):
                raise LogicalExportRegistryError()
            if item.policy == JsonPolicy.DENOMINATION_MAP and item.allowed_values:
                raise LogicalExportRegistryError()

        for field_name in spec.media_fields:
            if not isinstance(model._meta.get_field(field_name), models.FileField):
                raise LogicalExportRegistryError()
        for field_name in spec.scalar_fields:
            field = model._meta.get_field(field_name)
            if field.is_relation or isinstance(
                field,
                (models.JSONField, models.FileField),
            ):
                raise LogicalExportRegistryError()
        if set(dict(spec.scalar_policies)).difference(spec.scalar_fields):
            raise LogicalExportRegistryError()
        if len(spec.scalar_policies) != len(dict(spec.scalar_policies)):
            raise LogicalExportRegistryError()
        scalar_policies = spec.scalar_policy_map
        if scalar_policies.get("reference_id") == ScalarPolicy.OPAQUE_BUSINESS_REFERENCE:
            if (
                spec.model_label != "inventory.StockMovement"
                or "stock_movement_business_reference_matches" not in spec.validators
            ):
                raise LogicalExportRegistryError()
        for field_name, policy in scalar_policies.items():
            field = model._meta.get_field(field_name)
            if policy == ScalarPolicy.VALIDATED_UUID_SNAPSHOT and not isinstance(
                field, models.UUIDField
            ):
                raise LogicalExportRegistryError()

        for field_name in concrete_names:
            lowered = field_name.lower()
            if any(term in lowered for term in _SUSPICIOUS_FIELD_TERMS):
                if field_name not in omitted:
                    raise LogicalExportRegistryError()
        checkout_reason = omitted.get("checkout_token")
        if spec.model_label == "sales.Sale":
            if checkout_reason != OmissionReason.DESTINATION_LOCAL_TOKEN:
                raise LogicalExportRegistryError()
        elif checkout_reason is not None:
            raise LogicalExportRegistryError()
        if any(
            reason == OmissionReason.DESTINATION_LOCAL_TOKEN
            and not (spec.model_label == "sales.Sale" and field_name == "checkout_token")
            for field_name, reason in omitted.items()
        ):
            raise LogicalExportRegistryError()
        if (
            len(spec.validators) != len(set(spec.validators))
            or set(spec.validators).difference(_VALIDATOR_REFERENCE_MODEL_LABELS)
            or any(
                spec.model_label != _VALIDATOR_MODEL_LABELS[validator]
                for validator in spec.validators
            )
        ):
            raise LogicalExportRegistryError()

        declared_m2m = {item.field_name: item for item in spec.many_to_many_fields}
        actual_m2m = {field.name: field for field in model._meta.local_many_to_many}
        if set(declared_m2m) != set(actual_m2m):
            raise LogicalExportRegistryError()
        for field_name, item in declared_m2m.items():
            if actual_m2m[field_name].related_model._meta.label != item.target_model_label:
                raise LogicalExportRegistryError()

    def validate_component_item(self, item):
        from .pipeline import ComponentPlanItem

        if type(item) is not ComponentPlanItem:
            raise ComponentExportValidationError()
        try:
            definition = self.component_registry.get(item.key)
            expected = ComponentPlanItem.from_definition(definition)
        except (AttributeError, KeyError, LookupError, TypeError, ValueError):
            raise ComponentExportValidationError() from None
        if item != expected:
            raise ComponentExportValidationError()
        if (
            definition.restore_behavior == RestoreBehavior.NON_RESTORABLE
            or not definition.scope_eligibility
        ):
            raise ComponentExportValidationError()
        return definition

    def validate_component_plan(
        self,
        *,
        context,
        component_plan,
        require_full=False,
    ):
        from .context import BackupExecutionContext
        from .pipeline import ComponentPlanItem, order_component_definitions

        try:
            if type(context) is not BackupExecutionContext:
                raise TypeError
            if type(component_plan) is not tuple:
                raise TypeError
            items = component_plan
            if any(type(item) is not ComponentPlanItem for item in items):
                raise TypeError
            if type(context.requested_scope) is not BackupScope:
                raise TypeError
            if type(context.resolved_products) is not tuple or any(
                type(value) is not ProductOwner for value in context.resolved_products
            ):
                raise TypeError
            if len(context.resolved_products) != len(set(context.resolved_products)):
                raise ValueError
            scope = context.requested_scope
            enabled = frozenset(context.resolved_products)
            if (
                scope == BackupScope.POS
                and enabled != {ProductOwner.POS}
                or scope == BackupScope.WMS
                and enabled != {ProductOwner.WMS}
                or scope == BackupScope.ALL_ENABLED
                and (not enabled or not enabled.issubset({ProductOwner.POS, ProductOwner.WMS}))
            ):
                raise ValueError
            fully_resolved = tuple(self.component_registry.resolve(scope, enabled))
        except (AttributeError, TypeError, ValueError):
            raise ComponentExportValidationError() from None
        if not items or len({item.key for item in items}) != len(items):
            raise ComponentExportValidationError()
        definitions = tuple(self.validate_component_item(item) for item in items)
        keys = tuple(item.key for item in items)
        key_set = frozenset(keys)
        eligible_keys = frozenset(definition.key for definition in fully_resolved)
        if not key_set.issubset(eligible_keys):
            raise ComponentExportValidationError()
        for definition in definitions:
            if not set(definition.required_component_keys).issubset(key_set):
                raise ComponentExportValidationError()

        expected_order = tuple(
            definition.key
            for definition in order_component_definitions(
                definitions,
                order_attribute="export_order",
            )
        )
        if keys != expected_order:
            raise ComponentExportValidationError()
        if require_full:
            full_order = tuple(
                definition.key
                for definition in order_component_definitions(
                    fully_resolved,
                    order_attribute="export_order",
                )
            )
            if keys != full_order:
                raise ComponentExportValidationError()

        for definition in definitions:
            for spec in self.for_component(definition.key):
                references = (
                    *(item.target_model_label for item in spec.relation_fields),
                    *(item.target_model_label for item in spec.many_to_many_fields),
                    *(
                        target_label
                        for validator in spec.validators
                        for target_label in _VALIDATOR_REFERENCE_MODEL_LABELS[validator]
                    ),
                )
                for target_label in references:
                    if target_label in {"accounts.User", "tenants.Business"}:
                        continue
                    target_spec = self.maybe_get(target_label)
                    if target_spec is None or target_spec.component_key not in key_set:
                        raise ComponentExportValidationError()
        return tuple(ComponentPlanItem.from_definition(definition) for definition in definitions)


def _default_specs():
    from .logical_export_specs_pos import LOGICAL_EXPORT_SPECS_POS
    from .logical_export_specs_shared import LOGICAL_EXPORT_SPECS_SHARED
    from .logical_export_specs_wms import LOGICAL_EXPORT_SPECS_WMS

    return (
        *LOGICAL_EXPORT_SPECS_SHARED,
        *LOGICAL_EXPORT_SPECS_POS,
        *LOGICAL_EXPORT_SPECS_WMS,
    )


def build_default_logical_export_registry():
    return LogicalExportRegistry(_default_specs())


_default_registry = None


def get_logical_export_registry():
    global _default_registry
    if _default_registry is None:
        _default_registry = build_default_logical_export_registry()
    return _default_registry


class _LazyLogicalExportRegistry:
    def __getattr__(self, name):
        return getattr(get_logical_export_registry(), name)


LOGICAL_EXPORT_REGISTRY = _LazyLogicalExportRegistry()


__all__ = [
    "IdentityKind",
    "JsonPolicy",
    "LOGICAL_EXPORT_REGISTRY",
    "LogicalExportRegistry",
    "ManyToManyExportSpec",
    "ModelExportSpec",
    "OmissionReason",
    "OmittedFieldSpec",
    "RelationExportSpec",
    "ScalarPolicy",
    "build_default_logical_export_registry",
    "get_logical_export_registry",
    "json_field",
    "many_to_many",
    "omit",
    "relation",
]
