"""Nexa backup engine foundations through the internal Phase 2D-1 providers.

The public surface remains deliberately non-operational. It exposes typed
contracts, immutable planning metadata, and central availability guards.
"""

from .availability import (
    CANONICAL_MANIFEST_PROVIDER_READY,
    MEDIA_CAPTURE_PROVIDER_READY,
    SQLITE_SNAPSHOT_PROVIDER_READY,
    TENANT_LOGICAL_EXPORT_PROVIDER_READY,
    assert_real_execution_available,
    get_engine_capability,
    real_execution_available,
)
from .canonical_manifest import CanonicalManifestProvider
from .context import ActorIdentitySnapshot, BackupExecutionContext
from .contracts import Phase2D1Request, Phase2D1Result
from .exceptions import BackupEngineDisabled, BackupEngineError
from .logical_export import (
    SQLiteLogicalComponentExporter,
    export_snapshot_components,
)
from .media_capture import LocalFilesystemMediaCaptureProvider
from .orchestration import prepare_backup_execution
from .phase2d1 import Phase2D1Coordinator
from .pipeline import BackupExecutionPlan, PipelineStage
from .sqlite_snapshot import SQLiteSnapshotProvider

__all__ = [
    "ActorIdentitySnapshot",
    "BackupEngineDisabled",
    "BackupEngineError",
    "BackupExecutionContext",
    "BackupExecutionPlan",
    "CANONICAL_MANIFEST_PROVIDER_READY",
    "CanonicalManifestProvider",
    "LocalFilesystemMediaCaptureProvider",
    "MEDIA_CAPTURE_PROVIDER_READY",
    "Phase2D1Coordinator",
    "Phase2D1Request",
    "Phase2D1Result",
    "PipelineStage",
    "SQLITE_SNAPSHOT_PROVIDER_READY",
    "SQLiteSnapshotProvider",
    "SQLiteLogicalComponentExporter",
    "TENANT_LOGICAL_EXPORT_PROVIDER_READY",
    "assert_real_execution_available",
    "get_engine_capability",
    "prepare_backup_execution",
    "real_execution_available",
    "export_snapshot_components",
]
