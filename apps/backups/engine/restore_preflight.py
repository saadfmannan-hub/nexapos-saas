"""Phase 3A non-mutating restore-preflight coordination."""

from __future__ import annotations

import os
import re
import stat
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from django.conf import settings
from django.utils import timezone

from apps.backups import services
from apps.backups.enums import (
    BackupScope,
    BackupStatus,
    BackupTrigger,
    IntegrityStatus,
    OperationKind,
    ProductOwner,
    RestoreBehavior,
)
from apps.backups.models import BackupRecord
from apps.backups.registry import COMPONENT_REGISTRY, ComponentRegistry
from apps.backups.versioning import (
    BACKUP_FORMAT_VERSION,
    get_application_version,
    schema_migration_fingerprint,
)
from apps.tenants.models import Business

from .context import ActorIdentitySnapshot, BackupExecutionContext
from .contracts import (
    PackageBuildResult,
    PackageCompatibilityStatus,
    PackageVerificationRequest,
    PackageVerificationResult,
    PersistedStoredObjectDescriptor,
    StoredBackupObjectReference,
)
from .durable_storage import (
    LOCAL_DURABLE_STORAGE_BACKEND_IDENTIFIER,
    LOCAL_DURABLE_STORAGE_PROVIDER_IDENTIFIER,
    LocalPrivateDurableStorageProvider,
)
from .durable_storage_exceptions import (
    DurableObjectNotFound,
    DurableStorageEngineError,
)
from .encrypted_artifact import (
    ENCRYPTED_ARTIFACT_PROVIDER_IDENTIFIER,
    EncryptedArtifactProvider,
)
from .encryption_exceptions import EncryptedArtifactValidationError
from .package_exceptions import PackageNotFound, PackageValidationError
from .package_verification import IndependentPackageVerifier, PackageCompatibilityPolicy
from .restore_exceptions import (
    Phase3ACoordinationError,
    RestoreCompatibilityError,
    RestoreComponentPlanError,
    RestoreDecryptError,
    RestoreDurableObjectError,
    RestoreEngineError,
    RestoreLockLost,
    RestoreLockUnavailable,
    RestorePackageVerificationError,
    RestorePreflightCleanupError,
    RestoreSelectionError,
    RestoreTenantMismatch,
)
from .restore_workspace import (
    RESTORED_PACKAGE_PROVIDER_IDENTIFIER,
    RestoredPackageProvider,
)
from .runtime import RuntimeProviderStack, build_runtime_provider_stack
from .verification_exceptions import (
    Phase2EEngineError,
)
from .workspace import (
    BackupWorkspaceManager,
    WorkspaceArea,
    WorkspaceReference,
    path_is_link_like,
)

RESTORE_PREFLIGHT_PROVIDER_STACK_VERSION = "nexa.restore-preflight-stack.v1"
RESTORE_PREFLIGHT_EVIDENCE_SCHEMA = "nexa.restore-preflight.v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class RestorePreflightState(StrEnum):
    RESTORE_READY = "RESTORE_READY"
    NOT_RESTORE_READY = "NOT_RESTORE_READY"


@dataclass(frozen=True, slots=True)
class RestorePreflightReference:
    identifier: uuid.UUID


@dataclass(frozen=True, slots=True)
class RestorePreflightRequest:
    operation_public_id: uuid.UUID
    business_public_id: uuid.UUID
    backup_public_id: uuid.UUID
    actor_identity: ActorIdentitySnapshot
    idempotency_key: str
    worker_task_identifier: str = ""


@dataclass(frozen=True, slots=True)
class RestorePreflightCleanupRequest:
    operation_public_id: uuid.UUID
    business_public_id: uuid.UUID
    backup_public_id: uuid.UUID
    preflight_reference: RestorePreflightReference


@dataclass(frozen=True, slots=True)
class RestoreComponentPlanItem:
    component_key: str
    component_version: str
    restore_behavior: RestoreBehavior
    import_order: int
    dependencies: tuple[str, ...]
    model_sequence: tuple[str, ...]
    record_count: int
    media_reference_count: int


@dataclass(frozen=True, slots=True)
class RestorePreflightResult:
    operation_reference: uuid.UUID
    preflight_reference: RestorePreflightReference | None
    backup_public_id: uuid.UUID
    business_public_id: uuid.UUID
    state: RestorePreflightState
    restore_ready: bool
    compatibility_status: PackageCompatibilityStatus
    component_count: int
    record_count: int
    media_object_count: int
    plaintext_package_bytes: int
    verified_at: datetime
    preflight_completed_at: datetime
    durable_provider_identifier: str
    encryption_provider_identifier: str
    verification_provider_identifier: str
    package_provider_identifier: str
    issue_codes: tuple[str, ...]
    component_plan: tuple[RestoreComponentPlanItem, ...]


@dataclass(frozen=True, slots=True)
class RestorePreflightProviderStack:
    workspace_manager: BackupWorkspaceManager
    encrypted_artifact_provider: EncryptedArtifactProvider
    durable_storage_provider: LocalPrivateDurableStorageProvider
    restored_package_provider: RestoredPackageProvider
    verification_provider: IndependentPackageVerifier

    def validated(self):
        if (
            type(self.workspace_manager) is not BackupWorkspaceManager
            or type(self.encrypted_artifact_provider) is not EncryptedArtifactProvider
            or type(self.durable_storage_provider)
            is not LocalPrivateDurableStorageProvider
            or type(self.restored_package_provider) is not RestoredPackageProvider
            or type(self.verification_provider) is not IndependentPackageVerifier
            or self.encrypted_artifact_provider.workspace_manager
            is not self.workspace_manager
            or self.durable_storage_provider.encrypted_artifact_provider
            is not self.encrypted_artifact_provider
            or self.restored_package_provider.workspace_manager
            is not self.workspace_manager
            or self.verification_provider.package_provider
            is not self.restored_package_provider
            or self.verification_provider.workspace_manager
            is not self.workspace_manager
            or type(self.verification_provider.compatibility_policy)
            is not PackageCompatibilityPolicy
            or not _SHA256_PATTERN.fullmatch(
                self.verification_provider.compatibility_policy.current_schema_fingerprint
            )
            or not self.verification_provider.compatibility_policy.current_application_version
            or self.verification_provider.compatibility_policy.current_backup_format_version
            != BACKUP_FORMAT_VERSION
        ):
            raise Phase3ACoordinationError()
        return self


@dataclass(frozen=True, slots=True)
class _CompletedPreflight:
    request: RestorePreflightRequest
    context: BackupExecutionContext
    package: PackageBuildResult
    verification: PackageVerificationResult
    result: RestorePreflightResult
    document: dict


@dataclass(frozen=True, slots=True)
class RestorePreflightConsumption:
    """Internal identity-bound handoff from Phase 3A to the mutation engine."""

    context: BackupExecutionContext
    package: PackageBuildResult
    verification: PackageVerificationResult
    result: RestorePreflightResult
    document: dict


def build_restore_preflight_provider_stack(*, runtime_stack=None):
    """Compose a restart-clean retrieval stack without enabling restore mutation."""

    try:
        if runtime_stack is None:
            runtime_stack = build_runtime_provider_stack()
        elif type(runtime_stack) is not RuntimeProviderStack:
            raise Phase3ACoordinationError()
        runtime_stack = runtime_stack.validated()
        restored_package_provider = RestoredPackageProvider(
            workspace_manager=runtime_stack.workspace_manager,
        )
        verification_provider = IndependentPackageVerifier(
            package_provider=restored_package_provider,
            workspace_manager=runtime_stack.workspace_manager,
            compatibility_policy=PackageCompatibilityPolicy(
                current_schema_fingerprint=schema_migration_fingerprint(),
                current_application_version=get_application_version(),
                current_backup_format_version=BACKUP_FORMAT_VERSION,
            ),
        )
        return RestorePreflightProviderStack(
            workspace_manager=runtime_stack.workspace_manager,
            encrypted_artifact_provider=runtime_stack.encrypted_artifact_provider,
            durable_storage_provider=runtime_stack.durable_storage_provider,
            restored_package_provider=restored_package_provider,
            verification_provider=verification_provider,
        ).validated()
    except RestoreEngineError:
        raise
    except Exception:
        raise Phase3ACoordinationError() from None


class RestorePreflightCoordinator:
    """Retrieve, authenticate, verify, and privately expand one selected backup."""

    def __init__(
        self,
        *,
        provider_stack,
        component_registry=COMPONENT_REGISTRY,
        clock=None,
        lock_lease_seconds=None,
    ):
        if type(provider_stack) is not RestorePreflightProviderStack:
            raise Phase3ACoordinationError()
        self.provider_stack = provider_stack.validated()
        if (
            type(component_registry) is not ComponentRegistry
            or component_registry is not COMPONENT_REGISTRY
        ):
            raise Phase3ACoordinationError()
        self.component_registry = component_registry
        self.clock = clock or timezone.now
        selected_lease = (
            getattr(settings, "BACKUP_EXECUTION_LOCK_LEASE_SECONDS", 21_600)
            if lock_lease_seconds is None
            else lock_lease_seconds
        )
        if type(selected_lease) is not int or not 300 <= selected_lease <= 86_400:
            raise Phase3ACoordinationError()
        self.lock_lease_seconds = selected_lease
        self._completed = {}
        self._cleaned = {}
        self._state_lock = threading.RLock()

    @staticmethod
    def _validate_request(request):
        if (
            type(request) is not RestorePreflightRequest
            or type(request.operation_public_id) is not uuid.UUID
            or type(request.business_public_id) is not uuid.UUID
            or type(request.backup_public_id) is not uuid.UUID
            or type(request.actor_identity) is not ActorIdentitySnapshot
            or type(request.idempotency_key) is not str
            or not request.idempotency_key
            or len(request.idempotency_key) > 128
            or type(request.worker_task_identifier) is not str
            or len(request.worker_task_identifier) > 255
        ):
            raise RestoreSelectionError(issue_code="restore_request_invalid")

    @staticmethod
    def _resolve_business(request):
        business = Business.objects.filter(public_id=request.business_public_id).first()
        if business is None:
            raise RestoreTenantMismatch(issue_code="restore_selection_unavailable")
        return business

    @staticmethod
    def _resolve_backup(*, business, request):
        backup = (
            BackupRecord.objects.for_business(business)
            .select_related("business")
            .filter(public_id=request.backup_public_id)
            .first()
        )
        if backup is None:
            raise RestoreTenantMismatch(issue_code="restore_selection_unavailable")
        try:
            reference_uuid = uuid.UUID(str(backup.opaque_object_key))
        except (AttributeError, TypeError, ValueError):
            raise RestoreSelectionError(issue_code="durable_reference_invalid") from None
        if (
            backup.business_id != business.pk
            or backup.tenant_public_id_snapshot != business.public_id
            or backup.status != BackupStatus.SUCCEEDED
            or backup.integrity_status != IntegrityStatus.VERIFIED
            or backup.deleted_at is not None
            or backup.storage_backend_identifier
            != LOCAL_DURABLE_STORAGE_BACKEND_IDENTIFIER
            or str(reference_uuid) != backup.opaque_object_key
            or type(backup.backup_size_bytes) is not int
            or backup.backup_size_bytes <= 0
            or type(backup.whole_artifact_hash) is not str
            or not _SHA256_PATTERN.fullmatch(backup.whole_artifact_hash)
            or type(backup.encryption_key_identifier) is not str
            or not backup.encryption_key_identifier
            or any(
                not str(value or "").strip()
                for value in (
                    backup.format_version,
                    backup.application_version,
                    backup.schema_fingerprint,
                    backup.minimum_restore_version,
                )
            )
        ):
            raise RestoreSelectionError(issue_code="backup_not_restore_selectable")
        return backup, StoredBackupObjectReference(reference_uuid)

    @staticmethod
    def _build_context(*, business, backup, request, workspace_reference):
        try:
            products = tuple(ProductOwner(value) for value in backup.included_products)
            scope = BackupScope(backup.scope)
            trigger = BackupTrigger(backup.trigger)
        except (TypeError, ValueError):
            raise RestoreSelectionError(issue_code="backup_metadata_invalid") from None
        if not products or len(products) != len(set(products)):
            raise RestoreSelectionError(issue_code="backup_metadata_invalid")
        return BackupExecutionContext(
            backup_public_id=backup.public_id,
            business_id=business.pk,
            business_public_id=business.public_id,
            requested_scope=scope,
            resolved_products=products,
            trigger_type=trigger,
            actor_identity=request.actor_identity,
            application_version=backup.application_version,
            backup_format_version=backup.format_version,
            schema_migration_fingerprint=backup.schema_fingerprint,
            minimum_restore_version=backup.minimum_restore_version,
            idempotency_key=request.idempotency_key,
            operation_correlation_id=request.operation_public_id,
            workspace_reference=workspace_reference,
        )

    def _heartbeat(self, lock):
        if not services.heartbeat_tenant_operation_lock(
            lock,
            lock_token=lock.lock_token,
            lease_seconds=self.lock_lease_seconds,
        ):
            raise RestoreLockLost(issue_code="restore_lock_lost")

    @staticmethod
    def _component_plan(document, *, registry):
        try:
            components = document["components"]
            if type(components) is not list or not components:
                raise ValueError
            manifest_by_key = {}
            for item in components:
                if type(item) is not dict or type(item.get("key")) is not str:
                    raise ValueError
                key = item["key"]
                if key in manifest_by_key:
                    raise ValueError
                definition = registry.get(key)
                if (
                    item.get("component_version") != definition.component_version
                    or item.get("restore_behavior") != definition.restore_behavior
                    or item.get("required_component_keys")
                    != list(definition.required_component_keys)
                    or item.get("import_order") != definition.import_order
                ):
                    raise ValueError
                manifest_by_key[key] = item
            selected = frozenset(manifest_by_key)
            for key in selected:
                if not set(registry.get(key).required_component_keys).issubset(selected):
                    raise ValueError

            ordered_keys = []
            visiting = set()
            visited = set()

            def visit(key):
                if key in visited:
                    return
                if key in visiting:
                    raise ValueError
                visiting.add(key)
                definition = registry.get(key)
                for dependency in sorted(
                    definition.required_component_keys,
                    key=lambda value: (registry.get(value).import_order, value),
                ):
                    visit(dependency)
                visiting.remove(key)
                visited.add(key)
                ordered_keys.append(key)

            for key in sorted(
                selected,
                key=lambda value: (registry.get(value).import_order, value),
            ):
                visit(key)
            plan = []
            for key in ordered_keys:
                definition = registry.get(key)
                item = manifest_by_key[key]
                models = item.get("models")
                records = item.get("records")
                media_index = item.get("media_index")
                if (
                    type(models) is not list
                    or type(records) is not dict
                    or type(media_index) is not dict
                    or any(type(model) is not dict for model in models)
                ):
                    raise ValueError
                model_sequence = tuple(model.get("model") for model in models)
                if model_sequence != definition.included_model_labels:
                    raise ValueError
                record_count = records.get("record_count")
                media_reference_count = media_index.get("reference_count")
                if (
                    type(record_count) is not int
                    or record_count < 0
                    or type(media_reference_count) is not int
                    or media_reference_count < 0
                ):
                    raise ValueError
                plan.append(
                    RestoreComponentPlanItem(
                        component_key=key,
                        component_version=definition.component_version,
                        restore_behavior=RestoreBehavior(definition.restore_behavior),
                        import_order=definition.import_order,
                        dependencies=tuple(definition.required_component_keys),
                        model_sequence=model_sequence,
                        record_count=record_count,
                        media_reference_count=media_reference_count,
                    )
                )
            return tuple(plan)
        except RestoreComponentPlanError:
            raise
        except Exception:
            raise RestoreComponentPlanError(issue_code="restore_component_plan_invalid") from None

    @staticmethod
    def _safe_issue_codes(verification):
        codes = []
        for issue in verification.issues:
            code = str(getattr(issue, "code", ""))[:80]
            if code and code not in codes:
                codes.append(code)
        return tuple(codes[:50])

    def _result(
        self,
        *,
        request,
        backup,
        verification,
        package,
        document=None,
        component_plan=(),
        ready,
    ):
        completed_at = self.clock()
        if (
            type(completed_at) is not datetime
            or completed_at.tzinfo is None
            or completed_at.utcoffset() is None
        ):
            raise Phase3ACoordinationError()
        totals = document.get("totals", {}) if type(document) is dict else {}
        preflight_reference = (
            RestorePreflightReference(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"nexa-restore-preflight:{request.operation_public_id}",
                )
            )
            if ready
            else None
        )
        return RestorePreflightResult(
            operation_reference=request.operation_public_id,
            preflight_reference=preflight_reference,
            backup_public_id=backup.public_id,
            business_public_id=backup.tenant_public_id_snapshot,
            state=(
                RestorePreflightState.RESTORE_READY
                if ready
                else RestorePreflightState.NOT_RESTORE_READY
            ),
            restore_ready=ready,
            compatibility_status=verification.compatibility_status,
            component_count=(
                totals.get("component_count", backup.component_count)
                if ready
                else backup.component_count
            ),
            record_count=(
                totals.get("record_count", backup.total_row_count)
                if ready
                else backup.total_row_count
            ),
            media_object_count=(
                totals.get("unique_media_object_count", backup.media_count)
                if ready
                else backup.media_count
            ),
            plaintext_package_bytes=package.byte_count,
            verified_at=verification.verified_at,
            preflight_completed_at=completed_at,
            durable_provider_identifier=LOCAL_DURABLE_STORAGE_PROVIDER_IDENTIFIER,
            encryption_provider_identifier=ENCRYPTED_ARTIFACT_PROVIDER_IDENTIFIER,
            verification_provider_identifier=verification.provider_identifier,
            package_provider_identifier=RESTORED_PACKAGE_PROVIDER_IDENTIFIER,
            issue_codes=self._safe_issue_codes(verification),
            component_plan=tuple(component_plan),
        )

    @staticmethod
    def _preflight_evidence_document(result):
        return {
            "schema": RESTORE_PREFLIGHT_EVIDENCE_SCHEMA,
            "operation_public_id": str(result.operation_reference),
            "preflight_reference": str(result.preflight_reference.identifier),
            "backup_public_id": str(result.backup_public_id),
            "tenant_public_id": str(result.business_public_id),
            "state": result.state.value,
            "restore_ready": result.restore_ready,
            "compatibility_status": result.compatibility_status.value,
            "component_count": result.component_count,
            "record_count": result.record_count,
            "media_object_count": result.media_object_count,
            "plaintext_package_bytes": result.plaintext_package_bytes,
            "verified_timestamp": result.verified_at.isoformat(),
            "preflight_completed_timestamp": result.preflight_completed_at.isoformat(),
            "verification_provider": result.verification_provider_identifier,
            "package_provider": result.package_provider_identifier,
            "issue_codes": list(result.issue_codes),
        }

    def _cleanup_attempt(self, *, context, package, verification):
        error = None
        if verification is not None and verification.reference is not None:
            try:
                self.provider_stack.verification_provider.cleanup_verification_evidence(
                    context=context,
                    reference=verification.reference,
                )
                verification_area = self.provider_stack.workspace_manager.handle(
                    context.workspace_reference
                ).system_area_path(WorkspaceArea.VERIFICATION)
                if os.path.lexists(verification_area):
                    current = os.stat(verification_area, follow_symlinks=False)
                    if (
                        path_is_link_like(verification_area)
                        or not stat.S_ISDIR(current.st_mode)
                    ):
                        raise RestorePreflightCleanupError()
                    with os.scandir(verification_area) as contents:
                        if next(contents, None) is not None:
                            raise RestorePreflightCleanupError()
                    os.rmdir(verification_area)
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                raise
            except Exception as exc:
                error = exc
        if package is not None:
            try:
                self.provider_stack.restored_package_provider.cleanup_workspace(
                    context=context,
                    package=package,
                )
                self._cleanup_empty_workspace(
                    self.provider_stack.workspace_manager.handle(
                        context.workspace_reference
                    )
                )
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                raise
            except Exception as exc:
                error = error or exc
        if error is not None:
            raise RestorePreflightCleanupError() from None

    def _cleanup_empty_workspace(self, workspace):
        if workspace is None or not os.path.lexists(workspace.path):
            return
        if path_is_link_like(workspace.path):
            raise RestorePreflightCleanupError()
        try:
            with os.scandir(workspace.path) as contents:
                if next(contents, None) is not None:
                    raise RestorePreflightCleanupError()
            self.provider_stack.workspace_manager.cleanup(workspace.reference)
        except RestorePreflightCleanupError:
            raise
        except Exception:
            raise RestorePreflightCleanupError() from None

    @staticmethod
    def _safe_exception(exc):
        if isinstance(exc, RestoreEngineError):
            return exc
        if isinstance(exc, (DurableStorageEngineError, DurableObjectNotFound)):
            return RestoreDurableObjectError(issue_code="durable_object_invalid")
        if isinstance(exc, EncryptedArtifactValidationError):
            return RestoreDecryptError(issue_code="encrypted_artifact_invalid")
        if isinstance(exc, (PackageValidationError, PackageNotFound, Phase2EEngineError)):
            return RestorePackageVerificationError(issue_code="restored_package_invalid")
        return Phase3ACoordinationError()

    def run(self, request):
        self._validate_request(request)
        with self._state_lock:
            for completed in self._completed.values():
                if completed.request == request:
                    return completed.result
            for cleaned in self._cleaned.values():
                if cleaned.request == request:
                    raise RestoreSelectionError(
                        issue_code="restore_preflight_expired"
                    )
            if any(
                completed.request.operation_public_id == request.operation_public_id
                for completed in (*self._completed.values(), *self._cleaned.values())
            ):
                raise RestoreSelectionError(issue_code="restore_idempotency_conflict")

        business = self._resolve_business(request)
        lock = None
        workspace = None
        context = None
        package = None
        verification = None
        reattested = None
        result = None
        retain_workspace = False
        safe_error = None
        abort_error = None
        abort_traceback = None
        try:
            try:
                lock = services.acquire_tenant_operation_lock(
                    business=business,
                    operation_kind=OperationKind.RESTORE,
                    operation_public_id=request.operation_public_id,
                    worker_task_identifier=request.worker_task_identifier,
                    lease_seconds=self.lock_lease_seconds,
                )
            except services.TenantOperationLocked:
                raise RestoreLockUnavailable(issue_code="restore_lock_unavailable") from None
            backup, stored_reference = self._resolve_backup(
                business=business,
                request=request,
            )
            workspace = self.provider_stack.workspace_manager.create(
                WorkspaceReference(request.operation_public_id)
            )
            context = self._build_context(
                business=business,
                backup=backup,
                request=request,
                workspace_reference=workspace.reference,
            )
            descriptor = PersistedStoredObjectDescriptor(
                reference=stored_reference,
                backend_identifier=backup.storage_backend_identifier,
                byte_count=backup.backup_size_bytes,
                sha256=backup.whole_artifact_hash,
                backup_public_id=backup.public_id,
                tenant_public_id=business.public_id,
            )
            reattested = self.provider_stack.durable_storage_provider.reattest_stored_object(
                context=context,
                descriptor=descriptor,
            )
            self._heartbeat(lock)
            with self.provider_stack.durable_storage_provider.open_reattested_object(
                context=context,
                result=reattested,
            ) as encrypted_reader:
                with self.provider_stack.encrypted_artifact_provider.open_restored_plaintext(
                    context=context,
                    reader=encrypted_reader,
                    encrypted_byte_count=reattested.byte_count,
                    ciphertext_sha256=reattested.sha256,
                    encryption_key_identifier=backup.encryption_key_identifier,
                    encrypted_data_key_envelope=backup.encrypted_data_key_envelope,
                ) as (plaintext_reader, plaintext_evidence):
                    package = (
                        self.provider_stack.restored_package_provider.publish_plaintext(
                            context=context,
                            reader=plaintext_reader,
                            plaintext_evidence=plaintext_evidence,
                        )
                    )
            self._heartbeat(lock)
            verification = self.provider_stack.verification_provider.verify(
                PackageVerificationRequest(
                    context=context,
                    package=package,
                )
            )
            if verification.verified is not True:
                raise RestorePackageVerificationError(
                    issue_code=(
                        self._safe_issue_codes(verification)[0]
                        if self._safe_issue_codes(verification)
                        else "restored_package_invalid"
                    )
                )
            self._heartbeat(lock)
            if (
                verification.restore_ready is not True
                or verification.compatibility_status
                is not PackageCompatibilityStatus.COMPATIBLE
            ):
                result = self._result(
                    request=request,
                    backup=backup,
                    verification=verification,
                    package=package,
                    ready=False,
                )
            else:
                document = (
                    self.provider_stack.restored_package_provider.extract_verified_package(
                        context=context,
                        package=package,
                        verification=verification,
                    )
                )
                component_plan = self._component_plan(
                    document,
                    registry=self.component_registry,
                )
                self._heartbeat(lock)
                result = self._result(
                    request=request,
                    backup=backup,
                    verification=verification,
                    package=package,
                    document=document,
                    component_plan=component_plan,
                    ready=True,
                )
                self.provider_stack.restored_package_provider.publish_preflight_evidence(
                    context=context,
                    package=package,
                    document=self._preflight_evidence_document(result),
                )
                retain_workspace = True
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                abort_error = exc
                abort_traceback = exc.__traceback__
            else:
                safe_error = self._safe_exception(exc)
        finally:
            if reattested is not None and context is not None:
                try:
                    self.provider_stack.durable_storage_provider.release_reattested_object(
                        context=context,
                        result=reattested,
                    )
                except BaseException as exc:
                    if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                        abort_error = abort_error or exc
                        abort_traceback = abort_traceback or exc.__traceback__
                    elif safe_error is None:
                        safe_error = RestoreDurableObjectError(
                            issue_code="durable_attestation_release_failed"
                        )
            if lock is not None:
                try:
                    released = services.release_tenant_operation_lock(
                        lock,
                        lock_token=lock.lock_token,
                    )
                    if not released and safe_error is None and abort_error is None:
                        safe_error = RestoreLockLost(issue_code="restore_lock_release_failed")
                except BaseException as exc:
                    if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                        abort_error = abort_error or exc
                        abort_traceback = abort_traceback or exc.__traceback__
                    elif safe_error is None:
                        safe_error = RestoreLockLost(issue_code="restore_lock_release_failed")

            should_clean = (
                not retain_workspace
                or safe_error is not None
                or abort_error is not None
            )
            if should_clean:
                try:
                    if context is not None and package is not None:
                        self._cleanup_attempt(
                            context=context,
                            package=package,
                            verification=verification,
                        )
                    elif workspace is not None:
                        self._cleanup_empty_workspace(workspace)
                except BaseException as exc:
                    if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                        abort_error = abort_error or exc
                        abort_traceback = abort_traceback or exc.__traceback__
                    elif safe_error is None:
                        safe_error = RestorePreflightCleanupError()

        if abort_error is not None:
            raise abort_error.with_traceback(abort_traceback)
        if safe_error is not None:
            safe_error.__cause__ = None
            safe_error.__context__ = None
            raise safe_error.with_traceback(None) from None
        if result is None:
            raise Phase3ACoordinationError()
        if result.restore_ready:
            completed = _CompletedPreflight(
                request=request,
                context=context,
                package=package,
                verification=verification,
                result=result,
                document=document,
            )
            with self._state_lock:
                reference = result.preflight_reference.identifier
                if reference in self._completed or reference in self._cleaned:
                    self._cleanup_attempt(
                        context=context,
                        package=package,
                        verification=verification,
                    )
                    raise Phase3ACoordinationError()
                self._completed[reference] = completed
        return result

    def revalidate_for_execution(
        self,
        *,
        operation_public_id,
        business_public_id,
        backup_public_id,
        actor_identity,
        approved_result,
    ):
        """Re-attest a retained preflight under the caller's restore lease."""

        if (
            type(operation_public_id) is not uuid.UUID
            or type(business_public_id) is not uuid.UUID
            or type(backup_public_id) is not uuid.UUID
            or type(actor_identity) is not ActorIdentitySnapshot
            or type(approved_result) is not RestorePreflightResult
            or approved_result.restore_ready is not True
            or approved_result.state is not RestorePreflightState.RESTORE_READY
            or approved_result.preflight_reference is None
            or approved_result.operation_reference != operation_public_id
            or approved_result.business_public_id != business_public_id
            or approved_result.backup_public_id != backup_public_id
            or approved_result.compatibility_status
            is not PackageCompatibilityStatus.COMPATIBLE
        ):
            raise RestoreSelectionError(issue_code="restore_preflight_invalid")
        reference = approved_result.preflight_reference.identifier
        with self._state_lock:
            completed = self._completed.get(reference)
        if (
            completed is None
            or completed.result != approved_result
            or completed.request.operation_public_id != operation_public_id
            or completed.request.business_public_id != business_public_id
            or completed.request.backup_public_id != backup_public_id
            or completed.request.actor_identity != actor_identity
            or completed.context.operation_correlation_id != operation_public_id
            or completed.context.business_public_id != business_public_id
            or completed.context.backup_public_id != backup_public_id
        ):
            raise RestoreSelectionError(issue_code="restore_preflight_replaced")
        business = self._resolve_business(completed.request)
        backup, stored_reference = self._resolve_backup(
            business=business,
            request=completed.request,
        )
        policy = self.provider_stack.verification_provider.compatibility_policy
        if (
            policy.current_schema_fingerprint != schema_migration_fingerprint()
            or policy.current_application_version != get_application_version()
            or policy.current_backup_format_version != BACKUP_FORMAT_VERSION
            or completed.verification.verified is not True
            or completed.verification.restore_ready is not True
            or completed.verification.compatibility_status
            is not PackageCompatibilityStatus.COMPATIBLE
        ):
            raise RestoreCompatibilityError(issue_code="restore_compatibility_changed")
        try:
            self.provider_stack.restored_package_provider.validate_consumable_preflight(
                context=completed.context,
                package=completed.package,
                document=self._preflight_evidence_document(completed.result),
            )
            self.provider_stack.verification_provider.validate_verification_evidence(
                context=completed.context,
                package=completed.package,
                result=completed.verification,
            )
            descriptor = PersistedStoredObjectDescriptor(
                reference=stored_reference,
                backend_identifier=backup.storage_backend_identifier,
                byte_count=backup.backup_size_bytes,
                sha256=backup.whole_artifact_hash,
                backup_public_id=backup.public_id,
                tenant_public_id=business.public_id,
            )
            reattested = self.provider_stack.durable_storage_provider.reattest_stored_object(
                context=completed.context,
                descriptor=descriptor,
            )
            try:
                self.provider_stack.durable_storage_provider.validate_reattested_object(
                    context=completed.context,
                    result=reattested,
                )
            finally:
                self.provider_stack.durable_storage_provider.release_reattested_object(
                    context=completed.context,
                    result=reattested,
                )
        except RestoreEngineError:
            raise
        except (DurableStorageEngineError, Phase2EEngineError, PackageValidationError):
            raise RestoreSelectionError(issue_code="restore_preflight_revalidation_failed") from None
        except Exception:
            raise RestoreSelectionError(issue_code="restore_preflight_revalidation_failed") from None
        return RestorePreflightConsumption(
            context=completed.context,
            package=completed.package,
            verification=completed.verification,
            result=completed.result,
            document=completed.document,
        )

    def cleanup_consumed_preflight(self, consumption):
        """Clean an exact consumed preflight while the Phase 3B lease is held."""

        if type(consumption) is not RestorePreflightConsumption:
            raise RestorePreflightCleanupError()
        reference = consumption.result.preflight_reference.identifier
        with self._state_lock:
            completed = self._completed.get(reference)
            cleaned = self._cleaned.get(reference)
        if cleaned is not None:
            return False
        if (
            completed is None
            or completed.context != consumption.context
            or completed.package != consumption.package
            or completed.verification != consumption.verification
            or completed.result != consumption.result
            or completed.document != consumption.document
        ):
            raise RestorePreflightCleanupError()
        self._cleanup_attempt(
            context=completed.context,
            package=completed.package,
            verification=completed.verification,
        )
        with self._state_lock:
            if self._completed.get(reference) != completed:
                raise RestorePreflightCleanupError()
            del self._completed[reference]
            self._cleaned[reference] = completed
        return True

    def cleanup_restore_preflight(self, request):
        if (
            type(request) is not RestorePreflightCleanupRequest
            or type(request.operation_public_id) is not uuid.UUID
            or type(request.business_public_id) is not uuid.UUID
            or type(request.backup_public_id) is not uuid.UUID
            or type(request.preflight_reference) is not RestorePreflightReference
            or type(request.preflight_reference.identifier) is not uuid.UUID
        ):
            raise RestorePreflightCleanupError()
        reference = request.preflight_reference.identifier
        with self._state_lock:
            completed = self._completed.get(reference)
            cleaned = self._cleaned.get(reference)
        if cleaned is not None:
            if (
                cleaned.request.operation_public_id != request.operation_public_id
                or cleaned.result.business_public_id != request.business_public_id
                or cleaned.result.backup_public_id != request.backup_public_id
            ):
                raise RestorePreflightCleanupError()
            return False
        if (
            completed is None
            or completed.request.operation_public_id != request.operation_public_id
            or completed.result.business_public_id != request.business_public_id
            or completed.result.backup_public_id != request.backup_public_id
            or completed.result.preflight_reference != request.preflight_reference
        ):
            raise RestorePreflightCleanupError()
        business = Business.objects.filter(public_id=request.business_public_id).first()
        if business is None:
            raise RestorePreflightCleanupError()
        lock = None
        try:
            try:
                lock = services.acquire_tenant_operation_lock(
                    business=business,
                    operation_kind=OperationKind.RESTORE,
                    operation_public_id=request.operation_public_id,
                    lease_seconds=self.lock_lease_seconds,
                )
            except services.TenantOperationLocked:
                raise RestoreLockUnavailable(issue_code="restore_cleanup_lock_unavailable") from None
            self._cleanup_attempt(
                context=completed.context,
                package=completed.package,
                verification=completed.verification,
            )
            with self._state_lock:
                if self._completed.get(reference) != completed:
                    raise RestorePreflightCleanupError()
                del self._completed[reference]
                self._cleaned[reference] = completed
            return True
        finally:
            if lock is not None:
                try:
                    if not services.release_tenant_operation_lock(
                        lock,
                        lock_token=lock.lock_token,
                    ):
                        raise RestoreLockLost(issue_code="restore_cleanup_lock_release_failed")
                except RestoreEngineError:
                    raise
                except Exception:
                    raise RestoreLockLost(
                        issue_code="restore_cleanup_lock_release_failed"
                    ) from None


__all__ = [
    "RESTORE_PREFLIGHT_EVIDENCE_SCHEMA",
    "RESTORE_PREFLIGHT_PROVIDER_STACK_VERSION",
    "RestoreComponentPlanItem",
    "RestorePreflightCleanupRequest",
    "RestorePreflightConsumption",
    "RestorePreflightCoordinator",
    "RestorePreflightProviderStack",
    "RestorePreflightReference",
    "RestorePreflightRequest",
    "RestorePreflightResult",
    "RestorePreflightState",
    "build_restore_preflight_provider_stack",
]
