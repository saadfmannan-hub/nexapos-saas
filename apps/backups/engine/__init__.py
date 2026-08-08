"""Nexa backup engine foundations through the Phase 2J async boundary.

The public surface remains deliberately non-operational. It exposes typed
contracts, immutable planning metadata, and central availability guards.
"""

from .availability import (
    ASYNC_EXECUTION_BOUNDARY_READY,
    CANONICAL_MANIFEST_PROVIDER_READY,
    DETERMINISTIC_PACKAGE_PROVIDER_READY,
    DURABLE_STORAGE_PROVIDER_READY,
    ENCRYPTED_ARTIFACT_PROVIDER_READY,
    INDEPENDENT_PACKAGE_VERIFIER_READY,
    MEDIA_CAPTURE_PROVIDER_READY,
    RETENTION_ENGINE_READY,
    RUNTIME_COMPOSITION_READY,
    RUNTIME_ORCHESTRATOR_READY,
    SCHEDULE_DISPATCHER_READY,
    SQLITE_SNAPSHOT_PROVIDER_READY,
    TENANT_LOGICAL_EXPORT_PROVIDER_READY,
    assert_real_execution_available,
    async_configuration_ready,
    get_engine_capability,
    real_execution_available,
)
from .canonical_manifest import CanonicalManifestProvider
from .context import ActorIdentitySnapshot, BackupExecutionContext
from .contracts import (
    DurableBackupStorageProvider,
    EncryptedArtifactReference,
    EncryptedArtifactRequest,
    EncryptedArtifactResult,
    PackageBuildRequest,
    PackageBuildResult,
    PackageCompatibilityStatus,
    PackageVerificationRequest,
    PackageVerificationResult,
    Phase2D1Request,
    Phase2D1Result,
    Phase2D2Request,
    Phase2D2Result,
    RestoreReadinessResult,
    StoredBackupObjectReference,
    StoredBackupObjectRequest,
    StoredBackupObjectResult,
    StoredObjectDurabilityState,
    StoredObjectVerificationState,
    VerificationIssue,
    VerificationReference,
)
from .deterministic_package import DeterministicPackageProvider
from .durable_storage import LocalPrivateDurableStorageProvider
from .durable_storage_policy import DurableStoragePolicy
from .encrypted_artifact import EncryptedArtifactProvider
from .encryption_policy import EncryptionPolicy
from .exceptions import BackupEngineDisabled, BackupEngineError
from .key_management import KekProvider, LocalConfiguredKekProvider, WrappedDek
from .logical_export import (
    SQLiteLogicalComponentExporter,
    export_snapshot_components,
)
from .media_capture import LocalFilesystemMediaCaptureProvider
from .orchestration import prepare_backup_execution
from .package_verification import IndependentPackageVerifier
from .phase2d1 import Phase2D1Coordinator
from .phase2d2 import Phase2D2Coordinator
from .pipeline import BackupExecutionPlan, PipelineStage
from .retention import (
    BackupRetentionClass,
    RetentionAuditEvent,
    RetentionAuditEventType,
    RetentionCandidate,
    RetentionEngine,
    RetentionExecutionResult,
    RetentionExecutionState,
    RetentionPlan,
    RetentionPlanReference,
    RetentionSkipReason,
)
from .retention_policy import RetentionPolicy
from .runtime import (
    BackupExecutionCoordinator,
    BackupExecutionRequest,
    BackupExecutionResult,
    FinalStoredObjectEvidence,
    RuntimeProviderStack,
    RuntimeRetentionOutcome,
    build_runtime_provider_stack,
    request_backup_execution,
)
from .sqlite_snapshot import SQLiteSnapshotProvider

__all__ = [
    "ActorIdentitySnapshot",
    "ASYNC_EXECUTION_BOUNDARY_READY",
    "BackupEngineDisabled",
    "BackupEngineError",
    "BackupExecutionContext",
    "BackupExecutionCoordinator",
    "BackupExecutionPlan",
    "BackupExecutionRequest",
    "BackupExecutionResult",
    "BackupRetentionClass",
    "CANONICAL_MANIFEST_PROVIDER_READY",
    "CanonicalManifestProvider",
    "DETERMINISTIC_PACKAGE_PROVIDER_READY",
    "DeterministicPackageProvider",
    "DURABLE_STORAGE_PROVIDER_READY",
    "DurableBackupStorageProvider",
    "DurableStoragePolicy",
    "ENCRYPTED_ARTIFACT_PROVIDER_READY",
    "EncryptedArtifactProvider",
    "EncryptedArtifactReference",
    "EncryptedArtifactRequest",
    "EncryptedArtifactResult",
    "EncryptionPolicy",
    "INDEPENDENT_PACKAGE_VERIFIER_READY",
    "IndependentPackageVerifier",
    "LocalFilesystemMediaCaptureProvider",
    "LocalConfiguredKekProvider",
    "LocalPrivateDurableStorageProvider",
    "KekProvider",
    "MEDIA_CAPTURE_PROVIDER_READY",
    "PackageBuildRequest",
    "PackageBuildResult",
    "PackageCompatibilityStatus",
    "PackageVerificationRequest",
    "PackageVerificationResult",
    "Phase2D1Coordinator",
    "Phase2D1Request",
    "Phase2D1Result",
    "Phase2D2Coordinator",
    "Phase2D2Request",
    "Phase2D2Result",
    "PipelineStage",
    "RestoreReadinessResult",
    "RETENTION_ENGINE_READY",
    "RUNTIME_COMPOSITION_READY",
    "RetentionAuditEvent",
    "RetentionAuditEventType",
    "RetentionCandidate",
    "RetentionEngine",
    "RetentionExecutionResult",
    "RetentionExecutionState",
    "RetentionPlan",
    "RetentionPlanReference",
    "RetentionPolicy",
    "RetentionSkipReason",
    "RUNTIME_ORCHESTRATOR_READY",
    "SCHEDULE_DISPATCHER_READY",
    "RuntimeProviderStack",
    "RuntimeRetentionOutcome",
    "SQLITE_SNAPSHOT_PROVIDER_READY",
    "SQLiteSnapshotProvider",
    "SQLiteLogicalComponentExporter",
    "TENANT_LOGICAL_EXPORT_PROVIDER_READY",
    "StoredBackupObjectReference",
    "StoredBackupObjectRequest",
    "StoredBackupObjectResult",
    "StoredObjectDurabilityState",
    "StoredObjectVerificationState",
    "VerificationIssue",
    "VerificationReference",
    "WrappedDek",
    "assert_real_execution_available",
    "async_configuration_ready",
    "get_engine_capability",
    "build_runtime_provider_stack",
    "prepare_backup_execution",
    "real_execution_available",
    "request_backup_execution",
    "export_snapshot_components",
    "FinalStoredObjectEvidence",
]
