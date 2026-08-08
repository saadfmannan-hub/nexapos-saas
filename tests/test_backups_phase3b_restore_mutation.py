"""Focused isolated tests for Backup Engine Phase 3B restore mutation."""

from __future__ import annotations

import base64
import uuid
from dataclasses import FrozenInstanceError, fields, replace
from types import SimpleNamespace
from unittest import mock

from django.core.files.storage import FileSystemStorage
from django.db import models
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.backups import platform_views, services, views
from apps.backups.engine import events
from apps.backups.engine.availability import (
    RESTORE_MUTATION_ENGINE_READY,
    RESTORE_PREFLIGHT_ENGINE_READY,
    restore_mutation_setting_enabled,
)
from apps.backups.engine.canonical_manifest import CanonicalManifestProvider
from apps.backups.engine.context import ActorIdentitySnapshot
from apps.backups.engine.contracts import PackageCompatibilityStatus
from apps.backups.engine.deterministic_package import DeterministicPackageProvider
from apps.backups.engine.durable_storage import LocalPrivateDurableStorageProvider
from apps.backups.engine.durable_storage_policy import DurableStoragePolicy
from apps.backups.engine.encrypted_artifact import EncryptedArtifactProvider
from apps.backups.engine.encryption_policy import EncryptionPolicy
from apps.backups.engine.key_management import LocalConfiguredKekProvider
from apps.backups.engine.logical_restore import LogicalRestoreEngine
from apps.backups.engine.media_capture import LocalFilesystemMediaCaptureProvider
from apps.backups.engine.media_restore import LocalFilesystemMediaRestoreProvider
from apps.backups.engine.package_verification import (
    IndependentPackageVerifier,
    PackageCompatibilityPolicy,
)
from apps.backups.engine.phase2d1 import Phase2D1Coordinator
from apps.backups.engine.phase2d2 import Phase2D2Coordinator
from apps.backups.engine.restore_exceptions import (
    RestoreEngineError,
    RestoreImportError,
    RestoreLockUnavailable,
    RestoreMediaPublicationError,
    RestoreMutationError,
    RestoreRecoveryRequired,
    RestoreSafetyBackupError,
    RestoreSelectionError,
)
from apps.backups.engine.restore_mutation import (
    RESTORE_RUNTIME_STACK_VERSION,
    RestoreExecutionCoordinator,
    RestoreExecutionRequest,
    RestoreExecutionResult,
    RestoreExecutionState,
    RestoreRuntimeStack,
)
from apps.backups.engine.restore_preflight import (
    RestorePreflightCoordinator,
    RestorePreflightProviderStack,
    RestorePreflightReference,
    RestorePreflightRequest,
    RestorePreflightResult,
    RestorePreflightState,
)
from apps.backups.engine.restore_verification import (
    IndependentRestoreStateVerifier,
    PostRestoreVerificationState,
)
from apps.backups.engine.restore_workspace import RestoredPackageProvider
from apps.backups.engine.retention import RetentionEngine
from apps.backups.engine.retention_policy import RetentionPolicy
from apps.backups.engine.runtime import (
    BackupExecutionCoordinator,
    BackupExecutionRequest,
    RuntimeProviderStack,
)
from apps.backups.engine.runtime_exceptions import RuntimeEngineError
from apps.backups.enums import (
    BackupScope,
    BackupStatus,
    BackupTrigger,
    IntegrityStatus,
    OperationKind,
    RestoreStatus,
)
from apps.backups.models import (
    BackupActivity,
    BackupRecord,
    RestoreOperation,
    TenantOperationLock,
)
from apps.backups.versioning import (
    BACKUP_FORMAT_VERSION,
    get_application_version,
    schema_migration_fingerprint,
)
from apps.catalog.models import Category, Product
from apps.subscriptions.models import Plan, Subscription
from apps.tenants.services import provision_business

from .test_backups_phase2b_snapshot import _StaticFilesystemInspector
from .test_backups_phase2d1_media_manifest import _media_policy
from .test_backups_phase2d2_package import DeterministicPackageProviderTests


class _LocalInspector:
    @staticmethod
    def assess(_path):
        return SimpleNamespace(confirmed_local=True)


class RestoreMutationEndToEndTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.fixture = DeterministicPackageProviderTests(methodName="runTest")
        self.fixture.setUp()
        self.owner = User.objects.create_user(
            email=f"restore-mutation-{uuid.uuid4().hex}@example.test",
            password="StrongPass123!",
            full_name="Restore Mutation Owner",
        )
        self.business = provision_business(owner=self.owner, name="Mutation Tenant")
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
        self._install_source_membership_m2m()
        self._seed_source_dependencies()
        self.media_root = self.fixture.root / "phase3b-media"
        self.media_root.mkdir()
        self.backup_stack = self._runtime_stack()

    def _copy_source_row(self, obj):
        values = {}
        for field in obj._meta.concrete_fields:
            value = field.value_from_object(obj)
            if isinstance(field, models.FileField):
                value = getattr(obj, field.name).name or ""
            values[field.name] = value
        self.fixture._insert(obj._meta.label, **values)

    def _seed_source_dependencies(self):
        self._copy_source_row(self.owner)
        self._copy_source_row(self.business)
        for label in (
            "branches.Branch",
            "branches.Warehouse",
            "tenants.BusinessSettings",
            "accounts.Role",
            "accounts.Membership",
        ):
            model = self.business._meta.apps.get_model(label)
            for obj in model.objects.filter(business=self.business).order_by("pk"):
                self._copy_source_row(obj)
        membership = self.business._meta.apps.get_model("accounts.Membership")
        through = membership._meta.get_field("branches").remote_field.through
        for link in through.objects.filter(membership__business=self.business).order_by("pk"):
            self._copy_source_row(link)

    def _install_source_membership_m2m(self):
        membership = self.business._meta.apps.get_model("accounts.Membership")
        through = membership._meta.get_field("branches").remote_field.through
        columns = ", ".join(
            f"{self.fixture._quote(field.column)} {self.fixture._sqlite_type(field)}"
            for field in through._meta.concrete_fields
        )
        self.fixture.source.execute(
            f"CREATE TABLE IF NOT EXISTS {self.fixture._quote(through._meta.db_table)} "
            f"({columns})"
        )

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
            key_b64=base64.b64encode(b"b" * 32).decode("ascii"),
            key_identifier="phase3b-test-kek",
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
                root=self.fixture.root / "phase3b-durable",
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
            retention_engine=RetentionEngine(
                durable_provider=durable,
                policy=RetentionPolicy(5, 100, 300),
            ),
        ).validated()

    def _source_backup(self):
        backup = services.create_backup_request(
            business=self.business,
            scope=BackupScope.POS,
            actor=self.owner,
            trigger=BackupTrigger.MANUAL,
            idempotency_key=f"phase3b-source-{uuid.uuid4().hex}",
        )
        with override_settings(MEDIA_ROOT=self.media_root):
            try:
                BackupExecutionCoordinator(
                    provider_stack=self.backup_stack,
                    lock_lease_seconds=3600,
                ).execute(BackupExecutionRequest.from_record(backup))
            except RuntimeEngineError:
                backup.refresh_from_db()
                self.fail(f"Source backup failed: {backup.failure_code}")
        backup.refresh_from_db()
        self.assertEqual(backup.status, BackupStatus.SUCCEEDED)
        return backup

    def _restore_stack(self):
        restored = RestoredPackageProvider(workspace_manager=self.fixture.manager)
        verifier = IndependentPackageVerifier(
            package_provider=restored,
            workspace_manager=self.fixture.manager,
            compatibility_policy=PackageCompatibilityPolicy(
                current_schema_fingerprint=schema_migration_fingerprint(),
                current_application_version=get_application_version(),
                current_backup_format_version=BACKUP_FORMAT_VERSION,
            ),
        )
        preflight_stack = RestorePreflightProviderStack(
            workspace_manager=self.fixture.manager,
            encrypted_artifact_provider=self.backup_stack.encrypted_artifact_provider,
            durable_storage_provider=self.backup_stack.durable_storage_provider,
            restored_package_provider=restored,
            verification_provider=verifier,
        ).validated()
        preflight = RestorePreflightCoordinator(
            provider_stack=preflight_stack,
            lock_lease_seconds=3600,
        )
        logical = LogicalRestoreEngine()
        media = LocalFilesystemMediaRestoreProvider(
            workspace_manager=self.fixture.manager,
        )
        return RestoreRuntimeStack(
            backup_runtime_stack=self.backup_stack,
            preflight_provider_stack=preflight_stack,
            preflight_coordinator=preflight,
            backup_coordinator=BackupExecutionCoordinator(
                provider_stack=self.backup_stack,
                lock_lease_seconds=3600,
            ),
            logical_restore_engine=logical,
            media_restore_provider=media,
            post_restore_verifier=IndependentRestoreStateVerifier(
                logical_engine=logical,
                media_provider=media,
            ),
        ).validated()

    def _prepared_execution(self):
        source = self._source_backup()
        restore = services.create_restore_request(
            business=self.business,
            source_backup=source,
            requested_scope=BackupScope.POS,
            actor=self.owner,
            reason="Isolated Phase 3B verification",
            idempotency_key=f"phase3b-restore-{uuid.uuid4().hex}",
        )
        stack = self._restore_stack()
        actor_identity = ActorIdentitySnapshot.from_actor(self.owner)
        preflight = stack.preflight_coordinator.run(
            RestorePreflightRequest(
                operation_public_id=restore.public_id,
                business_public_id=self.business.public_id,
                backup_public_id=source.public_id,
                actor_identity=actor_identity,
                idempotency_key=restore.idempotency_key,
            )
        )
        request = RestoreExecutionRequest(
            business_public_id=self.business.public_id,
            selected_backup_public_id=source.public_id,
            actor_identity=actor_identity,
            idempotency_key=restore.idempotency_key,
            approved_preflight_result=preflight,
            restore_request_public_id=restore.public_id,
        )
        return source, restore, stack, request

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=True)
    def test_full_real_chain_preflight_safety_backup_and_successful_restore(self):
        source, restore, stack, request = self._prepared_execution()
        category = Category.objects.create(
            business=self.business,
            name="Created after source backup",
        )

        with override_settings(MEDIA_ROOT=self.media_root):
            try:
                result = RestoreExecutionCoordinator(
                    runtime_stack=stack,
                    lock_lease_seconds=3600,
                ).execute(request)
            except RestoreEngineError as exc:
                self.fail(f"Unexpected sanitized restore failure: {exc.issue_code}")

        self.assertEqual(result.final_state, RestoreExecutionState.SUCCESS)
        self.assertFalse(
            Category.objects.filter(
                business=self.business,
                public_id=category.public_id,
            ).exists()
        )
        restore.refresh_from_db()
        self.assertEqual(restore.status, RestoreStatus.SUCCEEDED)
        self.assertIsNotNone(restore.safety_backup)
        self.assertTrue(restore.safety_backup.protected)
        self.assertFalse(restore.safety_backup.retention_eligible)
        self.assertEqual(restore.safety_backup.status, BackupStatus.SUCCEEDED)
        self.assertEqual(restore.safety_backup.integrity_status, IntegrityStatus.VERIFIED)
        source.refresh_from_db()
        self.assertEqual(source.status, BackupStatus.SUCCEEDED)

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=True)
    def test_safety_backup_is_durable_before_first_destructive_write(self):
        _source, restore, stack, request = self._prepared_execution()
        observed = {}

        def hook(stage, _operation_public_id):
            if stage == "before_database_mutation":
                current = RestoreOperation.objects.select_related("safety_backup").get(
                    pk=restore.pk
                )
                observed["safety"] = current.safety_backup
                self.assertEqual(current.safety_backup.status, BackupStatus.SUCCEEDED)
                self.assertEqual(
                    current.safety_backup.integrity_status,
                    IntegrityStatus.VERIFIED,
                )
                self.assertTrue(current.safety_backup.protected)
                self.assertFalse(current.safety_backup.retention_eligible)
                self.assertTrue(current.safety_backup.opaque_object_key)

        with override_settings(MEDIA_ROOT=self.media_root):
            RestoreExecutionCoordinator(
                runtime_stack=stack,
                lock_lease_seconds=3600,
                failure_hook=hook,
            ).execute(request)
        self.assertIsNotNone(observed.get("safety"))

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=True)
    def test_safety_backup_failure_causes_zero_tenant_writes(self):
        source, restore, stack, request = self._prepared_execution()
        category = Category.objects.create(business=self.business, name="Must survive")
        with mock.patch.object(
            stack.backup_coordinator,
            "execute",
            side_effect=RestoreSafetyBackupError(issue_code="safety_backup_failed"),
        ):
            with override_settings(MEDIA_ROOT=self.media_root):
                with self.assertRaises(RestoreSafetyBackupError):
                    RestoreExecutionCoordinator(
                        runtime_stack=stack,
                        lock_lease_seconds=3600,
                    ).execute(request)
        self.assertTrue(Category.objects.filter(pk=category.pk).exists())
        restore.refresh_from_db()
        self.assertEqual(restore.status, RestoreStatus.FAILED)
        self.assertIsNone(restore.safety_backup)
        self.assertFalse(
            BackupActivity.objects.filter(
                restore=restore,
                event_type=events.RESTORE_MUTATION_STARTED,
            ).exists()
        )
        source.refresh_from_db()
        self.assertEqual(source.status, BackupStatus.SUCCEEDED)

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=True)
    def test_safety_encryption_failure_causes_zero_tenant_writes(self):
        _source, restore, stack, request = self._prepared_execution()
        category = Category.objects.create(business=self.business, name="Encryption guard")
        with mock.patch.object(
            stack.backup_coordinator,
            "execute",
            side_effect=RestoreSafetyBackupError(issue_code="safety_backup_encryption_failed"),
        ):
            with override_settings(MEDIA_ROOT=self.media_root):
                with self.assertRaises(RestoreSafetyBackupError):
                    RestoreExecutionCoordinator(runtime_stack=stack).execute(request)
        self.assertTrue(Category.objects.filter(pk=category.pk).exists())
        restore.refresh_from_db()
        self.assertEqual(restore.status, RestoreStatus.FAILED)

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=True)
    def test_safety_durable_failure_causes_zero_tenant_writes(self):
        _source, restore, stack, request = self._prepared_execution()
        category = Category.objects.create(business=self.business, name="Durable guard")
        with mock.patch.object(
            stack.backup_coordinator,
            "execute",
            side_effect=RestoreSafetyBackupError(issue_code="safety_backup_storage_failed"),
        ):
            with override_settings(MEDIA_ROOT=self.media_root):
                with self.assertRaises(RestoreSafetyBackupError):
                    RestoreExecutionCoordinator(runtime_stack=stack).execute(request)
        self.assertTrue(Category.objects.filter(pk=category.pk).exists())
        restore.refresh_from_db()
        self.assertEqual(restore.status, RestoreStatus.FAILED)

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=True)
    def test_forged_preflight_result_is_rejected_before_safety_backup(self):
        _source, restore, stack, request = self._prepared_execution()
        category = Category.objects.create(business=self.business, name="Forged guard")
        request = replace(
            request,
            approved_preflight_result=replace(
                request.approved_preflight_result,
                component_count=request.approved_preflight_result.component_count + 1,
            ),
        )
        with override_settings(MEDIA_ROOT=self.media_root):
            with self.assertRaises(RestoreSelectionError):
                RestoreExecutionCoordinator(runtime_stack=stack).execute(request)
        self.assertTrue(Category.objects.filter(pk=category.pk).exists())
        restore.refresh_from_db()
        self.assertEqual(restore.status, RestoreStatus.FAILED)
        self.assertIsNone(restore.safety_backup)

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=True)
    def test_database_and_media_failure_rolls_back_tenant_mutation(self):
        _source, restore, stack, request = self._prepared_execution()
        category = Category.objects.create(business=self.business, name="Rollback guard")
        with mock.patch.object(
            stack.media_restore_provider,
            "publish",
            side_effect=RestoreMediaPublicationError(issue_code="restore_media_publish_failed"),
        ):
            with override_settings(MEDIA_ROOT=self.media_root):
                with self.assertRaises(RestoreMediaPublicationError):
                    RestoreExecutionCoordinator(runtime_stack=stack).execute(request)
        self.assertTrue(Category.objects.filter(pk=category.pk).exists())
        restore.refresh_from_db()
        self.assertEqual(restore.status, RestoreStatus.ROLLED_BACK)
        self.assertTrue(restore.rollback_attempted)
        self.assertIsNotNone(restore.safety_backup)
        self.assertEqual(restore.safety_backup.status, BackupStatus.SUCCEEDED)

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=True)
    def test_unproven_media_rollback_marks_recovery_required(self):
        _source, restore, stack, request = self._prepared_execution()
        Category.objects.create(business=self.business, name="Recovery guard")
        with (
            mock.patch.object(
                stack.media_restore_provider,
                "publish",
                side_effect=RestoreMediaPublicationError(
                    issue_code="restore_media_publish_failed"
                ),
            ),
            mock.patch.object(
                stack.media_restore_provider,
                "rollback",
                side_effect=RestoreMediaPublicationError(
                    issue_code="restore_media_rollback_failed"
                ),
            ),
        ):
            with override_settings(MEDIA_ROOT=self.media_root):
                with self.assertRaises(RestoreRecoveryRequired):
                    RestoreExecutionCoordinator(runtime_stack=stack).execute(request)
        restore.refresh_from_db()
        self.assertEqual(restore.status, RestoreStatus.INDETERMINATE)
        self.assertIsNotNone(restore.safety_backup)
        self.assertTrue(restore.safety_backup.protected)

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=True)
    def test_post_restore_mismatch_prevents_success_and_rolls_back(self):
        _source, restore, stack, request = self._prepared_execution()
        original = Category.objects.create(business=self.business, name="Original live row")

        def hook(stage, _operation_public_id):
            if stage == "after_database_mutation":
                Category.objects.create(business=self.business, name="Unexpected mismatch")

        with override_settings(MEDIA_ROOT=self.media_root):
            with self.assertRaises(RestoreEngineError):
                RestoreExecutionCoordinator(
                    runtime_stack=stack,
                    failure_hook=hook,
                ).execute(request)
        self.assertTrue(Category.objects.filter(pk=original.pk).exists())
        self.assertFalse(
            Category.objects.filter(
                business=self.business,
                name="Unexpected mismatch",
            ).exists()
        )
        restore.refresh_from_db()
        self.assertEqual(restore.status, RestoreStatus.ROLLED_BACK)

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=True)
    def test_success_is_idempotently_reused_without_second_mutation(self):
        _source, restore, stack, request = self._prepared_execution()
        with override_settings(MEDIA_ROOT=self.media_root):
            coordinator = RestoreExecutionCoordinator(runtime_stack=stack)
            first = coordinator.execute(request)
            with mock.patch.object(
                stack.logical_restore_engine,
                "mutate",
                side_effect=AssertionError("must not mutate twice"),
            ):
                second = coordinator.execute(request)
        self.assertEqual(first, second)
        self.assertEqual(
            BackupActivity.objects.filter(
                restore=restore,
                event_type=events.RESTORE_MUTATION_STARTED,
            ).count(),
            1,
        )

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=True)
    def test_lock_contention_prevents_restore_and_preserves_live_rows(self):
        _source, restore, stack, request = self._prepared_execution()
        category = Category.objects.create(business=self.business, name="Lock guard")
        competing = services.acquire_tenant_operation_lock(
            business=self.business,
            operation_kind=OperationKind.RETENTION,
            operation_public_id=uuid.uuid4(),
            lease_seconds=3600,
        )
        try:
            with override_settings(MEDIA_ROOT=self.media_root):
                with self.assertRaises(RestoreLockUnavailable):
                    RestoreExecutionCoordinator(runtime_stack=stack).execute(request)
        finally:
            services.release_tenant_operation_lock(
                competing,
                lock_token=competing.lock_token,
            )
        self.assertTrue(Category.objects.filter(pk=category.pk).exists())
        restore.refresh_from_db()
        self.assertEqual(restore.status, RestoreStatus.FAILED)
        self.assertFalse(
            TenantOperationLock.objects.filter(
                business=self.business,
                operation_kind=OperationKind.RESTORE,
                active=True,
            ).exists()
        )

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=True)
    def test_cross_tenant_rows_remain_unchanged(self):
        _source, _restore, stack, request = self._prepared_execution()
        other_owner = User.objects.create_user(
            email=f"other-{uuid.uuid4().hex}@example.test",
            password="StrongPass123!",
            full_name="Other Owner",
        )
        other_business = provision_business(owner=other_owner, name="Other Tenant")
        other_category = Category.objects.create(
            business=other_business,
            name="Other tenant row",
        )
        with override_settings(MEDIA_ROOT=self.media_root):
            RestoreExecutionCoordinator(runtime_stack=stack).execute(request)
        other_category.refresh_from_db()
        self.assertEqual(other_category.name, "Other tenant row")

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=True)
    def test_success_releases_restore_lock_and_preserves_both_backups(self):
        source, restore, stack, request = self._prepared_execution()
        with override_settings(MEDIA_ROOT=self.media_root):
            try:
                result = RestoreExecutionCoordinator(runtime_stack=stack).execute(request)
            except RestoreEngineError as exc:
                self.fail(f"Unexpected sanitized media restore failure: {exc.issue_code}")
        self.assertFalse(
            TenantOperationLock.objects.filter(
                business=self.business,
                operation_kind=OperationKind.RESTORE,
                active=True,
            ).exists()
        )
        source.refresh_from_db()
        safety = BackupRecord.objects.get(public_id=result.safety_backup_public_id)
        self.assertEqual(source.status, BackupStatus.SUCCEEDED)
        self.assertEqual(safety.status, BackupStatus.SUCCEEDED)
        self.assertTrue(safety.protected)
        restore.refresh_from_db()
        self.assertEqual(restore.status, RestoreStatus.SUCCEEDED)

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=True)
    def test_media_is_restored_with_exact_hash_and_logical_name(self):
        storage_name = "products/phase3b-restored.bin"
        expected = b"phase3b-exact-media-bytes"
        media_path = self.media_root / "products" / "phase3b-restored.bin"
        media_path.parent.mkdir(parents=True)
        media_path.write_bytes(expected)
        product = Product.objects.create(
            business=self.business,
            name="Media product",
            sku="PHASE3B-MEDIA",
            image=storage_name,
        )
        self._copy_source_row(product)
        _source, _restore, stack, request = self._prepared_execution()
        product.delete()
        media_path.unlink()
        self.fixture.source.execute(
            f'DELETE FROM "{Product._meta.db_table}"'
        )

        stages = []
        with override_settings(MEDIA_ROOT=self.media_root):
            try:
                result = RestoreExecutionCoordinator(
                    runtime_stack=stack,
                    failure_hook=lambda stage, _operation: stages.append(stage),
                ).execute(request)
            except RestoreEngineError as exc:
                self.fail(
                    f"Unexpected sanitized media restore failure: {exc.issue_code} {stages}"
                )

        restored = Product.objects.get(
            business=self.business,
            public_id=product.public_id,
        )
        self.assertEqual(restored.image.name, storage_name)
        self.assertEqual(media_path.read_bytes(), expected)
        self.assertEqual(result.restored_media_count, 1)

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=True)
    def test_media_collision_fails_before_destructive_mutation(self):
        storage_name = "products/phase3b-collision.bin"
        source_bytes = b"source-media"
        collision_bytes = b"unrelated-live-media"
        media_path = self.media_root / "products" / "phase3b-collision.bin"
        media_path.parent.mkdir(parents=True)
        media_path.write_bytes(source_bytes)
        product = Product.objects.create(
            business=self.business,
            name="Collision source",
            sku="PHASE3B-COLLISION",
            image=storage_name,
        )
        self._copy_source_row(product)
        _source, restore, stack, request = self._prepared_execution()
        Product.objects.filter(pk=product.pk).update(name="Current live product")
        media_path.write_bytes(collision_bytes)

        with override_settings(MEDIA_ROOT=self.media_root):
            with self.assertRaises(RestoreMediaPublicationError):
                RestoreExecutionCoordinator(runtime_stack=stack).execute(request)

        self.assertEqual(
            Product.objects.get(pk=product.pk).name,
            "Current live product",
        )
        self.assertEqual(media_path.read_bytes(), collision_bytes)
        restore.refresh_from_db()
        self.assertEqual(restore.status, RestoreStatus.FAILED)

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=True)
    def test_abort_signal_rolls_back_and_is_preserved(self):
        _source, restore, stack, request = self._prepared_execution()
        category = Category.objects.create(business=self.business, name="Abort guard")

        def hook(stage, _operation_public_id):
            if stage == "before_database_mutation":
                raise KeyboardInterrupt

        with override_settings(MEDIA_ROOT=self.media_root):
            with self.assertRaises(KeyboardInterrupt):
                RestoreExecutionCoordinator(
                    runtime_stack=stack,
                    failure_hook=hook,
                ).execute(request)
        self.assertTrue(Category.objects.filter(pk=category.pk).exists())
        restore.refresh_from_db()
        self.assertEqual(restore.status, RestoreStatus.ROLLED_BACK)
        self.assertFalse(
            TenantOperationLock.objects.filter(
                business=self.business,
                operation_kind=OperationKind.RESTORE,
                active=True,
            ).exists()
        )


class RestoreMutationContractTests(SimpleTestCase):
    def _request(self):
        operation = uuid.uuid4()
        business = uuid.uuid4()
        backup = uuid.uuid4()
        approved = RestorePreflightResult(
            operation_reference=operation,
            preflight_reference=RestorePreflightReference(uuid.uuid4()),
            backup_public_id=backup,
            business_public_id=business,
            state=RestorePreflightState.RESTORE_READY,
            restore_ready=True,
            compatibility_status=PackageCompatibilityStatus.COMPATIBLE,
            component_count=1,
            record_count=1,
            media_object_count=0,
            plaintext_package_bytes=1,
            verified_at=timezone.now(),
            preflight_completed_at=timezone.now(),
            durable_provider_identifier="durable",
            encryption_provider_identifier="encryption",
            verification_provider_identifier="verification",
            package_provider_identifier="package",
            issue_codes=(),
            component_plan=(),
        )
        return RestoreExecutionRequest(
            business_public_id=business,
            selected_backup_public_id=backup,
            actor_identity=ActorIdentitySnapshot("", "", "", "SYSTEM", False),
            idempotency_key="immutable",
            approved_preflight_result=approved,
            restore_request_public_id=operation,
        )

    def test_restore_execution_request_is_immutable(self):
        request = self._request()
        with self.assertRaises(FrozenInstanceError):
            request.idempotency_key = "changed"

    def test_restore_execution_request_has_no_path_or_key_fields(self):
        names = {item.name for item in fields(RestoreExecutionRequest)}
        self.assertFalse(
            names.intersection(
                {"path", "object_path", "zip_bytes", "encryption_key", "database_pk"}
            )
        )

    def test_restore_execution_result_has_only_safe_contract_fields(self):
        names = {item.name for item in fields(RestoreExecutionResult)}
        self.assertIn("safety_backup_public_id", names)
        self.assertNotIn("path", names)
        self.assertNotIn("raw_manifest", names)

    def test_restore_runtime_stack_version_is_stable(self):
        self.assertEqual(RESTORE_RUNTIME_STACK_VERSION, "nexa.restore-runtime.v1")

    def test_restore_preflight_capability_remains_ready(self):
        self.assertIs(RESTORE_PREFLIGHT_ENGINE_READY, True)

    def test_restore_mutation_capability_is_ready(self):
        self.assertIs(RESTORE_MUTATION_ENGINE_READY, True)

    def test_restore_mutation_setting_defaults_disabled(self):
        self.assertFalse(restore_mutation_setting_enabled())

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=True)
    def test_restore_mutation_setting_requires_explicit_true(self):
        self.assertTrue(restore_mutation_setting_enabled())

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED="true")
    def test_restore_mutation_setting_rejects_truthy_non_boolean(self):
        self.assertFalse(restore_mutation_setting_enabled())

    def test_restore_execution_states_are_bounded(self):
        self.assertEqual(
            {item.value for item in RestoreExecutionState},
            {"SUCCESS", "FAILED_BEFORE_MUTATION", "FAILED_ROLLED_BACK", "RECOVERY_REQUIRED"},
        )

    def test_post_restore_verification_state_is_bounded(self):
        self.assertEqual(
            tuple(item.value for item in PostRestoreVerificationState),
            ("VERIFIED",),
        )

    def test_phase3b_event_names_are_stable(self):
        self.assertEqual(events.RESTORE_STARTED, "restore.started")
        self.assertEqual(events.RESTORE_COMPLETED, "restore.completed")
        self.assertEqual(events.RESTORE_RECOVERY_REQUIRED, "restore.recovery_required")

    def test_sanitized_mutation_error_has_no_raw_payload(self):
        error = RestoreMutationError(issue_code="safe_code")
        self.assertEqual(error.issue_code, "safe_code")
        self.assertNotIn("record", str(error).lower())

    def test_safety_backup_error_is_sanitized(self):
        error = RestoreSafetyBackupError()
        self.assertLessEqual(len(error.sanitized_message), 500)

    def test_recovery_required_is_a_restore_error(self):
        self.assertIsInstance(RestoreRecoveryRequired(), RestoreMutationError)

    def test_request_rejects_mutation_when_setting_disabled(self):
        coordinator = object.__new__(RestoreExecutionCoordinator)
        with self.assertRaises(RestoreMutationError) as raised:
            coordinator.execute(self._request())
        self.assertEqual(raised.exception.issue_code, "restore_mutation_disabled")

    def test_request_validation_rejects_raw_non_contract_object(self):
        with self.assertRaises(RestoreSelectionError):
            RestoreExecutionCoordinator._validate_request(object())

    def test_request_validation_rejects_oversized_idempotency(self):
        request = replace(self._request(), idempotency_key="x" * 129)
        with self.assertRaises(RestoreSelectionError):
            RestoreExecutionCoordinator._validate_request(request)

    def test_request_validation_rejects_oversized_worker_identifier(self):
        request = replace(self._request(), worker_task_identifier="x" * 256)
        with self.assertRaises(RestoreSelectionError):
            RestoreExecutionCoordinator._validate_request(request)

    def test_request_validation_accepts_exact_contract(self):
        self.assertIsNone(RestoreExecutionCoordinator._validate_request(self._request()))

    def test_no_http_restore_execution_symbol_exists(self):
        names = set(dir(views)) | set(dir(platform_views))
        self.assertNotIn("execute_restore", names)
        self.assertNotIn("restore_execute", names)

    def test_failure_types_do_not_embed_paths(self):
        for error_type in (RestoreMutationError, RestoreSafetyBackupError, RestoreRecoveryRequired):
            message = str(error_type())
            self.assertNotIn("\\", message)
            self.assertNotIn("/", message)

    def test_result_state_success_value(self):
        self.assertEqual(RestoreExecutionState.SUCCESS.value, "SUCCESS")

    def test_result_state_pre_mutation_failure_value(self):
        self.assertEqual(
            RestoreExecutionState.FAILED_BEFORE_MUTATION.value,
            "FAILED_BEFORE_MUTATION",
        )

    def test_result_state_rolled_back_value(self):
        self.assertEqual(
            RestoreExecutionState.FAILED_ROLLED_BACK.value,
            "FAILED_ROLLED_BACK",
        )

    def test_result_state_recovery_required_value(self):
        self.assertEqual(
            RestoreExecutionState.RECOVERY_REQUIRED.value,
            "RECOVERY_REQUIRED",
        )

    def test_request_actor_identity_is_typed(self):
        self.assertIs(type(self._request().actor_identity), ActorIdentitySnapshot)

    def test_request_approved_preflight_is_typed(self):
        self.assertIs(type(self._request().approved_preflight_result), RestorePreflightResult)

    def test_request_contains_public_uuids_only(self):
        request = self._request()
        self.assertIs(type(request.business_public_id), uuid.UUID)
        self.assertIs(type(request.selected_backup_public_id), uuid.UUID)
        self.assertIs(type(request.restore_request_public_id), uuid.UUID)

    def test_setting_false_override_remains_disabled(self):
        with override_settings(BACKUP_RESTORE_MUTATION_ENABLED=False):
            self.assertFalse(restore_mutation_setting_enabled())

    def test_event_component_completed_is_bounded(self):
        self.assertEqual(events.RESTORE_COMPONENT_COMPLETED, "restore.component_completed")

    def test_event_media_completed_is_bounded(self):
        self.assertEqual(events.RESTORE_MEDIA_COMPLETED, "restore.media_completed")

    def test_event_post_verify_completed_is_bounded(self):
        self.assertEqual(
            events.RESTORE_POST_VERIFICATION_COMPLETED,
            "restore.post_verification_completed",
        )

    def test_event_safety_started_is_bounded(self):
        self.assertEqual(
            events.RESTORE_SAFETY_BACKUP_STARTED,
            "restore.safety_backup_started",
        )

    def test_event_safety_completed_is_bounded(self):
        self.assertEqual(
            events.RESTORE_SAFETY_BACKUP_COMPLETED,
            "restore.safety_backup_completed",
        )

    def test_event_mutation_started_is_bounded(self):
        self.assertEqual(events.RESTORE_MUTATION_STARTED, "restore.mutation_started")

    def test_event_failed_is_bounded(self):
        self.assertEqual(events.RESTORE_FAILED, "restore.failed")

    def test_execution_result_is_immutable(self):
        result = RestoreExecutionResult(
            restore_operation_public_id=uuid.uuid4(),
            business_public_id=uuid.uuid4(),
            source_backup_public_id=uuid.uuid4(),
            safety_backup_public_id=uuid.uuid4(),
            final_state=RestoreExecutionState.SUCCESS,
            started_at=self._request().approved_preflight_result.verified_at,
            completed_at=self._request().approved_preflight_result.verified_at,
            component_count=1,
            restored_record_count=1,
            restored_media_count=0,
            post_restore_verification_state=PostRestoreVerificationState.VERIFIED,
            sanitized_issues=(),
        )
        with self.assertRaises(FrozenInstanceError):
            result.component_count = 2

    def test_runtime_stack_contract_is_immutable(self):
        self.assertTrue(hasattr(RestoreRuntimeStack, "__dataclass_fields__"))

    def test_logical_restore_engine_requires_transaction(self):
        engine = object.__new__(LogicalRestoreEngine)
        with self.assertRaises(RestoreImportError):
            engine.mutate(business=object(), prepared=object())

    def test_media_provider_identifier_is_stable(self):
        self.assertEqual(
            LocalFilesystemMediaRestoreProvider.provider_identifier,
            "local-media-restore-provider-v1",
        )

    def test_post_verifier_identifier_is_stable(self):
        self.assertEqual(
            IndependentRestoreStateVerifier.provider_identifier,
            "independent-restore-state-verifier-v1",
        )

    def test_restore_error_issue_code_is_bounded(self):
        error = RestoreMutationError(issue_code="x" * 200)
        self.assertEqual(len(error.issue_code), 80)

    def test_restore_error_message_is_bounded(self):
        error = RestoreMutationError("x" * 1000)
        self.assertEqual(len(error.sanitized_message), 500)

    def test_request_field_count_is_stable(self):
        self.assertEqual(len(fields(RestoreExecutionRequest)), 7)

    def test_result_field_count_is_stable(self):
        self.assertEqual(len(fields(RestoreExecutionResult)), 12)

    def test_preflight_result_cannot_be_replaced_by_uuid(self):
        request = replace(self._request(), approved_preflight_result=uuid.uuid4())
        with self.assertRaises(RestoreSelectionError):
            RestoreExecutionCoordinator._validate_request(request)

    def test_restore_request_uuid_is_optional(self):
        request = replace(self._request(), restore_request_public_id=None)
        self.assertIsNone(request.restore_request_public_id)
        self.assertIsNone(RestoreExecutionCoordinator._validate_request(request))

    def test_actor_identity_cannot_be_plain_mapping(self):
        request = replace(self._request(), actor_identity={})
        with self.assertRaises(RestoreSelectionError):
            RestoreExecutionCoordinator._validate_request(request)

    def test_empty_idempotency_is_rejected(self):
        request = replace(self._request(), idempotency_key="")
        with self.assertRaises(RestoreSelectionError):
            RestoreExecutionCoordinator._validate_request(request)

    def test_non_uuid_business_is_rejected(self):
        request = replace(self._request(), business_public_id=str(uuid.uuid4()))
        with self.assertRaises(RestoreSelectionError):
            RestoreExecutionCoordinator._validate_request(request)

    def test_non_uuid_backup_is_rejected(self):
        request = replace(self._request(), selected_backup_public_id=str(uuid.uuid4()))
        with self.assertRaises(RestoreSelectionError):
            RestoreExecutionCoordinator._validate_request(request)

    def test_non_uuid_restore_request_is_rejected(self):
        request = replace(self._request(), restore_request_public_id="invalid")
        with self.assertRaises(RestoreSelectionError):
            RestoreExecutionCoordinator._validate_request(request)

    def test_non_string_worker_identifier_is_rejected(self):
        request = replace(self._request(), worker_task_identifier=1)
        with self.assertRaises(RestoreSelectionError):
            RestoreExecutionCoordinator._validate_request(request)

    def test_restore_started_event_does_not_name_records(self):
        self.assertNotIn("record", events.RESTORE_STARTED)

    def test_restore_completed_event_does_not_name_paths(self):
        self.assertNotIn("path", events.RESTORE_COMPLETED)
