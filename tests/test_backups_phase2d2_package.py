"""Focused security tests for Phase 2D-2 deterministic package construction."""

from __future__ import annotations

import hashlib
import io
import json
import os
import uuid
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.core.files.storage import FileSystemStorage

from apps.backups.engine.availability import (
    DETERMINISTIC_PACKAGE_PROVIDER_READY,
    OPERATIONAL_PROVIDER_STACK_READY,
    get_engine_capability,
    real_execution_available,
)
from apps.backups.engine.canonical_manifest import CanonicalManifestProvider
from apps.backups.engine.contracts import (
    ComponentExportRequest,
    PackageBuildRequest,
    PackageReference,
    Phase2D1Request,
    Phase2D2Request,
)
from apps.backups.engine.deterministic_package import (
    DETERMINISTIC_PACKAGE_PROVIDER_IDENTIFIER,
    DETERMINISTIC_ZIP_TIMESTAMP,
    PACKAGE_FILE_NAME,
    DeterministicPackageProvider,
)
from apps.backups.engine.exceptions import (
    CanonicalManifestCleanupError,
    CanonicalManifestNotFound,
    ComponentExportNotFound,
    MediaObjectNotFound,
)
from apps.backups.engine.logical_export import ComponentExportStream
from apps.backups.engine.logical_export_registry import get_logical_export_registry
from apps.backups.engine.media_capture import LocalFilesystemMediaCaptureProvider
from apps.backups.engine.package_exceptions import (
    PackageCleanupError,
    PackageCreationError,
    PackageNotFound,
    PackageValidationError,
    SuccessfulStagingCleanupError,
)
from apps.backups.engine.phase2d1 import Phase2D1Coordinator
from apps.backups.engine.phase2d2 import Phase2D2Coordinator
from apps.backups.engine.pipeline import resolve_component_plan
from apps.backups.engine.workspace import WorkspaceArea
from apps.backups.enums import BackupScope, ProductOwner

from .test_backups_phase2b_snapshot import SQLiteSnapshotTestCase
from .test_backups_phase2d1_media_manifest import (
    RegistryAndTenantMediaIsolationTests,
    _media_policy,
    _write_stable_media_fixture,
)


class DeterministicPackageProviderTests(SQLiteSnapshotTestCase):
    _quote = staticmethod(RegistryAndTenantMediaIsolationTests._quote)
    _sqlite_type = staticmethod(
    RegistryAndTenantMediaIsolationTests._sqlite_type)
    _create_model_table = RegistryAndTenantMediaIsolationTests._create_model_table
    _install_export_schema = RegistryAndTenantMediaIsolationTests._install_export_schema
    _default_field_value = staticmethod(
    RegistryAndTenantMediaIsolationTests._default_field_value)
    _insert = RegistryAndTenantMediaIsolationTests._insert
    _seed_tenant = RegistryAndTenantMediaIsolationTests._seed_tenant
    _snapshot = RegistryAndTenantMediaIsolationTests._snapshot
    _exporter = RegistryAndTenantMediaIsolationTests._exporter

    def setUp(self):
        super().setUp()
        self.registry = get_logical_export_registry()
        self.plan = resolve_component_plan(
            scope=BackupScope.POS,
            enabled_products=(ProductOwner.POS,),
        ).export_components
        self.snapshot_cleanup = []
        self.export_cleanup = []
        self.phase2d1_cleanup = []
        self.package_cleanup = []
        self._next_ids = {}

    def tearDown(self):
        for provider, context, reference in reversed(self.package_cleanup):
            try:
                provider.cleanup_package(context=context, reference=reference)
            except Exception:
                pass
        for fixture in reversed(self.phase2d1_cleanup):
            phase_result = fixture["phase_result"]
            context = fixture["context"]
            try:
                fixture["manifest_provider"].cleanup_manifest(
                    context=context,
                    reference=phase_result.manifest.reference,
                )
            except Exception:
                pass
            for capture in reversed(phase_result.media_captures):
                try:
                    fixture["media_provider"].cleanup_media_capture(
                        context=context,
                        reference=capture.reference,
                    )
                except Exception:
                    pass
        for exporter, reference in reversed(self.export_cleanup):
            try:
                exporter.cleanup_component_export(
                    context=self.context,
                    reference=reference,
                    require_exact_evidence=True,
                )
            except Exception:
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

    def _build_phase2d1(self):
        self.context = replace(
            self.context,
            schema_migration_fingerprint="e" * 64,
            application_version="phase2d2-test",
            backup_format_version="2d2-test",
            minimum_restore_version="phase2d2-test",
        )
        self._install_export_schema()
        self._seed_tenant()
        self._insert("tenants.BusinessSettings")
        media_root = self.root / "phase2d2-media"
        (media_root / "products").mkdir(parents=True)
        media_payloads = (
            ("products/alpha.bin", b"alpha deterministic media"),
            ("products/beta.bin", b"beta deterministic media"),
        )
        for storage_name, content in media_payloads:
            _write_stable_media_fixture(media_root / storage_name, content)
        for suffix, storage_name in (
            ("A", "products/alpha.bin"),
            ("B", "products/alpha.bin"),
            ("C", "products/beta.bin"),
        ):
            self._insert(
                "catalog.Product",
                business=self.context.business_id,
                image=storage_name,
                name=f"Package product {suffix}",
                sku=f"PKG-{suffix}",
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
            (exporter, result.reference) for result in component_results
        )

        class _LocalInspector:
            @staticmethod
            def assess(_path):
                return SimpleNamespace(confirmed_local=True)

        media_provider = LocalFilesystemMediaCaptureProvider(
            snapshot_provider=snapshot_provider,
            workspace_manager=self.manager,
            policy=_media_policy(),
            storage_resolver=lambda: FileSystemStorage(location=str(media_root)),
            filesystem_inspector=_LocalInspector(),
            disk_usage_provider=lambda _path: SimpleNamespace(free=10**12),
        )
        manifest_provider = CanonicalManifestProvider(
            workspace_manager=self.manager,
        )
        phase2d1 = Phase2D1Coordinator(
            component_exporter=exporter,
            media_capture_provider=media_provider,
            manifest_provider=manifest_provider,
        )
        with self.settings(MEDIA_ROOT=str(media_root)):
            phase_result = phase2d1.build(
                Phase2D1Request(
                    context=self.context,
                    snapshot_result=snapshot,
                    component_plan=self.plan,
                    component_exports=component_results,
                )
            )
        fixture = {
            "context": self.context,
            "phase_result": phase_result,
            "exporter": exporter,
            "media_provider": media_provider,
            "manifest_provider": manifest_provider,
            "media_root": media_root,
        }
        self.phase2d1_cleanup.append(fixture)
        return fixture

    def _package_provider(self, fixture, **changes):
        values = {
            "component_exporter": fixture["exporter"],
            "media_capture_provider": fixture["media_provider"],
            "manifest_provider": fixture["manifest_provider"],
            "workspace_manager": self.manager,
            "disk_usage_provider": lambda _path: SimpleNamespace(free=10**12),
        }
        values.update(changes)
        return DeterministicPackageProvider(**values)

    @staticmethod
    def _read_package(provider, context, reference):
        with provider.open_package(context=context, reference=reference) as reader:
            return reader.read()

    def _assert_phase2d1_staging_open(self, fixture):
        context = fixture["context"]
        phase_result = fixture["phase_result"]
        for component in phase_result.component_exports:
            with fixture["exporter"].open_component_export(
                context=context,
                reference=component.reference,
                stream=ComponentExportStream.RECORDS,
            ) as reader:
                reader.read(1)
        for capture in phase_result.media_captures:
            with fixture["media_provider"].open_media_capture(
                context=context,
                reference=capture.reference,
            ) as reader:
                reader.read(1)
        with fixture["manifest_provider"].open_manifest(
            context=context,
            reference=phase_result.manifest.reference,
        ) as reader:
            self.assertGreater(len(reader.read(1)), 0)

    def test_identical_inputs_produce_identical_zip_bytes_and_external_hash(self):
        fixture = self._build_phase2d1()
        provider = self._package_provider(fixture)
        request = PackageBuildRequest(
            context=fixture["context"],
            phase2d1_result=fixture["phase_result"],
        )
        first = provider.build_package(request)
        second = provider.build_package(request)
        self.package_cleanup.extend(
            (
                (provider, fixture["context"], first.reference),
                (provider, fixture["context"], second.reference),
            )
        )
        first_bytes = self._read_package(
            provider,
            fixture["context"],
            first.reference,
        )
        second_bytes = self._read_package(
            provider,
            fixture["context"],
            second.reference,
        )
        self.assertNotEqual(first.reference, second.reference)
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(
            first.plaintext_sha256,
            hashlib.sha256(first_bytes).hexdigest(),
        )
        self.assertEqual(first.plaintext_sha256, second.plaintext_sha256)
        self.assertEqual(
            first.provider_identifier,
            DETERMINISTIC_PACKAGE_PROVIDER_IDENTIFIER,
        )

        with zipfile.ZipFile(io.BytesIO(first_bytes), mode="r") as archive:
            infos = archive.infolist()
            expected_names = ["manifest.json"]
            for ordinal, _component in enumerate(self.plan, start=1):
                expected_names.extend(
                    (
                        f"components/{ordinal:04d}/records.ndjson",
                        f"components/{ordinal:04d}/media-index.ndjson",
                    )
                )
            expected_names.extend(
                f"media/{ordinal:08d}.bin"
                for ordinal, _capture in enumerate(
                    fixture["phase_result"].media_captures,
                    start=1,
                )
            )
            self.assertEqual([info.filename for info in infos], expected_names)
            self.assertEqual(first.entry_count, len(expected_names))
            self.assertEqual(archive.comment, b"")
            for info in infos:
                self.assertEqual(info.date_time, DETERMINISTIC_ZIP_TIMESTAMP)
                self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
                self.assertEqual(info.file_size, info.compress_size)
                self.assertEqual((info.external_attr >> 16) & 0o777, 0o600)
                self.assertFalse(info.flag_bits & 0x1)
            manifest = json.loads(archive.read("manifest.json"))
        self.assertNotIn("package_sha256", manifest)
        self.assertNotIn(first.plaintext_sha256, first_bytes.decode("latin1"))

    def test_coordinator_cleans_successful_plaintext_staging_and_keeps_package(self):
        fixture = self._build_phase2d1()
        provider = self._package_provider(fixture)
        coordinator = Phase2D2Coordinator(
            component_exporter=fixture["exporter"],
            media_capture_provider=fixture["media_provider"],
            manifest_provider=fixture["manifest_provider"],
            package_provider=provider,
        )
        result = coordinator.build(
            Phase2D2Request(
                context=fixture["context"],
                phase2d1_result=fixture["phase_result"],
            )
        )
        self.package_cleanup.append(
            (provider, fixture["context"], result.package.reference)
        )
        package_bytes = self._read_package(
            provider,
            fixture["context"],
            result.package.reference,
        )
        self.assertEqual(
            hashlib.sha256(package_bytes).hexdigest(),
            result.package.plaintext_sha256,
        )
        for component in fixture["phase_result"].component_exports:
            with self.assertRaises(ComponentExportNotFound):
                with fixture["exporter"].open_component_export(
                    context=fixture["context"],
                    reference=component.reference,
                    stream=ComponentExportStream.RECORDS,
                ):
                    pass
        for capture in fixture["phase_result"].media_captures:
            with self.assertRaises(MediaObjectNotFound):
                with fixture["media_provider"].open_media_capture(
                    context=fixture["context"],
                    reference=capture.reference,
                ):
                    pass
        with self.assertRaises(CanonicalManifestNotFound):
            with fixture["manifest_provider"].open_manifest(
                context=fixture["context"],
                reference=fixture["phase_result"].manifest.reference,
            ):
                pass

    def test_package_failure_leaves_all_phase2d1_staging_intact(self):
        fixture = self._build_phase2d1()

        def fail(stage):
            if stage == "after_package_entry":
                raise OSError("private package path")

        provider = self._package_provider(fixture, failure_hook=fail)
        coordinator = Phase2D2Coordinator(
            component_exporter=fixture["exporter"],
            media_capture_provider=fixture["media_provider"],
            manifest_provider=fixture["manifest_provider"],
            package_provider=provider,
        )
        with self.assertRaises(PackageCreationError) as raised:
            coordinator.build(
                Phase2D2Request(
                    context=fixture["context"],
                    phase2d1_result=fixture["phase_result"],
                )
            )
        self.assertFalse(raised.exception.cleanup_incomplete)
        self.assertNotIn("private package path", str(raised.exception))
        self._assert_phase2d1_staging_open(fixture)
        package_parent = self.workspace.path / WorkspaceArea.PACKAGE.value
        if package_parent.exists():
            self.assertEqual(list(package_parent.iterdir()), [])

    def test_failed_post_build_validation_keeps_phase2d1_staging(self):
        fixture = self._build_phase2d1()
        provider = self._package_provider(fixture)
        coordinator = Phase2D2Coordinator(
            component_exporter=fixture["exporter"],
            media_capture_provider=fixture["media_provider"],
            manifest_provider=fixture["manifest_provider"],
            package_provider=provider,
        )
        published = {}
        original_build = provider.build_package

        def capture_result(request):
            result = original_build(request)
            published["result"] = result
            return result

        with (
            mock.patch.object(provider, "build_package", side_effect=capture_result),
            mock.patch.object(
                provider,
                "validate_package_evidence",
                side_effect=PackageValidationError(),
            ),
            self.assertRaises(PackageValidationError),
        ):
            coordinator.build(
                Phase2D2Request(
                    context=fixture["context"],
                    phase2d1_result=fixture["phase_result"],
                )
            )
        package = published["result"]
        self.package_cleanup.append(
            (provider, fixture["context"], package.reference)
        )
        self._assert_phase2d1_staging_open(fixture)
        self.assertGreater(
            len(
                self._read_package(
                    provider,
                    fixture["context"],
                    package.reference,
                )
            ),
            0,
        )

    def test_package_cleanup_retries_after_file_was_safely_unlinked(self):
        fixture = self._build_phase2d1()
        provider = self._package_provider(fixture)
        package = provider.build_package(
            PackageBuildRequest(
                context=fixture["context"],
                phase2d1_result=fixture["phase_result"],
            )
        )
        self.package_cleanup.append(
            (provider, fixture["context"], package.reference)
        )
        original_rmdir = os.rmdir
        failed = {"done": False}

        def fail_once(path, *args, **kwargs):
            if (
                Path(path).name == package.reference.identifier.hex
                and not failed["done"]
            ):
                failed["done"] = True
                raise OSError("private package cleanup detail")
            return original_rmdir(path, *args, **kwargs)

        with mock.patch(
            "apps.backups.engine.deterministic_package.os.rmdir",
            side_effect=fail_once,
        ):
            with self.assertRaises(PackageCleanupError) as raised:
                provider.cleanup_package(
                    context=fixture["context"],
                    reference=package.reference,
                )
        self.assertNotIn("private package cleanup detail", str(raised.exception))
        with self.assertRaises(PackageNotFound):
            self._read_package(
                provider,
                fixture["context"],
                package.reference,
            )
        self.assertIs(
            provider.cleanup_package(
                context=fixture["context"],
                reference=package.reference,
            ),
            True,
        )
        self.assertIs(
            provider.cleanup_package(
                context=fixture["context"],
                reference=package.reference,
            ),
            True,
        )

    def test_cleanup_failure_is_reported_after_other_staging_is_cleaned(self):
        fixture = self._build_phase2d1()
        provider = self._package_provider(fixture)
        coordinator = Phase2D2Coordinator(
            component_exporter=fixture["exporter"],
            media_capture_provider=fixture["media_provider"],
            manifest_provider=fixture["manifest_provider"],
            package_provider=provider,
        )
        published = {}
        original_build = provider.build_package

        def capture_result(request):
            result = original_build(request)
            published["result"] = result
            return result

        with (
            mock.patch.object(provider, "build_package", side_effect=capture_result),
            mock.patch.object(
                fixture["manifest_provider"],
                "cleanup_manifest",
                side_effect=CanonicalManifestCleanupError(),
            ),
            self.assertRaises(SuccessfulStagingCleanupError) as raised,
        ):
            coordinator.build(
                Phase2D2Request(
                    context=fixture["context"],
                    phase2d1_result=fixture["phase_result"],
                )
            )
        self.assertTrue(raised.exception.cleanup_incomplete)
        package = published["result"]
        self.package_cleanup.append(
            (provider, fixture["context"], package.reference)
        )
        self.assertGreater(
            len(
                self._read_package(
                    provider,
                    fixture["context"],
                    package.reference,
                )
            ),
            0,
        )
        with fixture["manifest_provider"].open_manifest(
            context=fixture["context"],
            reference=fixture["phase_result"].manifest.reference,
        ) as reader:
            self.assertGreater(len(reader.read(1)), 0)
        for capture in fixture["phase_result"].media_captures:
            with self.assertRaises(MediaObjectNotFound):
                with fixture["media_provider"].open_media_capture(
                    context=fixture["context"],
                    reference=capture.reference,
                ):
                    pass
        for component in fixture["phase_result"].component_exports:
            with self.assertRaises(ComponentExportNotFound):
                with fixture["exporter"].open_component_export(
                    context=fixture["context"],
                    reference=component.reference,
                    stream=ComponentExportStream.RECORDS,
                ):
                    pass

    def test_extra_hardlink_blocks_rollback_without_touching_unowned_alias(self):
        fixture = self._build_phase2d1()
        identifier = uuid.uuid4()
        unowned_path = None

        def fail_with_unowned_alias(stage):
            nonlocal unowned_path
            if stage != "after_package_publication_link":
                return
            directory = (
                self.workspace.path
                / WorkspaceArea.PACKAGE.value
                / identifier.hex
            )
            final_path = directory / PACKAGE_FILE_NAME
            unowned_path = directory / "unowned-alias.bin"
            os.link(final_path, unowned_path, follow_symlinks=False)
            raise OSError("private hardlink detail")

        provider = self._package_provider(
            fixture,
            reference_factory=lambda: PackageReference(identifier),
            failure_hook=fail_with_unowned_alias,
        )
        with self.assertRaises(PackageCreationError) as raised:
            provider.build_package(
                PackageBuildRequest(
                    context=fixture["context"],
                    phase2d1_result=fixture["phase_result"],
                )
            )
        self.assertTrue(raised.exception.cleanup_incomplete)
        self.assertNotIn("private hardlink detail", str(raised.exception))
        self.assertIsNotNone(unowned_path)
        directory = unowned_path.parent
        names = {path.name for path in directory.iterdir()}
        self.assertIn("unowned-alias.bin", names)
        self.assertIn(PACKAGE_FILE_NAME, names)
        self.assertTrue(any(name.endswith(".part") for name in names))
        link_counts = {path.stat().st_nlink for path in directory.iterdir()}
        self.assertEqual(link_counts, {3})
        self._assert_phase2d1_staging_open(fixture)
        for path in tuple(directory.iterdir()):
            path.unlink()
        directory.rmdir()

    def test_capability_remains_fail_closed_after_package_provider(self):
        self.assertIs(DETERMINISTIC_PACKAGE_PROVIDER_READY, True)
        self.assertIs(OPERATIONAL_PROVIDER_STACK_READY, False)
        self.assertIs(real_execution_available(), False)
        capability = get_engine_capability()
        self.assertIs(capability.deterministic_package_provider_ready, True)
        self.assertIs(capability.provider_stack_ready, False)

        repository_root = Path(__file__).resolve().parents[1]
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
        ):
            path = repository_root / relative
            if path.exists():
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("Phase2D2Coordinator", source)
                self.assertNotIn("DeterministicPackageProvider", source)
