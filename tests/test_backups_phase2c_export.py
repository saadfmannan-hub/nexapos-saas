"""Focused security and contract tests for Backup Engine Phase 2C."""

import inspect
import json
import os
import sqlite3
import stat
import uuid
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from unittest import mock

from django.apps import apps as django_apps
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.db.models.fields.files import FieldFile
from django.test import SimpleTestCase, override_settings

from apps.backups.engine.availability import (
    OPERATIONAL_PROVIDER_STACK_READY,
    SQLITE_SNAPSHOT_PROVIDER_READY,
    TENANT_LOGICAL_EXPORT_PROVIDER_READY,
    get_engine_capability,
    real_execution_available,
)
from apps.backups.engine.checks import (
    check_logical_export_policy_settings,
    check_logical_export_registry,
)
from apps.backups.engine.contracts import (
    ComponentExporter,
    ComponentExportReference,
    ComponentExportRequest,
    ComponentExportResult,
)
from apps.backups.engine.exceptions import (
    BackupScopeNotAllowed,
    ComponentExportCleanupError,
    ComponentExportCreationError,
    ComponentExportLimitExceeded,
    ComponentExportNotFound,
    ComponentExportTimeout,
    ComponentExportValidationError,
    LogicalExportEngineError,
    LogicalExportPolicyError,
    LogicalExportRegistryError,
    LogicalReferenceResolutionError,
    SnapshotCleanupAfterExportError,
    SnapshotTimeout,
    SnapshotValidationError,
    TenantIsolationViolation,
    UnknownBackupComponent,
    UnknownLogicalExportModel,
    UnsafeMediaReference,
    UnsupportedLogicalExportField,
)
from apps.backups.engine.logical_export import (
    DETERMINISTIC_ORDERING_VERSION,
    LOGICAL_EXPORT_PROVIDER_IDENTIFIER,
    MEDIA_INDEX_FILE_NAME,
    RECORDS_FILE_NAME,
    ComponentExportStream,
    SQLiteLogicalComponentExporter,
    _BoundedAtomicFile,
    export_snapshot_components,
)
from apps.backups.engine.logical_export_policy import (
    MAXIMUM_LOGICAL_FETCH_MEMORY_BYTES,
    LogicalExportPolicy,
)
from apps.backups.engine.logical_export_registry import (
    IdentityKind,
    JsonPolicy,
    LogicalExportRegistry,
    OmissionReason,
    ScalarPolicy,
    build_default_logical_export_registry,
    get_logical_export_registry,
    omit,
)
from apps.backups.engine.logical_serialization import (
    LOGICAL_MEDIA_REFERENCE_SCHEMA,
    LOGICAL_RECORD_SCHEMA,
    CanonicalLogicalSerializer,
    canonical_date,
    canonical_datetime,
    canonical_decimal,
    canonical_json,
    canonical_time,
    canonical_uuid,
    validate_media_storage_name,
)
from apps.backups.engine.orchestration import prepare_backup_execution
from apps.backups.engine.pipeline import (
    PipelineStage,
    PipelineStageState,
    planning_stage_reports,
    resolve_component_plan,
)
from apps.backups.engine.sqlite_snapshot import (
    SQLiteSnapshotProvider,
    SQLiteSnapshotReader,
)
from apps.backups.engine.workspace import (
    BackupWorkspaceManager,
    WorkspaceArea,
)
from apps.backups.enums import (
    BackupScope,
    ProductOwner,
    RestoreBehavior,
)
from apps.backups.registry import COMPONENT_REGISTRY
from apps.backups.tasks import execute_backup

from .test_backups_phase1 import BackupPhase1TestCase
from .test_backups_phase2b_snapshot import SQLiteSnapshotTestCase


def _default_policy(**changes):
    values = {
        "fetch_batch_size": 2,
        "component_timeout_seconds": 30.0,
        "maximum_records_bytes": 8 * 1024 * 1024,
        "maximum_media_index_bytes": 1024 * 1024,
        "maximum_row_input_bytes": 1024 * 1024,
        "maximum_json_depth": 20,
        "maximum_media_name_length": 1024,
    }
    values.update(changes)
    return LogicalExportPolicy(**values).validated()


def _replace_registry_spec(registry, model_label, replacement):
    return LogicalExportRegistry(
        tuple(replacement if spec.model_label == model_label else spec for spec in registry.specs)
    )


class _TrackingCursor:
    def __init__(self, batches=(), *, error=None, close_error=None):
        self.batches = list(batches)
        self.error = error
        self.close_error = close_error
        self.fetchmany_sizes = []
        self.fetchall_called = False
        self.closed = False

    def fetchmany(self, size):
        self.fetchmany_sizes.append(size)
        if self.error is not None:
            raise self.error
        if not self.batches:
            return []
        return self.batches.pop(0)

    def fetchall(self):
        self.fetchall_called = True
        raise AssertionError("iter_query must not use fetchall")

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _TrackingConnection:
    def __init__(self, cursor):
        self.cursor = cursor
        self.executions = []

    def execute(self, sql, parameters=()):
        self.executions.append((sql, tuple(parameters)))
        return self.cursor


class _CloseAbortConnection:
    def __init__(self, wrapped, close_error):
        self.wrapped = wrapped
        self.close_error = close_error

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    def close(self):
        self.wrapped.close()
        raise self.close_error


class _StaticRelationReader:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def first(self, sql, parameters=()):
        self.calls.append((sql, tuple(parameters)))
        return self.row


class _RecordingBatchExporter(ComponentExporter):
    def __init__(
        self,
        *,
        registry,
        snapshot_provider,
        fail_at=None,
        abort=None,
        cleanup_result=True,
        cleanup_error=None,
        result_changes=None,
        fixed_reference=None,
    ):
        self.registry = registry
        self.snapshot_provider = snapshot_provider
        self.fail_at = fail_at
        self.abort = abort
        self.cleanup_result = cleanup_result
        self.cleanup_error = cleanup_error
        self.result_changes = dict(result_changes or {})
        self.fixed_reference = fixed_reference
        self.requests = []
        self.cleanup_calls = []

    def export_component(self, request):
        index = len(self.requests)
        self.requests.append(request)
        if index == self.fail_at:
            if self.abort is not None:
                raise self.abort
            raise ComponentExportCreationError()
        model_counts = tuple(
            (spec.model_label, 0) for spec in self.registry.for_component(request.component.key)
        )
        result = ComponentExportResult(
            component_key=request.component.key,
            reference=self.fixed_reference or ComponentExportReference(uuid.uuid4()),
            row_count=0,
            media_count=0,
            deterministic_ordering_version=DETERMINISTIC_ORDERING_VERSION,
            model_counts=model_counts,
            component_version=request.component.component_version,
            record_schema_version=LOGICAL_RECORD_SCHEMA,
            created_at=datetime.now(UTC),
            provider_identifier=LOGICAL_EXPORT_PROVIDER_IDENTIFIER,
        )
        return replace(result, **self.result_changes)

    def cleanup_component_export(self, *, context, reference):
        self.cleanup_calls.append((context.workspace_reference, reference))
        if self.cleanup_error is not None:
            raise self.cleanup_error
        return self.cleanup_result


class LogicalExportRegistryTests(SimpleTestCase):
    """A. Registry/schema completeness and locked sensitive-field policy."""

    def setUp(self):
        super().setUp()
        self.registry = build_default_logical_export_registry()

    def test_default_registry_is_complete_and_has_one_spec_per_eligible_model(self):
        self.assertTrue(self.registry.validate_complete())
        expected = tuple(
            model_label
            for definition in COMPONENT_REGISTRY.definitions.values()
            if definition.scope_eligibility
            and definition.restore_behavior != RestoreBehavior.NON_RESTORABLE
            for model_label in definition.included_model_labels
        )
        self.assertEqual(set(self.registry.specs_by_model), set(expected))
        self.assertEqual(len(self.registry.specs), len(expected))

    def test_every_concrete_and_many_to_many_field_is_classified_exactly_once(self):
        for spec in self.registry.specs:
            with self.subTest(model=spec.model_label):
                model = django_apps.get_model(spec.model_label)
                concrete = {field.name for field in model._meta.concrete_fields}
                self.assertEqual(spec.classified_concrete_field_names, concrete)
                self.assertEqual(
                    spec.classified_many_to_many_field_names,
                    {field.name for field in model._meta.local_many_to_many},
                )

    def test_all_eligible_media_fields_are_explicit_and_cross_validated(self):
        expected = {
            f"{model._meta.label}.{field.name}"
            for model in django_apps.get_models()
            for field in model._meta.concrete_fields
            if isinstance(field, models.FileField)
            and self.registry.maybe_get(model._meta.label) is not None
        }
        actual = {
            f"{spec.model_label}.{field_name}"
            for spec in self.registry.specs
            for field_name in spec.media_fields
        }
        self.assertEqual(actual, expected)
        self.assertEqual(
            {
                "tenants.Business.logo",
                "catalog.Product.image",
                "catalog.ProductVariant.image",
                "purchases.Purchase.attachment",
                "expenses.Expense.attachment",
            },
            actual,
        )

    def test_unknown_duplicate_and_cross_component_models_fail_closed(self):
        with self.assertRaises(UnknownLogicalExportModel):
            self.registry.get("future.Unknown")
        spec = self.registry.get("catalog.Category")
        with self.assertRaises(LogicalExportRegistryError):
            LogicalExportRegistry((spec, spec))
        moved = replace(spec, component_key="pos.customers")
        with self.assertRaises(LogicalExportRegistryError):
            LogicalExportRegistry((spec, moved))

    def test_unknown_and_non_restorable_components_are_not_export_specs(self):
        spec = self.registry.get("catalog.Category")
        with self.assertRaises(LogicalExportRegistryError):
            LogicalExportRegistry((replace(spec, component_key="unknown.component"),))
        held = replace(
            spec,
            model_label="sales.HeldSale",
            component_key="pos.transient_sales",
        )
        with self.assertRaises(LogicalExportRegistryError):
            LogicalExportRegistry((held,))
        self.assertIsNone(self.registry.maybe_get("sales.HeldSale"))

    def test_missing_concrete_m2m_media_and_sensitive_policies_fail_completeness(self):
        product = self.registry.get("catalog.Product")
        missing_scalar = replace(
            product,
            scalar_fields=tuple(name for name in product.scalar_fields if name != "name"),
        )
        with self.assertRaises(LogicalExportRegistryError):
            _replace_registry_spec(
                self.registry,
                product.model_label,
                missing_scalar,
            ).validate_complete()

        membership = self.registry.get("accounts.Membership")
        missing_m2m = replace(membership, many_to_many_fields=())
        with self.assertRaises(LogicalExportRegistryError):
            _replace_registry_spec(
                self.registry,
                membership.model_label,
                missing_m2m,
            ).validate_complete()

        image_as_omission = replace(
            product,
            media_fields=(),
            omitted_fields=(
                *product.omitted_fields,
                omit("image", OmissionReason.INTERNAL_PRIMARY_KEY),
            ),
        )
        with self.assertRaises(LogicalExportRegistryError):
            _replace_registry_spec(
                self.registry,
                product.model_label,
                image_as_omission,
            ).validate_complete()

        sale = self.registry.get("sales.Sale")
        checkout_as_scalar = replace(
            sale,
            scalar_fields=(*sale.scalar_fields, "checkout_token"),
            omitted_fields=tuple(
                item for item in sale.omitted_fields if item.field_name != "checkout_token"
            ),
        )
        with self.assertRaises(LogicalExportRegistryError):
            _replace_registry_spec(
                self.registry,
                sale.model_label,
                checkout_as_scalar,
            ).validate_complete()

    def test_primary_key_and_checkout_token_omission_policies_are_locked(self):
        product = self.registry.get("catalog.Product")
        primary_key_name = django_apps.get_model(product.model_label)._meta.pk.name
        primary_key_omission = next(
            item for item in product.omitted_fields if item.field_name == primary_key_name
        )
        self.assertEqual(
            primary_key_omission,
            omit(primary_key_name, OmissionReason.INTERNAL_PRIMARY_KEY),
        )

        primary_key_as_scalar = replace(
            product,
            scalar_fields=(*product.scalar_fields, primary_key_name),
            omitted_fields=tuple(
                item for item in product.omitted_fields if item.field_name != primary_key_name
            ),
        )
        false_primary_key_omission = replace(
            product,
            omitted_fields=tuple(
                (
                    omit(primary_key_name, OmissionReason.DESTINATION_LOCAL_TOKEN)
                    if item.field_name == primary_key_name
                    else item
                )
                for item in product.omitted_fields
            ),
        )
        product_name_as_token = replace(
            product,
            scalar_fields=tuple(
                field_name for field_name in product.scalar_fields if field_name != "name"
            ),
            omitted_fields=(
                *product.omitted_fields,
                omit("name", OmissionReason.DESTINATION_LOCAL_TOKEN),
            ),
        )
        for label, replacement in (
            ("primary_key_as_scalar", primary_key_as_scalar),
            ("false_primary_key_omission", false_primary_key_omission),
            ("destination_token_on_other_field", product_name_as_token),
        ):
            with self.subTest(case=label):
                with self.assertRaises(LogicalExportRegistryError):
                    _replace_registry_spec(
                        self.registry,
                        product.model_label,
                        replacement,
                    ).validate_complete()

        sale = self.registry.get("sales.Sale")
        checkout_omissions = tuple(
            item for item in sale.omitted_fields if item.field_name == "checkout_token"
        )
        self.assertEqual(
            checkout_omissions,
            (omit("checkout_token", OmissionReason.DESTINATION_LOCAL_TOKEN),),
        )
        checkout_as_scalar = replace(
            sale,
            scalar_fields=(*sale.scalar_fields, "checkout_token"),
            omitted_fields=tuple(
                item for item in sale.omitted_fields if item.field_name != "checkout_token"
            ),
        )
        checkout_with_false_reason = replace(
            sale,
            omitted_fields=tuple(
                (
                    omit("checkout_token", OmissionReason.INTERNAL_PRIMARY_KEY)
                    if item.field_name == "checkout_token"
                    else item
                )
                for item in sale.omitted_fields
            ),
        )
        for label, replacement in (
            ("checkout_token_as_scalar", checkout_as_scalar),
            ("checkout_token_false_reason", checkout_with_false_reason),
        ):
            with self.subTest(case=label):
                with self.assertRaises(LogicalExportRegistryError):
                    _replace_registry_spec(
                        self.registry,
                        sale.model_label,
                        replacement,
                    ).validate_complete()

    def test_registry_definitions_and_specs_are_immutable_and_versioned(self):
        self.assertTrue(self.registry.specs)
        with self.assertRaises(TypeError):
            self.registry.specs_by_model["future.Model"] = object()
        with self.assertRaises(FrozenInstanceError):
            self.registry.get("catalog.Category").model_label = "changed.Model"
        for spec in self.registry.specs:
            with self.subTest(model=spec.model_label):
                self.assertTrue(spec.model_version)
                self.assertEqual(spec.model_version, spec.model_version.strip())
                definition = COMPONENT_REGISTRY.get(spec.component_key)
                self.assertTrue(definition.component_version)

    def test_locked_identity_json_reference_and_omission_policies_are_explicit(self):
        for spec in self.registry.specs:
            with self.subTest(model=spec.model_label):
                if spec.model_label == "tenants.BusinessSettings":
                    self.assertEqual(spec.identity_kind, IdentityKind.TENANT_SINGLETON)
                    self.assertIsNone(spec.identity_field)
                else:
                    self.assertEqual(spec.identity_kind, IdentityKind.PUBLIC_UUID)
                    self.assertEqual(spec.identity_field, "public_id")
                self.assertIn(
                    ("id", OmissionReason.INTERNAL_PRIMARY_KEY),
                    tuple((item.field_name, item.reason) for item in spec.omitted_fields),
                )
        sale = self.registry.get("sales.Sale")
        self.assertIn(
            ("checkout_token", OmissionReason.DESTINATION_LOCAL_TOKEN),
            tuple((item.field_name, item.reason) for item in sale.omitted_fields),
        )
        movement = self.registry.get("inventory.StockMovement")
        self.assertEqual(
            movement.scalar_policy_map["reference_id"],
            ScalarPolicy.OPAQUE_BUSINESS_REFERENCE,
        )

    def test_model_order_matches_component_definition_order(self):
        for component_key, specs in self.registry.specs_by_component.items():
            with self.subTest(component=component_key):
                definition = COMPONENT_REGISTRY.get(component_key)
                self.assertEqual(
                    tuple(spec.model_label for spec in specs),
                    tuple(
                        label
                        for label in definition.included_model_labels
                        if self.registry.maybe_get(label) is not None
                    ),
                )


class CanonicalLogicalSerializationTests(SimpleTestCase):
    """E/F/G. Canonical identities, scalar values, JSON and media names."""

    def setUp(self):
        super().setUp()
        self.serializer = CanonicalLogicalSerializer(
            maximum_json_depth=4,
            maximum_media_name_length=128,
        )

    def test_uuid_decimal_date_datetime_and_time_formats_are_exact(self):
        value = uuid.UUID("AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE")
        self.assertEqual(
            canonical_uuid(value),
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )
        self.assertEqual(
            canonical_decimal("12.340", decimal_places=3),
            "12.340",
        )
        self.assertEqual(canonical_date(date(2026, 7, 29)), "2026-07-29")
        self.assertEqual(
            canonical_datetime(datetime(2026, 7, 29, 3, 4, 5, 6789, tzinfo=UTC)),
            "2026-07-29T03:04:05.006789Z",
        )
        self.assertEqual(canonical_time(time(3, 4, 5, 6789)), "03:04:05.006789")

    def test_decimal_never_uses_float_or_exponent_fallback(self):
        for invalid in (1.25, True, None, "NaN", "Infinity", "1E+100"):
            with self.subTest(value=invalid):
                with self.assertRaises(UnsupportedLogicalExportField):
                    canonical_decimal(invalid, decimal_places=3)
        with self.assertRaises(UnsupportedLogicalExportField):
            canonical_decimal("1.2345", decimal_places=3)

    def test_canonical_scalars_do_not_use_custom_string_fallbacks(self):
        class StringTrap:
            def __init__(self, value):
                self.value = value

            def __str__(self):
                return self.value

        cases = (
            (
                canonical_uuid,
                (StringTrap("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),),
                {},
            ),
            (canonical_date, (StringTrap("2026-07-29"),), {}),
            (canonical_time, (StringTrap("03:04:05"),), {}),
            (
                canonical_decimal,
                (StringTrap("1.000"),),
                {"decimal_places": 3},
            ),
        )
        for function, args, kwargs in cases:
            with self.subTest(function=function.__name__):
                with self.assertRaises(UnsupportedLogicalExportField):
                    function(*args, **kwargs)

    def test_scalar_boolean_integer_null_and_string_policies_are_strict(self):
        product = django_apps.get_model("catalog.Product")
        sale = django_apps.get_model("sales.Sale")
        self.assertTrue(
            self.serializer.scalar(
                product._meta.get_field("track_inventory"),
                1,
            )
        )
        self.assertEqual(
            self.serializer.scalar(sale._meta.get_field("reprint_count"), 4),
            4,
        )
        self.assertIsNone(
            self.serializer.scalar(
                product._meta.get_field("price_includes_tax"),
                None,
            )
        )
        self.assertEqual(
            self.serializer.scalar(product._meta.get_field("name"), "قماش"),
            "قماش",
        )
        with self.assertRaises(UnsupportedLogicalExportField):
            self.serializer.scalar(product._meta.get_field("name"), object())
        for invalid in (
            0.0,
            1.0,
            Decimal(0),
            Decimal(1),
            "1",
            2,
        ):
            with self.subTest(boolean=invalid):
                with self.assertRaises(UnsupportedLogicalExportField):
                    self.serializer.scalar(
                        product._meta.get_field("track_inventory"),
                        invalid,
                    )
        for invalid in (False, True, Decimal(1), 1.0, 1.9, "4"):
            with self.subTest(integer=invalid):
                with self.assertRaises(UnsupportedLogicalExportField):
                    self.serializer.scalar(
                        sale._meta.get_field("reprint_count"),
                        invalid,
                    )

    def test_json_keys_are_sorted_lists_preserved_and_nan_rejected(self):
        value = {"z": [3, 2, 1], "a": {"y": True, "x": None}}
        self.assertEqual(
            list(canonical_json(value, maximum_depth=5)),
            ["a", "z"],
        )
        self.assertEqual(
            canonical_json(value, maximum_depth=5)["z"],
            [3, 2, 1],
        )
        for invalid in (
            '{"value": NaN}',
            '{"value": Infinity}',
            {1: "non-string key"},
            {"value": object()},
        ):
            with self.subTest(value=repr(invalid)):
                with self.assertRaises(UnsupportedLogicalExportField):
                    canonical_json(invalid, maximum_depth=5)
        with self.assertRaises(UnsupportedLogicalExportField):
            canonical_json('{"duplicate":1,"duplicate":2}', maximum_depth=5)
        with self.assertRaises(UnsupportedLogicalExportField):
            canonical_json({"binary_float": 1.25}, maximum_depth=5)
        exact = canonical_json('{"number":1.2300}', maximum_depth=5)
        self.assertEqual(
            self.serializer.encode_line(exact),
            b'{"number":1.23}\n',
        )
        exact_numbers = canonical_json(
            '{"integer":9007199254740993,'
            '"fraction":0.12345678901234567890123456789,'
            '"exponent":1.2300e30}',
            maximum_depth=5,
        )
        self.assertEqual(
            self.serializer.encode_line(exact_numbers),
            (
                b'{"exponent":1.23e30,'
                b'"fraction":0.12345678901234567890123456789,'
                b'"integer":9007199254740993}\n'
            ),
        )
        for hidden_identifier in (
            "productId",
            "productID",
            "product-id",
            "product.id",
            "product pk",
        ):
            with self.subTest(hidden_identifier=hidden_identifier):
                with self.assertRaises(UnsupportedLogicalExportField):
                    canonical_json(
                        {hidden_identifier: 1},
                        maximum_depth=5,
                    )
        with self.assertRaises(UnsupportedLogicalExportField):
            canonical_json(
                '{"number":1e999999999999999999999999999999999}',
                maximum_depth=5,
            )

    def test_full_hidden_database_id_key_matrix_is_rejected_at_every_depth(self):
        hidden_keys = (
            "id",
            "ID",
            "_id",
            "user_id",
            "userId",
            "user-id",
            "branch_id",
            "branchId",
            "warehouse_id",
            "product_id",
            "customer_id",
            "employee_id",
            "pk",
            "primary_key",
        )
        for key in hidden_keys:
            payloads = (
                {key: 7},
                {"container": {"nested": {key: 7}}},
                {"container": [{"nested": {key: 7}}]},
                [{"container": [{key: 7}]}],
            )
            for index, payload in enumerate(payloads):
                with self.subTest(key=key, payload=index):
                    with self.assertRaises(UnsupportedLogicalExportField):
                        canonical_json(payload, maximum_depth=10)

    def test_domain_json_policies_are_exact_and_fail_closed(self):
        registry = get_logical_export_registry()

        def json_spec(model_label, field_name):
            return next(
                item
                for item in registry.get(model_label).json_fields
                if item.field_name == field_name
            )

        attributes = json_spec("catalog.ProductVariant", "attributes")
        self.assertEqual(attributes.policy, JsonPolicy.FLAT_STRING_MAP)
        attributes_value = self.serializer.json(
            attributes,
            {"weave": "plain", "colour": "navy"},
        )
        self.assertEqual(list(attributes_value), ["colour", "weave"])
        for invalid in (
            {"nested": {"value": "forbidden"}},
            {"number": 1},
            {"product_id": "7"},
            ["not", "a", "map"],
        ):
            with self.subTest(policy="attributes", value=invalid):
                with self.assertRaises(UnsupportedLogicalExportField):
                    self.serializer.json(attributes, invalid)

        more_options = json_spec("customers.Customer", "more_options")
        self.assertEqual(more_options.policy, JsonPolicy.INDEXED_STRING_MAP)
        indexed_value = self.serializer.json(
            more_options,
            {"20": "last", "1": "first"},
        )
        self.assertEqual(list(indexed_value), ["1", "20"])
        for invalid in (
            {"0": "outside"},
            {"21": "outside"},
            {"1": 1},
            {"1": {"nested": "forbidden"}},
        ):
            with self.subTest(policy="indexed", value=invalid):
                with self.assertRaises(UnsupportedLogicalExportField):
                    self.serializer.json(more_options, invalid)

        denominations = json_spec("registers.Shift", "denominations")
        self.assertEqual(denominations.policy, JsonPolicy.DENOMINATION_MAP)
        denomination_value = self.serializer.json(
            denominations,
            {"10": 2, "0.500": Decimal("3")},
        )
        self.assertEqual(list(denomination_value), ["0.500", "10"])
        self.assertIsNone(self.serializer.json(denominations, None))
        for invalid in (
            {"0": 1},
            {"01": 1},
            {"-1": 1},
            {"10": -1},
            {"10": True},
            {"10": "2"},
            {"1": 1, "1.0": 2},
        ):
            with self.subTest(policy="denominations", value=invalid):
                with self.assertRaises(UnsupportedLogicalExportField):
                    self.serializer.json(denominations, invalid)

        tailoring = json_spec("sales.SaleItem", "tailoring_details")
        self.assertEqual(tailoring.policy, JsonPolicy.FLAT_STRING_MAP)
        tailoring_value = self.serializer.json(
            tailoring,
            {
                "workshop_notes": "hand finish",
                "design_type": "formal",
            },
        )
        self.assertEqual(
            list(tailoring_value),
            ["design_type", "workshop_notes"],
        )
        for invalid in (
            {"unknown_detail": "forbidden"},
            {"design_type": 4},
            {"design_type": {"nested": "forbidden"}},
        ):
            with self.subTest(policy="tailoring", value=invalid):
                with self.assertRaises(UnsupportedLogicalExportField):
                    self.serializer.json(tailoring, invalid)

    def test_json_input_node_member_and_string_budgets_fail_closed(self):
        invalid = (
            json.dumps(["x"] * 5000),
            json.dumps({"x" * 17_000: "value"}),
            json.dumps({"payload": "x" * 65_537}),
            ["x"] * 5000,
            {str(index): "x" for index in range(1025)},
        )
        for payload in invalid:
            with self.subTest(kind=type(payload).__name__):
                with self.assertRaises(UnsupportedLogicalExportField):
                    canonical_json(payload, maximum_depth=20)

    def test_canonical_edge_values_preserve_exact_data_and_reject_surrogates(self):
        self.assertEqual(
            canonical_decimal("-0.000", decimal_places=3),
            "0.000",
        )
        forty_digit_value = "1234567890123456789012345678901234567890.000"
        self.assertEqual(
            canonical_decimal(forty_digit_value, decimal_places=3),
            forty_digit_value,
        )

        decomposed = "e\u0301"
        composed = "\u00e9"
        self.assertNotEqual(decomposed, composed)
        canonical = canonical_json(
            {"value": decomposed},
            maximum_depth=5,
        )
        self.assertEqual(canonical["value"], decomposed)
        encoded = self.serializer.encode_line(canonical)
        self.assertIn(decomposed.encode("utf-8"), encoded)
        self.assertNotIn(composed.encode("utf-8"), encoded)
        self.assertEqual(
            self.serializer.encode_line({"value": "line\n\t\u0001"}),
            b'{"value":"line\\n\\t\\u0001"}\n',
        )

        summer_offset = timezone(timedelta(hours=-4))
        winter_offset = timezone(timedelta(hours=-5))
        self.assertEqual(
            canonical_datetime(
                datetime(
                    2026,
                    7,
                    1,
                    12,
                    30,
                    15,
                    123456,
                    tzinfo=summer_offset,
                )
            ),
            "2026-07-01T16:30:15.123456Z",
        )
        self.assertEqual(
            canonical_datetime(
                datetime(
                    2026,
                    1,
                    1,
                    12,
                    30,
                    15,
                    123456,
                    tzinfo=winter_offset,
                )
            ),
            "2026-01-01T17:30:15.123456Z",
        )

        lone_surrogate = "\ud800"
        for operation in (
            lambda: canonical_json(
                {"value": lone_surrogate},
                maximum_depth=5,
            ),
            lambda: self.serializer.encode_line({"value": lone_surrogate}),
            lambda: self.serializer.scalar(
                django_apps.get_model("catalog.Product")._meta.get_field("name"),
                lone_surrogate,
            ),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(UnsupportedLogicalExportField):
                    operation()

    def test_json_depth_is_bounded_and_no_default_str_or_repr_is_used(self):
        with self.assertRaises(UnsupportedLogicalExportField):
            canonical_json(
                {"a": {"b": {"c": {"d": {"e": 1}}}}},
                maximum_depth=3,
            )
        marker = object()
        with self.assertRaises(UnsupportedLogicalExportField):
            canonical_json({"marker": marker}, maximum_depth=5)

    def test_ndjson_encoding_is_compact_utf8_sorted_and_has_one_lf(self):
        encoded = self.serializer.encode_line({"z": "قماش", "a": 1})
        self.assertEqual(encoded, b'{"a":1,"z":"\xd9\x82\xd9\x85\xd8\xa7\xd8\xb4"}\n')
        self.assertFalse(encoded.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(encoded.count(b"\n"), 1)
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertNotIn(b"\r", encoded)
        chunks = tuple(
            self.serializer.iter_encoded_line(
                {"value": "x" * 9000},
            )
        )
        self.assertGreater(len(chunks), 3)
        self.assertEqual(b"".join(chunks), self.serializer.encode_line({"value": "x" * 9000}))

    def test_bounded_writer_completes_short_writes_and_rejects_invalid_counts(self):
        class ShortFile:
            def __init__(self, outcomes):
                self.outcomes = list(outcomes)
                self.calls = []

            def write(self, value):
                self.calls.append(bytes(value))
                return self.outcomes.pop(0)

        short_file = ShortFile((2, 4))
        writer = object.__new__(_BoundedAtomicFile)
        writer._file = short_file
        writer.byte_limit = 6
        writer.byte_count = 0
        writer.write(b"abcdef")
        self.assertEqual(short_file.calls, [b"abcdef", b"cdef"])
        self.assertEqual(writer.byte_count, 6)

        for invalid_count in (0, 7, None, True):
            with self.subTest(invalid_count=invalid_count):
                writer = object.__new__(_BoundedAtomicFile)
                writer._file = ShortFile((invalid_count,))
                writer.byte_limit = 6
                writer.byte_count = 0
                with self.assertRaises(ComponentExportCreationError):
                    writer.write(b"abcdef")
                self.assertEqual(writer.byte_count, 0)

    def test_safe_media_names_and_all_unsafe_name_classes(self):
        self.assertEqual(
            validate_media_storage_name(
                "products/variants/photo 1.png",
                maximum_length=128,
            ),
            "products/variants/photo 1.png",
        )
        invalid = (
            "",
            "   ",
            "/absolute.png",
            "C:/drive.png",
            r"products\backslash.png",
            "//server/share.png",
            "../escape.png",
            "products/../escape.png",
            "products/./photo.png",
            "products//photo.png",
            "products/%2e%2e/escape.png",
            "file://products/photo.png",
            "http://example.test/photo.png",
            "https://example.test/photo.png",
            "products/photo.png?download=1",
            "products/photo.png#fragment",
            "products/\x00photo.png",
            "products\u2044photo.png",
            "products\u2215photo.png",
            "products\uff0fphoto.png",
            "products\uff3cphoto.png",
            "products/photo:name.png",
            "products/\x1fphoto.png",
            "products/\x7fphoto.png",
            "products/<photo>.png",
            'products/"photo".png',
            "products/photo|copy.png",
            "products/photo*.png",
            "products/\u202ephoto.png",
            "products/photo\u2028name.png",
            "CON",
            "CON.txt",
            "LPT9",
            "COM¹.txt",
            "LPT²",
            "CONIN$",
            "CONOUT$.txt",
            "nul.txt",
            "products/COM1.png",
            "products/photo.png.",
            "products/photo.png ",
            "x" * 129,
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(UnsafeMediaReference):
                    validate_media_storage_name(value, maximum_length=128)


class SnapshotStreamingReaderTests(SQLiteSnapshotTestCase):
    """B. Bounded fetchmany streaming and retained read-only controls."""

    def test_iter_query_uses_fetchmany_with_exact_batch_and_never_fetchall(self):
        cursor = _TrackingCursor(batches=[[(1,), (2,)], [(3,)]])
        reader = SQLiteSnapshotReader(_TrackingConnection(cursor))
        self.assertEqual(
            tuple(reader.iter_query("SELECT value", (), batch_size=2)),
            ((1,), (2,), (3,)),
        )
        self.assertEqual(cursor.fetchmany_sizes, [2, 2, 2])
        self.assertFalse(cursor.fetchall_called)
        self.assertTrue(cursor.closed)

    def test_iter_query_closes_cursor_on_early_generator_close(self):
        cursor = _TrackingCursor(batches=[[(1,), (2,)], [(3,)]])
        reader = SQLiteSnapshotReader(_TrackingConnection(cursor))
        generator = reader.iter_query("SELECT value", (), batch_size=1)
        self.assertEqual(next(generator), (1,))
        generator.close()
        self.assertTrue(cursor.closed)

    def test_iter_query_closes_cursor_on_sqlite_error_and_timeout(self):
        cursor = _TrackingCursor(error=sqlite3.OperationalError("private SQL"))
        reader = SQLiteSnapshotReader(_TrackingConnection(cursor))
        with self.assertRaises(SnapshotValidationError) as caught:
            tuple(reader.iter_query("SELECT value", (), batch_size=1))
        self.assertTrue(cursor.closed)
        self.assertNotIn("private SQL", str(caught.exception))

        deadline_cursor = _TrackingCursor(batches=[[(1,)]])
        checks = iter((None, SnapshotTimeout()))

        def deadline():
            outcome = next(checks)
            if outcome is not None:
                raise outcome

        deadline_reader = SQLiteSnapshotReader(
            _TrackingConnection(deadline_cursor),
            deadline_check=deadline,
        )
        with self.assertRaises(SnapshotTimeout):
            tuple(deadline_reader.iter_query("SELECT value", (), batch_size=1))
        self.assertTrue(deadline_cursor.closed)

    def test_iter_query_preserves_active_abort_when_cursor_close_aborts(self):
        cleanup_abort = SystemExit("cleanup abort")
        cursor = _TrackingCursor(
            batches=[[(1,)]],
            close_error=cleanup_abort,
        )
        iterator = SQLiteSnapshotReader(_TrackingConnection(cursor)).iter_query(
            "SELECT value", (), batch_size=1
        )
        self.assertEqual(next(iterator), (1,))
        sentinel = KeyboardInterrupt("body abort")
        with self.assertRaises(KeyboardInterrupt) as caught:
            iterator.throw(sentinel)
        self.assertIs(caught.exception, sentinel)
        self.assertTrue(cursor.closed)

        normal_cursor = _TrackingCursor(
            batches=[],
            close_error=cleanup_abort,
        )
        with self.assertRaises(SystemExit) as cleanup_caught:
            tuple(
                SQLiteSnapshotReader(_TrackingConnection(normal_cursor)).iter_query(
                    "SELECT value", (), batch_size=1
                )
            )
        self.assertIs(cleanup_caught.exception, cleanup_abort)

    def test_iter_query_rejects_unbounded_or_invalid_batch_sizes(self):
        for value in (True, 0, -1, 10_001, 1.5, "2"):
            with self.subTest(value=value):
                reader = SQLiteSnapshotReader(_TrackingConnection(_TrackingCursor()))
                with self.assertRaises(SnapshotValidationError):
                    tuple(reader.iter_query("SELECT value", (), batch_size=value))

    def test_real_snapshot_reader_still_denies_write_attach_and_schema_change(self):
        provider, context, result = self.create_snapshot()
        try:
            with provider.open_snapshot(
                context=context,
                reference=result.reference,
            ) as reader:
                self.assertTrue(reader._SQLiteSnapshotReader__connection.in_transaction)
                for sql in (
                    "DELETE FROM marker",
                    "CREATE TABLE forbidden (id INTEGER)",
                    "ATTACH DATABASE 'forbidden.sqlite3' AS forbidden",
                ):
                    with self.subTest(sql=sql):
                        with self.assertRaises(SnapshotValidationError):
                            tuple(reader.iter_query(sql, (), batch_size=1))
        finally:
            provider.cleanup_snapshot(
                context=context,
                reference=result.reference,
            )

    def test_open_snapshot_preserves_body_abort_over_close_abort(self):
        provider, context, result = self.create_snapshot()
        original_open = provider._open_connection
        body_abort = KeyboardInterrupt("body abort")
        cleanup_abort = SystemExit("cleanup abort")

        def open_with_abort(*args, **kwargs):
            return _CloseAbortConnection(
                original_open(*args, **kwargs),
                cleanup_abort,
            )

        try:
            with mock.patch.object(
                provider,
                "_open_connection",
                side_effect=open_with_abort,
            ):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    with provider.open_snapshot(
                        context=context,
                        reference=result.reference,
                    ):
                        raise body_abort
            self.assertIs(caught.exception, body_abort)

            with mock.patch.object(
                provider,
                "_open_connection",
                side_effect=open_with_abort,
            ):
                with self.assertRaises(SystemExit) as cleanup_caught:
                    with provider.open_snapshot(
                        context=context,
                        reference=result.reference,
                    ):
                        pass
            self.assertIs(cleanup_caught.exception, cleanup_abort)
        finally:
            provider.cleanup_snapshot(
                context=context,
                reference=result.reference,
            )


class LogicalExportIntegrationTests(SQLiteSnapshotTestCase):
    """Snapshot-backed tenant export, storage, cleanup and failure tests."""

    def setUp(self):
        super().setUp()
        self.registry = get_logical_export_registry()
        self.plan = resolve_component_plan(
            scope=BackupScope.POS,
            enabled_products=(ProductOwner.POS,),
        ).export_components
        self.export_cleanup = []
        self.snapshot_cleanup = []
        self._next_ids = {}

    def tearDown(self):
        for exporter, context, reference in reversed(self.export_cleanup):
            try:
                exporter.cleanup_component_export(
                    context=context,
                    reference=reference,
                )
            except LogicalExportEngineError:
                pass
        for provider, context, reference in reversed(self.snapshot_cleanup):
            try:
                provider.cleanup_snapshot(
                    context=context,
                    reference=reference,
                )
            except Exception:
                pass
        super().tearDown()

    @staticmethod
    def _quote(value):
        return f'"{str(value).replace(chr(34), chr(34) * 2)}"'

    @staticmethod
    def _sqlite_type(field):
        if field.primary_key:
            return "INTEGER PRIMARY KEY"
        if field.is_relation or isinstance(
            field,
            (models.BooleanField, models.IntegerField),
        ):
            return "INTEGER"
        if isinstance(field, models.DecimalField):
            return "TEXT"
        return "TEXT"

    def _create_model_table(self, model_label):
        model = django_apps.get_model(model_label)
        columns = ", ".join(
            f"{self._quote(field.column)} {self._sqlite_type(field)}"
            for field in model._meta.concrete_fields
        )
        self.source.execute(
            f"CREATE TABLE IF NOT EXISTS {self._quote(model._meta.db_table)} " f"({columns})"
        )
        return model

    def _install_export_schema(self, *, include_held=False):
        for spec in self.registry.specs:
            self._create_model_table(spec.model_label)
        self._create_model_table("accounts.User")
        if include_held:
            self._create_model_table("sales.HeldSale")

    @staticmethod
    def _default_field_value(field):
        if field.null:
            return None
        if isinstance(field, models.UUIDField):
            return uuid.uuid4().hex
        if isinstance(field, models.JSONField):
            return "{}"
        if isinstance(field, models.FileField):
            return ""
        if isinstance(field, models.DateTimeField):
            return "2026-07-29 03:04:05.123456"
        if isinstance(field, models.DateField):
            return "2026-07-29"
        if isinstance(field, models.TimeField):
            return "03:04:05.123456"
        if isinstance(field, models.DecimalField):
            return f"{Decimal(0):.{field.decimal_places}f}"
        if isinstance(field, models.BooleanField):
            return 0
        if isinstance(field, models.IntegerField):
            return 0
        if isinstance(
            field,
            (models.CharField, models.TextField, models.EmailField),
        ):
            return ""
        if field.is_relation:
            raise AssertionError(f"Required relation {field.name} needs a value")
        raise AssertionError(f"Unhandled synthetic field {field}")

    def _insert(self, model_label, **overrides):
        model = django_apps.get_model(model_label)
        next_id = self._next_ids.get(model_label, 0) + 1
        self._next_ids[model_label] = next_id
        values = {}
        for field in model._meta.concrete_fields:
            if field.name in overrides:
                value = overrides[field.name]
            elif field.primary_key:
                value = next_id
            elif field.name == "public_id":
                value = uuid.uuid4()
            elif field.name == "business":
                value = self.context.business_id
            else:
                value = self._default_field_value(field)
            if isinstance(value, uuid.UUID):
                value = value.hex
            elif isinstance(value, Decimal):
                value = str(value)
            elif isinstance(value, (dict, list)):
                value = json.dumps(
                    value,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            values[field.column] = value
        columns = ", ".join(self._quote(name) for name in values)
        placeholders = ", ".join("?" for _ in values)
        self.source.execute(
            f"INSERT INTO {self._quote(model._meta.db_table)} "
            f"({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        return {field.name: values[field.column] for field in model._meta.concrete_fields}

    def _component(self, key):
        return next(item for item in self.plan if item.key == key)

    def _ensure_tenant_identity(self):
        business = django_apps.get_model("tenants.Business")
        user = django_apps.get_model("accounts.User")
        existing_business = self.source.execute(
            f"SELECT 1 FROM {self._quote(business._meta.db_table)} "
            f"WHERE {self._quote(business._meta.pk.column)} = ?",
            (self.context.business_id,),
        ).fetchone()
        if existing_business is not None:
            return
        existing_user = self.source.execute(
            f"SELECT {self._quote(user._meta.pk.column)} "
            f"FROM {self._quote(user._meta.db_table)} "
            f"ORDER BY {self._quote(user._meta.pk.column)} LIMIT 1"
        ).fetchone()
        if existing_user is None:
            self._seed_global_user()
            owner_id = 91
        else:
            owner_id = existing_user[0]
        self._seed_business(owner_id=owner_id)

    def _snapshot(self, provider=None):
        self._ensure_tenant_identity()
        selected = provider or self.provider()
        provider, context, result = self.create_snapshot(
            provider=selected,
            context=self.context,
        )
        self.snapshot_cleanup.append((provider, context, result.reference))
        return provider, result

    def _exporter(self, provider, **changes):
        values = {
            "snapshot_provider": provider,
            "workspace_manager": self.manager,
            "registry": self.registry,
            "policy": _default_policy(),
        }
        values.update(changes)
        return SQLiteLogicalComponentExporter(**values)

    def _export(self, *, provider, snapshot, component_key, **changes):
        exporter = self._exporter(provider, **changes)
        result = exporter.export_component(
            ComponentExportRequest(
                context=self.context,
                component=self._component(component_key),
                snapshot=snapshot.reference,
                component_plan=self.plan,
            )
        )
        self.export_cleanup.append((exporter, self.context, result.reference))
        return exporter, result

    @staticmethod
    def _stream_bytes(exporter, context, result, stream):
        with exporter.open_component_export(
            context=context,
            reference=result.reference,
            stream=stream,
        ) as reader:
            return reader.read()

    def _records(self, exporter, result):
        raw = self._stream_bytes(
            exporter,
            self.context,
            result,
            ComponentExportStream.RECORDS,
        )
        return raw, tuple(json.loads(line) for line in raw.decode("utf-8").splitlines())

    def _media(self, exporter, result):
        raw = self._stream_bytes(
            exporter,
            self.context,
            result,
            ComponentExportStream.MEDIA_INDEX,
        )
        return raw, tuple(json.loads(line) for line in raw.decode("utf-8").splitlines())

    def _seed_global_user(self, *, internal_id=91, public_id=None):
        return self._insert(
            "accounts.User",
            id=internal_id,
            public_id=public_id or uuid.uuid4(),
            email="secret-user@example.test",
            password="pbkdf2_private_password_hash",
            full_name="Private User Name",
            is_staff=1,
            is_superuser=1,
            is_platform_admin=1,
        )

    def _seed_business(self, *, owner_id=91, public_id=None, name="Snapshot Tenant"):
        return self._insert(
            "tenants.Business",
            id=self.context.business_id,
            public_id=public_id or self.context.business_public_id,
            owner=owner_id,
            name=name,
            logo="",
        )

    def test_export_reads_snapshot_not_live_database_and_filters_other_tenant(self):
        self._install_export_schema()
        snapshot_id = uuid.UUID("00000000-0000-0000-0000-000000000020")
        self._insert(
            "catalog.Product",
            id=20,
            public_id=snapshot_id,
            name="snapshot-value",
            sku="A",
        )
        self._insert(
            "catalog.Product",
            id=21,
            public_id=uuid.uuid4(),
            business=self.context.business_id + 1,
            name="other-tenant-value",
            sku="B",
        )
        provider, snapshot = self._snapshot()
        product = django_apps.get_model("catalog.Product")
        self.source.execute(
            f"UPDATE {self._quote(product._meta.db_table)} "
            f"SET {self._quote(product._meta.get_field('name').column)} = ? "
            "WHERE id = ?",
            ("live-mutated-value", 20),
        )
        exporter, result = self._export(
            provider=provider,
            snapshot=snapshot,
            component_key="pos.catalog",
        )
        raw, records = self._records(exporter, result)
        product_records = [record for record in records if record["model"] == "catalog.Product"]
        self.assertEqual(len(product_records), 1)
        self.assertEqual(
            product_records[0]["fields"]["name"],
            "snapshot-value",
        )
        self.assertNotIn(b"live-mutated-value", raw)
        self.assertNotIn(b"other-tenant-value", raw)

    def test_record_schema_identity_references_scalars_and_byte_counts_are_exact(self):
        self._install_export_schema()
        tax_public_id = uuid.UUID("00000000-0000-0000-0000-000000000010")
        product_public_id = uuid.UUID("00000000-0000-0000-0000-000000000020")
        self._insert(
            "catalog.TaxRate",
            id=10,
            public_id=tax_public_id,
            name="VAT",
            rate=Decimal("5.000"),
        )
        self._insert(
            "catalog.Product",
            id=20,
            public_id=product_public_id,
            name="قماش",
            sku="FABRIC",
            purchase_price=Decimal("1.250"),
            sale_price=Decimal("2.500"),
            tax_rate=10,
            price_includes_tax=None,
        )
        provider, snapshot = self._snapshot()
        exporter, result = self._export(
            provider=provider,
            snapshot=snapshot,
            component_key="pos.catalog",
        )
        raw, records = self._records(exporter, result)
        record = next(item for item in records if item["model"] == "catalog.Product")
        self.assertEqual(
            set(record),
            {
                "schema",
                "component",
                "component_version",
                "model",
                "tenant_public_id",
                "identity",
                "fields",
            },
        )
        self.assertEqual(record["schema"], LOGICAL_RECORD_SCHEMA)
        self.assertEqual(record["identity"], {"public_id": str(product_public_id)})
        self.assertEqual(
            record["tenant_public_id"],
            str(self.context.business_public_id),
        )
        self.assertEqual(record["fields"]["purchase_price"], "1.250")
        self.assertEqual(record["fields"]["sale_price"], "2.500")
        self.assertIsNone(record["fields"]["price_includes_tax"])
        self.assertEqual(
            record["fields"]["tax_rate"],
            {"model": "catalog.TaxRate", "public_id": str(tax_public_id)},
        )
        self.assertNotIn("id", record)
        self.assertNotIn("business", record["fields"])
        self.assertEqual(result.byte_count, len(raw))
        self.assertEqual(result.row_count, len(records))
        self.assertEqual(result.provider_identifier, LOGICAL_EXPORT_PROVIDER_IDENTIFIER)
        self.assertEqual(
            result.deterministic_ordering_version,
            DETERMINISTIC_ORDERING_VERSION,
        )
        self.assertNotIn(b"SELECT", raw)
        self.assertNotIn(b"PRAGMA", raw)
        self.assertNotIn(b"catalog_product", raw)

    def test_record_and_model_order_are_deterministic_and_public_uuid_based(self):
        self._install_export_schema()
        later = uuid.UUID("00000000-0000-0000-0000-000000000002")
        earlier = uuid.UUID("00000000-0000-0000-0000-000000000001")
        self._insert(
            "catalog.Category",
            id=1,
            public_id=later,
            name="physically-first",
        )
        self._insert(
            "catalog.Category",
            id=200,
            public_id=earlier,
            name="physically-second",
        )
        provider, snapshot = self._snapshot()
        exporter_a, result_a = self._export(
            provider=provider,
            snapshot=snapshot,
            component_key="pos.catalog",
        )
        exporter_b, result_b = self._export(
            provider=provider,
            snapshot=snapshot,
            component_key="pos.catalog",
        )
        raw_a, records_a = self._records(exporter_a, result_a)
        raw_b, records_b = self._records(exporter_b, result_b)
        self.assertEqual(raw_a, raw_b)
        category = django_apps.get_model("catalog.Category")
        self.source.execute(f"DELETE FROM {self._quote(category._meta.db_table)}")
        self._insert(
            "catalog.Category",
            id=999,
            public_id=earlier,
            name="physically-second",
        )
        self._insert(
            "catalog.Category",
            id=2,
            public_id=later,
            name="physically-first",
        )
        provider_c, snapshot_c = self._snapshot()
        exporter_c, result_c = self._export(
            provider=provider_c,
            snapshot=snapshot_c,
            component_key="pos.catalog",
        )
        raw_c, records_c = self._records(exporter_c, result_c)
        self.assertEqual(raw_a, raw_c)
        self.assertEqual(
            tuple(dict.fromkeys(record["model"] for record in records_a)),
            ("catalog.Category",),
        )
        category_ids = [
            record["identity"]["public_id"]
            for record in records_a
            if record["model"] == "catalog.Category"
        ]
        self.assertEqual(category_ids, [str(earlier), str(later)])
        self.assertEqual(records_a, records_b)
        self.assertEqual(records_a, records_c)

    def test_cross_tenant_and_missing_fk_targets_fail_without_partial_output(self):
        relation_cases = (
            ("branches.Branch", 501),
            ("branches.Warehouse", 502),
            ("catalog.Product", 503),
            ("customers.Customer", 504),
            ("suppliers.Supplier", 505),
            ("wms_core.WmsLocation", 506),
        )
        exporter = self._exporter(self.provider())
        for target, internal_id in relation_cases:
            with self.subTest(target=target):
                relation_spec = next(
                    spec
                    for model_spec in self.registry.specs
                    for spec in model_spec.relation_fields
                    if spec.target_model_label == target
                )
                reader = _StaticRelationReader((uuid.uuid4().hex, self.context.business_id + 1))
                with self.assertRaises(TenantIsolationViolation):
                    exporter._resolve_relation(
                        reader=reader,
                        context=self.context,
                        relation_spec=relation_spec,
                        raw_identifier=internal_id,
                    )
                missing = _StaticRelationReader(None)
                with self.assertRaises(TenantIsolationViolation):
                    exporter._resolve_relation(
                        reader=missing,
                        context=self.context,
                        relation_spec=relation_spec,
                        raw_identifier=internal_id,
                    )

    def test_every_tenant_relation_rejects_wrong_owner_and_missing_target(self):
        self._install_export_schema()
        paths = tuple(
            (source_spec.model_label, relation_spec)
            for source_spec in self.registry.specs
            for relation_spec in source_spec.relation_fields
            if not relation_spec.global_reference
            and relation_spec.target_model_label != "tenants.Business"
        )
        self.assertTrue(paths)
        wrong_ids = {}
        for internal_id, target_label in enumerate(
            sorted({relation.target_model_label for _source, relation in paths}),
            start=7001,
        ):
            target = django_apps.get_model(target_label)
            target_spec = self.registry.get(target_label)
            self.assertEqual(target_spec.ownership_field, "business")
            self.source.execute(
                f"INSERT INTO {self._quote(target._meta.db_table)} "
                f"({self._quote(target._meta.pk.column)}, "
                f"{self._quote(target._meta.get_field('public_id').column)}, "
                f"{self._quote(target._meta.get_field('business').column)}) "
                "VALUES (?, ?, ?)",
                (
                    internal_id,
                    uuid.uuid4().hex,
                    self.context.business_id + 1,
                ),
            )
            wrong_ids[target_label] = internal_id

        provider, snapshot = self._snapshot()
        exporter = self._exporter(provider)
        with provider.open_snapshot(
            context=self.context,
            reference=snapshot.reference,
        ) as reader:
            for source_label, relation_spec in paths:
                wrong_id = wrong_ids[relation_spec.target_model_label]
                for case, raw_identifier in (
                    ("wrong_owner", wrong_id),
                    ("missing", wrong_id + 1_000_000),
                ):
                    with self.subTest(
                        source=source_label,
                        field=relation_spec.field_name,
                        case=case,
                    ):
                        with self.assertRaises(TenantIsolationViolation):
                            exporter._resolve_relation(
                                reader=reader,
                                context=self.context,
                                relation_spec=relation_spec,
                                raw_identifier=raw_identifier,
                            )

    def test_every_model_query_has_explicit_tenant_ownership_predicate(self):
        exporter = self._exporter(self.provider())
        for spec in self.registry.specs:
            with self.subTest(model=spec.model_label):
                model, query = exporter._query_for_spec(
                    spec,
                    self.context,
                    maximum_row_input_bytes=1024,
                )
                if spec.model_label == "tenants.Business":
                    primary_column = self._quote(model._meta.pk.column)
                    public_column = self._quote(model._meta.get_field("public_id").column)
                    self.assertEqual(query.sql.count(f"{primary_column} = ?"), 1)
                    self.assertEqual(query.sql.count(f"{public_column} = ?"), 1)
                    self.assertEqual(
                        query.oversize_sql.count(f"{primary_column} = ?"),
                        1,
                    )
                    self.assertEqual(
                        query.oversize_sql.count(f"{public_column} = ?"),
                        1,
                    )
                    self.assertEqual(
                        query.parameters,
                        (
                            self.context.business_id,
                            self.context.business_public_id.hex,
                        ),
                    )
                    self.assertEqual(
                        query.oversize_parameters,
                        (
                            self.context.business_id,
                            self.context.business_public_id.hex,
                            1024,
                        ),
                    )
                else:
                    ownership_column = self._quote(
                        model._meta.get_field(spec.ownership_field).column
                    )
                    self.assertEqual(
                        query.sql.count(f"{ownership_column} = ?"),
                        1,
                    )
                    self.assertEqual(
                        query.oversize_sql.count(f"{ownership_column} = ?"),
                        1,
                    )
                    self.assertEqual(
                        query.parameters,
                        (self.context.business_id,),
                    )
                    self.assertEqual(
                        query.oversize_parameters,
                        (
                            self.context.business_id,
                            1024,
                        ),
                    )

    def test_every_nullable_relation_emits_none_without_lookup(self):
        nullable_paths = tuple(
            (spec.model_label, relation)
            for spec in self.registry.specs
            for relation in spec.relation_fields
            if relation.nullable
        )
        required_paths = tuple(
            (spec.model_label, relation)
            for spec in self.registry.specs
            for relation in spec.relation_fields
            if not relation.nullable
        )
        self.assertTrue(nullable_paths)
        reader = mock.Mock()
        exporter = self._exporter(self.provider())
        for source_label, relation_spec in nullable_paths:
            with self.subTest(source=source_label, field=relation_spec.field_name):
                self.assertIsNone(
                    exporter._resolve_relation(
                        reader=reader,
                        context=self.context,
                        relation_spec=relation_spec,
                        raw_identifier=None,
                    )
                )
        for source_label, relation_spec in required_paths:
            with self.subTest(
                source=source_label,
                field=relation_spec.field_name,
                required=True,
            ):
                with self.assertRaises(LogicalReferenceResolutionError):
                    exporter._resolve_relation(
                        reader=reader,
                        context=self.context,
                        relation_spec=relation_spec,
                        raw_identifier=None,
                    )
        reader.first.assert_not_called()

    def test_both_many_to_many_paths_reject_cross_tenant_targets(self):
        self._install_export_schema()
        cases = (
            ("accounts.Membership", "branches", 8101, 8201),
            ("wms_core.WmsUserAccess", "allowed_locations", 8102, 8202),
        )
        prepared = []
        for model_label, field_name, source_id, target_id in cases:
            model = django_apps.get_model(model_label)
            field = model._meta.get_field(field_name)
            through = field.remote_field.through
            self._create_model_table(through._meta.label)
            target = field.related_model
            target_spec = self.registry.get(target._meta.label)
            self.source.execute(
                f"INSERT INTO {self._quote(target._meta.db_table)} "
                f"({self._quote(target._meta.pk.column)}, "
                f"{self._quote(target._meta.get_field('public_id').column)}, "
                f"{self._quote(target._meta.get_field(target_spec.ownership_field).column)}) "
                "VALUES (?, ?, ?)",
                (
                    target_id,
                    uuid.uuid4().hex,
                    self.context.business_id + 1,
                ),
            )
            source_fk = through._meta.get_field(field.m2m_field_name())
            target_fk = through._meta.get_field(field.m2m_reverse_field_name())
            self.source.execute(
                f"INSERT INTO {self._quote(through._meta.db_table)} "
                f"({self._quote(source_fk.column)}, "
                f"{self._quote(target_fk.column)}) VALUES (?, ?)",
                (source_id, target_id),
            )
            m2m_spec = next(
                item
                for item in self.registry.get(model_label).many_to_many_fields
                if item.field_name == field_name
            )
            prepared.append((model, m2m_spec, source_id))

        provider, snapshot = self._snapshot()
        exporter = self._exporter(provider)
        with provider.open_snapshot(
            context=self.context,
            reference=snapshot.reference,
        ) as reader:
            for model, m2m_spec, source_id in prepared:
                with self.subTest(
                    model=model._meta.label,
                    field=m2m_spec.field_name,
                ):
                    with self.assertRaises(TenantIsolationViolation):
                        exporter._resolve_many_to_many(
                            reader=reader,
                            context=self.context,
                            model=model,
                            internal_pk=source_id,
                            m2m_spec=m2m_spec,
                            batch_size=2,
                            maximum_references=100,
                        )

    def test_stock_movement_business_reference_is_same_tenant_natural_key(self):
        self._install_export_schema()
        self._insert("branches.Branch", id=901, name="Branch")
        self._insert(
            "branches.Warehouse",
            id=902,
            name="Warehouse",
            branch=901,
        )
        self._insert("catalog.Product", id=903, name="Product")
        self._insert(
            "inventory.StockTransfer",
            id=904,
            transfer_number="TR-VALID",
            from_warehouse=902,
            to_warehouse=902,
        )
        movement_public_id = uuid.uuid4()
        self._insert(
            "inventory.StockMovement",
            id=905,
            public_id=movement_public_id,
            warehouse=902,
            product=903,
            movement_type="transfer_out",
            quantity=Decimal("-1.000"),
            reference_type="Transfer",
            reference_id="TR-VALID",
        )
        transfer = django_apps.get_model("inventory.StockTransfer")
        self.source.execute(
            f"INSERT INTO {self._quote(transfer._meta.db_table)} "
            f"({self._quote(transfer._meta.pk.column)}, "
            f"{self._quote(transfer._meta.get_field('public_id').column)}, "
            f"{self._quote(transfer._meta.get_field('business').column)}, "
            f"{self._quote(transfer._meta.get_field('transfer_number').column)}) "
            "VALUES (?, ?, ?, ?)",
            (
                906,
                uuid.uuid4().hex,
                self.context.business_id + 1,
                "TR-CROSS",
            ),
        )

        provider, snapshot = self._snapshot()
        exporter, result = self._export(
            provider=provider,
            snapshot=snapshot,
            component_key="pos.inventory",
        )
        _raw, records = self._records(exporter, result)
        movement = next(
            record
            for record in records
            if record["identity"]["public_id"] == str(movement_public_id)
        )
        self.assertEqual(movement["fields"]["reference_type"], "Transfer")
        self.assertEqual(movement["fields"]["reference_id"], "TR-VALID")

        with provider.open_snapshot(
            context=self.context,
            reference=snapshot.reference,
        ) as reader:
            for reference_id in ("TR-CROSS", "TR-MISSING", "904"):
                with self.subTest(reference_id=reference_id):
                    with self.assertRaises(LogicalReferenceResolutionError) as caught:
                        exporter._validate_stock_movement_reference(
                            reader=reader,
                            context=self.context,
                            fields={
                                "reference_type": "Transfer",
                                "reference_id": reference_id,
                            },
                        )
                    self.assertNotIn(reference_id, str(caught.exception))
            exporter._validate_stock_movement_reference(
                reader=reader,
                context=self.context,
                fields={
                    "reference_type": "Opening",
                    "reference_id": "",
                },
            )
            for fields in (
                {"reference_type": "Opening", "reference_id": "raw-id"},
                {"reference_type": "Unknown", "reference_id": "TR-VALID"},
            ):
                with self.assertRaises(LogicalReferenceResolutionError):
                    exporter._validate_stock_movement_reference(
                        reader=reader,
                        context=self.context,
                        fields=fields,
                    )

    def test_actual_wms_salary_export_validates_assignment_snapshot(self):
        self._install_export_schema()
        self._insert("branches.Branch", id=910, name="Workshop branch")
        self._insert(
            "wms_core.WmsLocation",
            id=911,
            branch=910,
            location_type="workshop",
        )
        self._insert(
            "wms_workforce.WmsEmployee",
            id=912,
            location=911,
            employee_code="EMP-1",
            full_name="Worker",
        )
        self._insert(
            "wms_workforce.WmsProductionCategory",
            id=913,
            name="Stitching",
            code="STITCH",
        )
        assignment_public_id = uuid.uuid4()
        self._insert(
            "wms_workforce.WmsEmployeeCategoryAssignment",
            id=914,
            public_id=assignment_public_id,
            employee=912,
            category=913,
        )
        self._insert(
            "wms_production.WmsProductionEntry",
            id=915,
            location=911,
            employee=912,
        )
        self._insert(
            "wms_production.WmsProductionEntryLine",
            id=916,
            entry=915,
            assignment=914,
            category=913,
        )
        self._insert(
            "wms_salary.WmsSalary",
            id=917,
            employee=912,
        )
        self._insert(
            "wms_salary.WmsSalaryDay",
            id=918,
            salary=917,
            location=911,
        )
        piece_public_id = uuid.uuid4()
        self._insert(
            "wms_salary.WmsSalaryPieceLine",
            id=919,
            public_id=piece_public_id,
            salary_day=918,
            production_line=916,
            assignment_public_id_snapshot=assignment_public_id,
        )
        context = replace(
            self.context,
            requested_scope=BackupScope.WMS,
            resolved_products=(ProductOwner.WMS,),
        )
        self._ensure_tenant_identity()
        plan = resolve_component_plan(
            scope=BackupScope.WMS,
            enabled_products=(ProductOwner.WMS,),
        ).export_components
        provider, _snapshot_context, snapshot = self.create_snapshot(
            context=context,
        )
        exporter = self._exporter(provider)
        component = next(item for item in plan if item.key == "wms.salary")
        result = exporter.export_component(
            ComponentExportRequest(
                context=context,
                component=component,
                snapshot=snapshot.reference,
                component_plan=plan,
            )
        )
        self.export_cleanup.append((exporter, context, result.reference))
        self.snapshot_cleanup.append((provider, context, snapshot.reference))
        raw = self._stream_bytes(
            exporter,
            context,
            result,
            ComponentExportStream.RECORDS,
        )
        records = tuple(json.loads(line) for line in raw.decode().splitlines())
        piece = next(
            record for record in records if record["identity"]["public_id"] == str(piece_public_id)
        )
        self.assertEqual(
            piece["fields"]["assignment_public_id_snapshot"],
            str(assignment_public_id),
        )
        production_line = django_apps.get_model("wms_production.WmsProductionEntryLine")
        self.source.execute(
            f"UPDATE {self._quote(production_line._meta.db_table)} "
            f"SET {self._quote(production_line._meta.get_field('assignment').column)} = ? "
            f"WHERE {self._quote(production_line._meta.pk.column)} = ?",
            (999_999, 916),
        )
        provider_bad, _context_bad, snapshot_bad = self.create_snapshot(
            context=context,
        )
        self.snapshot_cleanup.append((provider_bad, context, snapshot_bad.reference))
        exporter_bad = self._exporter(provider_bad)
        with self.assertRaises(TenantIsolationViolation):
            exporter_bad.export_component(
                ComponentExportRequest(
                    context=context,
                    component=component,
                    snapshot=snapshot_bad.reference,
                    component_plan=plan,
                )
            )

    def test_business_requires_internal_id_and_public_uuid_match(self):
        self._install_export_schema()
        self._seed_global_user()
        self._seed_business(public_id=uuid.uuid4())
        provider, snapshot = self._snapshot()
        exporter = self._exporter(provider)
        with self.assertRaises(TenantIsolationViolation) as caught:
            exporter.export_component(
                ComponentExportRequest(
                    context=self.context,
                    component=self._component("shared.tenant_identity"),
                    snapshot=snapshot.reference,
                    component_plan=self.plan,
                )
            )
        rendered = str(caught.exception)
        self.assertNotIn("Snapshot Tenant", rendered)
        self.assertNotIn(str(self.context.business_id), rendered)
        self.assertFalse(caught.exception.cleanup_incomplete)

    def test_direct_component_export_validates_context_tenant_identity(self):
        self._install_export_schema()
        self._insert(
            "catalog.Product",
            id=20,
            name="Tenant-bound product",
        )
        provider, snapshot = self._snapshot()
        exporter = self._exporter(provider)
        forged_context = replace(
            self.context,
            business_public_id=uuid.uuid4(),
        )
        with self.assertRaises(TenantIsolationViolation):
            exporter.export_component(
                ComponentExportRequest(
                    context=forged_context,
                    component=self._component("pos.catalog"),
                    snapshot=snapshot.reference,
                    component_plan=self.plan,
                )
            )
        components = self.workspace.path / WorkspaceArea.COMPONENTS.value
        if components.exists():
            self.assertFalse(
                any(child.is_dir() and len(child.name) == 32 for child in components.iterdir())
            )

    def test_snapshot_reference_cannot_cross_workspace_during_export(self):
        self._install_export_schema()
        provider, snapshot = self._snapshot()
        exporter = self._exporter(provider)
        other_workspace = self.manager.create()
        other_context = self.context_without_workspace.with_workspace(other_workspace.reference)
        with self.assertRaises(ComponentExportValidationError) as caught:
            exporter.export_component(
                ComponentExportRequest(
                    context=other_context,
                    component=self._component("pos.expenses"),
                    snapshot=snapshot.reference,
                    component_plan=self.plan,
                )
            )
        self.assertFalse(caught.exception.cleanup_incomplete)
        components = other_workspace.path / WorkspaceArea.COMPONENTS.value
        if components.exists():
            self.assertFalse(
                any(child.is_dir() and len(child.name) == 32 for child in components.iterdir())
            )

    def test_global_user_reference_contains_only_public_uuid(self):
        self._install_export_schema()
        user_public_id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self._seed_global_user(public_id=user_public_id)
        self._seed_business()
        provider, snapshot = self._snapshot()
        exporter, result = self._export(
            provider=provider,
            snapshot=snapshot,
            component_key="shared.tenant_identity",
        )
        raw, records = self._records(exporter, result)
        business = records[0]
        self.assertEqual(
            business["fields"]["owner"],
            {"model": "accounts.User", "public_id": str(user_public_id)},
        )
        for forbidden in (
            b"secret-user@example.test",
            b"pbkdf2_private_password_hash",
            b"Private User Name",
            b"is_staff",
            b"is_superuser",
            b"is_platform_admin",
        ):
            self.assertNotIn(forbidden, raw)

    def test_business_settings_singleton_identity_has_no_database_pk(self):
        self._install_export_schema()
        self._insert("tenants.BusinessSettings", id=987)
        provider, snapshot = self._snapshot()
        exporter, result = self._export(
            provider=provider,
            snapshot=snapshot,
            component_key="shared.tenant_settings",
        )
        raw, records = self._records(exporter, result)
        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["identity"],
            {
                "singleton_model": "tenants.BusinessSettings",
                "tenant_public_id": str(self.context.business_public_id),
            },
        )
        self.assertNotIn(b'"id":987', raw)

    def test_business_settings_requires_exactly_one_snapshot_row(self):
        self._install_export_schema()
        settings_model = django_apps.get_model("tenants.BusinessSettings")
        for count in (0, 2):
            with self.subTest(count=count):
                self.source.execute(f"DELETE FROM {self._quote(settings_model._meta.db_table)}")
                for _index in range(count):
                    self._insert("tenants.BusinessSettings")
                provider, snapshot = self._snapshot()
                exporter = self._exporter(provider)
                with self.assertRaises(TenantIsolationViolation):
                    exporter.export_component(
                        ComponentExportRequest(
                            context=self.context,
                            component=self._component("shared.tenant_settings"),
                            snapshot=snapshot.reference,
                            component_plan=self.plan,
                        )
                    )

    def test_membership_branches_are_sorted_typed_refs_without_through_ids(self):
        self._install_export_schema()
        through = (
            django_apps.get_model("accounts.Membership")
            ._meta.get_field("branches")
            .remote_field.through
        )
        self._create_model_table(through._meta.label)
        user_public = uuid.uuid4()
        self._seed_global_user(internal_id=9, public_id=user_public)
        role_public = uuid.uuid4()
        membership_public = uuid.uuid4()
        branch_later = uuid.UUID("00000000-0000-0000-0000-000000000002")
        branch_earlier = uuid.UUID("00000000-0000-0000-0000-000000000001")
        self._insert(
            "accounts.Role",
            id=10,
            public_id=role_public,
            name="Operator",
            permissions=[],
        )
        self._insert(
            "accounts.Membership",
            id=11,
            public_id=membership_public,
            user=9,
            role=10,
        )
        self._insert(
            "branches.Branch",
            id=12,
            public_id=branch_later,
            name="Later",
        )
        self._insert(
            "branches.Branch",
            id=13,
            public_id=branch_earlier,
            name="Earlier",
        )
        source_fk = through._meta.get_field("membership")
        target_fk = through._meta.get_field("branch")
        self.source.execute(
            f"INSERT INTO {self._quote(through._meta.db_table)} "
            f"({self._quote(source_fk.column)}, {self._quote(target_fk.column)}) "
            "VALUES (?, ?), (?, ?)",
            (11, 12, 11, 13),
        )
        provider, snapshot = self._snapshot()
        exporter, result = self._export(
            provider=provider,
            snapshot=snapshot,
            component_key="shared.access_control",
        )
        raw, records = self._records(exporter, result)
        membership = next(record for record in records if record["model"] == "accounts.Membership")
        self.assertEqual(
            membership["fields"]["branches"],
            [
                {"model": "branches.Branch", "public_id": str(branch_earlier)},
                {"model": "branches.Branch", "public_id": str(branch_later)},
            ],
        )
        self.assertNotIn(b'"id":11', raw)
        self.assertNotIn(through._meta.db_table.encode(), raw)

    def test_many_to_many_reference_accumulation_is_bounded(self):
        membership = django_apps.get_model("accounts.Membership")
        m2m_spec = self.registry.get("accounts.Membership").many_to_many_fields[0]
        rows = (
            (uuid.uuid4().hex, self.context.business_id),
            (uuid.uuid4().hex, self.context.business_id),
        )

        class StaticRows:
            def iter_query(self, *_args, **_kwargs):
                yield from rows

        with self.assertRaises(ComponentExportLimitExceeded):
            self._exporter(self.provider())._resolve_many_to_many(
                reader=StaticRows(),
                context=self.context,
                model=membership,
                internal_pk=1,
                m2m_spec=m2m_spec,
                batch_size=1,
                maximum_references=1,
            )

    def test_held_sale_and_checkout_token_are_absent_from_actual_sales_bytes(self):
        self._install_export_schema(include_held=True)
        token = "phase2c-secret-checkout-token"
        held_marker = "phase2c-held-cart-marker"
        self._seed_global_user(internal_id=90)
        self._insert("branches.Branch", id=91, name="Sales")
        self._insert(
            "branches.Warehouse",
            id=92,
            name="Main",
            branch=91,
        )
        self._insert(
            "customers.Customer",
            id=93,
            code="WALK",
            full_name="Walk In",
        )
        self._insert(
            "sales.Sale",
            id=94,
            branch=91,
            warehouse=92,
            cashier=90,
            customer=93,
            checkout_token=token,
            invoice_number="INV-1",
        )
        self._insert(
            "sales.HeldSale",
            id=95,
            branch=91,
            cashier=90,
            label="Held",
            cart={"items": [{"product_id": 44}], "marker": held_marker},
        )
        provider, snapshot = self._snapshot()
        seen_sql = []
        original_iter = SQLiteSnapshotReader.iter_query
        original_first = SQLiteSnapshotReader.first

        def recording_iter(reader, sql, parameters=(), *, batch_size):
            seen_sql.append(sql)
            yield from original_iter(
                reader,
                sql,
                parameters,
                batch_size=batch_size,
            )

        def recording_first(reader, sql, parameters=()):
            seen_sql.append(sql)
            return original_first(reader, sql, parameters)

        with (
            mock.patch.object(
                SQLiteSnapshotReader,
                "iter_query",
                recording_iter,
            ),
            mock.patch.object(
                SQLiteSnapshotReader,
                "first",
                recording_first,
            ),
        ):
            exporter, result = self._export(
                provider=provider,
                snapshot=snapshot,
                component_key="pos.sales",
            )
        records_raw, records = self._records(exporter, result)
        media_raw, _media = self._media(exporter, result)
        combined = records_raw + media_raw + repr(result).encode()
        self.assertNotIn(token.encode(), combined)
        self.assertNotIn(held_marker.encode(), combined)
        self.assertNotIn(b"sales.HeldSale", combined)
        sale = next(record for record in records if record["model"] == "sales.Sale")
        self.assertNotIn("checkout_token", sale["fields"])
        all_sql = "\n".join(seen_sql)
        held_model = django_apps.get_model("sales.HeldSale")
        sale_model = django_apps.get_model("sales.Sale")
        checkout = sale_model._meta.get_field("checkout_token")
        self.assertNotIn(self._quote(held_model._meta.db_table), all_sql)
        self.assertNotIn(self._quote(checkout.column), all_sql)
        self.assertIn(self._quote(sale_model._meta.db_table), all_sql)

    def test_media_discovery_is_deterministic_and_never_opens_media_storage(self):
        self._install_export_schema()
        product_public = uuid.uuid4()
        self._insert(
            "catalog.Product",
            id=10,
            public_id=product_public,
            name="Photo product",
            image="products/photo.jpg",
        )
        provider, snapshot = self._snapshot()
        blocked = AssertionError("media storage must not be accessed")
        with (
            mock.patch.object(FileSystemStorage, "open", side_effect=blocked),
            mock.patch.object(FileSystemStorage, "path", side_effect=blocked),
            mock.patch.object(FileSystemStorage, "url", side_effect=blocked),
            mock.patch.object(FileSystemStorage, "exists", side_effect=blocked),
            mock.patch.object(FileSystemStorage, "size", side_effect=blocked),
            mock.patch.object(FieldFile, "open", side_effect=blocked),
        ):
            exporter, result = self._export(
                provider=provider,
                snapshot=snapshot,
                component_key="pos.catalog",
            )
        _records_raw, records = self._records(exporter, result)
        media_raw, media = self._media(exporter, result)
        self.assertEqual(result.media_count, 1)
        self.assertEqual(len(media), 1)
        self.assertEqual(
            media[0],
            {
                "schema": LOGICAL_MEDIA_REFERENCE_SCHEMA,
                "component": "pos.catalog",
                "model": "catalog.Product",
                "tenant_public_id": str(self.context.business_public_id),
                "identity": {"public_id": str(product_public)},
                "field": "image",
                "storage_name": "products/photo.jpg",
            },
        )
        product = next(record for record in records if record["model"] == "catalog.Product")
        self.assertEqual(product["fields"]["image"], "products/photo.jpg")
        self.assertEqual(result.media_index_byte_count, len(media_raw))
        self.assertNotIn(str(self.root).encode(), media_raw)

    def test_identical_media_references_are_deduplicated(self):
        self._install_export_schema()
        product_public = uuid.uuid4()
        for internal_id in (10, 11):
            self._insert(
                "catalog.Product",
                id=internal_id,
                public_id=product_public,
                name="Duplicate logical product",
                image="products/shared-photo.jpg",
            )
        provider, snapshot = self._snapshot()
        exporter, result = self._export(
            provider=provider,
            snapshot=snapshot,
            component_key="pos.catalog",
        )
        _media_raw, media = self._media(exporter, result)
        self.assertEqual(result.media_count, 1)
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["identity"], {"public_id": str(product_public)})
        self.assertEqual(media[0]["storage_name"], "products/shared-photo.jpg")

    def test_null_and_empty_media_values_remain_distinct_without_references(self):
        self._install_export_schema()
        null_public = uuid.UUID("00000000-0000-0000-0000-000000000101")
        empty_public = uuid.UUID("00000000-0000-0000-0000-000000000102")
        self._insert(
            "catalog.Product",
            id=10,
            public_id=null_public,
            name="Null image",
            image=None,
        )
        self._insert(
            "catalog.Product",
            id=11,
            public_id=empty_public,
            name="Empty image",
            image="",
        )
        provider, snapshot = self._snapshot()
        exporter, result = self._export(
            provider=provider,
            snapshot=snapshot,
            component_key="pos.catalog",
        )
        _records_raw, records = self._records(exporter, result)
        media_raw, media = self._media(exporter, result)
        products = {
            record["identity"]["public_id"]: record
            for record in records
            if record["model"] == "catalog.Product"
        }
        self.assertIsNone(products[str(null_public)]["fields"]["image"])
        self.assertEqual(products[str(empty_public)]["fields"]["image"], "")
        self.assertEqual(media_raw, b"")
        self.assertEqual(media, ())
        self.assertEqual(result.media_count, 0)

    def test_all_unsafe_media_name_classes_fail_and_clean_partial_exports(self):
        self._install_export_schema()
        self._insert(
            "catalog.Product",
            id=10,
            name="Unsafe media",
            image="products/safe.jpg",
        )
        product = django_apps.get_model("catalog.Product")
        invalid = (
            "../escape.jpg",
            "/absolute.jpg",
            "C:/drive.jpg",
            r"products\backslash.jpg",
            "//server/share.jpg",
            "https://example.test/photo.jpg",
            "products/photo.jpg?x=1",
            "products/\x00photo.jpg",
        )
        for value in invalid:
            with self.subTest(value=value):
                self.source.execute(
                    f"UPDATE {self._quote(product._meta.db_table)} "
                    f"SET {self._quote(product._meta.get_field('image').column)} = ?",
                    (value,),
                )
                provider, snapshot = self._snapshot()
                exporter = self._exporter(provider)
                with self.assertRaises(UnsafeMediaReference) as caught:
                    exporter.export_component(
                        ComponentExportRequest(
                            context=self.context,
                            component=self._component("pos.catalog"),
                            snapshot=snapshot.reference,
                            component_plan=self.plan,
                        )
                    )
                self.assertNotIn(value, str(caught.exception))
                components = self.workspace.path / WorkspaceArea.COMPONENTS.value
                if components.exists():
                    self.assertFalse(
                        any(
                            child.is_dir() and len(child.name) == 32
                            for child in components.iterdir()
                        )
                    )

    def test_empty_component_is_zero_byte_deterministic_with_exact_counts(self):
        self._install_export_schema()
        provider, snapshot = self._snapshot()
        exporter_a, result_a = self._export(
            provider=provider,
            snapshot=snapshot,
            component_key="pos.expenses",
        )
        exporter_b, result_b = self._export(
            provider=provider,
            snapshot=snapshot,
            component_key="pos.expenses",
        )
        records_a, parsed_a = self._records(exporter_a, result_a)
        records_b, parsed_b = self._records(exporter_b, result_b)
        media_a, parsed_media_a = self._media(exporter_a, result_a)
        self.assertEqual(records_a, b"")
        self.assertEqual(records_a, records_b)
        self.assertEqual(media_a, b"")
        self.assertEqual(parsed_a, parsed_b)
        self.assertEqual(parsed_media_a, ())
        self.assertEqual(result_a.row_count, 0)
        self.assertEqual(result_a.media_count, 0)
        self.assertEqual(result_a.byte_count, 0)
        self.assertEqual(
            result_a.model_counts,
            (
                ("expenses.ExpenseCategory", 0),
                ("expenses.RecurringExpenseTemplate", 0),
                ("expenses.Expense", 0),
            ),
        )

    def test_component_reference_is_opaque_cross_workspace_rejected_and_reader_closes(self):
        self._install_export_schema()
        provider, snapshot = self._snapshot()
        exporter, result = self._export(
            provider=provider,
            snapshot=snapshot,
            component_key="pos.expenses",
        )
        self.assertIsInstance(result.reference.identifier, uuid.UUID)
        self.assertNotIn(str(self.staging_root), repr(result))
        directory = (
            self.workspace.path / WorkspaceArea.COMPONENTS.value / result.reference.identifier.hex
        )
        self.assertTrue(directory.is_dir())
        self.assertNotIn("Sensitive Tenant Name", str(directory))
        self.assertEqual(directory.name, result.reference.identifier.hex)
        with exporter.open_component_export(
            context=self.context,
            reference=result.reference,
        ) as reader:
            self.assertFalse(hasattr(reader, "name"))
            self.assertFalse(hasattr(reader, "path"))
            self.assertFalse(reader.closed)
            self.assertIsInstance(reader._OpaqueBinaryReader__file.name, int)
        self.assertTrue(reader.closed)

        other_workspace = self.manager.create()
        other_context = self.context_without_workspace.with_workspace(other_workspace.reference)
        with self.assertRaises(ComponentExportNotFound):
            with exporter.open_component_export(
                context=other_context,
                reference=result.reference,
            ):
                pass

    def test_component_reader_and_cleanup_reject_external_hardlink_alias(self):
        self._install_export_schema()
        provider, snapshot = self._snapshot()
        exporter, result = self._export(
            provider=provider,
            snapshot=snapshot,
            component_key="pos.expenses",
        )
        directory = (
            self.workspace.path / WorkspaceArea.COMPONENTS.value / result.reference.identifier.hex
        )
        records = directory / RECORDS_FILE_NAME
        alias = self.workspace.path / f"{result.reference.identifier.hex}.alias"
        try:
            os.link(records, alias)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"hard links unavailable: {type(exc).__name__}")
        try:
            with self.assertRaises(ComponentExportValidationError):
                with exporter.open_component_export(
                    context=self.context,
                    reference=result.reference,
                ):
                    pass
            with self.assertRaises(ComponentExportCleanupError):
                exporter.cleanup_component_export(
                    context=self.context,
                    reference=result.reference,
                )
        finally:
            if alias.exists():
                alias.unlink()
        self.assertTrue(
            exporter.cleanup_component_export(
                context=self.context,
                reference=result.reference,
            )
        )

    def test_component_file_link_like_substitution_is_rejected_portably(self):
        self._install_export_schema()
        provider, snapshot = self._snapshot()
        exporter, result = self._export(
            provider=provider,
            snapshot=snapshot,
            component_key="pos.expenses",
        )
        directory = (
            self.workspace.path / WorkspaceArea.COMPONENTS.value / result.reference.identifier.hex
        )
        records = directory / RECORDS_FILE_NAME

        def substituted(candidate):
            return os.path.abspath(candidate) == os.path.abspath(records)

        with mock.patch(
            "apps.backups.engine.logical_export.path_is_link_like",
            side_effect=substituted,
        ):
            with self.assertRaises(ComponentExportValidationError):
                with exporter.open_component_export(
                    context=self.context,
                    reference=result.reference,
                ):
                    pass
            with self.assertRaises(ComponentExportCleanupError):
                exporter.cleanup_component_export(
                    context=self.context,
                    reference=result.reference,
                )

    def test_random_missing_component_reference_is_sanitized_and_idempotent(self):
        self._install_export_schema()
        provider, snapshot = self._snapshot()
        exporter = self._exporter(provider)
        missing = ComponentExportReference(uuid.uuid4())
        with self.assertRaises(ComponentExportNotFound) as caught:
            with exporter.open_component_export(
                context=self.context,
                reference=missing,
            ):
                pass
        self.assertNotIn(str(self.workspace.path), str(caught.exception))
        self.assertFalse(
            exporter.cleanup_component_export(
                context=self.context,
                reference=missing,
            )
        )

    def test_component_exporter_rejects_snapshot_provider_subclasses(self):
        class ForgedSnapshotProvider(SQLiteSnapshotProvider):
            pass

        forged = object.__new__(ForgedSnapshotProvider)
        with self.assertRaises(ComponentExportValidationError):
            SQLiteLogicalComponentExporter(
                snapshot_provider=forged,
                workspace_manager=self.manager,
                registry=self.registry,
                policy=_default_policy(),
            )

    def test_request_component_cannot_escape_authoritative_pos_plan(self):
        self._install_export_schema()
        provider, snapshot = self._snapshot()
        exporter = self._exporter(provider)
        wms_component = resolve_component_plan(
            scope=BackupScope.WMS,
            enabled_products=(ProductOwner.WMS,),
        ).export_components[-1]
        cases = (
            replace(
                self._component("pos.catalog"),
                component_version="forged",
            ),
            wms_component,
        )
        for component in cases:
            with self.subTest(component=component.key):
                with self.assertRaises(ComponentExportValidationError):
                    exporter.export_component(
                        ComponentExportRequest(
                            context=self.context,
                            component=component,
                            snapshot=snapshot.reference,
                            component_plan=self.plan,
                        )
                    )
        components = self.workspace.path / WorkspaceArea.COMPONENTS.value
        if components.exists():
            self.assertFalse(
                any(child.is_dir() and len(child.name) == 32 for child in components.iterdir())
            )

    def test_cleanup_is_exact_idempotent_and_preserves_unrelated_content(self):
        self._install_export_schema()
        provider, snapshot = self._snapshot()
        exporter, result = self._export(
            provider=provider,
            snapshot=snapshot,
            component_key="pos.expenses",
        )
        directory = (
            self.workspace.path / WorkspaceArea.COMPONENTS.value / result.reference.identifier.hex
        )
        unrelated = directory / "unrelated.keep"
        unrelated.write_bytes(b"preserve")
        self.assertTrue(
            exporter.cleanup_component_export(
                context=self.context,
                reference=result.reference,
            )
        )
        self.assertTrue(unrelated.exists())
        self.assertFalse((directory / RECORDS_FILE_NAME).exists())
        self.assertFalse((directory / MEDIA_INDEX_FILE_NAME).exists())
        self.assertFalse(
            exporter.cleanup_component_export(
                context=self.context,
                reference=result.reference,
            )
        )
        self.assertEqual(unrelated.read_bytes(), b"preserve")

    def test_cleanup_retries_partial_unlink_and_directory_removal_failures(self):
        self._install_export_schema()
        provider, snapshot = self._snapshot()
        for failure_kind in ("second_unlink", "directory_remove"):
            with self.subTest(failure=failure_kind):
                exporter, result = self._export(
                    provider=provider,
                    snapshot=snapshot,
                    component_key="pos.expenses",
                )
                if failure_kind == "second_unlink":
                    original_unlink = os.unlink
                    calls = 0

                    def flaky_unlink(path, original_unlink=original_unlink):
                        nonlocal calls
                        calls += 1
                        if calls == 2:
                            raise PermissionError("simulated file lock")
                        return original_unlink(path)

                    patcher = mock.patch(
                        "apps.backups.engine.logical_export.os.unlink",
                        side_effect=flaky_unlink,
                    )
                else:
                    patcher = mock.patch(
                        "apps.backups.engine.logical_export.os.rmdir",
                        side_effect=PermissionError("simulated directory lock"),
                    )
                with patcher:
                    with self.assertRaises(ComponentExportCleanupError):
                        exporter.cleanup_component_export(
                            context=self.context,
                            reference=result.reference,
                        )
                self.assertTrue(
                    exporter.cleanup_component_export(
                        context=self.context,
                        reference=result.reference,
                    )
                )

    def test_private_modes_and_exclusive_reference_creation(self):
        self._install_export_schema()
        provider, snapshot = self._snapshot()
        fixed = ComponentExportReference(uuid.uuid4())
        exporter = self._exporter(
            provider,
            reference_factory=lambda: fixed,
        )
        result = exporter.export_component(
            ComponentExportRequest(
                context=self.context,
                component=self._component("pos.expenses"),
                snapshot=snapshot.reference,
                component_plan=self.plan,
            )
        )
        self.export_cleanup.append((exporter, self.context, result.reference))
        with self.assertRaises(ComponentExportCreationError):
            exporter.export_component(
                ComponentExportRequest(
                    context=self.context,
                    component=self._component("pos.expenses"),
                    snapshot=snapshot.reference,
                    component_plan=self.plan,
                )
            )
        if os.name != "nt":
            directory = self.workspace.path / WorkspaceArea.COMPONENTS.value / fixed.identifier.hex
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((directory / RECORDS_FILE_NAME).stat().st_mode),
                0o600,
            )

    def test_oversized_row_preflight_does_not_project_value_and_fails_sanitized(self):
        self._install_export_schema()
        secret = "row-limit-secret-" + ("x" * 4096)
        self._insert(
            "catalog.Product",
            id=10,
            name=secret,
        )
        provider, snapshot = self._snapshot()
        exporter = self._exporter(
            provider,
            policy=_default_policy(maximum_row_input_bytes=1024),
        )
        observed = []
        preflight = []
        original_iter = SQLiteSnapshotReader.iter_query
        original_first = SQLiteSnapshotReader.first

        def recording_iter(reader, sql, parameters=(), *, batch_size):
            for row in original_iter(
                reader,
                sql,
                parameters,
                batch_size=batch_size,
            ):
                observed.append(row)
                yield row

        def recording_first(reader, sql, parameters=()):
            result = original_first(reader, sql, parameters)
            if sql.startswith("SELECT 1 FROM") and result is not None:
                preflight.append((sql, result))
            return result

        with (
            mock.patch.object(
                SQLiteSnapshotReader,
                "iter_query",
                recording_iter,
            ),
            mock.patch.object(
                SQLiteSnapshotReader,
                "first",
                recording_first,
            ),
        ):
            with self.assertRaises(ComponentExportLimitExceeded) as caught:
                exporter.export_component(
                    ComponentExportRequest(
                        context=self.context,
                        component=self._component("pos.catalog"),
                        snapshot=snapshot.reference,
                        component_plan=self.plan,
                    )
                )
        self.assertEqual(len(preflight), 1)
        self.assertEqual(preflight[0][1], (1,))
        self.assertNotIn(secret, preflight[0][0])
        self.assertEqual(observed, [])
        for rendered in (str(caught.exception), repr(caught.exception)):
            self.assertNotIn(secret, rendered)
            self.assertNotIn("catalog_product", rendered)
            self.assertNotIn("SELECT", rendered)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertFalse(caught.exception.cleanup_incomplete)
        components = self.workspace.path / WorkspaceArea.COMPONENTS.value
        if components.exists():
            self.assertFalse(tuple(components.rglob("*.part")))
            self.assertFalse(tuple(components.rglob(RECORDS_FILE_NAME)))

    def test_record_and_media_byte_limits_clean_atomic_part_files(self):
        self._install_export_schema()
        self._insert(
            "catalog.Product",
            id=1,
            name="Limit record",
            image="products/photo.jpg",
        )
        for policy in (
            _default_policy(maximum_records_bytes=1),
            _default_policy(maximum_media_index_bytes=1),
        ):
            with self.subTest(policy=policy):
                provider, snapshot = self._snapshot()
                exporter = self._exporter(provider, policy=policy)
                with self.assertRaises(ComponentExportLimitExceeded) as caught:
                    exporter.export_component(
                        ComponentExportRequest(
                            context=self.context,
                            component=self._component("pos.catalog"),
                            snapshot=snapshot.reference,
                            component_plan=self.plan,
                        )
                    )
                self.assertFalse(caught.exception.cleanup_incomplete)
                components = self.workspace.path / WorkspaceArea.COMPONENTS.value
                if components.exists():
                    self.assertFalse(tuple(components.rglob("*.part")))
                    self.assertFalse(tuple(components.rglob(RECORDS_FILE_NAME)))
                    self.assertFalse(tuple(components.rglob(MEDIA_INDEX_FILE_NAME)))

    def test_streamed_multibyte_record_honors_exact_byte_limit(self):
        self._install_export_schema()
        self._insert(
            "catalog.Product",
            id=1,
            public_id=uuid.UUID("00000000-0000-0000-0000-000000000201"),
            name="قماش متعدد البايت",
        )
        provider, snapshot = self._snapshot()
        with mock.patch.object(
            CanonicalLogicalSerializer,
            "encode_line",
            side_effect=AssertionError("export must stream encoded chunks"),
        ):
            _baseline_exporter, baseline = self._export(
                provider=provider,
                snapshot=snapshot,
                component_key="pos.catalog",
            )
        exact_policy = _default_policy(
            maximum_records_bytes=baseline.byte_count,
        )
        _exact_exporter, exact = self._export(
            provider=provider,
            snapshot=snapshot,
            component_key="pos.catalog",
            policy=exact_policy,
        )
        self.assertEqual(exact.byte_count, baseline.byte_count)

        exporter = self._exporter(
            provider,
            policy=_default_policy(
                maximum_records_bytes=baseline.byte_count - 1,
            ),
        )
        with self.assertRaises(ComponentExportLimitExceeded):
            exporter.export_component(
                ComponentExportRequest(
                    context=self.context,
                    component=self._component("pos.catalog"),
                    snapshot=snapshot.reference,
                    component_plan=self.plan,
                )
            )
        components = self.workspace.path / WorkspaceArea.COMPONENTS.value
        self.assertFalse(tuple(components.rglob("*.part")))

    def test_deadline_and_failure_hooks_clean_component_output(self):
        self._install_export_schema()
        self._insert("catalog.Product", id=1, name="Timeout")
        provider, snapshot = self._snapshot()
        monotonic = iter((0.0, 0.0, 31.0))
        exporter = self._exporter(
            provider,
            monotonic=lambda: next(monotonic),
        )
        with self.assertRaises(ComponentExportTimeout):
            exporter.export_component(
                ComponentExportRequest(
                    context=self.context,
                    component=self._component("pos.catalog"),
                    snapshot=snapshot.reference,
                    component_plan=self.plan,
                )
            )
        components = self.workspace.path / WorkspaceArea.COMPONENTS.value
        if components.exists():
            self.assertFalse(
                any(child.is_dir() and len(child.name) == 32 for child in components.iterdir())
            )

        class AuditAbort(BaseException):
            pass

        for abort_type in (KeyboardInterrupt, SystemExit, GeneratorExit, AuditAbort):
            with self.subTest(abort=abort_type.__name__):
                provider, snapshot = self._snapshot()
                abort_object = abort_type("original abort")

                def abort(stage, abort_object=abort_object):
                    if stage == "after_component_creation":
                        raise abort_object

                exporter = self._exporter(provider, failure_hook=abort)
                captured_abort = None
                captured_traceback = None
                try:
                    exporter.export_component(
                        ComponentExportRequest(
                            context=self.context,
                            component=self._component("pos.catalog"),
                            snapshot=snapshot.reference,
                            component_plan=self.plan,
                        )
                    )
                except abort_type as caught:
                    captured_abort = caught
                    captured_traceback = caught.__traceback__
                else:
                    self.fail(f"{abort_type.__name__} was not raised")
                self.assertIs(captured_abort, abort_object)
                traceback_names = []
                current = captured_traceback
                while current is not None:
                    traceback_names.append(current.tb_frame.f_code.co_name)
                    current = current.tb_next
                self.assertIn("abort", traceback_names)
                if components.exists():
                    self.assertFalse(
                        any(
                            child.is_dir() and len(child.name) == 32
                            for child in components.iterdir()
                        )
                    )

    def test_failure_injection_at_atomic_and_result_stages_cleans_exactly(self):
        self._install_export_schema()
        provider, snapshot = self._snapshot()
        stages = (
            "before_component_directory_creation",
            "after_part_creation_records",
            "after_part_creation_media_index",
            "after_component_creation",
            "before_component_finalize",
            "after_flush_records",
            "after_fsync_records",
            "during_publication_records",
            "after_link_records",
            "after_temp_removal_records",
            "after_flush_media_index",
            "after_fsync_media_index",
            "during_publication_media_index",
            "after_link_media_index",
            "after_temp_removal_media_index",
            "after_component_finalize",
            "before_component_result_return",
        )
        for selected_stage in stages:
            with self.subTest(stage=selected_stage):

                def fail(stage, selected_stage=selected_stage):
                    if stage == selected_stage:
                        raise ComponentExportCreationError()

                exporter = self._exporter(provider, failure_hook=fail)
                with self.assertRaises(ComponentExportCreationError) as caught:
                    exporter.export_component(
                        ComponentExportRequest(
                            context=self.context,
                            component=self._component("pos.expenses"),
                            snapshot=snapshot.reference,
                            component_plan=self.plan,
                        )
                    )
                self.assertFalse(caught.exception.cleanup_incomplete)
                components = self.workspace.path / WorkspaceArea.COMPONENTS.value
                if components.exists():
                    self.assertFalse(tuple(components.rglob("*.part")))
                    self.assertFalse(
                        any(
                            child.is_dir() and len(child.name) == 32
                            for child in components.iterdir()
                        )
                    )

    def test_same_size_final_stream_substitution_is_detected_and_preserved(self):
        self._install_export_schema()
        self._insert(
            "catalog.Product",
            id=1,
            name="Bound artifact identity",
        )
        provider, snapshot = self._snapshot()
        reference = ComponentExportReference(uuid.uuid4())
        directory = self.workspace.path / WorkspaceArea.COMPONENTS.value / reference.identifier.hex
        replacement_path = directory / ".replacement.tmp"
        records_path = directory / RECORDS_FILE_NAME

        def substitute(stage):
            if stage != "before_component_result_return":
                return
            original = records_path.read_bytes()
            replacement_path.write_bytes(b"x" * len(original))
            os.chmod(replacement_path, 0o600)
            os.replace(replacement_path, records_path)

        exporter = self._exporter(
            provider,
            reference_factory=lambda: reference,
            failure_hook=substitute,
        )
        try:
            with self.assertRaises(ComponentExportValidationError) as caught:
                exporter.export_component(
                    ComponentExportRequest(
                        context=self.context,
                        component=self._component("pos.catalog"),
                        snapshot=snapshot.reference,
                        component_plan=self.plan,
                    )
                )
            self.assertTrue(caught.exception.cleanup_incomplete)
            self.assertTrue(records_path.is_file())
            self.assertFalse(replacement_path.exists())
            self.assertTrue(
                exporter.cleanup_component_export(
                    context=self.context,
                    reference=reference,
                )
            )
            self.assertFalse(directory.exists())
        finally:
            if directory.exists():
                try:
                    exporter.cleanup_component_export(
                        context=self.context,
                        reference=reference,
                    )
                except LogicalExportEngineError:
                    pass

    def test_operational_orm_queryset_is_never_a_logical_row_source(self):
        self._install_export_schema()
        self._insert(
            "catalog.Product",
            id=1,
            name="Snapshot-only logical row",
        )
        provider, snapshot = self._snapshot()
        with mock.patch(
            "django.db.models.query.QuerySet._fetch_all",
            side_effect=AssertionError(
                "the operational ORM must not materialize logical export rows"
            ),
        ):
            exporter, result = self._export(
                provider=provider,
                snapshot=snapshot,
                component_key="pos.catalog",
            )
        _raw, records = self._records(exporter, result)
        self.assertTrue(any(record["model"] == "catalog.Product" for record in records))

    def test_full_actual_pos_batch_always_cleans_snapshot_after_success(self):
        self._install_export_schema()
        self._seed_global_user()
        self._seed_business()
        self._insert("tenants.BusinessSettings")
        provider, snapshot = self._snapshot()
        exporter = self._exporter(provider)
        results = export_snapshot_components(
            context=self.context,
            snapshot_result=snapshot,
            component_plan=self.plan,
            snapshot_provider=provider,
            component_exporter=exporter,
        )
        self.assertEqual(
            tuple(result.component_key for result in results),
            tuple(item.key for item in self.plan),
        )
        self.assertFalse(self.snapshot_path(snapshot).exists())
        for result in results:
            self.export_cleanup.append((exporter, self.context, result.reference))

    def test_actual_scope_exports_query_exact_resolved_model_tables(self):
        self._install_export_schema()
        self._insert("tenants.BusinessSettings")
        self._ensure_tenant_identity()
        original_iter = SQLiteSnapshotReader.iter_query
        original_first = SQLiteSnapshotReader.first
        cases = (
            (BackupScope.POS, (ProductOwner.POS,)),
            (BackupScope.WMS, (ProductOwner.WMS,)),
            (
                BackupScope.ALL_ENABLED,
                (ProductOwner.POS, ProductOwner.WMS),
            ),
        )
        for scope, products in cases:
            with self.subTest(scope=scope):
                context = replace(
                    self.context,
                    requested_scope=scope,
                    resolved_products=products,
                )
                plan = resolve_component_plan(
                    scope=scope,
                    enabled_products=products,
                ).export_components
                seen_sql = []

                def recording_iter(
                    reader,
                    sql,
                    parameters=(),
                    *,
                    batch_size,
                    seen_sql=seen_sql,
                ):
                    seen_sql.append(sql)
                    yield from original_iter(
                        reader,
                        sql,
                        parameters,
                        batch_size=batch_size,
                    )

                def recording_first(
                    reader,
                    sql,
                    parameters=(),
                    seen_sql=seen_sql,
                ):
                    seen_sql.append(sql)
                    return original_first(reader, sql, parameters)

                provider, _snapshot_context, snapshot = self.create_snapshot(
                    context=context,
                )
                exporter = self._exporter(provider)
                with (
                    mock.patch.object(
                        SQLiteSnapshotReader,
                        "iter_query",
                        recording_iter,
                    ),
                    mock.patch.object(
                        SQLiteSnapshotReader,
                        "first",
                        recording_first,
                    ),
                ):
                    results = export_snapshot_components(
                        context=context,
                        snapshot_result=snapshot,
                        component_plan=plan,
                        snapshot_provider=provider,
                        component_exporter=exporter,
                    )
                for result in results:
                    self.export_cleanup.append((exporter, context, result.reference))

                expected_labels = {
                    spec.model_label
                    for component in plan
                    for spec in self.registry.for_component(component.key)
                }
                expected_tables = {
                    django_apps.get_model(label)._meta.db_table for label in expected_labels
                }
                user_table = django_apps.get_model("accounts.User")._meta.db_table
                known_tables = {
                    django_apps.get_model(spec.model_label)._meta.db_table
                    for spec in self.registry.specs
                } | {user_table}
                queried_tables = {
                    table
                    for table in known_tables
                    if any(self._quote(table) in sql for sql in seen_sql)
                }
                self.assertEqual(
                    queried_tables,
                    expected_tables | {user_table},
                )

    def test_batch_failure_cleans_prior_outputs_and_snapshot(self):
        self._install_export_schema()
        provider, snapshot = self._snapshot()
        fake = _RecordingBatchExporter(
            registry=self.registry,
            snapshot_provider=provider,
            fail_at=1,
        )
        with self.assertRaises(ComponentExportCreationError) as caught:
            export_snapshot_components(
                context=self.context,
                snapshot_result=snapshot,
                component_plan=self.plan,
                snapshot_provider=provider,
                component_exporter=fake,
            )
        self.assertEqual(len(fake.cleanup_calls), 1)
        self.assertFalse(self.snapshot_path(snapshot).exists())
        self.assertFalse(caught.exception.cleanup_incomplete)

    def test_failure_after_all_components_before_batch_return_cleans_everything(self):
        self._install_export_schema()
        self._insert("tenants.BusinessSettings")
        provider, snapshot = self._snapshot()

        def fail(stage):
            if stage == "before_batch_result_return":
                raise ComponentExportCreationError()

        exporter = self._exporter(provider, failure_hook=fail)
        with self.assertRaises(ComponentExportCreationError) as caught:
            export_snapshot_components(
                context=self.context,
                snapshot_result=snapshot,
                component_plan=self.plan,
                snapshot_provider=provider,
                component_exporter=exporter,
            )
        self.assertFalse(caught.exception.cleanup_incomplete)
        self.assertFalse(self.snapshot_path(snapshot).exists())
        components = self.workspace.path / WorkspaceArea.COMPONENTS.value
        if components.exists():
            self.assertFalse(
                any(child.is_dir() and len(child.name) == 32 for child in components.iterdir())
            )

    def test_batch_cleanup_false_marks_primary_failure_incomplete(self):
        self._install_export_schema()
        provider, snapshot = self._snapshot()
        fake = _RecordingBatchExporter(
            registry=self.registry,
            snapshot_provider=provider,
            fail_at=1,
            cleanup_result=False,
        )
        with self.assertRaises(ComponentExportCreationError) as caught:
            export_snapshot_components(
                context=self.context,
                snapshot_result=snapshot,
                component_plan=self.plan,
                snapshot_provider=provider,
                component_exporter=fake,
            )
        self.assertIs(caught.exception.cleanup_incomplete, True)
        self.assertEqual(len(fake.cleanup_calls), 1)
        self.assertFalse(self.snapshot_path(snapshot).exists())

    def test_batch_validation_failures_still_clean_owned_snapshot(self):
        self._install_export_schema()
        cases = (
            ("inconsistent_false", False),
            ("inconsistent_integer", 1),
            ("inconsistent_string", "true"),
            ("inconsistent_decimal", Decimal(1)),
            ("wrong_snapshot_provider_id", True),
            ("forged_plan", True),
            ("provider_mismatch", True),
        )
        for case, consistency in cases:
            with self.subTest(case=case):
                provider, snapshot = self._snapshot()
                fake = _RecordingBatchExporter(
                    registry=self.registry,
                    snapshot_provider=provider,
                )
                supplied_snapshot = replace(
                    snapshot,
                    consistent=consistency,
                )
                supplied_plan = self.plan
                if case == "wrong_snapshot_provider_id":
                    supplied_snapshot = replace(
                        supplied_snapshot,
                        provider_identifier="forged-provider",
                    )
                elif case == "forged_plan":
                    supplied_plan = tuple(reversed(self.plan))
                elif case == "provider_mismatch":
                    fake.snapshot_provider = object()
                with self.assertRaises(ComponentExportValidationError):
                    export_snapshot_components(
                        context=self.context,
                        snapshot_result=supplied_snapshot,
                        component_plan=supplied_plan,
                        snapshot_provider=provider,
                        component_exporter=fake,
                    )
                self.assertFalse(self.snapshot_path(snapshot).exists())

    def test_partial_batch_plan_is_rejected_before_component_export(self):
        self._install_export_schema()
        provider, snapshot = self._snapshot()
        fake = _RecordingBatchExporter(
            registry=self.registry,
            snapshot_provider=provider,
        )
        with self.assertRaises(ComponentExportValidationError):
            export_snapshot_components(
                context=self.context,
                snapshot_result=snapshot,
                component_plan=(self.plan[0],),
                snapshot_provider=provider,
                component_exporter=fake,
            )
        self.assertEqual(fake.requests, [])
        self.assertFalse(self.snapshot_path(snapshot).exists())

    def test_forged_component_results_and_duplicate_references_are_rejected(self):
        self._install_export_schema()
        cases = (
            {"provider_identifier": "forged-provider"},
            {"component_key": "forged.component"},
            {"component_version": "999.0.0"},
            {"row_count": True},
            {"created_at": None},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                provider, snapshot = self._snapshot()
                fake = _RecordingBatchExporter(
                    registry=self.registry,
                    snapshot_provider=provider,
                    result_changes=changes,
                )
                with self.assertRaises(ComponentExportValidationError):
                    export_snapshot_components(
                        context=self.context,
                        snapshot_result=snapshot,
                        component_plan=self.plan,
                        snapshot_provider=provider,
                        component_exporter=fake,
                    )
                self.assertEqual(len(fake.cleanup_calls), 1)
                self.assertFalse(self.snapshot_path(snapshot).exists())

        provider, snapshot = self._snapshot()
        fixed = ComponentExportReference(uuid.uuid4())
        duplicate_fake = _RecordingBatchExporter(
            registry=self.registry,
            snapshot_provider=provider,
            fixed_reference=fixed,
        )
        with self.assertRaises(ComponentExportValidationError):
            export_snapshot_components(
                context=self.context,
                snapshot_result=snapshot,
                component_plan=self.plan,
                snapshot_provider=provider,
                component_exporter=duplicate_fake,
            )
        self.assertEqual(len(duplicate_fake.cleanup_calls), 1)
        self.assertFalse(self.snapshot_path(snapshot).exists())

    def test_batch_abort_reraises_and_still_attempts_snapshot_cleanup(self):
        self._install_export_schema()
        for abort in (KeyboardInterrupt(), SystemExit(), GeneratorExit()):
            with self.subTest(abort=type(abort).__name__):
                provider, snapshot = self._snapshot()
                fake = _RecordingBatchExporter(
                    registry=self.registry,
                    snapshot_provider=provider,
                    fail_at=0,
                    abort=abort,
                )
                with self.assertRaises(type(abort)):
                    export_snapshot_components(
                        context=self.context,
                        snapshot_result=snapshot,
                        component_plan=self.plan,
                        snapshot_provider=provider,
                        component_exporter=fake,
                    )
                self.assertFalse(self.snapshot_path(snapshot).exists())

    def test_batch_preserves_original_abort_when_output_cleanup_aborts(self):
        self._install_export_schema()
        provider, snapshot = self._snapshot()
        original_abort = KeyboardInterrupt("original abort")
        fake = _RecordingBatchExporter(
            registry=self.registry,
            snapshot_provider=provider,
            fail_at=1,
            abort=original_abort,
            cleanup_error=SystemExit("cleanup abort"),
        )
        with self.assertRaises(KeyboardInterrupt) as caught:
            export_snapshot_components(
                context=self.context,
                snapshot_result=snapshot,
                component_plan=self.plan,
                snapshot_provider=provider,
                component_exporter=fake,
            )
        self.assertIs(caught.exception, original_abort)
        self.assertEqual(len(fake.cleanup_calls), 1)
        self.assertFalse(self.snapshot_path(snapshot).exists())

    def test_snapshot_cleanup_failure_prevents_successful_batch_result(self):
        self._install_export_schema()
        provider, snapshot = self._snapshot()
        fake = _RecordingBatchExporter(
            registry=self.registry,
            snapshot_provider=provider,
        )
        original_cleanup = provider.cleanup_snapshot
        with mock.patch.object(
            provider,
            "cleanup_snapshot",
            return_value=False,
        ):
            with self.assertRaises(SnapshotCleanupAfterExportError) as caught:
                export_snapshot_components(
                    context=self.context,
                    snapshot_result=snapshot,
                    component_plan=self.plan,
                    snapshot_provider=provider,
                    component_exporter=fake,
                )
        self.assertIs(caught.exception.cleanup_incomplete, True)
        original_cleanup(
            context=self.context,
            reference=snapshot.reference,
        )


class LogicalExportPlanAndCapabilityTests(BackupPhase1TestCase):
    """C/D/K/L/M. Scope, forged plans, checks and disabled execution."""

    def setUp(self):
        super().setUp()
        self.registry = get_logical_export_registry()

    def _context_for(self, scope, products):
        from apps.backups.engine.context import (
            ActorIdentitySnapshot,
            BackupExecutionContext,
        )
        from apps.backups.engine.workspace import WorkspaceReference
        from apps.backups.enums import BackupTrigger

        return BackupExecutionContext(
            backup_public_id=uuid.uuid4(),
            business_id=self.business_a.pk,
            business_public_id=self.business_a.public_id,
            requested_scope=scope,
            resolved_products=tuple(products),
            trigger_type=BackupTrigger.MANUAL,
            actor_identity=ActorIdentitySnapshot.from_actor(self.owner_a),
            application_version="phase2c-test",
            backup_format_version="phase2c-test",
            schema_migration_fingerprint="opaque",
            minimum_restore_version="phase2c-test",
            idempotency_key="opaque",
            operation_correlation_id=uuid.uuid4(),
            workspace_reference=WorkspaceReference.new(),
        )

    def test_pos_wms_and_all_enabled_scope_model_boundaries(self):
        cases = (
            (
                BackupScope.POS,
                (ProductOwner.POS,),
                ProductOwner.POS,
                ProductOwner.WMS,
            ),
            (
                BackupScope.WMS,
                (ProductOwner.WMS,),
                ProductOwner.WMS,
                ProductOwner.POS,
            ),
        )
        for scope, products, included, excluded in cases:
            with self.subTest(scope=scope):
                resolved = resolve_component_plan(
                    scope=scope,
                    enabled_products=products,
                )
                owners = {item.product_owner for item in resolved.export_components}
                self.assertIn(ProductOwner.SHARED, owners)
                self.assertIn(included, owners)
                self.assertNotIn(excluded, owners)

        all_enabled = resolve_component_plan(
            scope=BackupScope.ALL_ENABLED,
            enabled_products=(ProductOwner.POS,),
        )
        self.assertNotIn(
            ProductOwner.WMS,
            {item.product_owner for item in all_enabled.export_components},
        )

    def test_product_entitlement_and_unknown_component_fail_closed(self):
        with self.assertRaises(BackupScopeNotAllowed):
            resolve_component_plan(
                scope=BackupScope.WMS,
                enabled_products=(ProductOwner.POS,),
            )
        with self.assertRaises(UnknownBackupComponent):
            resolve_component_plan(
                scope=BackupScope.POS,
                enabled_products=(ProductOwner.POS,),
                requested_component_keys=("future.unknown",),
            )
        transient = COMPONENT_REGISTRY.get("pos.transient_sales")
        self.assertEqual(transient.restore_behavior, RestoreBehavior.NON_RESTORABLE)
        self.assertEqual(transient.scope_eligibility, ())

    def test_empty_shared_and_wrong_product_entitlements_fail_closed(self):
        invalid_resolution_cases = (
            (BackupScope.POS, ()),
            (BackupScope.POS, (ProductOwner.WMS,)),
            (BackupScope.WMS, (ProductOwner.POS,)),
            (BackupScope.ALL_ENABLED, ()),
            (BackupScope.ALL_ENABLED, (ProductOwner.SHARED,)),
            (
                BackupScope.ALL_ENABLED,
                (ProductOwner.POS, ProductOwner.SHARED),
            ),
            (BackupScope.ALL_ENABLED, ("future.product",)),
        )
        for scope, products in invalid_resolution_cases:
            with self.subTest(scope=scope, products=products):
                with self.assertRaises(BackupScopeNotAllowed):
                    resolve_component_plan(
                        scope=scope,
                        enabled_products=products,
                    )

        pos_plan = resolve_component_plan(
            scope=BackupScope.POS,
            enabled_products=(ProductOwner.POS,),
        ).export_components
        pos_context = self._context_for(
            BackupScope.POS,
            (ProductOwner.POS,),
        )
        invalid_contexts = (
            replace(pos_context, resolved_products=()),
            replace(
                pos_context,
                resolved_products=(ProductOwner.SHARED,),
            ),
            replace(
                pos_context,
                resolved_products=(ProductOwner.WMS,),
            ),
            replace(
                pos_context,
                resolved_products=(ProductOwner.POS, ProductOwner.WMS),
            ),
            replace(
                pos_context,
                resolved_products=(ProductOwner.POS, ProductOwner.POS),
            ),
            replace(
                pos_context,
                resolved_products=("POS",),
            ),
            replace(
                pos_context,
                requested_scope=BackupScope.WMS,
                resolved_products=(ProductOwner.WMS,),
            ),
        )
        for context in invalid_contexts:
            with self.subTest(
                scope=context.requested_scope,
                products=context.resolved_products,
            ):
                with self.assertRaises(ComponentExportValidationError):
                    self.registry.validate_component_plan(
                        context=context,
                        component_plan=pos_plan,
                    )

    def test_duck_typed_and_stateful_component_plan_items_fail_closed(self):
        context = self._context_for(BackupScope.POS, (ProductOwner.POS,))
        canonical = resolve_component_plan(
            scope=BackupScope.POS,
            enabled_products=(ProductOwner.POS,),
        ).export_components[0]

        class DuckTypedPlanItem:
            key = canonical.key
            product_owner = canonical.product_owner
            component_version = canonical.component_version
            restore_behavior = canonical.restore_behavior
            required_component_keys = canonical.required_component_keys
            export_order = canonical.export_order
            import_order = canonical.import_order

        class StatefulPlanItem:
            def __init__(self):
                self.read_count = 0

            def __getattr__(self, name):
                object.__setattr__(self, "read_count", self.read_count + 1)
                return getattr(canonical, name)

        duck_typed = DuckTypedPlanItem()
        stateful = StatefulPlanItem()
        for label, item in (
            ("duck_typed", duck_typed),
            ("stateful", stateful),
        ):
            with self.subTest(case=label):
                with self.assertRaises(ComponentExportValidationError):
                    self.registry.validate_component_plan(
                        context=context,
                        component_plan=(item,),
                    )
        self.assertEqual(stateful.read_count, 0)

    def test_forged_component_item_missing_dependency_and_order_fail_closed(self):
        context = self._context_for(BackupScope.POS, (ProductOwner.POS,))
        plan = resolve_component_plan(
            scope=BackupScope.POS,
            enabled_products=(ProductOwner.POS,),
        ).export_components
        forged = (
            *plan[:-1],
            replace(plan[-1], component_version="forged"),
        )
        with self.assertRaises(ComponentExportValidationError):
            self.registry.validate_component_plan(
                context=context,
                component_plan=forged,
            )
        with self.assertRaises(ComponentExportValidationError):
            self.registry.validate_component_plan(
                context=context,
                component_plan=plan[1:],
            )
        with self.assertRaises(ComponentExportValidationError):
            self.registry.validate_component_plan(
                context=context,
                component_plan=tuple(reversed(plan)),
            )

    def test_partial_plan_missing_forward_reference_target_fails_closed(self):
        context = self._context_for(BackupScope.POS, (ProductOwner.POS,))
        partial = resolve_component_plan(
            scope=BackupScope.POS,
            enabled_products=(ProductOwner.POS,),
            requested_component_keys=("pos.catalog",),
        ).export_components
        with self.assertRaises(ComponentExportValidationError):
            self.registry.validate_component_plan(
                context=context,
                component_plan=partial,
            )

    def test_restore_semantics_remain_reference_dependency_and_non_restorable(self):
        self.assertEqual(
            COMPONENT_REGISTRY.get("shared.tenant_identity").restore_behavior,
            RestoreBehavior.REFERENCE_ONLY,
        )
        self.assertEqual(
            COMPONENT_REGISTRY.get("shared.locations").restore_behavior,
            RestoreBehavior.DEPENDENCY_ONLY,
        )
        self.assertEqual(
            COMPONENT_REGISTRY.get("pos.transient_sales").restore_behavior,
            RestoreBehavior.NON_RESTORABLE,
        )
        self.assertIsNone(self.registry.maybe_get("sales.HeldSale"))

    def test_policy_bounds_and_system_check_ids_fail_closed(self):
        invalid_policies = (
            replace(_default_policy(), fetch_batch_size=0),
            replace(_default_policy(), fetch_batch_size=10_001),
            replace(_default_policy(), fetch_batch_size=1.0),
            replace(_default_policy(), fetch_batch_size=Decimal(1)),
            replace(_default_policy(), component_timeout_seconds=0),
            replace(_default_policy(), maximum_records_bytes=0),
            replace(_default_policy(), maximum_records_bytes=1.0),
            replace(_default_policy(), maximum_media_index_bytes=0),
            replace(_default_policy(), maximum_json_depth=101),
            replace(_default_policy(), maximum_media_name_length=4097),
        )
        for policy in invalid_policies:
            with self.subTest(policy=policy):
                with self.assertRaises(LogicalExportPolicyError):
                    policy.validated()

        with override_settings(BACKUP_LOGICAL_EXPORT_FETCH_BATCH_SIZE=0):
            self.assertEqual(
                {error.id for error in check_logical_export_policy_settings(None)},
                {"backups.E022"},
            )
        with override_settings(BACKUP_LOGICAL_EXPORT_MAX_ROW_INPUT_BYTES=0):
            self.assertEqual(
                {error.id for error in check_logical_export_policy_settings(None)},
                {"backups.E022"},
            )
        self.assertEqual(check_logical_export_registry(None), [])

    def test_row_input_and_fetch_memory_cross_product_bounds(self):
        for value in (
            True,
            0,
            -1,
            1.0,
            8 * 1024**2 + 1,
        ):
            with self.subTest(row_input_bytes=value):
                with self.assertRaises(LogicalExportPolicyError):
                    replace(
                        _default_policy(),
                        maximum_row_input_bytes=value,
                    ).validated()

        exact_boundary = _default_policy(
            fetch_batch_size=2,
            maximum_row_input_bytes=8 * 1024**2,
        )
        self.assertEqual(
            exact_boundary.fetch_batch_size * exact_boundary.maximum_row_input_bytes,
            MAXIMUM_LOGICAL_FETCH_MEMORY_BYTES,
        )
        for batch_size, row_input_bytes in (
            (9, 8 * 1024**2),
            (65, 1024**2),
            (10_000, 6_711),
        ):
            with self.subTest(
                fetch_batch_size=batch_size,
                row_input_bytes=row_input_bytes,
            ):
                with self.assertRaises(LogicalExportPolicyError):
                    _default_policy(
                        fetch_batch_size=batch_size,
                        maximum_row_input_bytes=row_input_bytes,
                    )

    def test_system_checks_do_not_create_workspace_open_sqlite_or_read_media(self):
        with (
            mock.patch.object(
                BackupWorkspaceManager,
                "create",
                side_effect=AssertionError("must not create a workspace"),
            ),
            mock.patch.object(
                SQLiteSnapshotProvider,
                "create_snapshot",
                side_effect=AssertionError("must not open SQLite"),
            ),
            mock.patch.object(
                FileSystemStorage,
                "open",
                side_effect=AssertionError("must not read media"),
            ),
        ):
            self.assertEqual(check_logical_export_policy_settings(None), [])
            self.assertEqual(check_logical_export_registry(None), [])

    def test_capability_flags_and_pipeline_stages_remain_non_operational(self):
        self.assertTrue(SQLITE_SNAPSHOT_PROVIDER_READY)
        self.assertTrue(TENANT_LOGICAL_EXPORT_PROVIDER_READY)
        self.assertFalse(OPERATIONAL_PROVIDER_STACK_READY)
        self.assertFalse(real_execution_available())
        capability = get_engine_capability()
        self.assertTrue(capability.snapshot_provider_ready)
        self.assertTrue(capability.logical_export_provider_ready)
        self.assertFalse(capability.provider_stack_ready)
        reports = {report.stage: report for report in planning_stage_reports()}
        self.assertEqual(
            reports[PipelineStage.PREPARE_SNAPSHOT].state,
            PipelineStageState.PLANNED,
        )
        self.assertEqual(
            reports[PipelineStage.EXPORT_COMPONENTS].state,
            PipelineStageState.PLANNED,
        )
        for stage in (
            PipelineStage.BUILD_PACKAGE,
            PipelineStage.VERIFY_ARTIFACT,
            PipelineStage.FINALIZE_METADATA,
            PipelineStage.COMPLETE,
        ):
            self.assertEqual(reports[stage].state, PipelineStageState.NOT_STARTED)

    def test_real_planning_performs_no_sqlite_or_workspace_export_work(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        backup = self.make_backup()
        with (
            mock.patch.object(
                BackupWorkspaceManager,
                "create",
                side_effect=AssertionError("planning must not create a workspace"),
            ),
            mock.patch.object(
                SQLiteSnapshotProvider,
                "create_snapshot",
                side_effect=AssertionError("planning must not snapshot"),
            ),
            mock.patch.object(
                SQLiteLogicalComponentExporter,
                "export_component",
                side_effect=AssertionError("planning must not export"),
            ),
        ):
            plan = prepare_backup_execution(
                business=self.business_a,
                backup_record=backup,
                actor=self.owner_a,
            )
        self.assertFalse(plan.real_execution_available)
        self.assertIsNone(plan.context.workspace_reference)
        reports = {item.stage: item.state for item in plan.stage_reports}
        self.assertEqual(reports[PipelineStage.PREPARE_SNAPSHOT], PipelineStageState.PLANNED)
        self.assertEqual(reports[PipelineStage.EXPORT_COMPONENTS], PipelineStageState.PLANNED)

    def test_execute_backup_remains_plain_disabled_and_unregistered(self):
        self.assertFalse(hasattr(execute_backup, "delay"))
        self.assertFalse(hasattr(execute_backup, "apply_async"))
        source = inspect.getsource(execute_backup)
        self.assertIn("assert_real_execution_available", source)
        self.assertNotIn("@shared_task", source)
        self.assertNotIn("@app.task", source)

    def test_no_http_surface_invokes_export_and_no_phase2d_artifact_is_created(self):
        from apps.backups import forms, platform_views, views
        from apps.backups.engine import manifest, packaging

        http_source = "\n".join(
            inspect.getsource(module) for module in (forms, views, platform_views)
        )
        self.assertNotIn("SQLiteLogicalComponentExporter", http_source)
        self.assertNotIn("export_snapshot_components", http_source)
        packaging_source = inspect.getsource(packaging)
        manifest_source = inspect.getsource(manifest)
        for forbidden in (
            "encrypt",
            "compress",
            "upload",
            "retention",
            "restore_package",
        ):
            self.assertNotIn(forbidden, packaging_source.lower())
        self.assertNotIn("records.ndjson", manifest_source)
