"""Deterministic planning and exact execution for durable backup retention."""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from django.utils import timezone

from .context import BackupExecutionContext
from .contracts import (
    DurableBackupStorageProvider,
    StoredBackupObjectReference,
    StoredBackupObjectResult,
    StoredObjectDurabilityState,
    StoredObjectVerificationState,
)
from .durable_storage_exceptions import (
    DurableObjectCleanupError,
    DurableObjectValidationError,
)
from .logical_serialization import encode_canonical_document
from .retention_exceptions import (
    Phase2HCoordinationError,
    RetentionConcurrencyError,
    RetentionDeleteError,
    RetentionDeleteValidationError,
    RetentionEligibilityError,
    RetentionEngineError,
    RetentionPlanError,
)
from .retention_policy import (
    RETENTION_POLICY_IDENTIFIER,
    RETENTION_POLICY_VERSION,
    RetentionPolicy,
)
from .workspace import WorkspaceReference


class BackupRetentionClass(StrEnum):
    DAILY_FULL = "DAILY_FULL"
    MANUAL = "MANUAL"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    PINNED = "PINNED"


class RetentionSkipReason(StrEnum):
    NON_DAILY_CLASS = "NON_DAILY_CLASS"
    PROTECTED = "PROTECTED"
    FAILED = "FAILED"
    INCOMPLETE = "INCOMPLETE"
    CLEANUP_INCOMPLETE = "CLEANUP_INCOMPLETE"
    CORRUPTED_OR_UNVERIFIED = "CORRUPTED_OR_UNVERIFIED"
    DELETING = "DELETING"
    DELETED = "DELETED"
    ACTIVE_OPERATION = "ACTIVE_OPERATION"
    OUTSIDE_CURRENT_DELETE_SET = "OUTSIDE_CURRENT_DELETE_SET"


class RetentionExecutionState(StrEnum):
    NO_ACTION_REQUIRED = "NO_ACTION_REQUIRED"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED_SAFE = "FAILED_SAFE"


class RetentionAuditEventType(StrEnum):
    PLAN_CREATED = "RETENTION_PLAN_CREATED"
    DELETE_STARTED = "RETENTION_OBJECT_DELETE_STARTED"
    OBJECT_DELETED = "RETENTION_OBJECT_DELETED"
    DELETE_FAILED = "RETENTION_OBJECT_DELETE_FAILED"
    COMPLETED = "RETENTION_COMPLETED"
    PARTIAL = "RETENTION_PARTIAL"


@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    context: BackupExecutionContext = field(repr=False)
    stored_object: StoredBackupObjectResult
    retention_class: BackupRetentionClass
    package_verified: bool
    encrypted_artifact_valid: bool
    durable_verified: bool
    failed: bool = False
    incomplete: bool = False
    cleanup_incomplete: bool = False
    corrupted: bool = False
    deleting: bool = False
    deleted: bool = False
    pinned: bool = False
    protected: bool = False
    active_operation: bool = False


@dataclass(frozen=True, slots=True)
class RetentionAuditEvent:
    event_type: RetentionAuditEventType
    occurred_at: datetime
    tenant_public_id: uuid.UUID
    backup_public_id: uuid.UUID | None
    outcome_code: str


@dataclass(frozen=True, slots=True)
class RetentionPlanReference:
    identifier: uuid.UUID


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    reference: RetentionPlanReference
    tenant_public_id: uuid.UUID
    retention_class: BackupRetentionClass
    keep_count: int
    successful_eligible_count: int
    retained_backup_public_ids: tuple[uuid.UUID, ...]
    delete_candidate_backup_public_ids: tuple[uuid.UUID, ...]
    protected_backup_public_ids: tuple[uuid.UUID, ...]
    skipped_backup_public_ids: tuple[uuid.UUID, ...]
    generated_at: datetime
    policy_identifier: str
    policy_version: str
    evidence_sha256: str
    audit_events: tuple[RetentionAuditEvent, ...]


@dataclass(frozen=True, slots=True)
class RetentionExecutionResult:
    tenant_public_id: uuid.UUID
    retention_policy_identifier: str
    retention_class: BackupRetentionClass
    keep_count: int
    eligible_successful_count: int
    retained_backup_public_ids: tuple[uuid.UUID, ...]
    deleted_backup_public_ids: tuple[uuid.UUID, ...]
    skipped_backup_public_ids: tuple[uuid.UUID, ...]
    failed_deletion_count: int
    completed_at: datetime
    execution_state: RetentionExecutionState
    audit_events: tuple[RetentionAuditEvent, ...]


@dataclass(frozen=True, slots=True)
class _PlanEvidence:
    plan: RetentionPlan
    candidates: tuple[RetentionCandidate, ...]
    fingerprint: str


@dataclass(slots=True)
class _ExecutionProgress:
    deleted: set[uuid.UUID]
    pending_deletions: set[uuid.UUID]
    runtime_skipped: set[uuid.UUID]
    failed_deletion_count: int
    audit_events: list[RetentionAuditEvent]


def _is_aware(value):
    return (
        type(value) is datetime
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _timestamp(value, *, error_type):
    if not _is_aware(value):
        raise error_type()
    try:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
            "+00:00",
            "Z",
        )
    except (OverflowError, TypeError, ValueError):
        raise error_type() from None


class RetentionEngine:
    """Plan daily-full retention and execute only exact revalidated deletes."""

    def __init__(
        self,
        *,
        durable_provider,
        policy=None,
        clock=None,
        monotonic=None,
        failure_hook=None,
    ):
        if not isinstance(durable_provider, DurableBackupStorageProvider):
            raise Phase2HCoordinationError()
        selected_policy = policy or RetentionPolicy.from_settings()
        if type(selected_policy) is not RetentionPolicy:
            raise Phase2HCoordinationError()
        self.durable_provider = durable_provider
        self.policy = selected_policy.validated()
        self.clock = clock or timezone.now
        self.monotonic = monotonic or time.monotonic
        self.failure_hook = failure_hook
        self._plans = {}
        self._plans_by_fingerprint = {}
        self._completed_executions = {}
        self._progress = {}
        self._active_tenants = set()
        self._state_lock = threading.RLock()

    def _run_hook(self, stage, backup_public_id=None):
        if self.failure_hook is not None:
            self.failure_hook(stage, backup_public_id)

    def _check_deadline(self, deadline, *, error_type):
        try:
            if self.monotonic() > deadline:
                raise error_type()
        except RetentionEngineError:
            raise
        except Exception:
            raise error_type() from None

    def _event(self, event_type, tenant_public_id, backup_public_id, outcome_code):
        occurred_at = self.clock()
        if not _is_aware(occurred_at):
            raise RetentionPlanError()
        return RetentionAuditEvent(
            event_type=event_type,
            occurred_at=occurred_at.astimezone(UTC),
            tenant_public_id=tenant_public_id,
            backup_public_id=backup_public_id,
            outcome_code=outcome_code,
        )

    @staticmethod
    def _candidate_document(candidate):
        context = candidate.context
        result = candidate.stored_object
        return {
            "backup_public_id": str(context.backup_public_id),
            "tenant_public_id": str(context.business_public_id),
            "workspace_reference": str(context.workspace_reference.identifier),
            "stored_reference": str(result.reference.identifier),
            "stored_at": _timestamp(result.stored_at, error_type=RetentionPlanError),
            "byte_count": result.byte_count,
            "sha256": result.sha256,
            "retention_class": candidate.retention_class.value,
            "package_verified": candidate.package_verified,
            "encrypted_artifact_valid": candidate.encrypted_artifact_valid,
            "durable_verified": candidate.durable_verified,
            "failed": candidate.failed,
            "incomplete": candidate.incomplete,
            "cleanup_incomplete": candidate.cleanup_incomplete,
            "corrupted": candidate.corrupted,
            "deleting": candidate.deleting,
            "deleted": candidate.deleted,
            "pinned": candidate.pinned,
            "protected": candidate.protected,
            "active_operation": candidate.active_operation,
        }

    def _fingerprint(self, tenant_public_id, candidates):
        try:
            documents = [self._candidate_document(candidate) for candidate in candidates]
            documents.sort(key=lambda value: value["backup_public_id"])
            raw = encode_canonical_document(
                {
                    "schema": "nexa.retention-input.v1",
                    "tenant_public_id": str(tenant_public_id),
                    "policy_identifier": RETENTION_POLICY_IDENTIFIER,
                    "policy_version": RETENTION_POLICY_VERSION,
                    "keep_count": self.policy.daily_full_keep_count,
                    "maximum_delete_batch": self.policy.maximum_delete_batch,
                    "candidates": documents,
                }
            )
            return hashlib.sha256(raw).hexdigest()
        except RetentionEngineError:
            raise
        except Exception:
            raise RetentionPlanError() from None

    def _validate_candidate_binding(self, tenant_public_id, candidate):
        if (
            type(candidate) is not RetentionCandidate
            or type(candidate.context) is not BackupExecutionContext
            or type(candidate.context.workspace_reference) is not WorkspaceReference
            or type(candidate.stored_object) is not StoredBackupObjectResult
            or type(candidate.stored_object.reference) is not StoredBackupObjectReference
            or type(candidate.retention_class) is not BackupRetentionClass
            or type(candidate.context.backup_public_id) is not uuid.UUID
            or type(candidate.context.business_public_id) is not uuid.UUID
            or candidate.context.business_public_id != tenant_public_id
            or candidate.stored_object.tenant_public_id != tenant_public_id
            or candidate.stored_object.backup_public_id
            != candidate.context.backup_public_id
        ):
            raise RetentionEligibilityError()
        boolean_values = (
            candidate.package_verified,
            candidate.encrypted_artifact_valid,
            candidate.durable_verified,
            candidate.failed,
            candidate.incomplete,
            candidate.cleanup_incomplete,
            candidate.corrupted,
            candidate.deleting,
            candidate.deleted,
            candidate.pinned,
            candidate.protected,
            candidate.active_operation,
        )
        if any(type(value) is not bool for value in boolean_values):
            raise RetentionEligibilityError()
        if not _is_aware(candidate.stored_object.stored_at):
            raise RetentionEligibilityError()

    def _assess_candidates(self, tenant_public_id, candidates, *, already_deleted=()):
        if type(tenant_public_id) is not uuid.UUID or type(candidates) is not tuple:
            raise RetentionPlanError()
        if len(candidates) > 100_000:
            raise RetentionPlanError()
        deleted_set = frozenset(already_deleted)
        eligible = []
        protected = []
        skipped = []
        reasons = {}
        backup_ids = set()
        references = set()
        for candidate in candidates:
            self._validate_candidate_binding(tenant_public_id, candidate)
            backup_id = candidate.context.backup_public_id
            reference = candidate.stored_object.reference.identifier
            if backup_id in backup_ids or reference in references:
                raise RetentionEligibilityError()
            backup_ids.add(backup_id)
            references.add(reference)
            if backup_id in deleted_set or candidate.deleted:
                skipped.append(candidate)
                reasons[backup_id] = RetentionSkipReason.DELETED
                continue
            if candidate.retention_class is not BackupRetentionClass.DAILY_FULL:
                protected.append(candidate)
                reasons[backup_id] = RetentionSkipReason.NON_DAILY_CLASS
                continue
            if candidate.pinned or candidate.protected:
                protected.append(candidate)
                reasons[backup_id] = RetentionSkipReason.PROTECTED
                continue
            if candidate.active_operation:
                protected.append(candidate)
                reasons[backup_id] = RetentionSkipReason.ACTIVE_OPERATION
                continue
            if candidate.failed:
                skipped.append(candidate)
                reasons[backup_id] = RetentionSkipReason.FAILED
                continue
            if candidate.incomplete:
                skipped.append(candidate)
                reasons[backup_id] = RetentionSkipReason.INCOMPLETE
                continue
            if (
                candidate.cleanup_incomplete
                or candidate.stored_object.encrypted_staging_cleanup_incomplete
            ):
                skipped.append(candidate)
                reasons[backup_id] = RetentionSkipReason.CLEANUP_INCOMPLETE
                continue
            if candidate.deleting:
                skipped.append(candidate)
                reasons[backup_id] = RetentionSkipReason.DELETING
                continue
            if (
                candidate.corrupted
                or candidate.package_verified is not True
                or candidate.encrypted_artifact_valid is not True
                or candidate.durable_verified is not True
                or candidate.stored_object.durability_state
                is not StoredObjectDurabilityState.STORED
                or candidate.stored_object.verification_state
                is not StoredObjectVerificationState.STORED_AND_VERIFIED
            ):
                skipped.append(candidate)
                reasons[backup_id] = RetentionSkipReason.CORRUPTED_OR_UNVERIFIED
                continue
            try:
                provider_owns_result = (
                    self.durable_provider.owns_stored_object_result(
                        context=candidate.context,
                        result=candidate.stored_object,
                    )
                )
            except Exception:
                raise RetentionEligibilityError() from None
            if not provider_owns_result:
                raise RetentionEligibilityError()
            try:
                self.durable_provider.validate_stored_object(
                    context=candidate.context,
                    result=candidate.stored_object,
                )
            except DurableObjectValidationError:
                skipped.append(candidate)
                reasons[backup_id] = RetentionSkipReason.CORRUPTED_OR_UNVERIFIED
                continue
            except Exception:
                raise RetentionEligibilityError() from None
            eligible.append(candidate)
        eligible.sort(
            key=lambda value: (
                value.stored_object.stored_at.astimezone(UTC),
                value.context.backup_public_id.int,
            ),
            reverse=True,
        )
        protected.sort(
            key=lambda value: value.context.backup_public_id.int,
            reverse=True,
        )
        skipped.sort(
            key=lambda value: value.context.backup_public_id.int,
            reverse=True,
        )
        return tuple(eligible), tuple(protected), tuple(skipped), reasons

    def build_retention_plan(self, *, tenant_public_id, candidates):
        deadline = self.monotonic() + self.policy.timeout_seconds
        self._check_deadline(deadline, error_type=RetentionPlanError)
        fingerprint = self._fingerprint(tenant_public_id, candidates)
        with self._state_lock:
            existing_reference = self._plans_by_fingerprint.get(fingerprint)
            existing = self._plans.get(existing_reference) if existing_reference else None
        if existing is not None:
            return existing.plan
        eligible, protected, skipped, _reasons = self._assess_candidates(
            tenant_public_id,
            candidates,
        )
        self._check_deadline(deadline, error_type=RetentionPlanError)
        keep_count = self.policy.daily_full_keep_count
        retained = eligible[:keep_count]
        outside_keep = eligible[keep_count:]
        if len(outside_keep) > self.policy.maximum_delete_batch:
            delete_candidates = outside_keep[-self.policy.maximum_delete_batch :]
        else:
            delete_candidates = outside_keep
        generated_at = self.clock()
        if not _is_aware(generated_at):
            raise RetentionPlanError()
        reference = RetentionPlanReference(
            uuid.uuid5(uuid.NAMESPACE_URL, f"nexa-retention:{fingerprint}")
        )
        event = self._event(
            RetentionAuditEventType.PLAN_CREATED,
            tenant_public_id,
            None,
            "plan_created",
        )
        plan = RetentionPlan(
            reference=reference,
            tenant_public_id=tenant_public_id,
            retention_class=BackupRetentionClass.DAILY_FULL,
            keep_count=keep_count,
            successful_eligible_count=len(eligible),
            retained_backup_public_ids=tuple(
                candidate.context.backup_public_id for candidate in retained
            ),
            delete_candidate_backup_public_ids=tuple(
                candidate.context.backup_public_id for candidate in delete_candidates
            ),
            protected_backup_public_ids=tuple(
                candidate.context.backup_public_id for candidate in protected
            ),
            skipped_backup_public_ids=tuple(
                candidate.context.backup_public_id for candidate in skipped
            ),
            generated_at=generated_at.astimezone(UTC),
            policy_identifier=RETENTION_POLICY_IDENTIFIER,
            policy_version=RETENTION_POLICY_VERSION,
            evidence_sha256=fingerprint,
            audit_events=(event,),
        )
        evidence = _PlanEvidence(plan=plan, candidates=candidates, fingerprint=fingerprint)
        with self._state_lock:
            current = self._plans.get(reference.identifier)
            if current is not None and current != evidence:
                raise RetentionPlanError()
            self._plans[reference.identifier] = evidence
            self._plans_by_fingerprint[fingerprint] = reference.identifier
        return plan

    def _execution_result(
        self,
        evidence,
        progress,
        *,
        state,
        current_keep=(),
    ):
        plan = evidence.plan
        deleted = tuple(
            backup_id
            for backup_id in plan.delete_candidate_backup_public_ids
            if backup_id in progress.deleted
        )
        runtime_skipped = set(progress.runtime_skipped)
        skipped = tuple(
            dict.fromkeys(
                plan.protected_backup_public_ids
                + plan.skipped_backup_public_ids
                + tuple(
                    backup_id
                    for backup_id in plan.delete_candidate_backup_public_ids
                    if backup_id in runtime_skipped
                )
            )
        )
        retained = tuple(dict.fromkeys(tuple(current_keep) + plan.retained_backup_public_ids))
        completed_at = self.clock()
        if not _is_aware(completed_at):
            raise RetentionDeleteError()
        return RetentionExecutionResult(
            tenant_public_id=plan.tenant_public_id,
            retention_policy_identifier=plan.policy_identifier,
            retention_class=plan.retention_class,
            keep_count=plan.keep_count,
            eligible_successful_count=plan.successful_eligible_count,
            retained_backup_public_ids=retained,
            deleted_backup_public_ids=deleted,
            skipped_backup_public_ids=skipped,
            failed_deletion_count=progress.failed_deletion_count,
            completed_at=completed_at.astimezone(UTC),
            execution_state=state,
            audit_events=tuple(plan.audit_events) + tuple(progress.audit_events),
        )

    def execute_retention_plan(self, *, plan, current_candidates):
        if (
            type(plan) is not RetentionPlan
            or type(plan.reference) is not RetentionPlanReference
            or type(current_candidates) is not tuple
        ):
            raise Phase2HCoordinationError()
        with self._state_lock:
            evidence = self._plans.get(plan.reference.identifier)
            completed = self._completed_executions.get(plan.reference.identifier)
        if evidence is None or evidence.plan != plan:
            raise Phase2HCoordinationError()
        if completed is not None:
            return completed
        tenant_public_id = plan.tenant_public_id
        with self._state_lock:
            if tenant_public_id in self._active_tenants:
                raise RetentionConcurrencyError()
            self._active_tenants.add(tenant_public_id)
            progress = self._progress.setdefault(
                plan.reference.identifier,
                _ExecutionProgress(set(), set(), set(), 0, []),
            )
        deadline = self.monotonic() + self.policy.timeout_seconds
        failure = False
        current_keep = ()
        try:
            current_by_backup = {}
            current_references = set()
            for candidate in current_candidates:
                self._validate_candidate_binding(tenant_public_id, candidate)
                backup_id = candidate.context.backup_public_id
                reference = candidate.stored_object.reference.identifier
                if backup_id in current_by_backup or reference in current_references:
                    raise RetentionEligibilityError()
                current_by_backup[backup_id] = candidate
                current_references.add(reference)
            original_by_backup = {
                candidate.context.backup_public_id: candidate
                for candidate in evidence.candidates
            }
            for backup_id in tuple(progress.pending_deletions):
                candidate = current_by_backup.get(backup_id)
                original = original_by_backup.get(backup_id)
                if (
                    candidate is None
                    or original is None
                    or candidate.context != original.context
                    or candidate.stored_object != original.stored_object
                    or candidate.retention_class != original.retention_class
                ):
                    failure = True
                    progress.failed_deletion_count += 1
                    progress.audit_events.append(
                        self._event(
                            RetentionAuditEventType.DELETE_FAILED,
                            tenant_public_id,
                            backup_id,
                            "pending_deletion_evidence_changed",
                        )
                    )
                    break
                try:
                    confirmed_absent = (
                        self.durable_provider.confirm_stored_object_absent(
                            context=candidate.context,
                            reference=candidate.stored_object.reference,
                        )
                    )
                    provider_owns_result = (
                        self.durable_provider.owns_stored_object_result(
                            context=candidate.context,
                            result=candidate.stored_object,
                        )
                    )
                except Exception:
                    confirmed_absent = False
                    provider_owns_result = False
                if confirmed_absent:
                    progress.pending_deletions.discard(backup_id)
                    progress.deleted.add(backup_id)
                    progress.audit_events.append(
                        self._event(
                            RetentionAuditEventType.OBJECT_DELETED,
                            tenant_public_id,
                            backup_id,
                            "deleted_and_absent_on_retry",
                        )
                    )
                elif not provider_owns_result:
                    failure = True
                    progress.failed_deletion_count += 1
                    progress.audit_events.append(
                        self._event(
                            RetentionAuditEventType.DELETE_FAILED,
                            tenant_public_id,
                            backup_id,
                            "pending_deletion_state_ambiguous",
                        )
                    )
                    break
            if failure:
                state = (
                    RetentionExecutionState.PARTIALLY_COMPLETED
                    if progress.deleted
                    else RetentionExecutionState.FAILED_SAFE
                )
                progress.audit_events.append(
                    self._event(
                        RetentionAuditEventType.PARTIAL,
                        tenant_public_id,
                        None,
                        state.value.lower(),
                    )
                )
                return self._execution_result(evidence, progress, state=state)

            eligible, _protected, _skipped, reasons = self._assess_candidates(
                tenant_public_id,
                current_candidates,
                already_deleted=progress.deleted,
            )
            current_keep = tuple(
                candidate.context.backup_public_id
                for candidate in eligible[: self.policy.daily_full_keep_count]
            )
            current_delete_set = {
                candidate.context.backup_public_id
                for candidate in eligible[self.policy.daily_full_keep_count :]
            }
            for backup_id in plan.delete_candidate_backup_public_ids:
                if backup_id in progress.deleted or backup_id in progress.runtime_skipped:
                    continue
                self._check_deadline(
                    deadline,
                    error_type=RetentionDeleteValidationError,
                )
                candidate = current_by_backup.get(backup_id)
                original = original_by_backup.get(backup_id)
                if candidate is None or original is None:
                    failure = True
                    progress.failed_deletion_count += 1
                    progress.audit_events.append(
                        self._event(
                            RetentionAuditEventType.DELETE_FAILED,
                            tenant_public_id,
                            backup_id,
                            "candidate_missing",
                        )
                    )
                    break
                if backup_id not in current_delete_set:
                    if (
                        backup_id in progress.pending_deletions
                        and reasons.get(backup_id)
                        in (
                            RetentionSkipReason.CORRUPTED_OR_UNVERIFIED,
                            RetentionSkipReason.DELETED,
                        )
                    ):
                        failure = True
                        progress.failed_deletion_count += 1
                        progress.audit_events.append(
                            self._event(
                                RetentionAuditEventType.DELETE_FAILED,
                                tenant_public_id,
                                backup_id,
                                "pending_deletion_state_ambiguous",
                            )
                        )
                        break
                    progress.pending_deletions.discard(backup_id)
                    progress.runtime_skipped.add(backup_id)
                    continue
                if (
                    candidate.context != original.context
                    or candidate.stored_object != original.stored_object
                    or candidate.retention_class != original.retention_class
                ):
                    failure = True
                    progress.failed_deletion_count += 1
                    progress.audit_events.append(
                        self._event(
                            RetentionAuditEventType.DELETE_FAILED,
                            tenant_public_id,
                            backup_id,
                            "candidate_changed",
                        )
                    )
                    break
                try:
                    if not self.durable_provider.owns_stored_object_result(
                        context=candidate.context,
                        result=candidate.stored_object,
                    ):
                        raise RetentionDeleteValidationError()
                    self.durable_provider.validate_stored_object(
                        context=candidate.context,
                        result=candidate.stored_object,
                    )
                    progress.audit_events.append(
                        self._event(
                            RetentionAuditEventType.DELETE_STARTED,
                            tenant_public_id,
                            backup_id,
                            "delete_started",
                        )
                    )
                    self._run_hook("before_retention_delete", backup_id)
                    progress.pending_deletions.add(backup_id)
                    self.durable_provider.delete_stored_object(
                        context=candidate.context,
                        reference=candidate.stored_object.reference,
                    )
                    if not self.durable_provider.confirm_stored_object_absent(
                        context=candidate.context,
                        reference=candidate.stored_object.reference,
                    ):
                        raise RetentionDeleteError(deletion_incomplete=True)
                    progress.pending_deletions.discard(backup_id)
                    progress.deleted.add(backup_id)
                    progress.audit_events.append(
                        self._event(
                            RetentionAuditEventType.OBJECT_DELETED,
                            tenant_public_id,
                            backup_id,
                            "deleted_and_absent",
                        )
                    )
                except (RetentionEngineError, DurableObjectValidationError, DurableObjectCleanupError):
                    failure = True
                    progress.failed_deletion_count += 1
                    progress.audit_events.append(
                        self._event(
                            RetentionAuditEventType.DELETE_FAILED,
                            tenant_public_id,
                            backup_id,
                            "delete_failed_safe",
                        )
                    )
                    break
                except Exception:
                    failure = True
                    progress.failed_deletion_count += 1
                    progress.audit_events.append(
                        self._event(
                            RetentionAuditEventType.DELETE_FAILED,
                            tenant_public_id,
                            backup_id,
                            "delete_failed_safe",
                        )
                    )
                    break
            if failure:
                state = (
                    RetentionExecutionState.PARTIALLY_COMPLETED
                    if progress.deleted
                    else RetentionExecutionState.FAILED_SAFE
                )
                progress.audit_events.append(
                    self._event(
                        RetentionAuditEventType.PARTIAL,
                        tenant_public_id,
                        None,
                        state.value.lower(),
                    )
                )
            elif not plan.delete_candidate_backup_public_ids:
                state = RetentionExecutionState.NO_ACTION_REQUIRED
                progress.audit_events.append(
                    self._event(
                        RetentionAuditEventType.COMPLETED,
                        tenant_public_id,
                        None,
                        "no_action_required",
                    )
                )
            else:
                state = RetentionExecutionState.COMPLETED
                progress.audit_events.append(
                    self._event(
                        RetentionAuditEventType.COMPLETED,
                        tenant_public_id,
                        None,
                        "completed",
                    )
                )
            result = self._execution_result(
                evidence,
                progress,
                state=state,
                current_keep=current_keep,
            )
            if state in (
                RetentionExecutionState.NO_ACTION_REQUIRED,
                RetentionExecutionState.COMPLETED,
            ):
                with self._state_lock:
                    self._completed_executions[plan.reference.identifier] = result
            return result
        finally:
            with self._state_lock:
                self._active_tenants.discard(tenant_public_id)


__all__ = [
    "BackupRetentionClass",
    "RetentionAuditEvent",
    "RetentionAuditEventType",
    "RetentionCandidate",
    "RetentionEngine",
    "RetentionExecutionResult",
    "RetentionExecutionState",
    "RetentionPlan",
    "RetentionPlanReference",
    "RetentionSkipReason",
]
