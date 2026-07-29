"""Focused security tests for Phase 2D-1 media capture and canonical manifests."""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import sqlite3
import stat
import tempfile
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.apps import apps as django_apps
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from apps.backups.engine.availability import (
    CANONICAL_MANIFEST_PROVIDER_READY,
    MEDIA_CAPTURE_PROVIDER_READY,
    OPERATIONAL_PROVIDER_STACK_READY,
    SQLITE_SNAPSHOT_PROVIDER_READY,
    TENANT_LOGICAL_EXPORT_PROVIDER_READY,
    get_engine_capability,
    real_execution_available,
)
from apps.backups.engine.canonical_manifest import (
    CanonicalManifestBuildRequest,
    CanonicalManifestProvider,
    ManifestMediaItem,
    MediaSource,
    PayloadDigest,
    ReconciledComponent,
    build_manifest_document,
    calculate_component_content_sha256,
    calculate_payload_set_sha256,
)
from apps.backups.engine.checks import (
    check_backup_capability_consistency,
    check_media_capture_policy_settings,
    check_media_storage_configuration,
)
from apps.backups.engine.context import (
    ActorIdentitySnapshot,
    BackupExecutionContext,
)
from apps.backups.engine.contracts import (
    ComponentExportReference,
    ComponentExportRequest,
    ComponentExportResult,
    MediaCaptureReference,
    MediaCaptureResult,
    Phase2D1Request,
    SnapshotReference,
    SnapshotResult,
)
from apps.backups.engine.exceptions import (
    CanonicalManifestCleanupError,
    CanonicalManifestCreationError,
    CanonicalManifestNotFound,
    ComponentContentMismatch,
    ComponentExportCleanupError,
    ComponentExportNotFound,
    CrossTenantMediaReference,
    InsufficientMediaCaptureCapacity,
    LogicalExportEngineError,
    MediaCaptureCleanupError,
    MediaCaptureCreationError,
    MediaCaptureLimitExceeded,
    MediaCapturePolicyError,
    MediaCaptureTimeout,
    MediaIndexValidationError,
    MediaObjectChanged,
    MediaObjectNotFound,
    MediaStorageAliasCollision,
    MediaStorageNameCollision,
    Phase2D1CoordinationError,
    UnsafeMediaReference,
    UnsafeMediaStorageObject,
    UnsupportedMediaStorageBackend,
)
from apps.backups.engine.logical_export import (
    LOGICAL_EXPORT_PROVIDER_IDENTIFIER,
    ComponentExportStream,
    SQLiteLogicalComponentExporter,
)
from apps.backups.engine.logical_export_policy import LogicalExportPolicy
from apps.backups.engine.logical_export_registry import (
    get_logical_export_registry,
)
from apps.backups.engine.logical_serialization import (
    DETERMINISTIC_ORDERING_VERSION,
    LOGICAL_MEDIA_REFERENCE_SCHEMA,
    LOGICAL_RECORD_SCHEMA,
    encode_canonical_document,
    validate_media_storage_name,
)
from apps.backups.engine.media_capture import (
    LOCAL_FILESYSTEM_MEDIA_CAPTURE_PROVIDER_IDENTIFIER,
    MEDIA_CONTENT_FILE_NAME,
    LocalFilesystemMediaCaptureProvider,
)
from apps.backups.engine.media_capture_policy import (
    MediaCapturePolicy,
    required_media_staging_capacity,
)
from apps.backups.engine.phase2d1 import Phase2D1Coordinator
from apps.backups.engine.pipeline import resolve_component_plan
from apps.backups.engine.sqlite_snapshot import (
    SQLITE_SNAPSHOT_PROVIDER_IDENTIFIER,
)
from apps.backups.engine.workspace import (
    WorkspaceArea,
    WorkspaceReference,
)
from apps.backups.enums import (
    BackupScope,
    BackupTrigger,
    ProductOwner,
)
from apps.backups.tasks import execute_backup

from .test_backups_phase2b_snapshot import SQLiteSnapshotTestCase


def _logical_policy(**changes):
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


def _media_policy(**changes):
    values = {
        "chunk_bytes": 4096,
        "maximum_file_bytes": 1024 * 1024,
        "maximum_total_bytes": 2 * 1024 * 1024,
        "maximum_objects": 100,
        "timeout_seconds": 30.0,
        "minimum_free_bytes": 1,
        "headroom_multiplier": 1.0,
        "require_local_staging": True,
        "media_index_maximum_line_bytes": 65_536,
    }
    values.update(changes)
    return MediaCapturePolicy(**values).validated()


def _write_stable_media_fixture(path, content):
    """Publish and settle test media before a snapshot cutoff is taken."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    write_flags |= getattr(os, "O_CLOEXEC", 0)
    write_flags |= getattr(os, "O_NOFOLLOW", 0)
    write_flags |= getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, write_flags, 0o600)
    try:
        view = memoryview(content)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise AssertionError("Media fixture write did not progress")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    metadata_deadline = time.monotonic() + 1.0
    previous_state = None
    stable_samples = 0
    while True:
        after = os.stat(path, follow_symlinks=False)
        current_state = tuple(
            getattr(after, name)
            for name in fields
        )
        if current_state == previous_state:
            stable_samples += 1
        else:
            stable_samples = 0
        if stable_samples >= 2:
            break
        if time.monotonic() > metadata_deadline:
            raise AssertionError("Media fixture metadata did not settle")
        previous_state = current_state
        time.sleep(0.005)
    safe_wall_time_ns = after.st_mtime_ns + 5_000_000
    deadline = time.monotonic() + 1.0
    while time.time_ns() <= safe_wall_time_ns:
        if time.monotonic() > deadline:
            raise AssertionError(
                "Media fixture timestamp did not precede cutoff"
            )
        time.sleep(0.001)
    return path


class SnapshotCutoffAndPolicyTests(SQLiteSnapshotTestCase):
    def test_snapshot_cutoff_is_aware_utc_and_precedes_result_creation(self):
        provider, context, result = self.create_snapshot()
        try:
            self.assertTrue(timezone.is_aware(result.consistency_cutoff_at))
            self.assertIs(result.consistency_cutoff_at.tzinfo, UTC)
            self.assertLessEqual(result.consistency_cutoff_at, result.created_at)
            self.assertEqual(
                result.provider_identifier,
                SQLITE_SNAPSHOT_PROVIDER_IDENTIFIER,
            )
        finally:
            provider.cleanup_snapshot(
                context=context,
                reference=result.reference,
            )

    def test_snapshot_samples_cutoff_before_destination_normalization(self):
        events = []
        connection_count = 0

        class _BackupOrderConnection:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def backup(self, target, **kwargs):
                events.append("backup_start")
                result = self.connection.backup(target, **kwargs)
                events.append("backup_end")
                return result

        def connection_factory(*args, **kwargs):
            nonlocal connection_count
            connection_count += 1
            connection = sqlite3.connect(*args, **kwargs)
            if connection_count == 1:
                return _BackupOrderConnection(connection)
            return connection

        provider = self.provider(connection_factory=connection_factory)
        cutoff = datetime(2026, 7, 29, 20, 0, tzinfo=UTC)
        created = cutoff + timedelta(seconds=1)
        original_normalize = provider._normalize_destination_journal

        def sample_time():
            events.append("clock")
            return (cutoff, created)[events.count("clock") - 1]

        with mock.patch(
            "apps.backups.engine.sqlite_snapshot.timezone.now",
            side_effect=sample_time,
        ) as clock:

            def checking_normalize(connection):
                self.assertEqual(clock.call_count, 1)
                events.append("normalize")
                return original_normalize(connection)

            with mock.patch.object(
                provider,
                "_normalize_destination_journal",
                side_effect=checking_normalize,
            ):
                selected, context, result = self.create_snapshot(provider=provider)
        try:
            self.assertEqual(result.consistency_cutoff_at, cutoff)
            self.assertEqual(result.created_at, created)
            self.assertLess(
                events.index("backup_end"),
                events.index("clock"),
            )
            self.assertLess(
                events.index("clock"),
                events.index("normalize"),
            )
        finally:
            selected.cleanup_snapshot(
                context=context,
                reference=result.reference,
            )

    def test_media_policy_defaults_capacity_and_fail_closed_values(self):
        defaults = MediaCapturePolicy.from_settings()
        self.assertEqual(defaults.chunk_bytes, 1_048_576)
        self.assertEqual(defaults.maximum_file_bytes, 67_108_864)
        self.assertEqual(defaults.maximum_total_bytes, 4_294_967_296)
        self.assertEqual(defaults.maximum_objects, 100_000)
        self.assertEqual(defaults.timeout_seconds, 1800.0)
        self.assertEqual(defaults.minimum_free_bytes, 1_073_741_824)
        self.assertEqual(defaults.headroom_multiplier, 1.25)
        self.assertIs(defaults.require_local_staging, True)
        self.assertEqual(defaults.media_index_maximum_line_bytes, 65_536)
        self.assertEqual(
            required_media_staging_capacity(
                byte_count=5,
                policy=_media_policy(
                    minimum_free_bytes=1,
                    headroom_multiplier=1.25,
                ),
            ),
            7,
        )

        invalid_changes = (
            {"chunk_bytes": True},
            {"chunk_bytes": 4095},
            {"maximum_file_bytes": False},
            {"maximum_total_bytes": 1024},
            {"maximum_objects": 0},
            {"timeout_seconds": float("nan")},
            {"timeout_seconds": float("inf")},
            {"minimum_free_bytes": True},
            {"headroom_multiplier": float("-inf")},
            {"headroom_multiplier": 0.99},
            {"require_local_staging": 1},
            {"media_index_maximum_line_bytes": 127},
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes):
                with self.assertRaises(MediaCapturePolicyError):
                    _media_policy(**changes)

    @override_settings(BACKUP_MEDIA_CAPTURE_CHUNK_BYTES=True)
    def test_policy_system_check_reports_bounded_error_without_io(self):
        with mock.patch("pathlib.Path.open") as open_file:
            errors = check_media_capture_policy_settings(None)
        self.assertEqual([error.id for error in errors], ["backups.E024"])
        open_file.assert_not_called()


class RegistryAndTenantMediaIsolationTests(SQLiteSnapshotTestCase):
    def setUp(self):
        super().setUp()
        self.registry = get_logical_export_registry()
        self.plan = resolve_component_plan(
            scope=BackupScope.POS,
            enabled_products=(ProductOwner.POS,),
        ).export_components
        self.snapshot_cleanup = []
        self.export_cleanup = []
        self._next_ids = {}

    def tearDown(self):
        for exporter, reference in reversed(self.export_cleanup):
            try:
                exporter.cleanup_component_export(
                    context=self.context,
                    reference=reference,
                )
            except LogicalExportEngineError:
                pass
        for provider, reference in reversed(self.snapshot_cleanup):
            try:
                provider.cleanup_snapshot(
                    context=self.context,
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
            f"CREATE TABLE IF NOT EXISTS {self._quote(model._meta.db_table)} "
            f"({columns})"
        )
        return model

    def _install_export_schema(self):
        for spec in self.registry.specs:
            self._create_model_table(spec.model_label)
        self._create_model_table("accounts.User")

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
        return {
            field.name: values[field.column]
            for field in model._meta.concrete_fields
        }

    def _seed_tenant(self):
        self._insert(
            "accounts.User",
            id=91,
            public_id=uuid.uuid4(),
            email="secret@example.test",
            password="private",
            full_name="Private",
        )
        self._insert(
            "tenants.Business",
            id=self.context.business_id,
            public_id=self.context.business_public_id,
            owner=91,
            name="Selected Tenant",
        )

    def _snapshot(self):
        provider, context, result = self.create_snapshot()
        self.snapshot_cleanup.append((provider, result.reference))
        return provider, context, result

    def _component(self, key):
        return next(item for item in self.plan if item.key == key)

    def _exporter(self, provider):
        return SQLiteLogicalComponentExporter(
            snapshot_provider=provider,
            workspace_manager=self.manager,
            registry=self.registry,
            policy=_logical_policy(),
        )

    def test_registry_classifies_exactly_all_five_concrete_media_fields(self):
        self.assertIs(self.registry.validate_complete(), True)
        declared = {
            f"{spec.model_label}.{field_name}"
            for spec in self.registry.specs
            for field_name in spec.media_fields
        }
        self.assertEqual(
            declared,
            {
                "tenants.Business.logo",
                "catalog.Product.image",
                "catalog.ProductVariant.image",
                "purchases.Purchase.attachment",
                "expenses.Expense.attachment",
            },
        )
        concrete = {
            f"{model._meta.label}.{field.name}"
            for model in django_apps.get_models()
            for field in model._meta.concrete_fields
            if isinstance(field, models.FileField)
        }
        self.assertEqual(concrete, declared)

    def test_cross_tenant_name_in_another_component_aborts_sanitized(self):
        self._install_export_schema()
        self._seed_tenant()
        storage_name = "private/cross-tenant-secret.pdf"
        self._insert(
            "catalog.Product",
            business=self.context.business_id,
            image=storage_name,
            name="Selected product",
            sku="SELECTED",
        )
        self._insert(
            "tenants.Business",
            id=18,
            public_id=uuid.uuid4(),
            owner=91,
            name="Other Tenant",
        )
        self._insert(
            "expenses.Expense",
            business=18,
            branch=800,
            category=801,
            attachment=storage_name,
        )
        provider, _context, snapshot = self._snapshot()
        exporter = self._exporter(provider)

        with self.assertRaises(CrossTenantMediaReference) as raised:
            exporter.export_component(
                ComponentExportRequest(
                    context=self.context,
                    component=self._component("pos.catalog"),
                    snapshot=snapshot.reference,
                    component_plan=self.plan,
                )
            )

        rendered = f"{raised.exception!s} {raised.exception!r}"
        self.assertNotIn(storage_name, rendered)
        self.assertNotIn("Selected Tenant", rendered)
        self.assertNotIn("Other Tenant", rendered)
        self.assertNotIn("expenses_", rendered)
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    def test_wms_scope_scans_conflicting_pos_media_field_outside_scope(self):
        wms_plan = resolve_component_plan(
            scope=BackupScope.WMS,
            enabled_products=(ProductOwner.WMS,),
        ).export_components
        self.assertNotIn(
            "pos.expenses",
            {component.key for component in wms_plan},
        )
        self.context = replace(
            self.context,
            requested_scope=BackupScope.WMS,
            resolved_products=(ProductOwner.WMS,),
        )
        self._install_export_schema()
        self._seed_tenant()
        storage_name = "tenant/strict-global-scan.png"
        business = django_apps.get_model("tenants.Business")
        self.source.execute(
            f"UPDATE {self._quote(business._meta.db_table)} "
            f"SET {self._quote(business._meta.get_field('logo').column)} = ? "
            f"WHERE {self._quote(business._meta.pk.column)} = ?",
            (storage_name, self.context.business_id),
        )
        self._insert(
            "tenants.Business",
            id=18,
            public_id=uuid.uuid4(),
            owner=91,
            name="Other POS Tenant",
        )
        self._insert(
            "expenses.Expense",
            business=18,
            branch=800,
            category=801,
            attachment=storage_name,
        )
        provider, _context, snapshot = self._snapshot()
        exporter = self._exporter(provider)
        component = next(
            item
            for item in wms_plan
            if item.key == "shared.tenant_identity"
        )
        with self.assertRaises(CrossTenantMediaReference) as raised:
            exporter.export_component(
                ComponentExportRequest(
                    context=self.context,
                    component=component,
                    snapshot=snapshot.reference,
                    component_plan=wms_plan,
                )
            )
        rendered = f"{raised.exception!s} {raised.exception!r}"
        self.assertNotIn(storage_name, rendered)
        self.assertNotIn("Other POS Tenant", rendered)
        self.assertNotIn("expenses_", rendered)

    def test_same_tenant_may_reference_exact_name_multiple_times(self):
        self._install_export_schema()
        self._seed_tenant()
        storage_name = "products/shared-safe.png"
        for suffix in ("A", "B"):
            self._insert(
                "catalog.Product",
                business=self.context.business_id,
                image=storage_name,
                name=f"Product {suffix}",
                sku=suffix,
            )
        provider, _context, snapshot = self._snapshot()
        exporter = self._exporter(provider)
        result = exporter.export_component(
            ComponentExportRequest(
                context=self.context,
                component=self._component("pos.catalog"),
                snapshot=snapshot.reference,
                component_plan=self.plan,
            )
        )
        self.export_cleanup.append((exporter, result.reference))
        self.assertEqual(result.media_count, 2)

    def test_real_phase2d1_coordinator_preserves_exact_outputs_and_sources(self):
        self.context = replace(
            self.context,
            schema_migration_fingerprint="b" * 64,
            application_version="phase2d1-e2e",
            backup_format_version="2d1-e2e",
            minimum_restore_version="phase2d1-e2e",
        )
        self._install_export_schema()
        self._seed_tenant()
        self._insert("tenants.BusinessSettings")
        media_root = self.root / "coordinator-media"
        (media_root / "products").mkdir(parents=True)
        common_content = b"same bytes with distinct restore semantics"
        for path in (
            media_root / "products/a.bin",
            media_root / "products/b.bin",
        ):
            _write_stable_media_fixture(path, common_content)
        for suffix, storage_name in (
            ("A", "products/a.bin"),
            ("B", "products/a.bin"),
            ("C", "products/b.bin"),
        ):
            self._insert(
                "catalog.Product",
                business=self.context.business_id,
                image=storage_name,
                name=f"Product {suffix}",
                sku=suffix,
            )
        snapshot_provider, _context, snapshot = self._snapshot()
        exporter = self._exporter(snapshot_provider)
        component_results = []
        for component in self.plan:
            exported = exporter.export_component(
                ComponentExportRequest(
                    context=self.context,
                    component=component,
                    snapshot=snapshot.reference,
                    component_plan=self.plan,
                )
            )
            component_results.append(exported)
            self.export_cleanup.append((exporter, exported.reference))

        class _LocalInspector:
            @staticmethod
            def assess(_path):
                return SimpleNamespace(confirmed_local=True)

        media_provider = LocalFilesystemMediaCaptureProvider(
            snapshot_provider=snapshot_provider,
            workspace_manager=self.manager,
            policy=_media_policy(),
            storage_resolver=lambda: FileSystemStorage(
                location=str(media_root)
            ),
            filesystem_inspector=_LocalInspector(),
            disk_usage_provider=lambda _path: SimpleNamespace(
                free=10**12
            ),
        )
        manifest_provider = CanonicalManifestProvider(
            workspace_manager=self.manager,
        )
        coordinator = Phase2D1Coordinator(
            component_exporter=exporter,
            media_capture_provider=media_provider,
            manifest_provider=manifest_provider,
        )
        phase_result = None
        try:
            with self.settings(MEDIA_ROOT=str(media_root)):
                phase_result = coordinator.build(
                    Phase2D1Request(
                        context=self.context,
                        snapshot_result=snapshot,
                        component_plan=self.plan,
                        component_exports=tuple(component_results),
                    )
                )
            self.assertEqual(
                phase_result.component_exports,
                tuple(component_results),
            )
            self.assertEqual(
                [
                    item.logical_storage_name
                    for item in phase_result.media_captures
                ],
                ["products/a.bin", "products/b.bin"],
            )
            self.assertEqual(
                [
                    item.source_reference_count
                    for item in phase_result.media_captures
                ],
                [2, 1],
            )
            self.assertEqual(
                phase_result.media_captures[0].sha256,
                phase_result.media_captures[1].sha256,
            )
            expected_media_sha256 = hashlib.sha256(
                common_content
            ).hexdigest()
            self.assertEqual(
                [item.sha256 for item in phase_result.media_captures],
                [expected_media_sha256, expected_media_sha256],
            )
            self.assertNotEqual(
                phase_result.media_captures[0].reference,
                phase_result.media_captures[1].reference,
            )
            with manifest_provider.open_manifest(
                context=self.context,
                reference=phase_result.manifest.reference,
            ) as reader:
                raw_manifest = reader.read()
            manifest = json.loads(raw_manifest)
            self.assertEqual(
                [item["storage_name"] for item in manifest["media"]],
                ["products/a.bin", "products/b.bin"],
            )
            self.assertEqual(
                [
                    item["source_reference_count"]
                    for item in manifest["media"]
                ],
                [2, 1],
            )
            self.assertEqual(
                [item["sha256"] for item in manifest["media"]],
                [
                    item.sha256
                    for item in phase_result.media_captures
                ],
            )
            self.assertEqual(
                [item["sha256"] for item in manifest["media"]],
                [expected_media_sha256, expected_media_sha256],
            )
            self.assertEqual(
                manifest["totals"]["media_reference_count"],
                3,
            )
            self.assertEqual(
                manifest["totals"]["unique_media_object_count"],
                2,
            )
            self.assertEqual(
                phase_result.manifest.sha256,
                hashlib.sha256(raw_manifest).hexdigest(),
            )
            rendered = raw_manifest.decode("utf-8")
            self.assertNotIn(str(self.source_path), rendered)
            self.assertNotIn(str(self.staging_root), rendered)
            self.assertNotIn("snapshot.sqlite3", rendered)
        finally:
            if phase_result is not None:
                manifest_provider.cleanup_manifest(
                    context=self.context,
                    reference=phase_result.manifest.reference,
                )
                for capture in reversed(phase_result.media_captures):
                    media_provider.cleanup_media_capture(
                        context=self.context,
                        reference=capture.reference,
                    )

    def test_coordinator_failure_cleans_manifest_media_and_all_components_in_reverse(self):
        self.context = replace(
            self.context,
            schema_migration_fingerprint="c" * 64,
            application_version="phase2d1-cleanup",
            backup_format_version="2d1-cleanup",
            minimum_restore_version="phase2d1-cleanup",
        )
        self._install_export_schema()
        self._seed_tenant()
        self._insert("tenants.BusinessSettings")
        media_root = self.root / "rollback-media"
        (media_root / "products").mkdir(parents=True)
        _write_stable_media_fixture(
            media_root / "products/item.bin",
            b"rollback content",
        )
        self._insert(
            "catalog.Product",
            business=self.context.business_id,
            image="products/item.bin",
            name="Rollback product",
            sku="ROLLBACK",
        )
        snapshot_provider, _context, snapshot = self._snapshot()
        exporter = self._exporter(snapshot_provider)
        component_results = tuple(
            exporter.export_component(
                ComponentExportRequest(
                    context=self.context,
                    component=component,
                    snapshot=snapshot.reference,
                    component_plan=self.plan,
                )
            )
            for component in self.plan
        )
        self.export_cleanup.extend(
            (exporter, item.reference) for item in component_results
        )

        class _LocalInspector:
            @staticmethod
            def assess(_path):
                return SimpleNamespace(confirmed_local=True)

        media_provider = LocalFilesystemMediaCaptureProvider(
            snapshot_provider=snapshot_provider,
            workspace_manager=self.manager,
            policy=_media_policy(),
            storage_resolver=lambda: FileSystemStorage(
                location=str(media_root)
            ),
            filesystem_inspector=_LocalInspector(),
            disk_usage_provider=lambda _path: SimpleNamespace(
                free=10**12
            ),
        )
        manifest_provider = CanonicalManifestProvider(
            workspace_manager=self.manager,
        )

        def fail_before_return(stage):
            if stage == "before_coordinator_result_return":
                raise OSError("private coordinator detail")

        coordinator = Phase2D1Coordinator(
            component_exporter=exporter,
            media_capture_provider=media_provider,
            manifest_provider=manifest_provider,
            failure_hook=fail_before_return,
        )
        events = []
        original_manifest_cleanup = (
            manifest_provider.cleanup_manifest
        )
        original_media_cleanup = (
            media_provider.cleanup_media_capture
        )
        original_component_cleanup = (
            exporter.cleanup_component_export
        )

        def cleanup_manifest(**kwargs):
            events.append("manifest")
            return original_manifest_cleanup(**kwargs)

        def cleanup_media(**kwargs):
            events.append("media")
            return original_media_cleanup(**kwargs)

        def cleanup_component(**kwargs):
            events.append(
                f"component:{kwargs['reference'].identifier}"
            )
            return original_component_cleanup(**kwargs)

        with (
            mock.patch.object(
                manifest_provider,
                "cleanup_manifest",
                side_effect=cleanup_manifest,
            ),
            mock.patch.object(
                media_provider,
                "cleanup_media_capture",
                side_effect=cleanup_media,
            ),
            mock.patch.object(
                exporter,
                "cleanup_component_export",
                side_effect=cleanup_component,
            ),
            self.settings(MEDIA_ROOT=str(media_root)),
        ):
            with self.assertRaises(
                Phase2D1CoordinationError
            ) as raised:
                coordinator.build(
                    Phase2D1Request(
                        context=self.context,
                        snapshot_result=snapshot,
                        component_plan=self.plan,
                        component_exports=component_results,
                    )
                )
        self.assertFalse(raised.exception.cleanup_incomplete)
        self.assertNotIn(
            "private coordinator detail",
            str(raised.exception),
        )
        self.assertEqual(events[:2], ["manifest", "media"])
        self.assertEqual(
            events[2:],
            [
                f"component:{item.reference.identifier}"
                for item in reversed(component_results)
            ],
        )
        for result in component_results:
            with self.assertRaises(ComponentExportNotFound):
                with exporter.open_component_export(
                    context=self.context,
                    reference=result.reference,
                    stream=ComponentExportStream.RECORDS,
                ):
                    pass
        for area in (WorkspaceArea.MEDIA, WorkspaceArea.MANIFEST):
            parent = self.workspace.path / area.value
            if parent.exists():
                self.assertEqual(list(parent.iterdir()), [])

    def test_coordinator_rejects_forged_snapshot_duplicate_reference_and_first_component_failure(
        self,
    ):
        self.context = replace(
            self.context,
            schema_migration_fingerprint="d" * 64,
            application_version="phase2d1-adversarial",
            backup_format_version="2d1-adversarial",
            minimum_restore_version="phase2d1-adversarial",
        )
        self._install_export_schema()
        self._seed_tenant()
        self._insert("tenants.BusinessSettings")
        media_root = self.root / "adversarial-media"
        media_root.mkdir()
        snapshot_provider, _context, snapshot = self._snapshot()
        exporter = self._exporter(snapshot_provider)

        class _LocalInspector:
            @staticmethod
            def assess(_path):
                return SimpleNamespace(confirmed_local=True)

        media_provider = LocalFilesystemMediaCaptureProvider(
            snapshot_provider=snapshot_provider,
            workspace_manager=self.manager,
            policy=_media_policy(),
            storage_resolver=lambda: FileSystemStorage(
                location=str(media_root)
            ),
            filesystem_inspector=_LocalInspector(),
            disk_usage_provider=lambda _path: SimpleNamespace(
                free=10**12
            ),
        )
        manifest_provider = CanonicalManifestProvider(
            workspace_manager=self.manager,
        )

        def export_batch():
            results = tuple(
                exporter.export_component(
                    ComponentExportRequest(
                        context=self.context,
                        component=component,
                        snapshot=snapshot.reference,
                        component_plan=self.plan,
                    )
                )
                for component in self.plan
            )
            self.export_cleanup.extend(
                (exporter, item.reference) for item in results
            )
            return results

        coordinator = Phase2D1Coordinator(
            component_exporter=exporter,
            media_capture_provider=media_provider,
            manifest_provider=manifest_provider,
        )
        forged_batch = export_batch()
        forged_snapshot = replace(
            snapshot,
            consistency_cutoff_at=(
                snapshot.consistency_cutoff_at
                - timedelta(seconds=1)
            ),
        )
        with self.settings(MEDIA_ROOT=str(media_root)):
            with self.assertRaises(Phase2D1CoordinationError):
                coordinator.build(
                    Phase2D1Request(
                        context=self.context,
                        snapshot_result=forged_snapshot,
                        component_plan=self.plan,
                        component_exports=forged_batch,
                    )
                )
        for result in forged_batch:
            with self.assertRaises(ComponentExportNotFound):
                with exporter.open_component_export(
                    context=self.context,
                    reference=result.reference,
                    stream=ComponentExportStream.RECORDS,
                ):
                    pass

        duplicate_batch = export_batch()
        duplicate_plan = (
            self.plan[0],
            self.plan[0],
            *self.plan[1:],
        )
        duplicate_results = (
            duplicate_batch[0],
            duplicate_batch[0],
            *duplicate_batch[1:],
        )
        with self.settings(MEDIA_ROOT=str(media_root)):
            with self.assertRaises(ComponentContentMismatch):
                coordinator.build(
                    Phase2D1Request(
                        context=self.context,
                        snapshot_result=snapshot,
                        component_plan=duplicate_plan,
                        component_exports=duplicate_results,
                    )
                )
        for result in duplicate_batch:
            with self.assertRaises(ComponentExportNotFound):
                with exporter.open_component_export(
                    context=self.context,
                    reference=result.reference,
                    stream=ComponentExportStream.RECORDS,
                ):
                    pass

        first_component_batch = export_batch()
        reconciliations = {"count": 0}

        def fail_after_first(stage):
            if stage == "after_component_reconciliation":
                reconciliations["count"] += 1
                raise OSError("private first component detail")

        first_failure_coordinator = Phase2D1Coordinator(
            component_exporter=exporter,
            media_capture_provider=media_provider,
            manifest_provider=manifest_provider,
            failure_hook=fail_after_first,
        )
        with self.settings(MEDIA_ROOT=str(media_root)):
            with self.assertRaises(
                Phase2D1CoordinationError
            ) as raised:
                first_failure_coordinator.build(
                    Phase2D1Request(
                        context=self.context,
                        snapshot_result=snapshot,
                        component_plan=self.plan,
                        component_exports=first_component_batch,
                    )
                )
        self.assertEqual(reconciliations["count"], 1)
        self.assertFalse(raised.exception.cleanup_incomplete)
        self.assertNotIn(
            "private first component detail",
            str(raised.exception),
        )
        for result in first_component_batch:
            with self.assertRaises(ComponentExportNotFound):
                with exporter.open_component_export(
                    context=self.context,
                    reference=result.reference,
                    stream=ComponentExportStream.RECORDS,
                ):
                    pass

        original_cleanup = exporter.cleanup_component_export
        for abort_type in (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(abort_type=abort_type):
                abort_batch = export_batch()
                cleanup_failed = {"done": False}

                def abort_after_first(
                    stage,
                    *,
                    selected=abort_type,
                ):
                    if stage == "after_component_reconciliation":
                        raise selected("preserve coordinator abort")

                def clean_then_fail_once(
                    *,
                    state=cleanup_failed,
                    cleanup=original_cleanup,
                    **kwargs,
                ):
                    cleaned = cleanup(**kwargs)
                    if not state["done"]:
                        state["done"] = True
                        raise OSError("private rollback cleanup detail")
                    return cleaned

                aborting_coordinator = Phase2D1Coordinator(
                    component_exporter=exporter,
                    media_capture_provider=media_provider,
                    manifest_provider=manifest_provider,
                    failure_hook=abort_after_first,
                )
                with (
                    mock.patch.object(
                        exporter,
                        "cleanup_component_export",
                        side_effect=clean_then_fail_once,
                    ),
                    self.settings(MEDIA_ROOT=str(media_root)),
                    self.assertRaises(abort_type) as raised_abort,
                ):
                    aborting_coordinator.build(
                        Phase2D1Request(
                            context=self.context,
                            snapshot_result=snapshot,
                            component_plan=self.plan,
                            component_exports=abort_batch,
                        )
                    )
                self.assertTrue(
                    raised_abort.exception.cleanup_incomplete
                )
                self.assertTrue(cleanup_failed["done"])
                self.assertNotIn(
                    "private rollback cleanup detail",
                    str(raised_abort.exception),
                )
                for result in abort_batch:
                    with self.assertRaises(ComponentExportNotFound):
                        with exporter.open_component_export(
                            context=self.context,
                            reference=result.reference,
                            stream=ComponentExportStream.RECORDS,
                        ):
                            pass

    def test_exact_component_cleanup_tombstone_rejects_forged_context(self):
        self._install_export_schema()
        self._seed_tenant()
        snapshot_provider, _context, snapshot = self._snapshot()
        exporter = self._exporter(snapshot_provider)
        component = self._component("shared.tenant_identity")
        result = exporter.export_component(
            ComponentExportRequest(
                context=self.context,
                component=component,
                snapshot=snapshot.reference,
                component_plan=self.plan,
            )
        )
        self.assertIs(
            exporter.cleanup_component_export(
                context=self.context,
                reference=result.reference,
                require_exact_evidence=True,
            ),
            True,
        )
        self.assertIs(
            exporter.cleanup_component_export(
                context=self.context,
                reference=result.reference,
                require_exact_evidence=True,
            ),
            True,
        )
        forged_contexts = (
            replace(self.context, backup_public_id=uuid.uuid4()),
            replace(self.context, business_public_id=uuid.uuid4()),
            replace(self.context, business_id=self.context.business_id + 1),
        )
        for forged_context in forged_contexts:
            self.assertEqual(
                forged_context.workspace_reference,
                self.context.workspace_reference,
            )
            with self.subTest(forged_context=forged_context):
                with self.assertRaises(ComponentExportCleanupError):
                    exporter.cleanup_component_export(
                        context=forged_context,
                        reference=result.reference,
                        require_exact_evidence=True,
                    )


class StrictMediaIndexAndReconciliationTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.registry = get_logical_export_registry()
        self.context = SimpleNamespace(
            business_public_id=uuid.UUID(
                "11111111-2222-3333-4444-555555555555"
            )
        )
        self.component = next(
            item
            for item in resolve_component_plan(
                scope=BackupScope.POS,
                enabled_products=(ProductOwner.POS,),
            ).export_components
            if item.key == "pos.catalog"
        )
        self.coordinator = object.__new__(Phase2D1Coordinator)
        self.coordinator.registry = self.registry

    def _payload(self, **changes):
        values = {
            "schema": LOGICAL_MEDIA_REFERENCE_SCHEMA,
            "component": self.component.key,
            "model": "catalog.Product",
            "tenant_public_id": str(self.context.business_public_id),
            "identity": {
                "public_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            },
            "field": "image",
            "storage_name": "products/safe.png",
        }
        values.update(changes)
        return values

    def _parse(self, raw):
        return self.coordinator._parse_media_line(
            raw_line=raw,
            context=self.context,
            component=self.component,
            component_ordinal=5,
            maximum_name_length=1024,
        )

    def test_public_canonical_document_api_is_exact_and_phase2c_compatible(self):
        payload = self._payload()
        encoded = encode_canonical_document(payload, trailing_lf=True)
        expected = (
            b'{"component":"pos.catalog","field":"image",'
            b'"identity":{"public_id":'
            b'"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},'
            b'"model":"catalog.Product",'
            b'"schema":"nexa.logical-media-reference.v1",'
            b'"storage_name":"products/safe.png",'
            b'"tenant_public_id":'
            b'"11111111-2222-3333-4444-555555555555"}\n'
        )
        self.assertEqual(encoded, expected)
        self.assertEqual(encoded, encode_canonical_document(payload) + b"\n")
        self.assertFalse(encoded.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(encoded.count(b"\n"), 1)
        self.assertEqual(self._parse(encoded).storage_name, "products/safe.png")
        self.assertEqual(
            json.loads(encoded),
            payload,
        )

    def test_strict_parser_rejects_noncanonical_and_forged_lines(self):
        canonical = encode_canonical_document(self._payload(), trailing_lf=True)
        invalid_lines = {
            "blank": b"\n",
            "crlf": canonical[:-1] + b"\r\n",
            "bom": b"\xef\xbb\xbf" + canonical,
            "missing_lf": canonical[:-1],
            "invalid_utf8": b"\xff\n",
            "malformed_json": b"{not-json}\n",
            "floating_identity": canonical.replace(
                (
                    b'"public_id":'
                    b'"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"'
                ),
                b'"public_id":1.5',
                1,
            ),
            "duplicate_key": canonical.replace(
                b'{"component":',
                b'{"component":"pos.catalog","component":',
                1,
            ),
            "noncanonical_order": json.dumps(
                self._payload(),
                separators=(",", ":"),
                sort_keys=False,
            ).encode()
            + b"\n",
            "extra_key": encode_canonical_document(
                self._payload(extra=True),
                trailing_lf=True,
            ),
            "wrong_schema": encode_canonical_document(
                self._payload(schema="wrong"),
                trailing_lf=True,
            ),
            "wrong_tenant": encode_canonical_document(
                self._payload(tenant_public_id=str(uuid.uuid4())),
                trailing_lf=True,
            ),
            "wrong_component": encode_canonical_document(
                self._payload(component="pos.sales"),
                trailing_lf=True,
            ),
            "unknown_model": encode_canonical_document(
                self._payload(model="catalog.Unknown"),
                trailing_lf=True,
            ),
            "unknown_field": encode_canonical_document(
                self._payload(field="name"),
                trailing_lf=True,
            ),
            "malformed_identity": encode_canonical_document(
                self._payload(identity={"public_id": "not-a-uuid"}),
                trailing_lf=True,
            ),
        }
        for label, raw in invalid_lines.items():
            with self.subTest(label=label):
                with self.assertRaises(MediaIndexValidationError):
                    self._parse(raw)

    def test_media_index_reader_enforces_line_and_metadata_bounds(self):
        canonical = encode_canonical_document(
            self._payload(),
            trailing_lf=True,
        )

        class _LineReader:
            def __init__(self, content):
                self.content = io.BytesIO(content)
                self.readline_sizes = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def readline(self, size):
                self.readline_sizes.append(size)
                return self.content.readline(size)

        exact_reader = _LineReader(canonical)
        self.coordinator.component_exporter = SimpleNamespace(
            open_component_export=mock.Mock(
                return_value=exact_reader
            )
        )
        exact_digest, exact_sources = (
            self.coordinator._read_media_index(
                context=self.context,
                component=self.component,
                result=SimpleNamespace(
                    reference=object(),
                    media_index_byte_count=len(canonical),
                    media_count=1,
                ),
                ordinal=5,
                logical_policy=_logical_policy(),
                media_policy=_media_policy(),
            )
        )
        self.assertEqual(exact_digest.byte_count, len(canonical))
        self.assertEqual(
            exact_digest.sha256,
            hashlib.sha256(canonical).hexdigest(),
        )
        self.assertEqual(
            exact_digest.package_path,
            "components/0005/media-index.ndjson",
        )
        self.assertEqual(
            [source.storage_name for source in exact_sources],
            ["products/safe.png"],
        )

        for label, content, expected_bytes, expected_count, policy in (
            (
                "overlong",
                canonical,
                len(canonical),
                1,
                _media_policy(
                    media_index_maximum_line_bytes=128
                ),
            ),
            (
                "byte_count",
                canonical,
                len(canonical) + 1,
                1,
                _media_policy(),
            ),
            (
                "reference_count",
                canonical,
                len(canonical),
                2,
                _media_policy(),
            ),
        ):
            with self.subTest(label=label):
                reader = _LineReader(content)
                self.coordinator.component_exporter = SimpleNamespace(
                    open_component_export=mock.Mock(
                        return_value=reader
                    )
                )
                expected_error = (
                    MediaIndexValidationError
                    if label == "overlong"
                    else ComponentContentMismatch
                )
                with self.assertRaises(expected_error):
                    self.coordinator._read_media_index(
                        context=self.context,
                        component=self.component,
                        result=SimpleNamespace(
                            reference=object(),
                            media_index_byte_count=expected_bytes,
                            media_count=expected_count,
                        ),
                        ordinal=5,
                        logical_policy=_logical_policy(),
                        media_policy=policy,
                    )
                self.assertTrue(reader.readline_sizes)
                self.assertEqual(
                    set(reader.readline_sizes),
                    {
                        policy.media_index_maximum_line_bytes + 1
                    },
                )

    def test_storage_name_validation_and_portable_collision_policy(self):
        unsafe_names = (
            "../escape",
            "/absolute",
            r"C:/drive.txt",
            "https://example.test/file",
            r"folder\file.txt",
            "folder/file:stream",
            "CON.txt",
            "folder\u2215file.txt",
            "folder//file.txt",
            "folder/../file.txt",
        )
        for storage_name in unsafe_names:
            with self.subTest(storage_name=storage_name):
                with self.assertRaises(UnsafeMediaReference):
                    validate_media_storage_name(
                        storage_name,
                        maximum_length=1024,
                    )

        for names in (
            ("products/Logo.PNG", "products/logo.png"),
            (
                "products/Caf\u00e9.png",
                "products/Cafe\u0301.png",
            ),
        ):
            with self.subTest(names=names):
                with self.assertRaises(MediaStorageNameCollision):
                    self.coordinator._validate_storage_name_collisions(
                        tuple(sorted(names))
                    )

    def test_component_stream_hashing_is_exact_bounded_and_reconciled(self):
        content = b'{"a":1}\n{"b":2}\n'

        class _Reader:
            def __init__(self, value):
                self.value = io.BytesIO(value)
                self.read_sizes = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size):
                self.read_sizes.append(size)
                return self.value.read(size)

        reader = _Reader(content)
        exporter = SimpleNamespace(
            open_component_export=mock.Mock(return_value=reader)
        )
        self.coordinator.component_exporter = exporter
        result = SimpleNamespace(
            reference=object(),
            byte_count=len(content),
            row_count=2,
        )
        digest = self.coordinator._hash_records_stream(
            context=object(),
            result=result,
            ordinal=5,
            chunk_bytes=4096,
        )
        self.assertEqual(digest.byte_count, len(content))
        self.assertEqual(digest.sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(
            digest.package_path,
            "components/0005/records.ndjson",
        )
        self.assertTrue(reader.read_sizes)
        self.assertEqual(set(reader.read_sizes), {4096})

        for changes in (
            {"byte_count": len(content) + 1},
            {"row_count": 3},
        ):
            with self.subTest(changes=changes):
                self.coordinator.component_exporter = SimpleNamespace(
                    open_component_export=mock.Mock(
                        return_value=_Reader(content)
                    )
                )
                with self.assertRaises(ComponentContentMismatch):
                    self.coordinator._hash_records_stream(
                        context=object(),
                        result=SimpleNamespace(
                            reference=object(),
                            byte_count=changes.get(
                                "byte_count",
                                len(content),
                            ),
                            row_count=changes.get("row_count", 2),
                        ),
                        ordinal=1,
                        chunk_bytes=4096,
                    )

        self.coordinator.component_exporter = SimpleNamespace(
            open_component_export=mock.Mock(
                return_value=_Reader(b'{"a":1}')
            )
        )
        with self.assertRaises(ComponentContentMismatch):
            self.coordinator._hash_records_stream(
                context=object(),
                result=SimpleNamespace(
                    reference=object(),
                    byte_count=7,
                    row_count=1,
                ),
                ordinal=1,
                chunk_bytes=4096,
            )

    def test_duplicate_source_and_forged_component_metadata_fail_closed(self):
        line = encode_canonical_document(
            self._payload(),
            trailing_lf=True,
        )
        model_counts = tuple(
            (spec.model_label, 0)
            for spec in self.registry.for_component(self.component.key)
        )
        valid = ComponentExportResult(
            component_key=self.component.key,
            reference=ComponentExportReference(uuid.uuid4()),
            row_count=0,
            media_count=2,
            deterministic_ordering_version=(
                DETERMINISTIC_ORDERING_VERSION
            ),
            model_counts=model_counts,
            byte_count=0,
            media_index_byte_count=len(line) * 2,
            component_version=self.component.component_version,
            record_schema_version=LOGICAL_RECORD_SCHEMA,
            created_at=datetime.now(UTC),
            duration_ms=0,
            provider_identifier=LOGICAL_EXPORT_PROVIDER_IDENTIFIER,
        )

        class _Stream:
            def __init__(self, content):
                self.content = io.BytesIO(content)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size):
                return self.content.read(size)

            def readline(self, size):
                return self.content.readline(size)

        def open_export(*, stream, **_kwargs):
            if stream == ComponentExportStream.RECORDS:
                return _Stream(b"")
            return _Stream(line + line)

        self.coordinator.component_exporter = SimpleNamespace(
            open_component_export=open_export
        )
        with self.assertRaises(MediaIndexValidationError):
            self.coordinator._reconcile_component(
                context=self.context,
                component=self.component,
                result=valid,
                ordinal=5,
                logical_policy=_logical_policy(),
                media_policy=_media_policy(),
                media_by_name={},
                seen_sources=set(),
            )

        forged_values = (
            {"provider_identifier": "forged"},
            {"component_version": "9.9"},
            {"record_schema_version": "forged"},
            {"deterministic_ordering_version": "forged"},
            {"created_at": datetime.now()},
            {"row_count": True},
            {"model_counts": tuple(reversed(model_counts))},
        )
        valid_without_media = replace(
            valid,
            media_count=0,
            media_index_byte_count=0,
        )
        for changes in forged_values:
            with self.subTest(changes=changes):
                with self.assertRaises(ComponentContentMismatch):
                    self.coordinator._validate_component_result(
                        component=self.component,
                        result=replace(valid_without_media, **changes),
                        logical_policy=_logical_policy(),
                    )


class LocalMediaCaptureProviderTests(SQLiteSnapshotTestCase):
    def setUp(self):
        super().setUp()
        self.media_root = self.root / "media-root"
        self.media_root.mkdir(mode=0o700)
        self.snapshot_provider = None
        self.snapshot_result = None
        self.capture_cleanup = []

    def tearDown(self):
        for provider, reference in reversed(self.capture_cleanup):
            try:
                provider.cleanup_media_capture(
                    context=self.context,
                    reference=reference,
                )
            except Exception:
                pass
        if self.snapshot_provider is not None and self.snapshot_result is not None:
            try:
                self.snapshot_provider.cleanup_snapshot(
                    context=self.context,
                    reference=self.snapshot_result.reference,
                )
            except Exception:
                pass
        super().tearDown()

    def _source(self, storage_name, content=b"media-content"):
        path = self.media_root.joinpath(*storage_name.split("/"))
        return _write_stable_media_fixture(path, content)

    @staticmethod
    def _changed_state(value, **changes):
        names = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime",
            "st_mtime_ns",
            "st_ctime",
            "st_ctime_ns",
        )
        values = {name: getattr(value, name) for name in names}
        values.update(changes)
        return SimpleNamespace(**values)

    def _snapshot(self):
        self.snapshot_provider, _context, self.snapshot_result = (
            self.create_snapshot()
        )
        return self.snapshot_result

    def _provider(self, **changes):
        class _LocalInspector:
            @staticmethod
            def assess(_path):
                return SimpleNamespace(confirmed_local=True)

        values = {
            "snapshot_provider": self.snapshot_provider,
            "workspace_manager": self.manager,
            "policy": _media_policy(),
            "storage_resolver": lambda: FileSystemStorage(
                location=str(self.media_root)
            ),
            "filesystem_inspector": _LocalInspector(),
            "disk_usage_provider": (
                lambda _path: SimpleNamespace(free=10**12)
            ),
        }
        values.update(changes)
        return LocalFilesystemMediaCaptureProvider(**values)

    def _capture(self, provider, snapshot, media_sources):
        with self.settings(MEDIA_ROOT=str(self.media_root)):
            results = provider.capture_media(
                context=self.context,
                snapshot_result=snapshot,
                media_sources=media_sources,
            )
        self.capture_cleanup.extend(
            (provider, result.reference) for result in results
        )
        return results

    def _assert_no_capture_directories(self):
        parent = self.workspace.path / WorkspaceArea.MEDIA.value
        if parent.exists():
            self.assertEqual(list(parent.iterdir()), [])

    def test_exact_local_capture_stream_hash_open_modes_and_exact_cleanup(self):
        content = b"bounded-" * 2000
        self._source("alpha/object.bin", content)
        self._source("beta/object.bin", content)
        self._source("zero.bin", b"")
        snapshot = self._snapshot()
        provider = self._provider()
        original_read = os.read
        read_sizes = []

        def bounded_read(descriptor, size):
            read_sizes.append(size)
            return original_read(descriptor, size)

        with mock.patch(
            "apps.backups.engine.media_capture.os.read",
            side_effect=bounded_read,
        ):
            results = self._capture(
                provider,
                snapshot,
                (
                    ("alpha/object.bin", 3),
                    ("beta/object.bin", 1),
                    ("zero.bin", 1),
                ),
            )
        self.assertEqual(len(results), 3)
        first, second, zero = results
        self.assertNotEqual(first.reference, second.reference)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(
            first.sha256,
            hashlib.sha256(content).hexdigest(),
        )
        self.assertEqual(first.byte_count, len(content))
        self.assertEqual(first.source_reference_count, 3)
        self.assertEqual(zero.byte_count, 0)
        self.assertEqual(
            zero.sha256,
            hashlib.sha256(b"").hexdigest(),
        )
        self.assertTrue(timezone.is_aware(first.captured_at))
        self.assertEqual(
            first.provider_identifier,
            LOCAL_FILESYSTEM_MEDIA_CAPTURE_PROVIDER_IDENTIFIER,
        )
        self.assertTrue(read_sizes)
        self.assertEqual(set(read_sizes), {4096})
        with provider.open_media_capture(
            context=self.context,
            reference=first.reference,
        ) as reader:
            self.assertFalse(hasattr(reader, "name"))
            self.assertFalse(hasattr(reader, "fileno"))
            self.assertEqual(reader.read(), content)
        directory = (
            self.workspace.path
            / WorkspaceArea.MEDIA.value
            / first.reference.identifier.hex
        )
        self.assertEqual(
            {item.name for item in directory.iterdir()},
            {MEDIA_CONTENT_FILE_NAME},
        )
        if os.name != "nt":
            self.assertEqual(
                stat.S_IMODE(directory.stat().st_mode),
                0o700,
            )
            self.assertEqual(
                stat.S_IMODE(
                    (directory / MEDIA_CONTENT_FILE_NAME).stat().st_mode
                ),
                0o600,
            )
        self.assertIs(
            provider.cleanup_media_capture(
                context=self.context,
                reference=first.reference,
            ),
            True,
        )
        self.capture_cleanup.remove((provider, first.reference))
        self.assertIs(
            provider.cleanup_media_capture(
                context=self.context,
                reference=first.reference,
            ),
            True,
        )
        with self.assertRaises(MediaObjectNotFound):
            with provider.open_media_capture(
                context=self.context,
                reference=first.reference,
            ):
                pass

    def test_backend_root_and_storage_name_guards_fail_closed(self):
        source = self._source("safe/object.bin")
        snapshot = self._snapshot()

        class _FileSystemStorageSubclass(FileSystemStorage):
            pass

        configurations = (
            (
                "subclass",
                str(self.media_root),
                self._provider(
                    storage_resolver=lambda: _FileSystemStorageSubclass(
                        location=str(self.media_root)
                    )
                ),
                UnsupportedMediaStorageBackend,
            ),
            (
                "mismatch",
                str(self.media_root),
                self._provider(
                    storage_resolver=lambda: FileSystemStorage(
                        location=str(self.root)
                    )
                ),
                UnsupportedMediaStorageBackend,
            ),
            (
                "relative",
                "relative/media",
                self._provider(),
                UnsupportedMediaStorageBackend,
            ),
            (
                "missing-root",
                str(self.root / "missing-media"),
                self._provider(),
                UnsupportedMediaStorageBackend,
            ),
        )
        for label, media_root, provider, error_type in configurations:
            with self.subTest(label=label):
                with self.settings(MEDIA_ROOT=media_root):
                    with self.assertRaises(error_type):
                        provider.capture_media(
                            context=self.context,
                            snapshot_result=snapshot,
                            media_sources=(("safe/object.bin", 1),),
                        )

        provider = self._provider()
        with self.settings(MEDIA_ROOT=str(self.media_root)):
            with mock.patch(
                "apps.backups.engine.media_capture."
                "path_has_link_like_component",
                return_value=True,
            ):
                with self.assertRaises(
                    UnsupportedMediaStorageBackend
                ):
                    provider.capture_media(
                        context=self.context,
                        snapshot_result=snapshot,
                        media_sources=(("safe/object.bin", 1),),
                    )

        unsafe_names = (
            "../escape",
            "/absolute",
            "C:/drive.bin",
            "https://example.test/media",
            r"folder\object.bin",
            "folder/object.bin:stream",
            "CON.bin",
            "folder\u2215object.bin",
        )
        for storage_name in unsafe_names:
            with self.subTest(storage_name=storage_name):
                with self.settings(MEDIA_ROOT=str(self.media_root)):
                    with self.assertRaises(UnsafeMediaReference):
                        provider.capture_media(
                            context=self.context,
                            snapshot_result=snapshot,
                            media_sources=((storage_name, 1),),
                        )
        self.assertTrue(source.exists())
        self._assert_no_capture_directories()

    def test_missing_nonregular_link_and_alias_objects_abort_sanitized(self):
        self._source("directory/child.bin")
        directory = self.media_root / "not-a-file"
        directory.mkdir()
        hardlink_source = self._source("links/source.bin")
        hardlink_alias = self.media_root / "links/alias.bin"
        os.link(hardlink_source, hardlink_alias)
        snapshot = self._snapshot()
        provider = self._provider()
        cases = (
            ("missing.bin", MediaObjectNotFound),
            ("not-a-file", UnsafeMediaStorageObject),
            ("links/source.bin", UnsafeMediaStorageObject),
        )
        if os.name != "nt" and hasattr(os, "mkfifo"):
            fifo = self.media_root / "unsafe.fifo"
            os.mkfifo(fifo)
            cases += (("unsafe.fifo", UnsafeMediaStorageObject),)
        for storage_name, error_type in cases:
            with self.subTest(storage_name=storage_name):
                with self.assertRaises(error_type) as raised:
                    self._capture(
                        provider,
                        snapshot,
                        ((storage_name, 1),),
                    )
                rendered = (
                    f"{raised.exception!s} {raised.exception!r}"
                )
                self.assertNotIn(storage_name, rendered)
                self.assertNotIn(str(self.media_root), rendered)

        with self.assertRaises(MediaStorageNameCollision):
            self._capture(
                provider,
                snapshot,
                tuple(
                    sorted(
                        (
                            ("directory/child.bin", 1),
                            ("DIRECTORY/CHILD.BIN", 1),
                        )
                    )
                ),
            )
        self._assert_no_capture_directories()

    def test_distinct_names_resolving_to_same_single_link_identity_abort(self):
        first = self._source("aliases/first.bin", b"first")
        second = self._source("aliases/second.bin", b"second")
        self.assertEqual(first.stat().st_nlink, 1)
        self.assertEqual(second.stat().st_nlink, 1)
        snapshot = self._snapshot()
        provider = self._provider()
        original_source_path = provider._source_path
        first_identity = (
            first.stat().st_dev,
            first.stat().st_ino,
        )

        def alias_identity(
            root,
            root_state,
            storage_name,
            mount_boundaries,
        ):
            normalized, path, state = original_source_path(
                root,
                root_state,
                storage_name,
                mount_boundaries,
            )
            if storage_name == "aliases/second.bin":
                state = self._changed_state(
                    state,
                    st_dev=first_identity[0],
                    st_ino=first_identity[1],
                    st_nlink=1,
                )
            return normalized, path, state

        with mock.patch.object(
            provider,
            "_source_path",
            side_effect=alias_identity,
        ):
            with self.assertRaises(MediaStorageAliasCollision):
                self._capture(
                    provider,
                    snapshot,
                    (
                        ("aliases/first.bin", 1),
                        ("aliases/second.bin", 1),
                    ),
                )
        self.assertEqual(first.stat().st_nlink, 1)
        self.assertEqual(second.stat().st_nlink, 1)
        self._assert_no_capture_directories()

    def test_cutoff_mutation_replacement_and_stream_changes_are_detected(self):
        path = self._source("race/object.bin", b"A" * 9000)
        snapshot = self._snapshot()
        future = snapshot.created_at.timestamp() + 10
        os.utime(path, (future, future))
        with self.assertRaises(MediaObjectChanged):
            self._capture(
                self._provider(),
                snapshot,
                (("race/object.bin", 1),),
            )

        path.write_bytes(b"A" * 9000)
        old = snapshot.consistency_cutoff_at.timestamp() - 1
        os.utime(path, (old, old))
        replacement = self.media_root / "race/replacement.bin"
        replacement.write_bytes(b"B" * 9000)
        os.utime(replacement, (old, old))
        replaced = {"done": False}

        def replace_before_open(stage):
            if stage == "before_media_source_open" and not replaced["done"]:
                replaced["done"] = True
                os.replace(replacement, path)

        with self.assertRaises(MediaObjectChanged):
            self._capture(
                self._provider(failure_hook=replace_before_open),
                snapshot,
                (("race/object.bin", 1),),
            )

        path.write_bytes(b"C" * 9000)
        os.utime(path, (old, old))
        changed = {"done": False}

        def extend_after_chunk(stage):
            if stage == "after_media_source_chunk" and not changed["done"]:
                changed["done"] = True
                with path.open("ab") as stream:
                    stream.write(b"extension")

        with self.assertRaises(MediaObjectChanged):
            self._capture(
                self._provider(failure_hook=extend_after_chunk),
                snapshot,
                (("race/object.bin", 1),),
            )
        self._assert_no_capture_directories()

    def test_source_replacement_after_stream_chunk_is_detected(self):
        path = self._source("stream-replace/object.bin", b"A" * 9000)
        replacement = self._source(
            "stream-replace/replacement.bin",
            b"B" * 9000,
        )
        snapshot = self._snapshot()
        replacement_state = {"done": False, "injected": False}

        def replace_after_chunk(stage):
            if (
                stage == "after_media_source_chunk"
                and not replacement_state["done"]
            ):
                replacement_state["done"] = True
                try:
                    os.replace(replacement, path)
                except OSError:
                    replacement_state["injected"] = True

        provider = self._provider(failure_hook=replace_after_chunk)
        original_source_path = provider._source_path

        def source_path_with_windows_replacement(
            root,
            root_state,
            storage_name,
            mount_boundaries,
        ):
            normalized, source_path, source_state = (
                original_source_path(
                    root,
                    root_state,
                    storage_name,
                    mount_boundaries,
                )
            )
            if (
                replacement_state["done"]
                and replacement_state["injected"]
                and storage_name == "stream-replace/object.bin"
            ):
                source_state = self._changed_state(
                    source_state,
                    st_ino=source_state.st_ino + 1,
                )
            return normalized, source_path, source_state

        with mock.patch.object(
            provider,
            "_source_path",
            side_effect=source_path_with_windows_replacement,
        ):
            with self.assertRaises(MediaObjectChanged):
                self._capture(
                    provider,
                    snapshot,
                    (("stream-replace/object.bin", 1),),
                )
        self.assertTrue(replacement_state["done"])
        self._assert_no_capture_directories()

    def test_forged_snapshot_result_and_context_binding_are_rejected(self):
        self._source("evidence/object.bin", b"bound evidence")
        snapshot = self._snapshot()
        provider = self._provider()
        forged_results = (
            replace(
                snapshot,
                consistency_cutoff_at=(
                    snapshot.consistency_cutoff_at
                    - timedelta(seconds=1)
                ),
            ),
            replace(
                snapshot,
                created_at=snapshot.created_at + timedelta(seconds=1),
            ),
            replace(
                snapshot,
                duration_ms=snapshot.duration_ms + 1,
            ),
        )
        for forged in forged_results:
            with self.subTest(forged_field=forged):
                with self.settings(MEDIA_ROOT=str(self.media_root)):
                    with self.assertRaises(MediaCaptureCreationError):
                        provider.capture_media(
                            context=self.context,
                            snapshot_result=forged,
                            media_sources=(("evidence/object.bin", 1),),
                        )

        forged_contexts = (
            replace(self.context, backup_public_id=uuid.uuid4()),
            replace(self.context, business_public_id=uuid.uuid4()),
            replace(self.context, business_id=self.context.business_id + 1),
        )
        for forged_context in forged_contexts:
            with self.subTest(
                changed_context=forged_context.business_public_id
            ):
                with self.settings(MEDIA_ROOT=str(self.media_root)):
                    with self.assertRaises(MediaCaptureCreationError):
                        provider.capture_media(
                            context=forged_context,
                            snapshot_result=snapshot,
                            media_sources=(("evidence/object.bin", 1),),
                        )
        self._assert_no_capture_directories()

    def test_media_directory_creation_rollback_failure_preserves_abort_and_unowned_data(
        self,
    ):
        self._source("directory-failure/object.bin", b"directory failure")
        snapshot = self._snapshot()
        media_parent = (
            self.workspace.path / WorkspaceArea.MEDIA.value
        )
        media_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        unowned_directory = media_parent / "unowned-sentinel"
        unowned_directory.mkdir()
        unowned_marker = unowned_directory / "keep.txt"
        unowned_marker.write_bytes(b"must remain")
        try:
            cases = (
                (
                    "ordinary",
                    OSError,
                    MediaCaptureCreationError,
                ),
                (
                    "keyboard-interrupt",
                    KeyboardInterrupt,
                    KeyboardInterrupt,
                ),
                ("system-exit", SystemExit, SystemExit),
                ("generator-exit", GeneratorExit, GeneratorExit),
            )
            for label, raised_type, expected_type in cases:
                with self.subTest(failure=label):
                    identifier = uuid.uuid4()
                    target = media_parent / identifier.hex
                    attempted_removals = []

                    def fail_after_directory(
                        stage,
                        *,
                        selected=raised_type,
                    ):
                        if stage == "after_media_directory_creation":
                            raise selected(
                                "preserve media directory abort"
                            )

                    provider = self._provider(
                        reference_factory=lambda value=identifier: (
                            MediaCaptureReference(value)
                        ),
                        failure_hook=fail_after_directory,
                    )
                    original_rmdir = os.rmdir

                    def reject_owned_removal(
                        candidate,
                        *args,
                        selected_target=target,
                        removals=attempted_removals,
                        rmdir=original_rmdir,
                        **kwargs,
                    ):
                        candidate = Path(candidate)
                        removals.append(candidate)
                        if candidate == selected_target:
                            raise OSError(
                                "private forced media rollback failure"
                            )
                        return rmdir(
                            candidate,
                            *args,
                            **kwargs,
                        )

                    with (
                        mock.patch(
                            "apps.backups.engine.media_capture.os.rmdir",
                            side_effect=reject_owned_removal,
                        ),
                        self.assertRaises(expected_type) as raised,
                    ):
                        self._capture(
                            provider,
                            snapshot,
                            (("directory-failure/object.bin", 1),),
                        )
                    self.assertTrue(
                        raised.exception.cleanup_incomplete
                    )
                    self.assertEqual(attempted_removals, [target])
                    self.assertTrue(target.is_dir())
                    self.assertEqual(
                        unowned_marker.read_bytes(),
                        b"must remain",
                    )
                    self.assertNotIn(
                        unowned_directory,
                        attempted_removals,
                    )
                    if expected_type is MediaCaptureCreationError:
                        self.assertNotIn(
                            "private forced media rollback failure",
                            str(raised.exception),
                        )
                    target.rmdir()
        finally:
            if unowned_marker.exists():
                unowned_marker.unlink()
            if unowned_directory.exists():
                unowned_directory.rmdir()
        self._assert_no_capture_directories()

    def test_media_cleanup_tombstone_rejects_forged_context(self):
        self._source("tombstone/object.bin", b"tombstone")
        snapshot = self._snapshot()
        provider = self._provider()
        result = self._capture(
            provider,
            snapshot,
            (("tombstone/object.bin", 1),),
        )[0]
        self.assertIs(
            provider.cleanup_media_capture(
                context=self.context,
                reference=result.reference,
            ),
            True,
        )
        self.capture_cleanup.remove((provider, result.reference))
        self.assertIs(
            provider.cleanup_media_capture(
                context=self.context,
                reference=result.reference,
            ),
            True,
        )
        forged_contexts = (
            replace(self.context, backup_public_id=uuid.uuid4()),
            replace(self.context, business_public_id=uuid.uuid4()),
            replace(self.context, business_id=self.context.business_id + 1),
        )
        for forged_context in forged_contexts:
            self.assertEqual(
                forged_context.workspace_reference,
                self.context.workspace_reference,
            )
            with self.subTest(forged_context=forged_context):
                with self.assertRaises(MediaCaptureCleanupError):
                    provider.cleanup_media_capture(
                        context=forged_context,
                        reference=result.reference,
                    )

    def test_source_and_parent_links_reparse_and_device_boundaries_are_rejected(self):
        source_target = self._source(
            "real/source-target.bin",
            b"source target",
        )
        parent_target_file = self._source(
            "real-parent/object.bin",
            b"parent target",
        )
        self._source("junction/object.bin", b"junction injection")
        self._source("mount/object.bin", b"mount injection")
        snapshot = self._snapshot()
        provider = self._provider()

        link_cases = (
            ("source-link.bin", source_target, False),
            ("parent-link", parent_target_file.parent, True),
        )
        for link_name, target, target_is_directory in link_cases:
            link_path = self.media_root / link_name
            created = False
            try:
                os.symlink(
                    target,
                    link_path,
                    target_is_directory=target_is_directory,
                )
                created = True
            except (NotImplementedError, OSError):
                pass
            if created:
                storage_name = (
                    f"{link_name}/object.bin"
                    if target_is_directory
                    else link_name
                )
                with self.subTest(real_link=storage_name):
                    with self.assertRaises(UnsafeMediaStorageObject):
                        self._capture(
                            provider,
                            snapshot,
                            ((storage_name, 1),),
                        )

        injected_cases = (
            ("source", "real/source-target.bin", "source-target.bin"),
            ("parent", "real-parent/object.bin", "real-parent"),
            ("junction-reparse", "junction/object.bin", "junction"),
        )
        for label, storage_name, linked_name in injected_cases:
            with self.subTest(injected_link=label):

                def link_like(candidate, *, selected=linked_name):
                    return Path(candidate).name == selected

                with mock.patch(
                    "apps.backups.engine.media_capture.path_is_link_like",
                    side_effect=link_like,
                ):
                    with self.assertRaises(UnsafeMediaStorageObject):
                        self._capture(
                            provider,
                            snapshot,
                            ((storage_name, 1),),
                        )

        mount_parent = self.media_root / "mount"
        original_stat = os.stat

        def changed_device(candidate, *args, **kwargs):
            current = original_stat(candidate, *args, **kwargs)
            if Path(candidate) == mount_parent:
                return self._changed_state(
                    current,
                    st_dev=current.st_dev + 1,
                )
            return current

        with mock.patch(
            "apps.backups.engine.media_capture.os.stat",
            side_effect=changed_device,
        ):
            with self.assertRaises(UnsafeMediaStorageObject):
                self._capture(
                    provider,
                    snapshot,
                    (("mount/object.bin", 1),),
                )
        with mock.patch(
            "apps.backups.engine.media_capture._linux_mount_boundaries",
            return_value=frozenset({mount_parent}),
        ):
            with self.assertRaises(UnsafeMediaStorageObject):
                self._capture(
                    provider,
                    snapshot,
                    (("mount/object.bin", 1),),
                )
        self._assert_no_capture_directories()

    def test_socket_and_injected_nonregular_objects_are_rejected(self):
        self._source("injected-socket.bin", b"not really a socket")
        snapshot = self._snapshot()
        provider = self._provider()
        socket_path = self.media_root / "real.sock"
        unix_socket = None
        if hasattr(socket, "AF_UNIX"):
            try:
                unix_socket = socket.socket(
                    socket.AF_UNIX,
                    socket.SOCK_STREAM,
                )
                unix_socket.bind(str(socket_path))
            except OSError:
                if unix_socket is not None:
                    unix_socket.close()
                unix_socket = None
        try:
            if unix_socket is not None:
                with self.assertRaises(UnsafeMediaStorageObject):
                    self._capture(
                        provider,
                        snapshot,
                        (("real.sock", 1),),
                    )
        finally:
            if unix_socket is not None:
                unix_socket.close()

        original_stat = os.stat
        injected_path = self.media_root / "injected-socket.bin"
        for label, object_mode in (
            ("socket", stat.S_IFSOCK),
            ("character-device", stat.S_IFCHR),
            ("block-device", stat.S_IFBLK),
        ):
            with self.subTest(injected_object=label):

                def nonregular_state(
                    candidate,
                    *args,
                    selected_mode=object_mode,
                    **kwargs,
                ):
                    current = original_stat(candidate, *args, **kwargs)
                    if Path(candidate) == injected_path:
                        return self._changed_state(
                            current,
                            st_mode=selected_mode | 0o600,
                        )
                    return current

                with mock.patch(
                    "apps.backups.engine.media_capture.os.stat",
                    side_effect=nonregular_state,
                ):
                    with self.assertRaises(
                        UnsafeMediaStorageObject
                    ):
                        self._capture(
                            provider,
                            snapshot,
                            (("injected-socket.bin", 1),),
                        )
        self._assert_no_capture_directories()

    def test_truncation_and_same_size_content_mutation_during_stream_are_detected(self):
        path = self._source("stream/object.bin", b"A" * 12_000)
        snapshot = self._snapshot()
        original_mtime_ns = path.stat().st_mtime_ns
        original_atime_ns = path.stat().st_atime_ns

        truncated = {"done": False}

        def truncate(stage):
            if stage == "after_media_source_chunk" and not truncated["done"]:
                truncated["done"] = True
                with path.open("r+b") as stream:
                    stream.truncate(100)

        with self.assertRaises(MediaObjectChanged):
            self._capture(
                self._provider(failure_hook=truncate),
                snapshot,
                (("stream/object.bin", 1),),
            )
        self._assert_no_capture_directories()

        path.write_bytes(b"A" * 12_000)
        os.utime(path, ns=(original_atime_ns, original_mtime_ns))
        modified = {"done": False}

        def same_size_modify(stage):
            if stage == "after_media_source_chunk" and not modified["done"]:
                modified["done"] = True
                with path.open("r+b") as stream:
                    stream.seek(4096)
                    stream.write(b"Z" * 4096)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.utime(
                    path,
                    ns=(
                        original_atime_ns,
                        original_mtime_ns + 2_000_000_000,
                    ),
                )

        with self.assertRaises(MediaObjectChanged):
            self._capture(
                self._provider(failure_hook=same_size_modify),
                snapshot,
                (("stream/object.bin", 1),),
            )
        self.assertEqual(path.stat().st_size, 12_000)
        self._assert_no_capture_directories()

    def test_explicit_source_state_transitions_fail_stable_read_validation(self):
        path = self._source("state/object.bin", b"S" * 9000)
        snapshot = self._snapshot()
        original_source_path = (
            LocalFilesystemMediaCaptureProvider._source_path
        )
        transition_factories = (
            (
                "mtime",
                lambda state: {
                    "st_mtime": state.st_mtime + 1,
                    "st_mtime_ns": state.st_mtime_ns + 1,
                },
            ),
            (
                "ctime",
                lambda state: {
                    "st_ctime": state.st_ctime + 1,
                    "st_ctime_ns": state.st_ctime_ns + 1,
                },
            ),
            (
                "identity",
                lambda state: {"st_ino": state.st_ino + 1},
            ),
            (
                "link-count",
                lambda _state: {"st_nlink": 2},
            ),
        )
        for label, changes_for in transition_factories:
            with self.subTest(transition=label):
                calls = {"count": 0}

                def transitioning_source(
                    root,
                    root_state,
                    storage_name,
                    mount_boundaries,
                    *,
                    call_state=calls,
                    transition=changes_for,
                ):
                    result = original_source_path(
                        root,
                        root_state,
                        storage_name,
                        mount_boundaries,
                    )
                    call_state["count"] += 1
                    if call_state["count"] == 3:
                        name, source_path, source_state = result
                        return (
                            name,
                            source_path,
                            self._changed_state(
                                source_state,
                                **transition(source_state),
                            ),
                        )
                    return result

                with mock.patch.object(
                    LocalFilesystemMediaCaptureProvider,
                    "_source_path",
                    side_effect=transitioning_source,
                ):
                    with self.assertRaises(MediaObjectChanged):
                        self._capture(
                            self._provider(),
                            snapshot,
                            (("state/object.bin", 1),),
                        )
                self.assertEqual(calls["count"], 3)
                self.assertTrue(path.exists())
                self._assert_no_capture_directories()

    def test_media_cleanup_unlink_failure_is_sanitized_and_retry_succeeds(self):
        self._source("cleanup/object.bin", b"cleanup content")
        snapshot = self._snapshot()
        provider = self._provider()
        result = self._capture(
            provider,
            snapshot,
            (("cleanup/object.bin", 1),),
        )[0]
        original_unlink = os.unlink
        failed = {"done": False}

        def fail_once(candidate, *args, **kwargs):
            if (
                Path(candidate).name == MEDIA_CONTENT_FILE_NAME
                and not failed["done"]
            ):
                failed["done"] = True
                raise OSError("private cleanup path")
            return original_unlink(candidate, *args, **kwargs)

        with mock.patch(
            "apps.backups.engine.media_capture.os.unlink",
            side_effect=fail_once,
        ):
            with self.assertRaises(MediaCaptureCleanupError) as raised:
                provider.cleanup_media_capture(
                    context=self.context,
                    reference=result.reference,
                )
        self.assertNotIn("private cleanup path", str(raised.exception))
        self.assertIs(
            provider.cleanup_media_capture(
                context=self.context,
                reference=result.reference,
            ),
            True,
        )
        self.capture_cleanup.remove((provider, result.reference))
        self.assertIs(
            provider.cleanup_media_capture(
                context=self.context,
                reference=result.reference,
            ),
            True,
        )
        self._assert_no_capture_directories()

    def test_exact_limits_overflow_timeout_capacity_and_zero_bytes(self):
        self._source("a.bin", b"1234")
        self._source("b.bin", b"5678")
        self._source("zero.bin", b"")
        snapshot = self._snapshot()
        exact_file = self._provider(
            policy=_media_policy(
                maximum_file_bytes=4,
                maximum_total_bytes=4,
            )
        )
        result = self._capture(
            exact_file,
            snapshot,
            (("a.bin", 1),),
        )
        self.assertEqual(result[0].byte_count, 4)

        cases = (
            (
                "file-overflow",
                self._provider(
                    policy=_media_policy(
                        maximum_file_bytes=3,
                        maximum_total_bytes=3,
                    )
                ),
                (("a.bin", 1),),
                MediaCaptureLimitExceeded,
            ),
            (
                "total-overflow",
                self._provider(
                    policy=_media_policy(
                        maximum_file_bytes=4,
                        maximum_total_bytes=7,
                    )
                ),
                (("a.bin", 1), ("b.bin", 1)),
                MediaCaptureLimitExceeded,
            ),
            (
                "objects",
                self._provider(
                    policy=_media_policy(maximum_objects=1)
                ),
                (("a.bin", 1), ("b.bin", 1)),
                MediaCaptureLimitExceeded,
            ),
            (
                "capacity",
                self._provider(
                    disk_usage_provider=lambda _path: (
                        SimpleNamespace(free=0)
                    )
                ),
                (("a.bin", 1),),
                InsufficientMediaCaptureCapacity,
            ),
            (
                "timeout",
                self._provider(
                    monotonic=iter((0.0, 31.0)).__next__
                ),
                (("a.bin", 1),),
                MediaCaptureTimeout,
            ),
        )
        for label, provider, sources, error_type in cases:
            with self.subTest(label=label):
                with self.assertRaises(error_type):
                    self._capture(provider, snapshot, sources)

        total_exact = self._provider(
            policy=_media_policy(
                maximum_file_bytes=4,
                maximum_total_bytes=8,
            )
        )
        exact_results = self._capture(
            total_exact,
            snapshot,
            (("a.bin", 1), ("b.bin", 1)),
        )
        self.assertEqual(sum(item.byte_count for item in exact_results), 8)
        zero = self._capture(
            self._provider(),
            snapshot,
            (("zero.bin", 1),),
        )
        self.assertEqual(zero[0].byte_count, 0)

    def test_atomic_failure_stages_abort_and_batch_rollback_are_exact(self):
        self._source("a.bin", b"A" * 5000)
        self._source("b.bin", b"B" * 5000)
        snapshot = self._snapshot()
        stages = (
            "after_media_part_creation",
            "before_media_source_open",
            "before_media_source_read",
            "before_media_destination_write",
            "after_media_destination_write",
            "before_media_flush",
            "after_media_flush",
            "before_media_fsync",
            "after_media_fsync",
            "before_media_publication",
            "after_media_publication",
            "before_media_result_return",
            "after_one_media_capture",
            "before_media_batch_result_return",
        )
        for stage in stages:
            with self.subTest(stage=stage):

                def fail(candidate, *, selected=stage):
                    if candidate == selected:
                        raise OSError("private media path")

                with self.assertRaises(
                    MediaCaptureCreationError
                ) as raised:
                    self._capture(
                        self._provider(failure_hook=fail),
                        snapshot,
                        (("a.bin", 1),),
                    )
                self.assertFalse(raised.exception.cleanup_incomplete)
                self.assertNotIn(
                    "private media path",
                    str(raised.exception),
                )
                self._assert_no_capture_directories()

        for abort_type in (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(abort_type=abort_type):

                def abort(stage, *, selected=abort_type):
                    if stage == "before_media_destination_write":
                        raise selected("preserved")

                with self.assertRaises(abort_type):
                    self._capture(
                        self._provider(failure_hook=abort),
                        snapshot,
                        (("a.bin", 1),),
                    )
                self._assert_no_capture_directories()

        seen = {"captures": 0}

        def fail_after_second(stage):
            if stage == "after_one_media_capture":
                seen["captures"] += 1
                if seen["captures"] == 2:
                    raise OSError("batch failure")

        with self.assertRaises(MediaCaptureCreationError):
            self._capture(
                self._provider(failure_hook=fail_after_second),
                snapshot,
                (("a.bin", 1), ("b.bin", 1)),
            )
        self._assert_no_capture_directories()


class CanonicalManifestProviderTests(SQLiteSnapshotTestCase):
    def setUp(self):
        super().setUp()
        self.plan = resolve_component_plan(
            scope=BackupScope.POS,
            enabled_products=(ProductOwner.POS,),
        ).export_components
        self.registry = get_logical_export_registry()
        self.manifest_provider = CanonicalManifestProvider(
            workspace_manager=self.manager,
        )
        self.snapshot_provider = None
        self.snapshot_result = None
        self.manifest_references = []

    def tearDown(self):
        for reference in reversed(self.manifest_references):
            try:
                self.manifest_provider.cleanup_manifest(
                    context=self.context,
                    reference=reference,
                )
            except Exception:
                pass
        if self.snapshot_provider is not None and self.snapshot_result is not None:
            try:
                self.snapshot_provider.cleanup_snapshot(
                    context=self.context,
                    reference=self.snapshot_result.reference,
                )
            except Exception:
                pass
        super().tearDown()

    def _request(self):
        self.context = replace(
            self.context,
            schema_migration_fingerprint="a" * 64,
            application_version="phase2d1-test",
            backup_format_version="2d1-test",
            minimum_restore_version="phase2d1-test",
        )
        self.snapshot_provider, _context, self.snapshot_result = (
            self.create_snapshot(context=self.context)
        )
        empty_sha256 = hashlib.sha256(b"").hexdigest()
        components = []
        for ordinal, plan_item in enumerate(self.plan, start=1):
            models_for_component = tuple(
                (spec.model_label, 0)
                for spec in self.registry.for_component(plan_item.key)
            )
            records = PayloadDigest(
                package_path=(
                    f"components/{ordinal:04d}/records.ndjson"
                ),
                byte_count=0,
                sha256=empty_sha256,
            )
            media_index = PayloadDigest(
                package_path=(
                    f"components/{ordinal:04d}/media-index.ndjson"
                ),
                byte_count=0,
                sha256=empty_sha256,
            )
            components.append(
                ReconciledComponent(
                    plan_item=plan_item,
                    models=models_for_component,
                    records=records,
                    record_count=0,
                    media_index=media_index,
                    media_reference_count=0,
                    component_content_sha256=(
                        calculate_component_content_sha256(
                            plan_item=plan_item,
                            models=models_for_component,
                            records=records,
                            record_count=0,
                            media_index=media_index,
                            media_reference_count=0,
                        )
                    ),
                )
            )
        return CanonicalManifestBuildRequest(
            context=self.context,
            snapshot_result=self.snapshot_result,
            component_plan=self.plan,
            components=tuple(components),
            media=(),
        )

    def _request_with_media(self):
        from apps.backups.engine.media_capture import (
            LOCAL_FILESYSTEM_MEDIA_CAPTURE_PROVIDER_IDENTIFIER,
        )

        request = self._request()
        component_ordinal = next(
            ordinal
            for ordinal, item in enumerate(self.plan, start=1)
            if item.key == "pos.catalog"
        )
        component_index = component_ordinal - 1
        component = request.components[component_index]
        media_index_bytes = b"three-validated-logical-references\n"
        media_index = replace(
            component.media_index,
            byte_count=len(media_index_bytes),
            sha256=hashlib.sha256(media_index_bytes).hexdigest(),
        )
        component = replace(
            component,
            media_index=media_index,
            media_reference_count=3,
            component_content_sha256=(
                calculate_component_content_sha256(
                    plan_item=component.plan_item,
                    models=component.models,
                    records=component.records,
                    record_count=component.record_count,
                    media_index=media_index,
                    media_reference_count=3,
                )
            ),
        )
        components = list(request.components)
        components[component_index] = component
        common_content = b"same bytes, independent restore names"
        common_sha256 = hashlib.sha256(common_content).hexdigest()
        sources_by_name = (
            (
                "docs/alpha.bin",
                (
                    (
                        "catalog.Product",
                        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    ),
                    (
                        "catalog.Product",
                        "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
                    ),
                ),
            ),
            (
                "docs/beta.bin",
                (
                    (
                        "catalog.ProductVariant",
                        "cccccccc-dddd-eeee-ffff-000000000000",
                    ),
                ),
            ),
        )
        media = []
        for ordinal, (storage_name, source_specs) in enumerate(
            sources_by_name,
            start=1,
        ):
            sources = []
            for model, public_id in source_specs:
                identity = {"public_id": public_id}
                sources.append(
                    MediaSource(
                        component_ordinal=component_ordinal,
                        component="pos.catalog",
                        model=model,
                        identity_items=(("public_id", public_id),),
                        identity_canonical_bytes=(
                            encode_canonical_document(identity)
                        ),
                        field="image",
                        storage_name=storage_name,
                    )
                )
            capture = MediaCaptureResult(
                reference=MediaCaptureReference(uuid.uuid4()),
                logical_storage_name=storage_name,
                byte_count=len(common_content),
                sha256=common_sha256,
                source_reference_count=len(sources),
                captured_at=request.snapshot_result.created_at,
                duration_ms=1,
                provider_identifier=(
                    LOCAL_FILESYSTEM_MEDIA_CAPTURE_PROVIDER_IDENTIFIER
                ),
            )
            media.append(
                ManifestMediaItem(
                    ordinal=ordinal,
                    package_path=f"media/{ordinal:08d}.bin",
                    capture=capture,
                    sources=tuple(sources),
                )
            )
        return replace(
            request,
            components=tuple(components),
            media=tuple(media),
        )

    @staticmethod
    def _read(provider, context, reference):
        with provider.open_manifest(
            context=context,
            reference=reference,
        ) as reader:
            return reader.read()

    def test_manifest_schema_paths_hash_domains_and_external_self_hash(self):
        request = self._request()
        expected_document = build_manifest_document(request)
        result = self.manifest_provider.build_manifest(request)
        self.manifest_references.append(result.reference)
        raw = self._read(
            self.manifest_provider,
            self.context,
            result.reference,
        )
        document = json.loads(raw)

        self.assertEqual(raw, encode_canonical_document(document) + b"\n")
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(raw.endswith(b"\n"))
        self.assertFalse(raw.endswith(b"\n\n"))
        self.assertEqual(document, expected_document)
        self.assertEqual(
            set(document),
            {
                "schema",
                "manifest_version",
                "canonical_json_version",
                "hash_algorithm",
                "package_format",
                "backup",
                "compatibility",
                "source_consistency",
                "components",
                "media",
                "totals",
                "payload_set_schema",
                "payload_set_sha256",
                "missing_media_policy",
                "missing_media_count",
                "restore_verification_state",
            },
        )
        self.assertEqual(document["schema"], "nexa.backup-manifest.v1")
        self.assertEqual(document["manifest_version"], "1.0.0")
        self.assertEqual(
            document["canonical_json_version"],
            "nexa.canonical-json.v1",
        )
        self.assertEqual(document["hash_algorithm"], "sha256")
        self.assertEqual(
            document["package_format"],
            "nexa.zip-store.v1",
        )
        self.assertEqual(
            document["payload_set_schema"],
            "nexa.backup-payload-set.v1",
        )
        self.assertEqual(
            document["missing_media_policy"],
            "FAIL_BACKUP",
        )
        self.assertEqual(document["missing_media_count"], 0)
        self.assertNotIn("manifest_sha256", document)
        self.assertNotIn("package_sha256", document)
        self.assertNotIn("snapshot_path", raw.decode())
        self.assertNotIn(str(self.source_path), raw.decode())
        self.assertNotIn(str(self.staging_root), raw.decode())
        self.assertNotIn("business_id", document["backup"])
        self.assertNotIn("database_id", raw.decode())
        self.assertEqual(result.byte_count, len(raw))
        self.assertEqual(result.sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(
            result.payload_set_sha256,
            calculate_payload_set_sha256(request.components, request.media),
        )
        self.assertEqual(
            result.created_at,
            self.snapshot_result.created_at,
        )

        for ordinal, component in enumerate(
            document["components"],
            start=1,
        ):
            with self.subTest(component=component["key"]):
                self.assertEqual(
                    set(component),
                    {
                        "ordinal",
                        "key",
                        "product_owner",
                        "component_version",
                        "restore_behavior",
                        "required_component_keys",
                        "export_order",
                        "import_order",
                        "record_schema",
                        "media_reference_schema",
                        "deterministic_ordering_version",
                        "models",
                        "records",
                        "media_index",
                        "component_content_schema",
                        "component_content_sha256",
                        "restore_verification_state",
                    },
                )
                self.assertEqual(
                    set(component["records"]),
                    {
                        "package_path",
                        "record_count",
                        "byte_count",
                        "sha256",
                    },
                )
                self.assertEqual(
                    set(component["media_index"]),
                    {
                        "package_path",
                        "reference_count",
                        "byte_count",
                        "sha256",
                    },
                )
                self.assertEqual(component["ordinal"], ordinal)
                self.assertEqual(
                    component["records"]["package_path"],
                    f"components/{ordinal:04d}/records.ndjson",
                )
                self.assertEqual(
                    component["media_index"]["package_path"],
                    (
                        f"components/{ordinal:04d}/"
                        "media-index.ndjson"
                    ),
                )
                self.assertEqual(
                    component["component_content_sha256"],
                    request.components[
                        ordinal - 1
                    ].component_content_sha256,
                )
                evidence = request.components[ordinal - 1]
                independent_descriptor = {
                    "schema": "nexa.component-content-digest.v1",
                    "component_key": evidence.plan_item.key,
                    "component_version": (
                        evidence.plan_item.component_version
                    ),
                    "record_schema": "nexa.logical-record.v1",
                    "deterministic_ordering_version": (
                        "nexa.logical-order.v1"
                    ),
                    "records": {
                        "record_count": evidence.record_count,
                        "byte_count": evidence.records.byte_count,
                        "sha256": evidence.records.sha256,
                    },
                    "media_index": {
                        "reference_count": (
                            evidence.media_reference_count
                        ),
                        "byte_count": evidence.media_index.byte_count,
                        "sha256": evidence.media_index.sha256,
                    },
                    "models": [
                        {"model": label, "record_count": count}
                        for label, count in evidence.models
                    ],
                }
                self.assertEqual(
                    component["component_content_sha256"],
                    hashlib.sha256(
                        encode_canonical_document(
                            independent_descriptor
                        )
                    ).hexdigest(),
                )

        independent_payloads = []
        for component in request.components:
            independent_payloads.extend(
                (
                    {
                        "kind": "COMPONENT_RECORDS",
                        "package_path": (
                            component.records.package_path
                        ),
                        "byte_count": component.records.byte_count,
                        "sha256": component.records.sha256,
                    },
                    {
                        "kind": "COMPONENT_MEDIA_INDEX",
                        "package_path": (
                            component.media_index.package_path
                        ),
                        "byte_count": (
                            component.media_index.byte_count
                        ),
                        "sha256": component.media_index.sha256,
                    },
                )
            )
        independent_payload_sha256 = hashlib.sha256(
            encode_canonical_document(
                {
                    "schema": "nexa.backup-payload-set.v1",
                    "payloads": independent_payloads,
                }
            )
        ).hexdigest()
        self.assertEqual(
            document["payload_set_sha256"],
            independent_payload_sha256,
        )

    def test_identical_immutable_inputs_build_identical_manifest_bytes(self):
        request = self._request()
        first = self.manifest_provider.build_manifest(request)
        second = self.manifest_provider.build_manifest(request)
        self.manifest_references.extend(
            (first.reference, second.reference)
        )
        first_bytes = self._read(
            self.manifest_provider,
            self.context,
            first.reference,
        )
        second_bytes = self._read(
            self.manifest_provider,
            self.context,
            second.reference,
        )
        self.assertNotEqual(first.reference, second.reference)
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.payload_set_sha256, second.payload_set_sha256)

    def test_nonempty_manifest_preserves_sources_without_content_deduplication(self):
        request = self._request_with_media()
        result = self.manifest_provider.build_manifest(request)
        self.manifest_references.append(result.reference)
        raw = self._read(
            self.manifest_provider,
            self.context,
            result.reference,
        )
        document = json.loads(raw)
        media = document["media"]
        self.assertEqual(
            [item["storage_name"] for item in media],
            ["docs/alpha.bin", "docs/beta.bin"],
        )
        self.assertEqual(
            [item["package_path"] for item in media],
            ["media/00000001.bin", "media/00000002.bin"],
        )
        self.assertEqual(media[0]["sha256"], media[1]["sha256"])
        self.assertEqual(
            [item["source_reference_count"] for item in media],
            [2, 1],
        )
        expected_media_sha256 = hashlib.sha256(
            b"same bytes, independent restore names"
        ).hexdigest()
        self.assertEqual(
            [item["sha256"] for item in media],
            [item.capture.sha256 for item in request.media],
        )
        self.assertEqual(
            [item["sha256"] for item in media],
            [expected_media_sha256, expected_media_sha256],
        )
        self.assertEqual(
            [
                source["identity"]["public_id"]
                for source in media[0]["sources"]
            ],
            [
                "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
            ],
        )
        self.assertEqual(
            set(media[0]),
            {
                "ordinal",
                "storage_name",
                "package_path",
                "byte_count",
                "sha256",
                "source_reference_count",
                "sources",
                "capture_state",
                "restore_verification_state",
            },
        )
        self.assertNotIn("captured_at", raw.decode())
        self.assertNotIn("reference", media[0])
        totals = document["totals"]
        self.assertEqual(totals["media_reference_count"], 3)
        self.assertEqual(totals["unique_media_object_count"], 2)
        self.assertEqual(
            totals["media_bytes"],
            sum(item.capture.byte_count for item in request.media),
        )
        expected_payloads = []
        for component in request.components:
            expected_payloads.extend(
                (
                    {
                        "kind": "COMPONENT_RECORDS",
                        "package_path": component.records.package_path,
                        "byte_count": component.records.byte_count,
                        "sha256": component.records.sha256,
                    },
                    {
                        "kind": "COMPONENT_MEDIA_INDEX",
                        "package_path": (
                            component.media_index.package_path
                        ),
                        "byte_count": (
                            component.media_index.byte_count
                        ),
                        "sha256": component.media_index.sha256,
                    },
                )
            )
        expected_payloads.extend(
            {
                "kind": "MEDIA",
                "package_path": item.package_path,
                "storage_name": (
                    item.capture.logical_storage_name
                ),
                "byte_count": item.capture.byte_count,
                "sha256": item.capture.sha256,
            }
            for item in request.media
        )
        expected_payload_hash = hashlib.sha256(
            encode_canonical_document(
                {
                    "schema": "nexa.backup-payload-set.v1",
                    "payloads": expected_payloads,
                }
            )
        ).hexdigest()
        self.assertEqual(
            document["payload_set_sha256"],
            expected_payload_hash,
        )
        self.assertEqual(result.sha256, hashlib.sha256(raw).hexdigest())

    def test_open_is_opaque_and_cleanup_is_exact_idempotent(self):
        request = self._request()
        result = self.manifest_provider.build_manifest(request)
        manifest_directory = (
            self.workspace.path
            / WorkspaceArea.MANIFEST.value
            / result.reference.identifier.hex
        )
        self.assertEqual(
            {item.name for item in manifest_directory.iterdir()},
            {"manifest.json"},
        )
        if os.name != "nt":
            self.assertEqual(
                stat.S_IMODE(manifest_directory.stat().st_mode),
                0o700,
            )
            self.assertEqual(
                stat.S_IMODE(
                    (manifest_directory / "manifest.json").stat().st_mode
                ),
                0o600,
            )
        with self.manifest_provider.open_manifest(
            context=self.context,
            reference=result.reference,
        ) as reader:
            self.assertFalse(hasattr(reader, "name"))
            self.assertFalse(hasattr(reader, "fileno"))
            self.assertGreater(len(reader.read(16)), 0)
        self.assertIs(
            self.manifest_provider.cleanup_manifest(
                context=self.context,
                reference=result.reference,
            ),
            True,
        )
        self.assertIs(
            self.manifest_provider.cleanup_manifest(
                context=self.context,
                reference=result.reference,
            ),
            True,
        )
        self.assertFalse(manifest_directory.exists())
        with self.assertRaises(CanonicalManifestNotFound):
            with self.manifest_provider.open_manifest(
                context=self.context,
                reference=result.reference,
            ):
                pass

    def test_manifest_directory_creation_rollback_failure_preserves_abort_and_unowned_data(
        self,
    ):
        request = self._request()
        manifest_parent = (
            self.workspace.path / WorkspaceArea.MANIFEST.value
        )
        manifest_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        unowned_directory = manifest_parent / "unowned-sentinel"
        unowned_directory.mkdir()
        unowned_marker = unowned_directory / "keep.txt"
        unowned_marker.write_bytes(b"must remain")
        try:
            cases = (
                (
                    "ordinary",
                    OSError,
                    CanonicalManifestCreationError,
                ),
                (
                    "keyboard-interrupt",
                    KeyboardInterrupt,
                    KeyboardInterrupt,
                ),
                ("system-exit", SystemExit, SystemExit),
                ("generator-exit", GeneratorExit, GeneratorExit),
            )
            for label, raised_type, expected_type in cases:
                with self.subTest(failure=label):
                    identifier = uuid.uuid4()
                    target = manifest_parent / identifier.hex
                    attempted_removals = []

                    def fail_after_directory(
                        stage,
                        *,
                        selected=raised_type,
                    ):
                        if stage == "after_manifest_directory_creation":
                            raise selected(
                                "preserve manifest directory abort"
                            )

                    provider = CanonicalManifestProvider(
                        workspace_manager=self.manager,
                        reference_factory=lambda value=identifier: value,
                        failure_hook=fail_after_directory,
                    )
                    original_rmdir = os.rmdir

                    def reject_owned_removal(
                        candidate,
                        *args,
                        selected_target=target,
                        removals=attempted_removals,
                        rmdir=original_rmdir,
                        **kwargs,
                    ):
                        candidate = Path(candidate)
                        removals.append(candidate)
                        if candidate == selected_target:
                            raise OSError(
                                "private forced manifest rollback failure"
                            )
                        return rmdir(
                            candidate,
                            *args,
                            **kwargs,
                        )

                    with (
                        mock.patch(
                            "apps.backups.engine.canonical_manifest."
                            "os.rmdir",
                            side_effect=reject_owned_removal,
                        ),
                        self.assertRaises(expected_type) as raised,
                    ):
                        provider.build_manifest(request)
                    self.assertTrue(
                        raised.exception.cleanup_incomplete
                    )
                    self.assertEqual(attempted_removals, [target])
                    self.assertTrue(target.is_dir())
                    self.assertEqual(
                        unowned_marker.read_bytes(),
                        b"must remain",
                    )
                    self.assertNotIn(
                        unowned_directory,
                        attempted_removals,
                    )
                    if (
                        expected_type
                        is CanonicalManifestCreationError
                    ):
                        self.assertNotIn(
                            "private forced manifest rollback failure",
                            str(raised.exception),
                        )
                    target.rmdir()
        finally:
            if unowned_marker.exists():
                unowned_marker.unlink()
            if unowned_directory.exists():
                unowned_directory.rmdir()
        self.assertEqual(list(manifest_parent.iterdir()), [])

    def test_manifest_cleanup_tombstone_rejects_forged_context(self):
        request = self._request()
        result = self.manifest_provider.build_manifest(request)
        self.assertIs(
            self.manifest_provider.cleanup_manifest(
                context=self.context,
                reference=result.reference,
            ),
            True,
        )
        self.assertIs(
            self.manifest_provider.cleanup_manifest(
                context=self.context,
                reference=result.reference,
            ),
            True,
        )
        forged_contexts = (
            replace(self.context, backup_public_id=uuid.uuid4()),
            replace(self.context, business_public_id=uuid.uuid4()),
            replace(self.context, business_id=self.context.business_id + 1),
        )
        for forged_context in forged_contexts:
            self.assertEqual(
                forged_context.workspace_reference,
                self.context.workspace_reference,
            )
            with self.subTest(forged_context=forged_context):
                with self.assertRaises(CanonicalManifestCleanupError):
                    self.manifest_provider.cleanup_manifest(
                        context=forged_context,
                        reference=result.reference,
                    )

    def test_atomic_publication_failures_clean_engine_owned_outputs(self):
        request = self._request()
        stages = (
            "after_manifest_part_creation",
            "during_manifest_write",
            "after_manifest_flush",
            "after_manifest_fsync",
            "during_manifest_publication",
            "after_manifest_publication",
            "before_manifest_result_return",
        )
        for stage in stages:
            with self.subTest(stage=stage):

                def fail(candidate, *, selected=stage):
                    if candidate == selected:
                        raise OSError("private manifest path")

                provider = CanonicalManifestProvider(
                    workspace_manager=self.manager,
                    failure_hook=fail,
                )
                with self.assertRaises(
                    CanonicalManifestCreationError
                ) as raised:
                    provider.build_manifest(request)
                self.assertFalse(raised.exception.cleanup_incomplete)
                manifest_parent = (
                    self.workspace.path / WorkspaceArea.MANIFEST.value
                )
                if manifest_parent.exists():
                    self.assertEqual(list(manifest_parent.iterdir()), [])
                rendered = (
                    f"{raised.exception!s} {raised.exception!r}"
                )
                self.assertNotIn(str(self.staging_root), rendered)
                self.assertNotIn("private manifest path", rendered)

    def test_abort_is_preserved_and_cleanup_failure_is_retryable_exactly(self):
        request = self._request()

        def abort(stage):
            if stage == "during_manifest_write":
                raise KeyboardInterrupt("preserve abort")

        aborting_provider = CanonicalManifestProvider(
            workspace_manager=self.manager,
            failure_hook=abort,
        )
        with self.assertRaisesRegex(
            KeyboardInterrupt,
            "preserve abort",
        ):
            aborting_provider.build_manifest(request)
        manifest_parent = (
            self.workspace.path / WorkspaceArea.MANIFEST.value
        )
        if manifest_parent.exists():
            self.assertEqual(list(manifest_parent.iterdir()), [])

        selected_stage = {"value": None}

        def cleanup_hook(stage):
            if stage == selected_stage["value"]:
                raise OSError("private cleanup detail")

        provider = CanonicalManifestProvider(
            workspace_manager=self.manager,
            failure_hook=cleanup_hook,
        )
        result = provider.build_manifest(request)
        selected_stage["value"] = "before_manifest_cleanup_unlink"
        with self.assertRaises(
            CanonicalManifestCleanupError
        ) as raised:
            provider.cleanup_manifest(
                context=self.context,
                reference=result.reference,
            )
        self.assertNotIn("private cleanup detail", str(raised.exception))
        selected_stage["value"] = None
        self.assertIs(
            provider.cleanup_manifest(
                context=self.context,
                reference=result.reference,
            ),
            True,
        )


class CapabilityAndCheckGuardTests(SimpleTestCase):
    def test_internal_flags_are_ready_but_operational_execution_is_impossible(self):
        self.assertIs(SQLITE_SNAPSHOT_PROVIDER_READY, True)
        self.assertIs(TENANT_LOGICAL_EXPORT_PROVIDER_READY, True)
        self.assertIs(MEDIA_CAPTURE_PROVIDER_READY, True)
        self.assertIs(CANONICAL_MANIFEST_PROVIDER_READY, True)
        self.assertIs(OPERATIONAL_PROVIDER_STACK_READY, False)
        self.assertIs(real_execution_available(), False)
        capability = get_engine_capability()
        self.assertIs(capability.provider_stack_ready, False)
        self.assertIs(capability.real_execution_available, False)
        self.assertEqual(check_backup_capability_consistency(None), [])

    def test_no_http_admin_signal_scheduler_or_celery_surface_invokes_phase2d1(self):
        repository_root = Path(__file__).resolve().parents[1]
        searched = tuple(
            repository_root / relative
            for relative in (
                "apps/backups/views.py",
                "apps/backups/services.py",
                "apps/backups/tasks.py",
                "apps/backups/urls.py",
                "apps/backups/admin.py",
                "apps/backups/apps.py",
                "apps/backups/forms.py",
                "apps/backups/signals.py",
                "config/urls.py",
                "config/celery.py",
                "config/settings/base.py",
                "config/settings/development.py",
                "config/settings/production.py",
            )
        )
        forbidden = (
            "Phase2D1Coordinator",
            "LocalFilesystemMediaCaptureProvider",
            "CanonicalManifestProvider",
            "capture_media(",
            "build_manifest(",
        )
        for path in searched:
            if not path.exists():
                continue
            source = path.read_text(encoding="utf-8")
            for token in forbidden:
                with self.subTest(path=str(path), token=token):
                    self.assertNotIn(token, source)

        self.assertFalse(hasattr(execute_backup, "delay"))
        self.assertFalse(hasattr(execute_backup, "apply_async"))

    def test_media_storage_check_accepts_only_exact_local_matching_root(self):
        with tempfile.TemporaryDirectory() as directory:
            real_root = Path(directory).resolve()
            mismatch = real_root / "other"
            mismatch.mkdir()
            with self.settings(
                MEDIA_ROOT=str(real_root),
                STORAGES={
                    "default": {
                        "BACKEND": (
                            "django.core.files.storage.FileSystemStorage"
                        ),
                        "OPTIONS": {"location": str(real_root)},
                    }
                },
            ):
                self.assertEqual(
                    check_media_storage_configuration(None),
                    [],
                )

            unsafe_configurations = (
                {
                    "MEDIA_ROOT": "relative/media",
                    "STORAGES": {
                        "default": {
                            "BACKEND": (
                                "django.core.files.storage."
                                "FileSystemStorage"
                            )
                        }
                    },
                },
                {
                    "MEDIA_ROOT": str(real_root / "missing"),
                    "STORAGES": {
                        "default": {
                            "BACKEND": (
                                "django.core.files.storage."
                                "FileSystemStorage"
                            )
                        }
                    },
                },
                {
                    "MEDIA_ROOT": str(real_root),
                    "STORAGES": {
                        "default": {
                            "BACKEND": (
                                "django.core.files.storage."
                                "InMemoryStorage"
                            )
                        }
                    },
                },
                {
                    "MEDIA_ROOT": str(real_root),
                    "STORAGES": {
                        "default": {
                            "BACKEND": (
                                "django.core.files.storage."
                                "FileSystemStorage"
                            ),
                            "OPTIONS": {
                                "location": str(mismatch),
                            },
                        },
                    },
                },
            )
            for configuration in unsafe_configurations:
                with self.subTest(configuration=configuration):
                    with self.settings(**configuration):
                        self.assertEqual(
                            [
                                error.id
                                for error in (
                                    check_media_storage_configuration(
                                        None
                                    )
                                )
                            ],
                            ["backups.E025"],
                        )

    def test_phase2d1_requires_non_null_aware_ordered_snapshot_cutoff(self):
        valid_created = datetime(2026, 7, 29, 20, 0, tzinfo=UTC)
        valid_snapshot = SnapshotResult(
            reference=SnapshotReference(uuid.uuid4()),
            created_at=valid_created,
            consistent=True,
            byte_count=4096,
            page_count=1,
            page_size=4096,
            schema_version=1,
            journal_mode="wal",
            provider_identifier=SQLITE_SNAPSHOT_PROVIDER_IDENTIFIER,
            consistency_cutoff_at=valid_created,
        )
        fixture = object.__new__(Phase2D1Coordinator)
        fixture.registry = get_logical_export_registry()
        fixture.component_exporter = SimpleNamespace(
            policy=_logical_policy()
        )
        fixture.media_capture_provider = SimpleNamespace(
            policy=_media_policy()
        )
        plan = resolve_component_plan(
            scope=BackupScope.POS,
            enabled_products=(ProductOwner.POS,),
        ).export_components
        valid_context = BackupExecutionContext(
            backup_public_id=uuid.uuid4(),
            business_id=1,
            business_public_id=uuid.uuid4(),
            requested_scope=BackupScope.POS,
            resolved_products=(ProductOwner.POS,),
            trigger_type=BackupTrigger.MANUAL,
            actor_identity=ActorIdentitySnapshot(
                public_id="actor",
                email="",
                full_name="",
                actor_type="TENANT",
                platform_staff=False,
            ),
            application_version="test",
            backup_format_version="test",
            schema_migration_fingerprint="a" * 64,
            minimum_restore_version="test",
            idempotency_key="test",
            operation_correlation_id=uuid.uuid4(),
            workspace_reference=WorkspaceReference(uuid.uuid4()),
        )
        for cutoff in (
            None,
            valid_created.replace(tzinfo=None),
            valid_created + timedelta(microseconds=1),
        ):
            with self.subTest(cutoff=cutoff):
                with self.assertRaises(Phase2D1CoordinationError):
                    fixture._validate_request(
                        Phase2D1Request(
                            context=valid_context,
                            snapshot_result=replace(
                                valid_snapshot,
                                consistency_cutoff_at=cutoff,
                            ),
                            component_plan=plan,
                            component_exports=(None,) * len(plan),
                        )
                    )
