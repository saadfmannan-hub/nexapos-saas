"""Nexa backup engine planning foundation.

The public surface in Phase 2A is deliberately non-operational.  It exposes
typed contracts, immutable planning metadata, and central availability guards.
"""

from .availability import (
    SQLITE_SNAPSHOT_PROVIDER_READY,
    assert_real_execution_available,
    get_engine_capability,
    real_execution_available,
)
from .context import ActorIdentitySnapshot, BackupExecutionContext
from .exceptions import BackupEngineDisabled, BackupEngineError
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
    "assert_real_execution_available",
    "get_engine_capability",
    "prepare_backup_execution",
    "real_execution_available",
]
