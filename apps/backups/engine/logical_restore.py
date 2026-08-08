"""Registry-authoritative logical tenant import for Phase 3B restore mutation."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from django.apps import apps as django_apps
from django.conf import settings
from django.db import connection

from apps.backups.enums import RestoreBehavior
from apps.backups.registry import COMPONENT_REGISTRY, ComponentRegistry

from .logical_export import LOGICAL_RECORD_SCHEMA
from .logical_export_registry import (
    IdentityKind,
    LogicalExportRegistry,
    get_logical_export_registry,
)
from .logical_serialization import (
    CanonicalLogicalSerializer,
    canonical_uuid,
    encode_canonical_document,
)
from .restore_exceptions import (
    RestoreImportError,
    RestorePostVerificationError,
    RestoreRelationResolutionError,
    RestoreTenantDeletionError,
)
from .restore_preflight import RestorePreflightConsumption
from .restore_workspace import RestoredPackageProvider

_MAXIMUM_RECORD_LINE_BYTES = 16 * 1024 * 1024


def _reject_constant(_value):
    raise ValueError


def _duplicate_rejecting_object(pairs):
    value = {}
    for key, item in pairs:
        if type(key) is not str or key in value:
            raise ValueError
        value[key] = item
    return value


@dataclass(frozen=True, slots=True)
class PreparedLogicalRecord:
    component_key: str
    component_version: str
    model_label: str
    identity: dict
    fields: dict


@dataclass(frozen=True, slots=True)
class PreparedLogicalRestore:
    records: tuple[PreparedLogicalRecord, ...]
    component_keys: tuple[str, ...]
    record_count: int
    media_storage_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LogicalRestoreResult:
    component_count: int
    restored_record_count: int


class LogicalRestoreEngine:
    """Parse, validate, replace, relate, and verify explicit logical records."""

    def __init__(
        self,
        *,
        registry=None,
        component_registry=COMPONENT_REGISTRY,
        component_completed_hook=None,
    ):
        selected_registry = registry or get_logical_export_registry()
        if (
            type(selected_registry) is not LogicalExportRegistry
            or type(component_registry) is not ComponentRegistry
            or selected_registry.component_registry is not component_registry
        ):
            raise RestoreImportError(issue_code="restore_registry_invalid")
        selected_registry.validate_complete()
        self.registry = selected_registry
        self.component_registry = component_registry
        self.component_completed_hook = component_completed_hook
        self.serializer = CanonicalLogicalSerializer(
            maximum_json_depth=getattr(
                settings,
                "BACKUP_LOGICAL_EXPORT_MAX_JSON_DEPTH",
                12,
            ),
            maximum_media_name_length=getattr(
                settings,
                "BACKUP_LOGICAL_EXPORT_MAX_MEDIA_NAME_LENGTH",
                500,
            ),
        )

    @staticmethod
    def _decode_line(raw):
        if (
            type(raw) is not bytes
            or not raw.endswith(b"\n")
            or len(raw) > _MAXIMUM_RECORD_LINE_BYTES
        ):
            raise RestoreImportError(issue_code="restore_record_invalid")
        try:
            value = json.loads(
                raw[:-1].decode("utf-8", errors="strict"),
                object_pairs_hook=_duplicate_rejecting_object,
                parse_constant=_reject_constant,
            )
            if encode_canonical_document(value, trailing_lf=True) != raw:
                raise ValueError
        except (TypeError, UnicodeError, ValueError, json.JSONDecodeError):
            raise RestoreImportError(issue_code="restore_record_invalid") from None
        if type(value) is not dict:
            raise RestoreImportError(issue_code="restore_record_invalid")
        return value

    def _validate_identity(self, *, spec, identity, business_public_id):
        if type(identity) is not dict:
            raise RestoreImportError(issue_code="restore_identity_invalid")
        if spec.identity_kind is IdentityKind.PUBLIC_UUID:
            if set(identity) != {"public_id"}:
                raise RestoreImportError(issue_code="restore_identity_invalid")
            try:
                public_id = canonical_uuid(identity["public_id"])
            except (TypeError, ValueError):
                raise RestoreImportError(issue_code="restore_identity_invalid") from None
            if (
                spec.model_label == "tenants.Business"
                and public_id != str(business_public_id)
            ):
                raise RestoreImportError(issue_code="restore_tenant_mismatch")
            return {"public_id": public_id}
        expected = {
            "singleton_model": spec.model_label,
            "tenant_public_id": str(business_public_id),
        }
        if identity != expected:
            raise RestoreImportError(issue_code="restore_identity_invalid")
        return dict(expected)

    def _scalar_value(self, field, value):
        try:
            converted = field.to_python(value)
            if self.serializer.scalar(field, converted) != value:
                raise ValueError
            return converted
        except (TypeError, ValueError):
            raise RestoreImportError(issue_code="restore_field_invalid") from None

    @staticmethod
    def _relation_document(value, relation_spec):
        if value is None:
            if relation_spec.nullable:
                return None
            raise RestoreRelationResolutionError(issue_code="restore_relation_missing")
        if (
            type(value) is not dict
            or set(value) != {"model", "public_id"}
            or value.get("model") != relation_spec.target_model_label
        ):
            raise RestoreRelationResolutionError(issue_code="restore_relation_invalid")
        try:
            public_id = canonical_uuid(value["public_id"])
        except (TypeError, ValueError):
            raise RestoreRelationResolutionError(issue_code="restore_relation_invalid") from None
        return {"model": relation_spec.target_model_label, "public_id": public_id}

    def _validate_fields(self, *, spec, fields):
        if type(fields) is not dict:
            raise RestoreImportError(issue_code="restore_fields_invalid")
        expected = {
            *spec.scalar_fields,
            *(item.field_name for item in spec.json_fields),
            *(item.field_name for item in spec.relation_fields),
            *(item.field_name for item in spec.many_to_many_fields),
            *spec.media_fields,
        }
        if set(fields) != expected:
            raise RestoreImportError(issue_code="restore_fields_invalid")
        model = django_apps.get_model(spec.model_label)
        for name in spec.scalar_fields:
            self._scalar_value(model._meta.get_field(name), fields[name])
        for item in spec.json_fields:
            try:
                if self.serializer.json(item, fields[item.field_name]) != fields[item.field_name]:
                    raise ValueError
            except (TypeError, ValueError):
                raise RestoreImportError(issue_code="restore_field_invalid") from None
        for item in spec.relation_fields:
            self._relation_document(fields[item.field_name], item)
        for item in spec.many_to_many_fields:
            references = fields[item.field_name]
            if type(references) is not list:
                raise RestoreRelationResolutionError(issue_code="restore_relation_invalid")
            normalized = []
            for value in references:
                if (
                    type(value) is not dict
                    or set(value) != {"model", "public_id"}
                    or value.get("model") != item.target_model_label
                ):
                    raise RestoreRelationResolutionError(issue_code="restore_relation_invalid")
                try:
                    normalized.append(canonical_uuid(value["public_id"]))
                except (TypeError, ValueError):
                    raise RestoreRelationResolutionError(
                        issue_code="restore_relation_invalid"
                    ) from None
            if normalized != sorted(set(normalized)):
                raise RestoreRelationResolutionError(issue_code="restore_relation_invalid")
        for name in spec.media_fields:
            value = fields[name]
            if value in (None, ""):
                continue
            try:
                if self.serializer.media_name(value) != value:
                    raise ValueError
            except (TypeError, ValueError):
                raise RestoreImportError(issue_code="restore_media_name_invalid") from None

    def _prepared_record(self, value, *, plan_item, business_public_id):
        if set(value) != {
            "schema",
            "component",
            "component_version",
            "model",
            "tenant_public_id",
            "identity",
            "fields",
        }:
            raise RestoreImportError(issue_code="restore_record_invalid")
        model_label = value.get("model")
        if (
            value.get("schema") != LOGICAL_RECORD_SCHEMA
            or value.get("component") != plan_item.component_key
            or value.get("component_version") != plan_item.component_version
            or value.get("tenant_public_id") != str(business_public_id)
            or type(model_label) is not str
            or model_label not in plan_item.model_sequence
        ):
            raise RestoreImportError(issue_code="restore_record_invalid")
        spec = self.registry.get(model_label)
        if spec.component_key != plan_item.component_key:
            raise RestoreImportError(issue_code="restore_record_invalid")
        identity = self._validate_identity(
            spec=spec,
            identity=value["identity"],
            business_public_id=business_public_id,
        )
        self._validate_fields(spec=spec, fields=value["fields"])
        return PreparedLogicalRecord(
            component_key=plan_item.component_key,
            component_version=plan_item.component_version,
            model_label=model_label,
            identity=identity,
            fields=value["fields"],
        )

    def prepare(self, *, consumption, package_provider):
        if (
            type(consumption) is not RestorePreflightConsumption
            or type(package_provider) is not RestoredPackageProvider
        ):
            raise RestoreImportError(issue_code="restore_preflight_invalid")
        document = consumption.document
        components = document.get("components") if type(document) is dict else None
        if type(components) is not list:
            raise RestoreImportError(issue_code="restore_manifest_invalid")
        manifest_by_key = {
            item.get("key"): item for item in components if type(item) is dict
        }
        if len(manifest_by_key) != len(components):
            raise RestoreImportError(issue_code="restore_manifest_invalid")
        records = []
        media_names = set()
        seen_identities = set()
        for plan_item in consumption.result.component_plan:
            manifest = manifest_by_key.get(plan_item.component_key)
            if type(manifest) is not dict:
                raise RestoreImportError(issue_code="restore_component_missing")
            payload = manifest.get("records")
            if type(payload) is not dict:
                raise RestoreImportError(issue_code="restore_manifest_invalid")
            path = payload.get("package_path")
            byte_count = payload.get("byte_count")
            digest = payload.get("sha256")
            expected_count = payload.get("record_count")
            component_records = []
            with package_provider.open_extracted_entry(
                context=consumption.context,
                package=consumption.package,
                package_path=path,
                expected_byte_count=byte_count,
                expected_sha256=digest,
            ) as reader:
                consumed = 0
                while True:
                    raw = reader.readline(_MAXIMUM_RECORD_LINE_BYTES + 1)
                    if not raw:
                        break
                    consumed += len(raw)
                    if consumed > byte_count:
                        raise RestoreImportError(issue_code="restore_record_stream_invalid")
                    record = self._prepared_record(
                        self._decode_line(raw),
                        plan_item=plan_item,
                        business_public_id=consumption.result.business_public_id,
                    )
                    spec = self.registry.get(record.model_label)
                    identity_key = (
                        record.model_label,
                        tuple(sorted(record.identity.items())),
                    )
                    if identity_key in seen_identities:
                        raise RestoreImportError(issue_code="restore_identity_duplicate")
                    seen_identities.add(identity_key)
                    for media_name in spec.media_fields:
                        value = record.fields[media_name]
                        if value not in (None, ""):
                            media_names.add(value)
                    component_records.append(record)
                if consumed != byte_count:
                    raise RestoreImportError(issue_code="restore_record_stream_invalid")
            if len(component_records) != expected_count or len(component_records) != plan_item.record_count:
                raise RestoreImportError(issue_code="restore_record_count_invalid")
            expected_models = {
                item["model"]: item["record_count"] for item in manifest.get("models", ())
            }
            actual_models = {
                label: sum(record.model_label == label for record in component_records)
                for label in plan_item.model_sequence
            }
            if actual_models != expected_models:
                raise RestoreImportError(issue_code="restore_model_count_invalid")
            records.extend(component_records)
        if len(records) != consumption.result.record_count:
            raise RestoreImportError(issue_code="restore_record_count_invalid")
        return PreparedLogicalRestore(
            records=tuple(records),
            component_keys=tuple(item.component_key for item in consumption.result.component_plan),
            record_count=len(records),
            media_storage_names=tuple(sorted(media_names)),
        )

    @staticmethod
    def _record_key(record):
        if "public_id" in record.identity:
            return record.model_label, record.identity["public_id"]
        return record.model_label, record.identity["tenant_public_id"]

    @staticmethod
    def _query_for_spec(*, spec, business):
        model = django_apps.get_model(spec.model_label)
        if spec.model_label == "tenants.Business":
            return model.objects.filter(public_id=business.public_id)
        if spec.ownership_field != "business":
            raise RestoreImportError(issue_code="restore_ownership_invalid")
        return model.objects.filter(business=business)

    def _object_for_record(self, *, record, spec, business):
        queryset = self._query_for_spec(spec=spec, business=business)
        try:
            if spec.identity_kind is IdentityKind.PUBLIC_UUID:
                return queryset.get(public_id=uuid.UUID(record.identity["public_id"]))
            return queryset.get()
        except (ValueError, queryset.model.DoesNotExist, queryset.model.MultipleObjectsReturned):
            raise RestorePostVerificationError(issue_code="restore_identity_unresolved") from None

    def _current_fields(self, *, obj, spec, business):
        fields = {}
        for name in spec.scalar_fields:
            fields[name] = self.serializer.scalar(
                obj._meta.get_field(name),
                getattr(obj, name),
            )
        for item in spec.json_fields:
            fields[item.field_name] = self.serializer.json(
                item,
                getattr(obj, item.field_name),
            )
        for item in spec.relation_fields:
            related = getattr(obj, item.field_name)
            if related is None:
                fields[item.field_name] = None
                continue
            if related._meta.label != item.target_model_label:
                raise RestorePostVerificationError(issue_code="restore_relation_mismatch")
            if not item.global_reference:
                if item.target_model_label == "tenants.Business":
                    if related.pk != business.pk:
                        raise RestorePostVerificationError(issue_code="restore_cross_tenant_relation")
                else:
                    target_spec = self.registry.get(item.target_model_label)
                    if getattr(related, f"{target_spec.ownership_field}_id") != business.pk:
                        raise RestorePostVerificationError(issue_code="restore_cross_tenant_relation")
            fields[item.field_name] = {
                "model": item.target_model_label,
                "public_id": canonical_uuid(related.public_id),
            }
        for item in spec.many_to_many_fields:
            references = []
            target_spec = self.registry.get(item.target_model_label)
            for related in getattr(obj, item.field_name).all().order_by("public_id"):
                if getattr(related, f"{target_spec.ownership_field}_id") != business.pk:
                    raise RestorePostVerificationError(issue_code="restore_cross_tenant_relation")
                references.append(
                    {
                        "model": item.target_model_label,
                        "public_id": canonical_uuid(related.public_id),
                    }
                )
            fields[item.field_name] = references
        for name in spec.media_fields:
            value = getattr(obj, name)
            raw_name = value.name if value is not None else None
            if raw_name in (None, ""):
                fields[name] = raw_name
            else:
                fields[name] = self.serializer.media_name(raw_name)
        return fields

    def _seed_non_mutating_components(self, *, business, prepared):
        identity_map = {}
        records_by_model = {}
        for record in prepared.records:
            records_by_model.setdefault(record.model_label, []).append(record)
        for component_key in prepared.component_keys:
            definition = self.component_registry.get(component_key)
            if definition.restore_behavior not in {
                RestoreBehavior.REFERENCE_ONLY,
                RestoreBehavior.DEPENDENCY_ONLY,
            }:
                continue
            for spec in self.registry.for_component(component_key):
                source_records = records_by_model.get(spec.model_label, [])
                queryset = self._query_for_spec(spec=spec, business=business)
                if queryset.count() != len(source_records):
                    raise RestoreImportError(
                        issue_code=(
                            "restore_dependency_count_"
                            + spec.model_label.replace(".", "_").lower()
                        )[:80]
                    )
                for record in source_records:
                    obj = self._object_for_record(record=record, spec=spec, business=business)
                    current_fields = self._current_fields(
                        obj=obj,
                        spec=spec,
                        business=business,
                    )
                    if current_fields != record.fields:
                        mismatch = next(
                            (
                                name
                                for name in sorted(set(current_fields) | set(record.fields))
                                if current_fields.get(name) != record.fields.get(name)
                            ),
                            "unknown",
                        )
                        def value_kind(value):
                            if value is None:
                                return "none"
                            if value == "":
                                return "empty"
                            return "value"

                        raise RestoreImportError(
                            issue_code=(
                                "restore_dependency_content_"
                                + spec.model_label.replace(".", "_").lower()
                                + "_"
                                + mismatch.lower()
                                + "_"
                                + value_kind(current_fields.get(mismatch))
                                + "_"
                                + value_kind(record.fields.get(mismatch))
                            )[:80]
                        )
                    identity_map[self._record_key(record)] = obj
        return identity_map

    def validate_non_mutating_dependencies(self, *, business, prepared):
        self._seed_non_mutating_components(business=business, prepared=prepared)
        return True

    def _resolve_relation(self, *, value, relation_spec, business, identity_map):
        document = self._relation_document(value, relation_spec)
        if document is None:
            return None
        key = (document["model"], document["public_id"])
        if relation_spec.global_reference:
            target = django_apps.get_model(relation_spec.target_model_label)
            try:
                return target.objects.get(public_id=uuid.UUID(document["public_id"]))
            except (ValueError, target.DoesNotExist, target.MultipleObjectsReturned):
                raise RestoreRelationResolutionError(
                    issue_code="restore_global_relation_unresolved"
                ) from None
        if relation_spec.target_model_label == "tenants.Business":
            if document["public_id"] != str(business.public_id):
                raise RestoreRelationResolutionError(issue_code="restore_cross_tenant_relation")
            return business
        related = identity_map.get(key)
        if related is None:
            raise RestoreRelationResolutionError(issue_code="restore_relation_unresolved")
        target_spec = self.registry.get(relation_spec.target_model_label)
        if getattr(related, f"{target_spec.ownership_field}_id") != business.pk:
            raise RestoreRelationResolutionError(issue_code="restore_cross_tenant_relation")
        return related

    def _create_record(self, *, record, spec, business, identity_map):
        model = django_apps.get_model(record.model_label)
        values = {}
        if spec.identity_kind is IdentityKind.PUBLIC_UUID:
            public_id = uuid.UUID(record.identity["public_id"])
            if model.objects.filter(public_id=public_id).exists():
                raise RestoreImportError(issue_code="restore_identity_collision")
            values[spec.identity_field] = public_id
        if spec.ownership_field:
            values[spec.ownership_field] = business
        auto_values = {}
        for name in spec.scalar_fields:
            field = model._meta.get_field(name)
            converted = self._scalar_value(field, record.fields[name])
            values[name] = converted
            if getattr(field, "auto_now", False) or getattr(field, "auto_now_add", False):
                auto_values[name] = converted
        for item in spec.json_fields:
            values[item.field_name] = record.fields[item.field_name]
        for item in spec.media_fields:
            values[item] = record.fields[item]
        for item in spec.relation_fields:
            values[item.field_name] = self._resolve_relation(
                value=record.fields[item.field_name],
                relation_spec=item,
                business=business,
                identity_map=identity_map,
            )
        try:
            obj = model(**values)
            obj.full_clean(validate_unique=False, validate_constraints=False)
            obj.save(force_insert=True)
            if auto_values:
                queryset = self._query_for_spec(spec=spec, business=business)
                if spec.identity_kind is IdentityKind.PUBLIC_UUID:
                    queryset = queryset.filter(public_id=values[spec.identity_field])
                if queryset.update(**auto_values) != 1:
                    raise RestoreImportError(issue_code="restore_record_write_failed")
                obj.refresh_from_db()
        except RestoreImportError:
            raise
        except Exception:
            raise RestoreImportError(issue_code="restore_record_write_failed") from None
        identity_map[self._record_key(record)] = obj
        return obj

    def mutate(self, *, business, prepared):
        if not connection.in_atomic_block:
            raise RestoreImportError(issue_code="restore_transaction_required")
        identity_map = self._seed_non_mutating_components(
            business=business,
            prepared=prepared,
        )
        records_by_component = {}
        for record in prepared.records:
            records_by_component.setdefault(record.component_key, []).append(record)
        try:
            for component_key in reversed(prepared.component_keys):
                definition = self.component_registry.get(component_key)
                if definition.restore_behavior != RestoreBehavior.REPLACEABLE:
                    continue
                for spec in reversed(self.registry.for_component(component_key)):
                    if spec.ownership_field != "business":
                        raise RestoreTenantDeletionError(issue_code="restore_delete_unscoped")
                    model = django_apps.get_model(spec.model_label)
                    model.objects.filter(business=business).delete()
        except RestoreTenantDeletionError:
            raise
        except Exception:
            raise RestoreTenantDeletionError(issue_code="restore_delete_failed") from None

        created = {}
        restored_count = 0
        for component_key in prepared.component_keys:
            definition = self.component_registry.get(component_key)
            if definition.restore_behavior != RestoreBehavior.REPLACEABLE:
                continue
            component_records = records_by_component.get(component_key, ())
            records_by_model = {}
            for record in component_records:
                records_by_model.setdefault(record.model_label, []).append(record)
            for spec in self.registry.for_component(component_key):
                for record in records_by_model.get(spec.model_label, ()):
                    created[self._record_key(record)] = self._create_record(
                        record=record,
                        spec=spec,
                        business=business,
                        identity_map=identity_map,
                    )
                    restored_count += 1
            if self.component_completed_hook is not None:
                self.component_completed_hook(component_key)

        for record in prepared.records:
            obj = created.get(self._record_key(record))
            if obj is None:
                continue
            spec = self.registry.get(record.model_label)
            for item in spec.many_to_many_fields:
                targets = []
                for reference in record.fields[item.field_name]:
                    target = identity_map.get((reference["model"], reference["public_id"]))
                    if target is None:
                        raise RestoreRelationResolutionError(
                            issue_code="restore_relation_unresolved"
                        )
                    target_spec = self.registry.get(item.target_model_label)
                    if getattr(target, f"{target_spec.ownership_field}_id") != business.pk:
                        raise RestoreRelationResolutionError(
                            issue_code="restore_cross_tenant_relation"
                        )
                    targets.append(target)
                getattr(obj, item.field_name).set(targets)
        return LogicalRestoreResult(
            component_count=len(prepared.component_keys),
            restored_record_count=restored_count,
        )

    def verify(self, *, business, prepared):
        records_by_model = {}
        for record in prepared.records:
            records_by_model.setdefault(record.model_label, []).append(record)
        for component_key in prepared.component_keys:
            definition = self.component_registry.get(component_key)
            for spec in self.registry.for_component(component_key):
                source_records = records_by_model.get(spec.model_label, [])
                queryset = self._query_for_spec(spec=spec, business=business)
                if queryset.count() != len(source_records):
                    raise RestorePostVerificationError(
                        issue_code="restore_record_count_mismatch"
                    )
                for record in source_records:
                    obj = self._object_for_record(record=record, spec=spec, business=business)
                    if self._current_fields(obj=obj, spec=spec, business=business) != record.fields:
                        raise RestorePostVerificationError(
                            issue_code="restore_logical_mismatch"
                        )
            if definition.restore_behavior == RestoreBehavior.NON_RESTORABLE:
                raise RestorePostVerificationError(issue_code="restore_component_invalid")
        return True


__all__ = [
    "LogicalRestoreEngine",
    "LogicalRestoreResult",
    "PreparedLogicalRecord",
    "PreparedLogicalRestore",
]
