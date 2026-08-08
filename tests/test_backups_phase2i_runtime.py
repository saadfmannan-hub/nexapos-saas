"""Focused operational-boundary tests for Backup Engine Phase 2I."""

from __future__ import annotations

import base64
import json
import unittest
import uuid
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.core.files.storage import FileSystemStorage
from django.test import TransactionTestCase, override_settings

from apps.accounts.models import User
from apps.backups import services
from apps.backups.engine.availability import (
    OPERATIONAL_PROVIDER_STACK_READY,
    RUNTIME_ORCHESTRATOR_READY,
    get_engine_capability,
    real_execution_available,
)
from apps.backups.engine.canonical_manifest import CanonicalManifestProvider
from apps.backups.engine.deterministic_package import DeterministicPackageProvider
from apps.backups.engine.durable_storage import LocalPrivateDurableStorageProvider
from apps.backups.engine.durable_storage_exceptions import (
    DurableObjectCreationError,
    DurableObjectValidationError,
)
from apps.backups.engine.durable_storage_policy import DurableStoragePolicy
from apps.backups.engine.encrypted_artifact import EncryptedArtifactProvider
from apps.backups.engine.encryption_policy import EncryptionPolicy
from apps.backups.engine.exceptions import BackupTenantMismatch
from apps.backups.engine.key_management import LocalConfiguredKekProvider
from apps.backups.engine.media_capture import LocalFilesystemMediaCaptureProvider
from apps.backups.engine.package_verification import IndependentPackageVerifier
from apps.backups.engine.phase2d1 import Phase2D1Coordinator
from apps.backups.engine.phase2d2 import Phase2D2Coordinator
from apps.backups.engine.retention import RetentionEngine
from apps.backups.engine.retention_policy import RetentionPolicy
from apps.backups.engine.runtime import (
    RUNTIME_PROVIDER_STACK_VERSION,
    BackupExecutionCoordinator,
    BackupExecutionRequest,
    RuntimeProviderStack,
    RuntimeRetentionOutcome,
    build_runtime_provider_stack,
)
from apps.backups.engine.runtime_exceptions import (
    RuntimeAlreadyCompleted,
    RuntimeEngineError,
    RuntimeLockUnavailable,
    RuntimeStateError,
)
from apps.backups.enums import (
    BackupScope,
    BackupStatus,
    BackupTrigger,
    IntegrityStatus,
    OperationKind,
)
from apps.backups.models import BackupActivity, BackupRecord, TenantOperationLock
from apps.backups.tasks import (
    check_backup_async_execution_configuration,
    execute_backup,
)
from apps.backups.versioning import current_version_metadata
from apps.subscriptions.models import Plan, Subscription
from apps.tenants.services import provision_business

from .test_backups_phase2b_snapshot import _StaticFilesystemInspector
from .test_backups_phase2d1_media_manifest import _media_policy
from .test_backups_phase2d2_package import DeterministicPackageProviderTests


class _LocalInspector:
    @staticmethod
    def assess(_path):
        return SimpleNamespace(confirmed_local=True)


class BackupRuntimeTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.fixture = DeterministicPackageProviderTests(methodName="runTest")
        self.fixture.setUp()
        self.owner = User.objects.create_user(
            email=f"runtime-{uuid.uuid4().hex}@example.test",
            password="StrongPass123!",
            full_name="Runtime Owner",
        )
        self.business = provision_business(
            owner=self.owner,
            name="Runtime Tenant",
        )
        plan_id = Subscription.objects.get(business=self.business).plan_id
        Plan.objects.filter(pk=plan_id).update(feature_sales=True)
        self.fixture.context = replace(
            self.fixture.context,
            business_id=self.business.pk,
            business_public_id=self.business.public_id,
            requested_scope=BackupScope.POS,
            resolved_products=self.fixture.context.resolved_products,
            trigger_type=BackupTrigger.MANUAL,
        )
        self.fixture._install_export_schema()
        self.fixture._seed_tenant()
        self.fixture._insert("tenants.BusinessSettings")
        self.media_root = self.fixture.root / "runtime-media"
        self.media_root.mkdir()
        self.stack = self._provider_stack()

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

    def _provider_stack(self):
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
        manifest = CanonicalManifestProvider(
            workspace_manager=self.fixture.manager,
        )
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
            key_identifier="runtime-test-kek",
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
                root=self.fixture.root / "runtime-durable",
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

    def _backup(self, *, trigger=BackupTrigger.MANUAL, system_actor=False):
        scheduled = trigger == BackupTrigger.SCHEDULED
        return services.create_backup_request(
            business=self.business,
            scope=BackupScope.ALL_ENABLED if scheduled else BackupScope.POS,
            actor=None if system_actor else self.owner,
            trigger=trigger,
            scheduled_local_date=(
                datetime(2026, 8, 8).date() if scheduled else None
            ),
            idempotency_key=f"runtime-{uuid.uuid4().hex}",
            system_actor=system_actor,
        )

    def _execute(self, backup, **changes):
        request = BackupExecutionRequest.from_record(backup)
        coordinator = BackupExecutionCoordinator(
            provider_stack=self.stack,
            lock_lease_seconds=3600,
            **changes,
        )
        with override_settings(MEDIA_ROOT=self.media_root):
            return coordinator.execute(request)

    def _durable_path(self, backup):
        return (
            self.stack.durable_storage_provider.root
            / "objects"
            / backup.tenant_public_id_snapshot.hex
            / backup.public_id.hex
            / uuid.UUID(backup.opaque_object_key).hex
            / "artifact.nxb"
        )

    def test_real_end_to_end_happy_path_reaches_durable_success_and_cleans_staging(self):
        backup = self._backup()
        result = self._execute(backup)
        backup.refresh_from_db()

        self.assertEqual(result.final_status, BackupStatus.SUCCEEDED)
        self.assertEqual(result.provider_stack_version, RUNTIME_PROVIDER_STACK_VERSION)
        self.assertEqual(backup.status, BackupStatus.SUCCEEDED)
        self.assertEqual(backup.integrity_status, IntegrityStatus.VERIFIED)
        self.assertTrue(backup.opaque_object_key)
        self.assertEqual(backup.whole_artifact_hash, result.stored_object.sha256)
        self.assertEqual(backup.backup_size_bytes, result.stored_object.byte_count)
        self.assertTrue(self._durable_path(backup).exists())
        workspace = self.fixture.manager.handle(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"nexa-backup:{backup.public_id}:{backup.idempotency_key}",
            )
        )
        self.assertFalse(workspace.path.exists())
        self.assertEqual(
            result.retention_outcome,
            RuntimeRetentionOutcome.NO_ACTION_REQUIRED,
        )
        event_types = list(
            BackupActivity.objects.filter(backup=backup).values_list(
                "event_type",
                flat=True,
            )
        )
        for event_type in (
            "backup.execution_started",
            "backup.snapshot_completed",
            "backup.export_completed",
            "backup.manifest_completed",
            "backup.package_completed",
            "backup.verified",
            "backup.encrypted",
            "backup.durable_stored",
            "backup.completed",
            "retention.completed",
        ):
            self.assertIn(event_type, event_types)
        self.assertFalse(
            TenantOperationLock.objects.filter(
                business=self.business,
                active=True,
            ).exists()
        )

    def test_stage_failures_are_sanitized_failed_and_release_the_lock(self):
        cases = (
            ("after_snapshot", "snapshot_failure"),
            ("after_export", "logical_export_failure"),
            ("after_manifest", "media_manifest_failure"),
            ("after_package", "package_failure"),
            ("after_verification", "package_verification_failure"),
            ("after_encryption", "encryption_failure"),
        )
        for failure_stage, expected_code in cases:
            with self.subTest(stage=failure_stage):
                backup = self._backup()

                def fail(stage, _backup_id, selected=failure_stage):
                    if stage == selected:
                        raise RuntimeError(
                            r"C:\private\tenant.sqlite SELECT * FROM secret_table"
                        )

                with self.assertRaises(RuntimeEngineError) as raised:
                    self._execute(backup, failure_hook=fail)
                backup.refresh_from_db()
                self.assertEqual(backup.status, BackupStatus.FAILED)
                self.assertEqual(backup.failure_code, expected_code)
                self.assertFalse(backup.opaque_object_key)
                self.assertFalse(
                    TenantOperationLock.objects.filter(
                        business=self.business,
                        active=True,
                    ).exists()
                )
                rendered = " ".join(
                    (
                        str(raised.exception),
                        json.dumps(
                            list(
                                BackupActivity.objects.filter(backup=backup).values(
                                    "sanitized_message",
                                    "structured_metadata",
                                )
                            ),
                            sort_keys=True,
                        ),
                        backup.sanitized_failure_summary,
                    )
                )
                self.assertNotIn(str(self.fixture.root), rendered)
                self.assertNotIn("SELECT *", rendered)
                self.assertNotIn("secret_table", rendered)

    def test_durable_revalidation_failure_preserves_object_but_never_succeeds(self):
        storage_failure = self._backup()
        provider = self.stack.durable_storage_provider

        def fail_publication(stage):
            if stage == "before_durable_publication":
                raise DurableObjectCreationError()

        provider.failure_hook = fail_publication
        try:
            with self.assertRaises(RuntimeEngineError):
                self._execute(storage_failure)
        finally:
            provider.failure_hook = None
        storage_failure.refresh_from_db()
        self.assertEqual(storage_failure.status, BackupStatus.FAILED)
        self.assertFalse(storage_failure.opaque_object_key)

        backup = self._backup()
        with mock.patch.object(
            provider,
            "validate_stored_object",
            side_effect=DurableObjectValidationError(),
        ):
            with self.assertRaises(RuntimeEngineError):
                self._execute(backup)
        backup.refresh_from_db()
        self.assertEqual(backup.status, BackupStatus.FAILED)
        self.assertNotEqual(backup.integrity_status, IntegrityStatus.VERIFIED)
        self.assertTrue(backup.opaque_object_key)
        self.assertTrue(self._durable_path(backup).exists())
        failed = BackupActivity.objects.filter(
            backup=backup,
            event_type="backup.failed",
        ).get()
        self.assertTrue(failed.structured_metadata["durable_object_preserved"])
        self.assertFalse(
            TenantOperationLock.objects.filter(business=self.business, active=True).exists()
        )

    def test_explicit_verification_failure_never_reaches_encryption_or_retention(self):
        backup = self._backup()
        verifier = self.stack.verification_provider
        original_verify = verifier.verify

        def unready(request):
            return replace(
                original_verify(request),
                verified=False,
                restore_ready=False,
            )

        with mock.patch.object(verifier, "verify", side_effect=unready):
            with self.assertRaises(RuntimeEngineError):
                self._execute(backup)
        backup.refresh_from_db()
        self.assertEqual(backup.status, BackupStatus.FAILED)
        self.assertEqual(backup.failure_code, "package_verification_failure")
        self.assertFalse(backup.opaque_object_key)
        self.assertFalse(
            BackupActivity.objects.filter(
                backup=backup,
                event_type__startswith="retention.",
            ).exists()
        )

    def test_crash_like_failure_after_publication_records_recovery_identity(self):
        backup = self._backup()

        def fail(stage, _backup_id):
            if stage == "after_durable_publication":
                raise RuntimeError("private provider detail")

        with self.assertRaises(RuntimeEngineError):
            self._execute(backup, failure_hook=fail)
        backup.refresh_from_db()
        self.assertEqual(backup.status, BackupStatus.FAILED)
        self.assertTrue(backup.opaque_object_key)
        self.assertTrue(self._durable_path(backup).exists())

    def test_retention_failure_is_an_explicit_warning_not_false_backup_failure(self):
        backup = self._backup()
        self.stack.retention_engine.clock = lambda: datetime(2026, 8, 8)
        result = self._execute(backup)
        backup.refresh_from_db()
        self.assertEqual(backup.status, BackupStatus.SUCCEEDED)
        self.assertEqual(result.retention_outcome, RuntimeRetentionOutcome.FAILED_SAFE)
        self.assertEqual(result.retention_warning_code, "retention_failed_safe")
        self.assertTrue(self._durable_path(backup).exists())
        self.assertTrue(
            BackupActivity.objects.filter(
                backup=backup,
                event_type="retention.failed",
            ).exists()
        )

    def test_unknown_historical_success_is_deferred_not_fabricated_or_deleted(self):
        versions = current_version_metadata()
        prior = BackupRecord.objects.create(
            business=self.business,
            tenant_public_id_snapshot=self.business.public_id,
            scope=BackupScope.ALL_ENABLED,
            included_products=["POS"],
            included_components=[],
            trigger=BackupTrigger.SCHEDULED,
            scheduled_local_date=datetime(2026, 8, 7).date(),
            status=BackupStatus.SUCCEEDED,
            integrity_status=IntegrityStatus.VERIFIED,
            retention_eligible=True,
            format_version=versions["format_version"],
            application_version=versions["application_version"],
            schema_fingerprint=versions["schema_fingerprint"],
            minimum_restore_version=versions["minimum_restore_version"],
            storage_backend_identifier="historical-provider",
            opaque_object_key=str(uuid.uuid4()),
            whole_artifact_hash="a" * 64,
            backup_size_bytes=100,
            idempotency_key=f"historical-{uuid.uuid4().hex}",
        )
        backup = self._backup()
        result = self._execute(backup)
        prior.refresh_from_db()
        self.assertEqual(
            result.retention_outcome,
            RuntimeRetentionOutcome.HISTORICAL_EVIDENCE_DEFERRED,
        )
        self.assertEqual(prior.status, BackupStatus.SUCCEEDED)
        self.assertTrue(prior.opaque_object_key)
        self.assertTrue(
            BackupActivity.objects.filter(
                backup=backup,
                event_type="retention.historical_evidence_deferred",
            ).exists()
        )

    def test_wrong_binding_system_actor_idempotency_and_interrupts(self):
        wrong = self._backup()
        request = replace(
            BackupExecutionRequest.from_record(wrong),
            business_public_id=uuid.uuid4(),
        )
        coordinator = BackupExecutionCoordinator(
            provider_stack=self.stack,
            lock_lease_seconds=3600,
        )
        with self.assertRaises(BackupTenantMismatch):
            coordinator.execute(request)

        scheduled = self._backup(
            trigger=BackupTrigger.SCHEDULED,
            system_actor=True,
        )
        scheduled_request = BackupExecutionRequest.from_record(scheduled)
        self.assertEqual(scheduled_request.actor_identity.actor_type, "SYSTEM")
        scheduled_result = self._execute(scheduled)
        self.assertEqual(scheduled_result.final_status, BackupStatus.SUCCEEDED)

        key = f"exact-{uuid.uuid4().hex}"
        first = services.create_backup_request(
            business=self.business,
            scope=BackupScope.POS,
            actor=self.owner,
            idempotency_key=key,
        )
        repeated = services.create_backup_request(
            business=self.business,
            scope=BackupScope.POS,
            actor=self.owner,
            idempotency_key=key,
        )
        self.assertEqual(first.pk, repeated.pk)

        for abort_type in (KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(abort_type=abort_type.__name__):
                aborting = self._backup()

                def abort(stage, _backup_id, selected=abort_type):
                    if stage == "after_execution_started":
                        raise selected()

                with self.assertRaises(abort_type):
                    self._execute(aborting, failure_hook=abort)
                self.assertFalse(
                    TenantOperationLock.objects.filter(
                        business=self.business,
                        active=True,
                    ).exists()
                )

    def test_enabling_without_async_and_operational_safety_fails_checks(self):
        with override_settings(
            BACKUP_EXECUTION_ENGINE_ENABLED=True,
            BACKUP_ENGINE_ENABLED=True,
            CELERY_BROKER_URL="",
            CELERY_TASK_ALWAYS_EAGER=True,
        ):
            errors = check_backup_async_execution_configuration(None)
            self.assertFalse(real_execution_available())
        self.assertEqual(
            {error.id for error in errors},
            {"backups.E010", "backups.E011", "backups.E012"},
        )
        self.assertFalse(hasattr(execute_backup, "delay"))

    def test_unknown_workspace_content_is_preserved_with_cleanup_warning(self):
        backup = self._backup()
        reference = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"nexa-backup:{backup.public_id}:{backup.idempotency_key}",
        )
        workspace = self.fixture.manager.handle(reference)

        def interfere(stage, _backup_id):
            if stage == "after_execution_started":
                (workspace.path / "unknown-recovery-evidence.bin").write_bytes(
                    b"unowned"
                )
                raise RuntimeError("stop safely")

        with self.assertRaises(RuntimeEngineError):
            self._execute(backup, failure_hook=interfere)
        self.assertTrue((workspace.path / "unknown-recovery-evidence.bin").exists())
        self.assertTrue(
            BackupActivity.objects.filter(
                backup=backup,
                event_type="backup.workspace_cleanup_deferred",
            ).exists()
        )

    def test_capability_and_provider_factory_remain_fail_closed(self):
        self.assertIs(RUNTIME_ORCHESTRATOR_READY, True)
        self.assertIs(OPERATIONAL_PROVIDER_STACK_READY, False)
        self.assertIs(real_execution_available(), False)
        capability = get_engine_capability()
        self.assertIs(capability.runtime_orchestrator_ready, True)
        self.assertIs(capability.provider_stack_ready, False)
        with self.assertRaises(FrozenInstanceError):
            self.stack.workspace_manager = None

        factory_root = self.fixture.root / "factory-staging"
        factory_root.mkdir()
        factory_durable = self.fixture.root / "factory-durable"
        factory_durable.mkdir()
        with override_settings(
            BACKUP_STAGING_ROOT=factory_root,
            BACKUP_DURABLE_STORAGE_ROOT=factory_durable,
            BACKUP_DURABLE_STORAGE_REQUIRE_LOCAL=False,
            BACKUP_LOCAL_KEK_B64=base64.b64encode(b"f" * 32).decode("ascii"),
            BACKUP_LOCAL_KEK_ID="factory-test",
            BACKUP_LOCAL_KEK_VERSION="v1",
        ):
            built = build_runtime_provider_stack()
        self.assertIs(type(built), RuntimeProviderStack)
        self.assertIs(
            built.retention_engine.durable_provider,
            built.durable_storage_provider,
        )

    def test_invalid_state_repeat_and_lock_contention_fail_safely(self):
        backup = self._backup()
        backup = services.transition_backup(backup, BackupStatus.PREPARING)
        with self.assertRaises(RuntimeStateError):
            self._execute(backup)

        completed = self._backup()
        self._execute(completed)
        completed.refresh_from_db()
        with self.assertRaises(RuntimeAlreadyCompleted):
            self._execute(completed)

        blocked = self._backup()
        held = services.acquire_tenant_operation_lock(
            business=self.business,
            operation_kind=OperationKind.RESTORE,
            operation_public_id=uuid.uuid4(),
            lease_seconds=3600,
        )
        try:
            with self.assertRaises(RuntimeLockUnavailable):
                self._execute(blocked)
            blocked.refresh_from_db()
            self.assertEqual(blocked.status, BackupStatus.QUEUED)
        finally:
            services.release_tenant_operation_lock(
                held,
                lock_token=held.lock_token,
            )

    def test_no_runtime_surface_is_connected_to_http_or_scheduler(self):
        repository_root = Path(__file__).resolve().parents[1]
        for relative in (
            "apps/backups/views.py",
            "apps/backups/platform_views.py",
            "apps/backups/urls.py",
            "apps/backups/admin.py",
            "apps/backups/forms.py",
            "apps/backups/signals.py",
            "config/urls.py",
            "config/celery.py",
        ):
            path = repository_root / relative
            if path.exists():
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("BackupExecutionCoordinator", source)
                self.assertNotIn("request_backup_execution", source)


def load_tests(loader, standard_tests, pattern):
    """Run Phase 2I cases; predecessor regressions use dedicated commands."""

    del loader, standard_tests, pattern
    names = sorted(
        name
        for name, value in BackupRuntimeTests.__dict__.items()
        if name.startswith("test_") and callable(value)
    )
    return unittest.TestSuite(BackupRuntimeTests(name) for name in names)
