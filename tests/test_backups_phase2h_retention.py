"""Focused fail-closed tests for Phase 2H durable retention lifecycle."""

from __future__ import annotations

import io
import threading
import unittest
import uuid
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.test import override_settings

from apps.backups.engine.availability import (
    OPERATIONAL_PROVIDER_STACK_READY,
    RETENTION_ENGINE_READY,
    get_engine_capability,
    real_execution_available,
)
from apps.backups.engine.checks import check_retention_policy_settings
from apps.backups.engine.context import ActorIdentitySnapshot, BackupExecutionContext
from apps.backups.engine.contracts import (
    DurableBackupStorageProvider,
    StoredBackupObjectReference,
    StoredBackupObjectResult,
    StoredObjectDurabilityState,
    StoredObjectVerificationState,
)
from apps.backups.engine.durable_storage_exceptions import (
    DurableObjectCleanupError,
    DurableObjectValidationError,
)
from apps.backups.engine.retention import (
    BackupRetentionClass,
    RetentionAuditEventType,
    RetentionCandidate,
    RetentionEngine,
    RetentionExecutionState,
)
from apps.backups.engine.retention_exceptions import (
    RetentionConcurrencyError,
    RetentionDeleteError,
    RetentionEligibilityError,
    RetentionPolicyError,
)
from apps.backups.engine.retention_policy import (
    DAILY_FULL_KEEP_COUNT,
    RETENTION_POLICY_IDENTIFIER,
    RETENTION_POLICY_VERSION,
    RetentionPolicy,
)
from apps.backups.engine.workspace import WorkspaceReference
from apps.backups.enums import BackupScope, BackupTrigger, ProductOwner


class _MemoryDurableProvider(DurableBackupStorageProvider):
    """Exact-evidence provider used to test provider-neutral retention."""

    def __init__(self):
        self.held = {}
        self.deleted = {}
        self.invalid = set()
        self.fail_delete = set()
        self.fail_absence_once = set()
        self.delete_calls = []

    @staticmethod
    def _key(context, reference):
        return (
            context.workspace_reference.identifier,
            reference.identifier,
        )

    def register(self, context, result):
        self.held[self._key(context, result.reference)] = (context, result)

    def store_encrypted_artifact(self, request):
        raise AssertionError("Retention must not create durable objects.")

    def validate_stored_object(self, *, context, result):
        key = self._key(context, result.reference)
        if (
            key in self.invalid
            or self.held.get(key) != (context, result)
            or key in self.deleted
        ):
            raise DurableObjectValidationError()
        return True

    @contextmanager
    def open_stored_object(self, *, context, reference):
        if not self.owns_stored_object_reference(
            context=context,
            reference=reference,
        ):
            raise DurableObjectValidationError()
        yield io.BytesIO(b"opaque encrypted object")

    def owns_stored_object_reference(self, *, context, reference):
        key = self._key(context, reference)
        evidence = self.held.get(key)
        return evidence is not None and evidence[0] == context

    def owns_stored_object_result(self, *, context, result):
        return self.held.get(self._key(context, result.reference)) == (
            context,
            result,
        )

    def confirm_stored_object_absent(self, *, context, reference):
        key = self._key(context, reference)
        if key in self.fail_absence_once:
            self.fail_absence_once.remove(key)
            return False
        evidence = self.deleted.get(key)
        return evidence is not None and evidence[0] == context and key not in self.held

    def delete_stored_object(self, *, context, reference):
        key = self._key(context, reference)
        self.delete_calls.append(key)
        if key in self.fail_delete:
            raise DurableObjectCleanupError()
        if key in self.deleted:
            return True
        evidence = self.held.get(key)
        if evidence is None or evidence[0] != context:
            raise DurableObjectCleanupError()
        self.deleted[key] = self.held.pop(key)
        return True


class RetentionEngineTests(unittest.TestCase):
    def setUp(self):
        self.tenant_public_id = uuid.UUID(int=10_001)
        self.now = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)
        self.provider = _MemoryDurableProvider()
        self.policy = RetentionPolicy(
            daily_full_keep_count=5,
            maximum_delete_batch=100,
            timeout_seconds=300,
        )
        self.engine = RetentionEngine(
            durable_provider=self.provider,
            policy=self.policy,
            clock=lambda: self.now,
        )

    def _candidate(
        self,
        index,
        *,
        stored_at=None,
        retention_class=BackupRetentionClass.DAILY_FULL,
        register=True,
        **changes,
    ):
        backup_public_id = uuid.UUID(int=index + 1)
        context = BackupExecutionContext(
            backup_public_id=backup_public_id,
            business_id=17,
            business_public_id=self.tenant_public_id,
            requested_scope=BackupScope.POS,
            resolved_products=(ProductOwner.POS,),
            trigger_type=BackupTrigger.SCHEDULED,
            actor_identity=ActorIdentitySnapshot(
                public_id="system",
                email="",
                full_name="",
                actor_type="SYSTEM",
                platform_staff=False,
            ),
            application_version="phase2h-test",
            backup_format_version="phase2h-test",
            schema_migration_fingerprint="opaque",
            minimum_restore_version="phase2h-test",
            idempotency_key=f"retention-{index}",
            operation_correlation_id=uuid.UUID(int=20_000 + index),
            workspace_reference=WorkspaceReference(uuid.UUID(int=30_000 + index)),
        )
        result = StoredBackupObjectResult(
            reference=StoredBackupObjectReference(uuid.UUID(int=40_000 + index)),
            backend_identifier="memory-private",
            object_schema_identifier="nexa.stored-backup-object.v1",
            byte_count=1024 + index,
            sha256=f"{index + 1:064x}",
            source_encrypted_artifact_sha256=f"{index + 100:064x}",
            backup_public_id=backup_public_id,
            tenant_public_id=self.tenant_public_id,
            stored_at=stored_at
            or datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index),
            provider_identifier="memory-durable-v1",
            durability_state=StoredObjectDurabilityState.STORED,
            verification_state=StoredObjectVerificationState.STORED_AND_VERIFIED,
            encrypted_format_identifier="nexa.encrypted-backup.v1",
            encryption_algorithm="AES-256-GCM",
            kek_provider_identifier="test-kek",
            kek_key_identifier="safe-id",
            kek_version="v1",
            encrypted_staging_cleanup_incomplete=False,
        )
        candidate = RetentionCandidate(
            context=context,
            stored_object=result,
            retention_class=retention_class,
            package_verified=True,
            encrypted_artifact_valid=True,
            durable_verified=True,
        )
        candidate = replace(candidate, **changes)
        if register:
            self.provider.register(context, result)
        return candidate

    def _candidates(self, count):
        return tuple(self._candidate(index) for index in range(count))

    def test_default_policy_and_boundaries_zero_one_five_six_and_ten(self):
        self.assertEqual(DAILY_FULL_KEEP_COUNT, 5)
        for count in (0, 1, 5, 6, 10):
            with self.subTest(count=count):
                provider = _MemoryDurableProvider()
                engine = RetentionEngine(
                    durable_provider=provider,
                    policy=self.policy,
                    clock=lambda: self.now,
                )
                candidates = tuple(self._candidate(index) for index in range(count))
                for candidate in candidates:
                    provider.register(candidate.context, candidate.stored_object)
                plan = engine.build_retention_plan(
                    tenant_public_id=self.tenant_public_id,
                    candidates=candidates,
                )
                self.assertEqual(plan.successful_eligible_count, count)
                self.assertEqual(len(plan.retained_backup_public_ids), min(count, 5))
                self.assertEqual(
                    len(plan.delete_candidate_backup_public_ids),
                    max(count - 5, 0),
                )

    def test_plan_is_deterministic_by_timestamp_then_public_uuid(self):
        tied = datetime(2026, 1, 10, tzinfo=UTC)
        candidates = tuple(
            self._candidate(index, stored_at=tied) for index in range(7)
        )
        plan = self.engine.build_retention_plan(
            tenant_public_id=self.tenant_public_id,
            candidates=tuple(reversed(candidates)),
        )
        self.assertEqual(
            plan.retained_backup_public_ids,
            tuple(uuid.UUID(int=value) for value in (7, 6, 5, 4, 3)),
        )
        self.assertEqual(
            plan.delete_candidate_backup_public_ids,
            tuple(uuid.UUID(int=value) for value in (2, 1)),
        )
        repeated = self.engine.build_retention_plan(
            tenant_public_id=self.tenant_public_id,
            candidates=candidates,
        )
        self.assertIs(repeated, plan)

    def test_batch_limit_selects_only_oldest_current_candidates(self):
        policy = RetentionPolicy(5, 2, 300)
        engine = RetentionEngine(
            durable_provider=self.provider,
            policy=policy,
            clock=lambda: self.now,
        )
        candidates = self._candidates(10)
        plan = engine.build_retention_plan(
            tenant_public_id=self.tenant_public_id,
            candidates=candidates,
        )
        self.assertEqual(
            plan.delete_candidate_backup_public_ids,
            (uuid.UUID(int=2), uuid.UUID(int=1)),
        )

    def test_non_daily_and_unsafe_states_never_count_or_delete(self):
        candidates = list(self._candidates(6))
        candidates.extend(
            (
                self._candidate(20, retention_class=BackupRetentionClass.MANUAL),
                self._candidate(21, retention_class=BackupRetentionClass.WEEKLY),
                self._candidate(22, retention_class=BackupRetentionClass.MONTHLY),
                self._candidate(23, retention_class=BackupRetentionClass.PINNED),
                self._candidate(24, pinned=True),
                self._candidate(25, protected=True),
                self._candidate(26, active_operation=True),
                self._candidate(27, failed=True),
                self._candidate(28, incomplete=True),
                self._candidate(29, cleanup_incomplete=True),
                self._candidate(30, corrupted=True),
                self._candidate(31, package_verified=False),
                self._candidate(32, encrypted_artifact_valid=False),
                self._candidate(33, durable_verified=False),
                self._candidate(34, deleting=True),
                self._candidate(35, deleted=True),
            )
        )
        plan = self.engine.build_retention_plan(
            tenant_public_id=self.tenant_public_id,
            candidates=tuple(reversed(candidates)),
        )
        self.assertEqual(plan.successful_eligible_count, 6)
        self.assertEqual(len(plan.delete_candidate_backup_public_ids), 1)
        self.assertEqual(len(plan.protected_backup_public_ids), 7)
        self.assertEqual(len(plan.skipped_backup_public_ids), 9)
        result = self.engine.execute_retention_plan(
            plan=plan,
            current_candidates=tuple(candidates),
        )
        self.assertEqual(result.deleted_backup_public_ids, (uuid.UUID(int=1),))
        self.assertEqual(len(self.provider.delete_calls), 1)

    def test_provider_reported_corruption_is_skipped_not_counted(self):
        candidates = self._candidates(6)
        corrupt = candidates[-1]
        self.provider.invalid.add(
            self.provider._key(corrupt.context, corrupt.stored_object.reference)
        )
        plan = self.engine.build_retention_plan(
            tenant_public_id=self.tenant_public_id,
            candidates=candidates,
        )
        self.assertEqual(plan.successful_eligible_count, 5)
        self.assertEqual(plan.delete_candidate_backup_public_ids, ())
        self.assertIn(corrupt.context.backup_public_id, plan.skipped_backup_public_ids)

    def test_execution_deletes_exact_candidates_confirms_absence_and_is_idempotent(self):
        candidates = self._candidates(7)
        plan = self.engine.build_retention_plan(
            tenant_public_id=self.tenant_public_id,
            candidates=candidates,
        )
        result = self.engine.execute_retention_plan(
            plan=plan,
            current_candidates=candidates,
        )
        self.assertEqual(result.execution_state, RetentionExecutionState.COMPLETED)
        self.assertEqual(
            result.deleted_backup_public_ids,
            (uuid.UUID(int=2), uuid.UUID(int=1)),
        )
        self.assertEqual(len(self.provider.delete_calls), 2)
        for candidate in candidates[-5:]:
            self.assertTrue(
                self.provider.owns_stored_object_result(
                    context=candidate.context,
                    result=candidate.stored_object,
                )
            )
        self.assertEqual(
            [event.event_type for event in result.audit_events].count(
                RetentionAuditEventType.OBJECT_DELETED
            ),
            2,
        )
        repeated = self.engine.execute_retention_plan(
            plan=plan,
            current_candidates=candidates,
        )
        self.assertIs(repeated, result)
        self.assertEqual(len(self.provider.delete_calls), 2)

    def test_unconfirmed_absence_is_failed_safe_then_reconciled_on_retry(self):
        candidates = self._candidates(6)
        plan = self.engine.build_retention_plan(
            tenant_public_id=self.tenant_public_id,
            candidates=candidates,
        )
        oldest = candidates[0]
        key = self.provider._key(oldest.context, oldest.stored_object.reference)
        self.provider.fail_absence_once.add(key)
        failed = self.engine.execute_retention_plan(
            plan=plan,
            current_candidates=candidates,
        )
        self.assertEqual(failed.execution_state, RetentionExecutionState.FAILED_SAFE)
        self.assertEqual(failed.deleted_backup_public_ids, ())
        retried = self.engine.execute_retention_plan(
            plan=plan,
            current_candidates=candidates,
        )
        self.assertEqual(retried.execution_state, RetentionExecutionState.COMPLETED)
        self.assertEqual(retried.deleted_backup_public_ids, (uuid.UUID(int=1),))
        self.assertEqual(len(self.provider.delete_calls), 1)

    def test_no_action_plan_returns_explicit_completed_evidence(self):
        candidates = self._candidates(5)
        plan = self.engine.build_retention_plan(
            tenant_public_id=self.tenant_public_id,
            candidates=candidates,
        )
        result = self.engine.execute_retention_plan(
            plan=plan,
            current_candidates=candidates,
        )
        self.assertEqual(
            result.execution_state,
            RetentionExecutionState.NO_ACTION_REQUIRED,
        )
        self.assertEqual(result.deleted_backup_public_ids, ())
        self.assertEqual(self.provider.delete_calls, [])

    def test_first_delete_failure_stops_batch_and_retry_resumes_safely(self):
        candidates = self._candidates(8)
        plan = self.engine.build_retention_plan(
            tenant_public_id=self.tenant_public_id,
            candidates=candidates,
        )
        second = candidates[1]
        second_key = self.provider._key(second.context, second.stored_object.reference)
        self.provider.fail_delete.add(second_key)
        partial = self.engine.execute_retention_plan(
            plan=plan,
            current_candidates=candidates,
        )
        self.assertEqual(
            partial.execution_state,
            RetentionExecutionState.PARTIALLY_COMPLETED,
        )
        self.assertEqual(partial.deleted_backup_public_ids, (uuid.UUID(int=3),))
        self.assertEqual(len(self.provider.delete_calls), 2)
        self.provider.fail_delete.clear()
        completed = self.engine.execute_retention_plan(
            plan=plan,
            current_candidates=candidates,
        )
        self.assertEqual(completed.execution_state, RetentionExecutionState.COMPLETED)
        self.assertEqual(
            completed.deleted_backup_public_ids,
            (uuid.UUID(int=3), uuid.UUID(int=2), uuid.UUID(int=1)),
        )
        self.assertEqual(len(self.provider.delete_calls), 4)

    def test_failure_on_first_candidate_stops_without_additional_mutation(self):
        candidates = self._candidates(8)
        plan = self.engine.build_retention_plan(
            tenant_public_id=self.tenant_public_id,
            candidates=candidates,
        )
        first = candidates[2]
        self.provider.fail_delete.add(
            self.provider._key(first.context, first.stored_object.reference)
        )
        result = self.engine.execute_retention_plan(
            plan=plan,
            current_candidates=candidates,
        )
        self.assertEqual(result.execution_state, RetentionExecutionState.FAILED_SAFE)
        self.assertEqual(result.deleted_backup_public_ids, ())
        self.assertEqual(len(self.provider.delete_calls), 1)

    def test_candidate_becoming_pinned_is_revalidated_and_skipped(self):
        candidates = self._candidates(6)
        plan = self.engine.build_retention_plan(
            tenant_public_id=self.tenant_public_id,
            candidates=candidates,
        )
        current = (replace(candidates[0], pinned=True),) + candidates[1:]
        result = self.engine.execute_retention_plan(
            plan=plan,
            current_candidates=current,
        )
        self.assertEqual(result.execution_state, RetentionExecutionState.COMPLETED)
        self.assertEqual(result.deleted_backup_public_ids, ())
        self.assertIn(uuid.UUID(int=1), result.skipped_backup_public_ids)
        self.assertEqual(self.provider.delete_calls, [])

    def test_missing_candidate_fails_closed_before_any_delete(self):
        candidates = self._candidates(6)
        plan = self.engine.build_retention_plan(
            tenant_public_id=self.tenant_public_id,
            candidates=candidates,
        )
        result = self.engine.execute_retention_plan(
            plan=plan,
            current_candidates=candidates[1:],
        )
        self.assertEqual(result.execution_state, RetentionExecutionState.FAILED_SAFE)
        self.assertEqual(self.provider.delete_calls, [])

    def test_forged_result_and_cross_tenant_bindings_are_rejected(self):
        candidate = self._candidate(0)
        forged = replace(
            candidate,
            stored_object=replace(candidate.stored_object, byte_count=1),
        )
        with self.assertRaises(RetentionEligibilityError):
            self.engine.build_retention_plan(
                tenant_public_id=self.tenant_public_id,
                candidates=(forged,),
            )
        wrong_tenant = replace(
            candidate,
            context=replace(candidate.context, business_public_id=uuid.uuid4()),
        )
        with self.assertRaises(RetentionEligibilityError):
            self.engine.build_retention_plan(
                tenant_public_id=self.tenant_public_id,
                candidates=(wrong_tenant,),
            )
        wrong_backup = replace(
            candidate,
            context=replace(candidate.context, backup_public_id=uuid.uuid4()),
        )
        forged_reference = replace(
            candidate,
            stored_object=replace(
                candidate.stored_object,
                reference=StoredBackupObjectReference(uuid.uuid4()),
            ),
        )
        for forged_candidate in (wrong_backup, forged_reference):
            with self.assertRaises(RetentionEligibilityError):
                self.engine.build_retention_plan(
                    tenant_public_id=self.tenant_public_id,
                    candidates=(forged_candidate,),
                )

    def test_failed_new_attempt_cannot_displace_known_good_backup(self):
        candidates = list(self._candidates(5))
        candidates.append(
            self._candidate(
                50,
                stored_at=datetime(2026, 2, 1, tzinfo=UTC),
                failed=True,
            )
        )
        failed_plan = self.engine.build_retention_plan(
            tenant_public_id=self.tenant_public_id,
            candidates=tuple(candidates),
        )
        self.assertEqual(failed_plan.successful_eligible_count, 5)
        self.assertEqual(failed_plan.delete_candidate_backup_public_ids, ())

        successful = replace(candidates[-1], failed=False)
        successful_plan = RetentionEngine(
            durable_provider=self.provider,
            policy=self.policy,
            clock=lambda: self.now,
        ).build_retention_plan(
            tenant_public_id=self.tenant_public_id,
            candidates=tuple(candidates[:-1]) + (successful,),
        )
        self.assertEqual(successful_plan.successful_eligible_count, 6)
        self.assertEqual(
            successful_plan.delete_candidate_backup_public_ids,
            (uuid.UUID(int=1),),
        )

    def test_mutated_candidate_and_unknown_orphan_are_never_deleted(self):
        candidates = self._candidates(6)
        orphan = self._candidate(80)
        plan = self.engine.build_retention_plan(
            tenant_public_id=self.tenant_public_id,
            candidates=candidates,
        )
        oldest = candidates[0]
        oldest_key = self.provider._key(
            oldest.context,
            oldest.stored_object.reference,
        )
        orphan_key = self.provider._key(orphan.context, orphan.stored_object.reference)
        # Phase 2G reports byte mutation, replacement, or link ambiguity through
        # exact validation failure; retention must not call delete in that state.
        self.provider.invalid.add(oldest_key)
        result = self.engine.execute_retention_plan(
            plan=plan,
            current_candidates=candidates,
        )
        self.assertEqual(result.execution_state, RetentionExecutionState.COMPLETED)
        self.assertEqual(result.deleted_backup_public_ids, ())
        self.assertEqual(self.provider.delete_calls, [])
        self.assertIn(oldest_key, self.provider.held)
        self.assertIn(orphan_key, self.provider.held)

    def test_same_tenant_concurrent_execution_fails_safe(self):
        entered = threading.Event()
        release = threading.Event()

        def hook(stage, _backup_id):
            if stage == "before_retention_delete":
                entered.set()
                if not release.wait(timeout=10):
                    raise RuntimeError

        engine = RetentionEngine(
            durable_provider=self.provider,
            policy=self.policy,
            clock=lambda: self.now,
            failure_hook=hook,
        )
        candidates = self._candidates(6)
        plan = engine.build_retention_plan(
            tenant_public_id=self.tenant_public_id,
            candidates=candidates,
        )
        outcomes = []
        worker = threading.Thread(
            target=lambda: outcomes.append(
                engine.execute_retention_plan(
                    plan=plan,
                    current_candidates=candidates,
                )
            )
        )
        worker.start()
        self.assertTrue(entered.wait(timeout=10))
        try:
            with self.assertRaises(RetentionConcurrencyError):
                engine.execute_retention_plan(
                    plan=plan,
                    current_candidates=candidates,
                )
        finally:
            release.set()
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcomes), 1)

    def test_abort_signals_are_not_swallowed(self):
        candidates = self._candidates(6)
        for abort_type in (KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(abort_type=abort_type.__name__):
                provider = _MemoryDurableProvider()
                for candidate in candidates:
                    provider.register(candidate.context, candidate.stored_object)

                def hook(stage, _backup_id, selected=abort_type):
                    if stage == "before_retention_delete":
                        raise selected()

                engine = RetentionEngine(
                    durable_provider=provider,
                    policy=self.policy,
                    clock=lambda: self.now,
                    failure_hook=hook,
                )
                plan = engine.build_retention_plan(
                    tenant_public_id=self.tenant_public_id,
                    candidates=candidates,
                )
                with self.assertRaises(abort_type):
                    engine.execute_retention_plan(
                        plan=plan,
                        current_candidates=candidates,
                    )
                self.assertEqual(provider.delete_calls, [])

    def test_policy_is_strict_immutable_and_system_checked(self):
        with self.assertRaises(FrozenInstanceError):
            self.policy.daily_full_keep_count = 7
        for changes in (
            {"daily_full_keep_count": True},
            {"daily_full_keep_count": 0},
            {"maximum_delete_batch": 0},
            {"maximum_delete_batch": 1001},
            {"timeout_seconds": float("nan")},
            {"timeout_seconds": 0},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(RetentionPolicyError):
                    replace(self.policy, **changes).validated()
        with override_settings(BACKUP_RETENTION_DAILY_FULL_KEEP_COUNT=0):
            errors = check_retention_policy_settings(None)
        self.assertEqual([error.id for error in errors], ["backups.E031"])

    def test_plan_and_result_expose_only_safe_immutable_metadata(self):
        candidates = self._candidates(6)
        plan = self.engine.build_retention_plan(
            tenant_public_id=self.tenant_public_id,
            candidates=candidates,
        )
        result = self.engine.execute_retention_plan(
            plan=plan,
            current_candidates=candidates,
        )
        rendered = repr((plan, result))
        self.assertEqual(plan.policy_identifier, RETENTION_POLICY_IDENTIFIER)
        self.assertEqual(plan.policy_version, RETENTION_POLICY_VERSION)
        self.assertNotIn("workspace_reference", rendered)
        self.assertNotIn("opaque encrypted object", rendered)
        self.assertNotIn("private-durable", rendered)
        sanitized = RetentionDeleteError(
            r"C:\private\tenant\object.nxb",
            code=r"C:\private\raw-code",
        )
        self.assertNotIn("private", str(sanitized).lower())
        self.assertNotIn("private", sanitized.engine_code.lower())
        with self.assertRaises(FrozenInstanceError):
            plan.keep_count = 99

    def test_capability_ready_but_runtime_surfaces_remain_disabled(self):
        self.assertIs(RETENTION_ENGINE_READY, True)
        self.assertIs(OPERATIONAL_PROVIDER_STACK_READY, False)
        self.assertIs(real_execution_available(), False)
        capability = get_engine_capability()
        self.assertIs(capability.retention_engine_ready, True)
        self.assertIs(capability.provider_stack_ready, False)
        repository_root = Path(__file__).resolve().parents[1]
        for relative in (
            "apps/backups/views.py",
            "apps/backups/platform_views.py",
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
                self.assertNotIn("RetentionEngine", source)
                self.assertNotIn("execute_retention_plan", source)


def load_tests(loader, standard_tests, pattern):
    """Run Phase 2H cases; predecessor regressions have dedicated commands."""

    del loader, standard_tests, pattern
    names = sorted(
        name
        for name, value in RetentionEngineTests.__dict__.items()
        if name.startswith("test_") and callable(value)
    )
    return unittest.TestSuite(RetentionEngineTests(name) for name in names)
