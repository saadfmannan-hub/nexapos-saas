"""Nexa backup engine foundations through the internal Phase 2C providers.

The public surface remains deliberately non-operational. It exposes typed
contracts, immutable planning metadata, and central availability guards.
"""

from .availability import (
    SQLITE_SNAPSHOT_PROVIDER_READY,
    TENANT_LOGICAL_EXPORT_PROVIDER_READY,
    assert_real_execution_available,
    get_engine_capability,
    real_execution_available,
)
from .context import ActorIdentitySnapshot, BackupExecutionContext
from .exceptions import BackupEngineDisabled, BackupEngineError
from .logical_export import (
    SQLiteLogicalComponentExporter,
    export_snapshot_components,
)
from .orchestration import prepare_backup_execution
from .pipeline import BackupExecutionPlan, PipelineStage
from .sqlite_snapshot import SQLiteSnapshotProvider

__all__ = [
    "ActorIdentitySnapshot",
    "BackupEngineDisabled",
    "BackupEngineError",
    "BackupExecutionContext",
    "BackupExecutionPlan",
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
