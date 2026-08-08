"""Focused non-mutating restore-preflight tests for Backup Engine Phase 3A."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import uuid
import zipfile
from dataclasses import FrozenInstanceError, fields, replace
from types import SimpleNamespace
from unittest import mock

from django.core.files.storage import FileSystemStorage
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.backups import services
from apps.backups.engine import events
from apps.backups.engine import restore_workspace as restore_workspace_module
from apps.backups.engine.availability import (
    RESTORE_MUTATION_ENGINE_READY,
    RESTORE_PREFLIGHT_ENGINE_READY,
)
from apps.backups.engine.canonical_manifest import CanonicalManifestProvider
from apps.backups.engine.checks import check_restore_preflight_configuration
from apps.backups.engine.context import ActorIdentitySnapshot
from apps.backups.engine.contracts import PackageCompatibilityStatus
from apps.backups.engine.deterministic_package import DeterministicPackageProvider
from apps.backups.engine.durable_storage import LocalPrivateDurableStorageProvider
from apps.backups.engine.durable_storage_policy import DurableStoragePolicy
from apps.backups.engine.encrypted_artifact import EncryptedArtifactProvider
from apps.backups.engine.encryption_policy import EncryptionPolicy
from apps.backups.engine.key_management import LocalConfiguredKekProvider
from apps.backups.engine.logical_serialization import encode_canonical_document
from apps.backups.engine.media_capture import LocalFilesystemMediaCaptureProvider
from apps.backups.engine.package_exceptions import PackageValidationError
from apps.backups.engine.package_verification import (
    IndependentPackageVerifier,
    PackageCompatibilityPolicy,
)
from apps.backups.engine.phase2d1 import Phase2D1Coordinator
from apps.backups.engine.phase2d2 import Phase2D2Coordinator
from apps.backups.engine.restore_exceptions import (
    RestoreDecryptError,
    RestoreDurableObjectError,
    RestoreEngineError,
    RestoreLockUnavailable,
    RestorePreflightCleanupError,
    RestoreSelectionError,
    RestoreTenantMismatch,
)
from apps.backups.engine.restore_preflight import (
    RESTORE_PREFLIGHT_EVIDENCE_SCHEMA,
    RestoreComponentPlanItem,
    RestorePreflightCleanupRequest,
    RestorePreflightCoordinator,
    RestorePreflightProviderStack,
    RestorePreflightReference,
    RestorePreflightRequest,
    RestorePreflightResult,
    RestorePreflightState,
)
from apps.backups.engine.restore_workspace import (
    RestoredPackageProvider,
    _safe_archive_name,
    _validate_zipinfo,
)
from apps.backups.engine.retention import RetentionEngine
from apps.backups.engine.retention_policy import RetentionPolicy
from apps.backups.engine.runtime import (
    BackupExecutionCoordinator,
    BackupExecutionRequest,
    RuntimeProviderStack,
)
from apps.backups.engine.workspace import WorkspaceArea
from apps.backups.enums import (
    BackupScope,
    BackupStatus,
    BackupTrigger,
    IntegrityStatus,
    OperationKind,
    RestoreBehavior,
)
from apps.backups.models import BackupRecord, TenantOperationLock
from apps.backups.versioning import (
    BACKUP_FORMAT_VERSION,
    get_application_version,
    schema_migration_fingerprint,
)
from apps.subscriptions.models import Plan, Subscription
from apps.tenants.services import provision_business

from .test_backups_phase2b_snapshot import _StaticFilesystemInspector
from .test_backups_phase2d1_media_manifest import _media_policy
from .test_backups_phase2d2_package import DeterministicPackageProviderTests


class _LocalInspector:
    @staticmethod
    def assess(_path):
        return SimpleNamespace(confirmed_local=True)


class RestorePreflightEndToEndTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.fixture = DeterministicPackageProviderTests(methodName="runTest")
        self.fixture.setUp()
        self.owner = User.objects.create_user(
            email=f"restore-{uuid.uuid4().hex}@example.test",
            password="StrongPass123!",
            full_name="Restore Owner",
        )
        self.business = provision_business(owner=self.owner, name="Restore Tenant")
        plan_id = Subscription.objects.get(business=self.business).plan_id
        Plan.objects.filter(pk=plan_id).update(feature_sales=True)
        self.fixture.context = replace(
            self.fixture.context,
            business_id=self.business.pk,
            business_public_id=self.business.public_id,
            requested_scope=BackupScope.POS,
            trigger_type=BackupTrigger.MANUAL,
        )
        self.fixture._install_export_schema()
        self.fixture._seed_tenant()
        self.fixture._insert("tenants.BusinessSettings")
        self.media_root = self.fixture.root / "restore-media"
        self.media_root.mkdir()
        self.stack = self._runtime_stack()

    def tearDown(self):
        try:
            self.fixture.tearDown()
        finally:
            super().tearDown()

    @staticmethod
    def _encryption_policy():
        return EncryptionPolicy(
            chunk_bytes=4096,
            maximum_plaintext_bytes=64 * 1024**2,
            maximum_artifact_bytes=65 * 1024**2,
            timeout_seconds=60,
            minimum_free_bytes=0,
            headroom_multiplier=1.0,
            maximum_header_bytes=65_536,
        )

    def _runtime_stack(self):
        snapshot = self.fixture.provider()
        exporter = self.fixture._exporter(snapshot)
        media = LocalFilesystemMediaCaptureProvider(
            snapshot_provider=snapshot,
            workspace_manager=self.fixture.manager,
            policy=_media_policy(),
            storage_resolver=lambda: FileSystemStorage(location=str(self.media_root)),
            filesystem_inspector=_LocalInspector(),
            disk_usage_provider=lambda _path: SimpleNamespace(free=10**12),
        )
        manifest = CanonicalManifestProvider(workspace_manager=self.fixture.manager)
        phase2d1 = Phase2D1Coordinator(
            component_exporter=exporter,
            media_capture_provider=media,
            manifest_provider=manifest,
        )
        package = DeterministicPackageProvider(
            component_exporter=exporter,
            media_capture_provider=media,
            manifest_provider=manifest,
            workspace_manager=self.fixture.manager,
            disk_usage_provider=lambda _path: SimpleNamespace(free=10**12),
        )
        phase2d2 = Phase2D2Coordinator(
            component_exporter=exporter,
            media_capture_provider=media,
            manifest_provider=manifest,
            package_provider=package,
        )
        verifier = IndependentPackageVerifier(
            package_provider=package,
            workspace_manager=self.fixture.manager,
        )
        kek = LocalConfiguredKekProvider(
            key_b64=base64.b64encode(b"r" * 32).decode("ascii"),
            key_identifier="restore-test-kek",
            key_version="v1",
        )
        encrypted = EncryptedArtifactProvider(
            package_provider=package,
            verification_provider=verifier,
            kek_provider=kek,
            workspace_manager=self.fixture.manager,
            policy=self._encryption_policy(),
            disk_usage_provider=lambda _path: SimpleNamespace(free=10**12),
        )
        durable = LocalPrivateDurableStorageProvider(
            encrypted_artifact_provider=encrypted,
            policy=DurableStoragePolicy(
                root=self.fixture.root / "restore-durable",
                chunk_bytes=4096,
                maximum_object_bytes=65 * 1024**2,
                timeout_seconds=60,
                minimum_free_bytes=0,
                headroom_multiplier=1.0,
                require_local=True,
            ),
            filesystem_inspector=_StaticFilesystemInspector(),
            disk_usage_provider=lambda _path: SimpleNamespace(free=10**12),
        )
        retention = RetentionEngine(
            durable_provider=durable,
            policy=RetentionPolicy(5, 100, 300),
        )
        return RuntimeProviderStack(
            workspace_manager=self.fixture.manager,
            snapshot_provider=snapshot,
            component_exporter=exporter,
            media_capture_provider=media,
            manifest_provider=manifest,
            phase2d1_coordinator=phase2d1,
            package_provider=package,
            phase2d2_coordinator=phase2d2,
            verification_provider=verifier,
            kek_provider=kek,
            encrypted_artifact_provider=encrypted,
            durable_storage_provider=durable,
            retention_engine=retention,
        ).validated()

    def _backup(self):
        backup = services.create_backup_request(
            business=self.business,
            scope=BackupScope.POS,
            actor=self.owner,
            trigger=BackupTrigger.MANUAL,
            idempotency_key=f"restore-backup-{uuid.uuid4().hex}",
        )
        coordinator = BackupExecutionCoordinator(
            provider_stack=self.stack,
            lock_lease_seconds=3600,
        )
        with override_settings(MEDIA_ROOT=self.media_root):
            coordinator.execute(BackupExecutionRequest.from_record(backup))
        backup.refresh_from_db()
        self.assertEqual(backup.status, BackupStatus.SUCCEEDED)
        return backup

    def _durable_path(self, backup):
        return (
            self.stack.durable_storage_provider.root
            / "objects"
            / backup.tenant_public_id_snapshot.hex
            / backup.public_id.hex
            / uuid.UUID(backup.opaque_object_key).hex
            / "artifact.nxb"
        )

    def _restore_coordinator(self, *, kek_provider=None, current_application_version=None):
        creation_encrypted = self.stack.encrypted_artifact_provider
        selected_kek = kek_provider or self.stack.kek_provider
        encrypted = EncryptedArtifactProvider(
            package_provider=self.stack.package_provider,
            verification_provider=self.stack.verification_provider,
            kek_provider=selected_kek,
            workspace_manager=self.fixture.manager,
            policy=self._encryption_policy(),
            disk_usage_provider=lambda _path: SimpleNamespace(free=10**12),
        )
        durable = LocalPrivateDurableStorageProvider(
            encrypted_artifact_provider=encrypted,
            policy=self.stack.durable_storage_provider.policy,
            filesystem_inspector=_StaticFilesystemInspector(),
            disk_usage_provider=lambda _path: SimpleNamespace(free=10**12),
        )
        self.assertIsNot(durable, self.stack.durable_storage_provider)
        self.assertEqual(durable._stored, {})
        restored = RestoredPackageProvider(workspace_manager=self.fixture.manager)
        verifier = IndependentPackageVerifier(
            package_provider=restored,
            workspace_manager=self.fixture.manager,
            compatibility_policy=PackageCompatibilityPolicy(
                current_schema_fingerprint=schema_migration_fingerprint(),
                current_application_version=(
                    current_application_version or get_application_version()
                ),
                current_backup_format_version=BACKUP_FORMAT_VERSION,
            ),
        )
        del creation_encrypted
        return RestorePreflightCoordinator(
            provider_stack=RestorePreflightProviderStack(
                workspace_manager=self.fixture.manager,
                encrypted_artifact_provider=encrypted,
                durable_storage_provider=durable,
                restored_package_provider=restored,
                verification_provider=verifier,
            ),
            lock_lease_seconds=3600,
        )

    def _request(self, backup, **changes):
        values = {
            "operation_public_id": uuid.uuid4(),
            "business_public_id": self.business.public_id,
            "backup_public_id": backup.public_id,
            "actor_identity": ActorIdentitySnapshot.from_actor(self.owner),
            "idempotency_key": f"preflight-{uuid.uuid4().hex}",
        }
        values.update(changes)
        return RestorePreflightRequest(**values)

    def _cleanup(self, coordinator, request, result):
        return coordinator.cleanup_restore_preflight(
            RestorePreflightCleanupRequest(
                operation_public_id=request.operation_public_id,
                business_public_id=request.business_public_id,
                backup_public_id=request.backup_public_id,
                preflight_reference=result.preflight_reference,
            )
        )

    def _replace_durable_bytes(self, backup, raw):
        path = self._durable_path(backup)
        with path.open("wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        BackupRecord.objects.filter(pk=backup.pk).update(
            backup_size_bytes=len(raw),
            whole_artifact_hash=hashlib.sha256(raw).hexdigest(),
        )
        backup.refresh_from_db()

    @staticmethod
    def _mutated_header(raw, field, value):
        prefix = struct.Struct(">8sI")
        magic, header_size = prefix.unpack(raw[: prefix.size])
        document = json.loads(raw[prefix.size : prefix.size + header_size])
        document[field] = value
        header = encode_canonical_document(document)
        return prefix.pack(magic, len(header)) + header + raw[prefix.size + header_size :]

    def test_real_full_chain_to_restart_clean_restore_preflight_happy_path(self):
        backup = self._backup()
        coordinator = self._restore_coordinator()
        request = self._request(backup)
        result = coordinator.run(request)

        self.assertTrue(result.restore_ready)
        self.assertEqual(result.state, RestorePreflightState.RESTORE_READY)
        self.assertEqual(result.business_public_id, self.business.public_id)
        self.assertEqual(result.backup_public_id, backup.public_id)
        self.assertEqual(result.record_count, backup.total_row_count)
        workspace = self.fixture.manager.handle(request.operation_public_id)
        preflight = workspace.system_area_path(WorkspaceArea.RESTORE_PREFLIGHT)
        self.assertTrue((preflight / "package.zip").is_file())
        self.assertTrue((preflight / "extracted" / "manifest.json").is_file())
        self.assertTrue((preflight / "preflight.json").is_file())
        self.assertFalse(
            TenantOperationLock.objects.filter(business=self.business, active=True).exists()
        )
        self.assertTrue(self._cleanup(coordinator, request, result))
        self.assertFalse(workspace.path.exists())

    def test_non_success_and_deleted_backups_are_rejected(self):
        queued = services.create_backup_request(
            business=self.business,
            scope=BackupScope.POS,
            actor=self.owner,
            trigger=BackupTrigger.MANUAL,
            idempotency_key=f"queued-{uuid.uuid4().hex}",
        )
        coordinator = self._restore_coordinator()
        with self.assertRaises(RestoreSelectionError):
            coordinator.run(self._request(queued))
        BackupRecord.objects.filter(pk=queued.pk).update(
            status=BackupStatus.DELETED,
            integrity_status=IntegrityStatus.CORRUPTED,
            deleted_at=timezone.now(),
        )
        queued.refresh_from_db()
        with self.assertRaises(RestoreSelectionError):
            coordinator.run(self._request(queued))

    def test_wrong_tenant_and_forged_backup_uuid_are_indistinguishable(self):
        backup = self._backup()
        other_owner = User.objects.create_user(
            email=f"other-{uuid.uuid4().hex}@example.test",
            password="StrongPass123!",
        )
        other_business = provision_business(owner=other_owner, name="Other Tenant")
        coordinator = self._restore_coordinator()
        for request in (
            self._request(backup, business_public_id=other_business.public_id),
            self._request(backup, backup_public_id=uuid.uuid4()),
        ):
            with self.subTest(request=request):
                with self.assertRaises(RestoreTenantMismatch) as raised:
                    coordinator.run(request)
                self.assertEqual(raised.exception.issue_code, "restore_selection_unavailable")

    def test_missing_durable_object_is_rejected_without_deleting_metadata(self):
        backup = self._backup()
        self._durable_path(backup).unlink()
        coordinator = self._restore_coordinator()
        with self.assertRaises(RestoreDurableObjectError):
            coordinator.run(self._request(backup))
        backup.refresh_from_db()
        self.assertEqual(backup.status, BackupStatus.SUCCEEDED)
        self.assertEqual(backup.integrity_status, IntegrityStatus.VERIFIED)

    def test_durable_mutation_truncation_and_append_are_rejected(self):
        backup = self._backup()
        path = self._durable_path(backup)
        original = path.read_bytes()
        mutations = (
            bytes([original[0] ^ 1]) + original[1:],
            original[:-1],
            original + b"appended",
        )
        for raw in mutations:
            with self.subTest(size=len(raw)):
                with path.open("wb") as output:
                    output.write(raw)
                coordinator = self._restore_coordinator()
                with self.assertRaises(RestoreDurableObjectError):
                    coordinator.run(self._request(backup))
                with path.open("wb") as output:
                    output.write(original)

    def test_wrong_header_tenant_and_backup_uuid_are_rejected_by_phase2f(self):
        for field in ("tenant_public_id", "backup_public_id"):
            with self.subTest(field=field):
                backup = self._backup()
                raw = self._durable_path(backup).read_bytes()
                self._replace_durable_bytes(
                    backup,
                    self._mutated_header(raw, field, str(uuid.uuid4())),
                )
                with self.assertRaises(RestoreDecryptError):
                    self._restore_coordinator().run(self._request(backup))

    def test_ciphertext_and_auth_tag_corruption_are_rejected_by_phase2f(self):
        for offset in (-17, -1):
            with self.subTest(offset=offset):
                backup = self._backup()
                raw = bytearray(self._durable_path(backup).read_bytes())
                raw[offset] ^= 1
                self._replace_durable_bytes(backup, bytes(raw))
                with self.assertRaises(RestoreDecryptError):
                    self._restore_coordinator().run(self._request(backup))

    def test_wrong_kek_is_rejected_and_plaintext_workspace_is_cleaned(self):
        backup = self._backup()
        wrong_kek = LocalConfiguredKekProvider(
            key_b64=base64.b64encode(b"w" * 32).decode("ascii"),
            key_identifier="restore-test-kek",
            key_version="v1",
        )
        coordinator = self._restore_coordinator(kek_provider=wrong_kek)
        request = self._request(backup)
        with self.assertRaises(RestoreDecryptError):
            coordinator.run(request)
        self.assertFalse(self.fixture.manager.handle(request.operation_public_id).path.exists())

    def test_restore_lock_contention_fails_closed(self):
        backup = services.create_backup_request(
            business=self.business,
            scope=BackupScope.POS,
            actor=self.owner,
            trigger=BackupTrigger.MANUAL,
            idempotency_key=f"locked-{uuid.uuid4().hex}",
        )
        existing = services.acquire_tenant_operation_lock(
            business=self.business,
            operation_kind=OperationKind.BACKUP,
            operation_public_id=uuid.uuid4(),
            lease_seconds=3600,
        )
        try:
            with self.assertRaises(RestoreLockUnavailable):
                self._restore_coordinator().run(self._request(backup))
            existing.refresh_from_db()
            self.assertTrue(existing.active)
        finally:
            services.release_tenant_operation_lock(
                existing,
                lock_token=existing.lock_token,
            )

    def test_lock_is_released_after_restore_failure(self):
        backup = self._backup()
        self._durable_path(backup).unlink()
        with self.assertRaises(RestoreDurableObjectError):
            self._restore_coordinator().run(self._request(backup))
        self.assertFalse(
            TenantOperationLock.objects.filter(business=self.business, active=True).exists()
        )

    def test_preflight_cleanup_is_idempotent_and_rejects_forged_context(self):
        backup = self._backup()
        coordinator = self._restore_coordinator()
        request = self._request(backup)
        result = coordinator.run(request)
        forged = RestorePreflightCleanupRequest(
            operation_public_id=request.operation_public_id,
            business_public_id=uuid.uuid4(),
            backup_public_id=backup.public_id,
            preflight_reference=result.preflight_reference,
        )
        with self.assertRaises(RestorePreflightCleanupError):
            coordinator.cleanup_restore_preflight(forged)
        cleanup = RestorePreflightCleanupRequest(
            operation_public_id=request.operation_public_id,
            business_public_id=self.business.public_id,
            backup_public_id=backup.public_id,
            preflight_reference=result.preflight_reference,
        )
        self.assertTrue(coordinator.cleanup_restore_preflight(cleanup))
        self.assertFalse(coordinator.cleanup_restore_preflight(cleanup))

    def test_cleanup_refuses_hardlink_ambiguity_without_deleting_owned_files(self):
        backup = self._backup()
        coordinator = self._restore_coordinator()
        request = self._request(backup)
        result = coordinator.run(request)
        workspace = self.fixture.manager.handle(request.operation_public_id)
        manifest = (
            workspace.system_area_path(WorkspaceArea.RESTORE_PREFLIGHT)
            / "extracted"
            / "manifest.json"
        )
        extra = self.fixture.root / f"hardlink-{uuid.uuid4().hex}"
        os.link(manifest, extra)
        try:
            with self.assertRaises(RestorePreflightCleanupError):
                self._cleanup(coordinator, request, result)
            self.assertTrue(manifest.exists())
        finally:
            extra.unlink()
        self.assertTrue(self._cleanup(coordinator, request, result))

    def test_cleanup_preserves_abort_and_resumes_from_exact_owned_evidence(self):
        backup = self._backup()
        coordinator = self._restore_coordinator()
        request = self._request(backup)
        result = coordinator.run(request)
        completed = next(iter(coordinator._completed.values()))
        coordinator.provider_stack.verification_provider.cleanup_verification_evidence(
            context=completed.context,
            reference=completed.verification.reference,
        )
        verification_area = self.fixture.manager.handle(
            request.operation_public_id
        ).system_area_path(WorkspaceArea.VERIFICATION)
        os.rmdir(verification_area)
        real_unlink = os.unlink

        def abort_after_unlink(path):
            real_unlink(path)
            raise KeyboardInterrupt

        with mock.patch.object(
            restore_workspace_module.os,
            "unlink",
            side_effect=abort_after_unlink,
        ):
            with self.assertRaises(KeyboardInterrupt):
                coordinator.provider_stack.restored_package_provider.cleanup_workspace(
                    context=completed.context,
                    package=completed.package,
                )
        self.assertTrue(self._cleanup(coordinator, request, result))

    def test_preflight_does_not_write_live_media_or_change_business_records(self):
        backup = self._backup()
        before_business = tuple(
            self.business.__class__.objects.filter(pk=self.business.pk).values_list(
                "name", "public_id"
            )
        )
        before_media = tuple(
            sorted(path.relative_to(self.media_root) for path in self.media_root.rglob("*"))
        )
        coordinator = self._restore_coordinator()
        request = self._request(backup)
        result = coordinator.run(request)
        after_business = tuple(
            self.business.__class__.objects.filter(pk=self.business.pk).values_list(
                "name", "public_id"
            )
        )
        after_media = tuple(
            sorted(path.relative_to(self.media_root) for path in self.media_root.rglob("*"))
        )
        self.assertEqual(before_business, after_business)
        self.assertEqual(before_media, after_media)
        self._cleanup(coordinator, request, result)

    def test_abort_signal_is_preserved_and_lock_workspace_are_released(self):
        backup = self._backup()
        coordinator = self._restore_coordinator()
        request = self._request(backup)
        with mock.patch.object(
            coordinator.provider_stack.durable_storage_provider,
            "reattest_stored_object",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                coordinator.run(request)
        self.assertFalse(self.fixture.manager.handle(request.operation_public_id).path.exists())
        self.assertFalse(
            TenantOperationLock.objects.filter(business=self.business, active=True).exists()
        )

    def test_errors_do_not_expose_paths_object_keys_or_raw_data(self):
        backup = self._backup()
        self._durable_path(backup).unlink()
        with self.assertRaises(RestoreDurableObjectError) as raised:
            self._restore_coordinator().run(self._request(backup))
        rendered = str(raised.exception)
        self.assertNotIn(str(self.fixture.root), rendered)
        self.assertNotIn(backup.opaque_object_key, rendered)
        self.assertNotIn("artifact.nxb", rendered)

    def test_restore_time_verifier_is_invoked_even_for_historically_verified_backup(self):
        backup = self._backup()
        coordinator = self._restore_coordinator()
        request = self._request(backup)
        with mock.patch.object(
            coordinator.provider_stack.verification_provider,
            "verify",
            wraps=coordinator.provider_stack.verification_provider.verify,
        ) as verify:
            result = coordinator.run(request)
        verify.assert_called_once()
        self.assertTrue(result.restore_ready)
        self._cleanup(coordinator, request, result)

    def test_newer_minimum_restore_version_returns_not_ready_and_cleans_plaintext(self):
        backup = self._backup()
        coordinator = self._restore_coordinator(current_application_version="0.0.1")
        request = self._request(backup)
        result = coordinator.run(request)
        self.assertFalse(result.restore_ready)
        self.assertEqual(result.state, RestorePreflightState.NOT_RESTORE_READY)
        self.assertIsNone(result.preflight_reference)
        self.assertFalse(self.fixture.manager.handle(request.operation_public_id).path.exists())


class RestorePreflightContractTests(SimpleTestCase):
    def test_capabilities_keep_preflight_ready_when_guarded_mutation_is_complete(self):
        self.assertIs(RESTORE_PREFLIGHT_ENGINE_READY, True)
        self.assertIs(RESTORE_MUTATION_ENGINE_READY, True)

    def test_request_is_immutable(self):
        request = RestorePreflightRequest(
            operation_public_id=uuid.uuid4(),
            business_public_id=uuid.uuid4(),
            backup_public_id=uuid.uuid4(),
            actor_identity=ActorIdentitySnapshot.from_actor(None),
            idempotency_key="immutable",
        )
        with self.assertRaises(FrozenInstanceError):
            request.idempotency_key = "changed"

    def test_request_contract_contains_no_raw_path_or_key_input(self):
        names = {field.name for field in fields(RestorePreflightRequest)}
        self.assertEqual(
            names,
            {
                "operation_public_id",
                "business_public_id",
                "backup_public_id",
                "actor_identity",
                "idempotency_key",
                "worker_task_identifier",
            },
        )
        self.assertFalse(any("path" in name or "key" == name for name in names))

    def test_cleanup_request_is_context_bound_and_immutable(self):
        request = RestorePreflightCleanupRequest(
            operation_public_id=uuid.uuid4(),
            business_public_id=uuid.uuid4(),
            backup_public_id=uuid.uuid4(),
            preflight_reference=RestorePreflightReference(uuid.uuid4()),
        )
        with self.assertRaises(FrozenInstanceError):
            request.business_public_id = uuid.uuid4()

    def test_result_contract_exposes_no_paths_keys_or_internal_ids(self):
        names = {field.name for field in fields(RestorePreflightResult)}
        forbidden = {"path", "object_key", "encryption_key", "business_id", "backup_id"}
        self.assertTrue(forbidden.isdisjoint(names))
        self.assertIn("business_public_id", names)
        self.assertIn("backup_public_id", names)

    def test_component_plan_contract_is_immutable(self):
        item = RestoreComponentPlanItem(
            component_key="example",
            component_version="1.0",
            restore_behavior=RestoreBehavior.REPLACEABLE,
            import_order=1,
            dependencies=(),
            model_sequence=("example.Model",),
            record_count=0,
            media_reference_count=0,
        )
        with self.assertRaises(FrozenInstanceError):
            item.import_order = 2

    def test_preflight_states_are_exact_and_fail_closed(self):
        self.assertEqual(
            {state.value for state in RestorePreflightState},
            {"RESTORE_READY", "NOT_RESTORE_READY"},
        )

    def test_compatibility_states_are_exact(self):
        self.assertEqual(
            {state.value for state in PackageCompatibilityStatus},
            {"COMPATIBLE", "INCOMPATIBLE", "NOT_PROVEN"},
        )

    def test_restore_workspace_area_is_private_engine_owned_vocabulary(self):
        self.assertEqual(WorkspaceArea.RESTORE_PREFLIGHT.value, "restore-preflight")

    def test_restore_activity_event_vocabulary_is_stable(self):
        self.assertEqual(events.RESTORE_PREFLIGHT_STARTED, "restore.preflight_started")
        self.assertEqual(
            events.RESTORE_DURABLE_OBJECT_VALIDATED,
            "restore.durable_object_validated",
        )
        self.assertEqual(events.RESTORE_DECRYPTED, "restore.decrypted")
        self.assertEqual(events.RESTORE_PACKAGE_VERIFIED, "restore.package_verified")
        self.assertEqual(
            events.RESTORE_COMPATIBILITY_CHECKED,
            "restore.compatibility_checked",
        )
        self.assertEqual(events.RESTORE_PREFLIGHT_READY, "restore.preflight_ready")
        self.assertEqual(events.RESTORE_PREFLIGHT_FAILED, "restore.preflight_failed")

    def test_restore_errors_are_sanitized_and_do_not_echo_causes(self):
        error = RestoreDurableObjectError(
            r"C:\private\artifact.nxb SELECT secret",
            issue_code="safe_code",
        )
        self.assertIsInstance(error, RestoreEngineError)
        self.assertEqual(error.issue_code, "safe_code")
        generic = RestoreDurableObjectError(issue_code="safe_code")
        self.assertNotIn("artifact.nxb", str(generic))

    def test_durable_provider_exposes_restart_reattest_without_path_argument(self):
        self.assertTrue(hasattr(LocalPrivateDurableStorageProvider, "reattest_stored_object"))
        self.assertTrue(hasattr(LocalPrivateDurableStorageProvider, "open_reattested_object"))
        self.assertTrue(
            hasattr(LocalPrivateDurableStorageProvider, "release_reattested_object")
        )

    def test_encryption_provider_exposes_authoritative_restore_decryption_boundary(self):
        self.assertTrue(hasattr(EncryptedArtifactProvider, "open_restored_plaintext"))

    def test_restored_package_provider_is_explicitly_marked_for_verifier_access(self):
        self.assertEqual(
            RestoredPackageProvider.package_access_provider_schema,
            "nexa.package-access.v1",
        )

    def test_preflight_evidence_schema_is_versioned(self):
        self.assertEqual(RESTORE_PREFLIGHT_EVIDENCE_SCHEMA, "nexa.restore-preflight.v1")

    def test_coordinator_has_no_restore_mutation_entrypoint(self):
        self.assertFalse(hasattr(RestorePreflightCoordinator, "execute_restore"))
        self.assertFalse(hasattr(RestorePreflightCoordinator, "apply_restore"))

    def test_operation_kind_reuses_exclusive_restore_lock_mode(self):
        self.assertEqual(OperationKind.RESTORE, "RESTORE")

    def test_generated_package_paths_are_accepted(self):
        for name in (
            "manifest.json",
            "components/0001/records.ndjson",
            "components/0001/media-index.ndjson",
            "media/00000001.bin",
        ):
            self.assertEqual(_safe_archive_name(name), name)

    def test_extraction_traversal_is_rejected(self):
        for name in ("../manifest.json", "/manifest.json", "a/../../b", "a//b"):
            with self.subTest(name=name):
                with self.assertRaises(PackageValidationError):
                    _safe_archive_name(name)

    def test_extraction_backslash_is_rejected(self):
        with self.assertRaises(PackageValidationError):
            _safe_archive_name(r"components\0001\records.ndjson")

    def test_symlink_like_zip_entry_is_rejected(self):
        info = zipfile.ZipInfo("manifest.json")
        info.external_attr = (0o120777 & 0xFFFF) << 16
        with self.assertRaises(PackageValidationError):
            _validate_zipinfo(info)

    def test_restore_configuration_check_is_non_mutating(self):
        errors = check_restore_preflight_configuration(None)
        self.assertIsInstance(errors, list)

    def test_backup_record_persists_opaque_restore_selection_evidence(self):
        names = {field.name for field in BackupRecord._meta.fields}
        self.assertTrue(
            {
                "public_id",
                "tenant_public_id_snapshot",
                "storage_backend_identifier",
                "opaque_object_key",
                "encryption_key_identifier",
                "whole_artifact_hash",
                "backup_size_bytes",
            }.issubset(names)
        )
        self.assertNotIn("durable_filesystem_path", names)

    def test_preflight_reference_is_immutable_and_opaque(self):
        reference = RestorePreflightReference(uuid.uuid4())
        with self.assertRaises(FrozenInstanceError):
            reference.identifier = uuid.uuid4()

    def test_current_restore_behaviors_are_explicitly_enumerated(self):
        self.assertEqual(
            {behavior.value for behavior in RestoreBehavior},
            {
                "REPLACEABLE",
                "REFERENCE_ONLY",
                "DEPENDENCY_ONLY",
                "NON_RESTORABLE",
            },
        )
