"""Sanitized exceptions raised by the backup engine boundary."""

from django.core.exceptions import ValidationError


class BackupEngineError(ValidationError):
    """Base error whose text is safe for activity evidence and user interfaces."""

    default_message = "The backup operation could not be prepared safely."
    default_code = "backup_engine_error"

    def __init__(self, message=None, *, code=None):
        self.sanitized_message = str(message or self.default_message)
        self.engine_code = str(code or self.default_code)
        super().__init__(self.sanitized_message, code=self.engine_code)


class BackupEngineDisabled(BackupEngineError):
    default_message = "Backup execution is disabled while the engine is incomplete."
    default_code = "backup_engine_disabled"


class InvalidBackupExecutionState(BackupEngineError):
    default_message = "The backup record is not in an allowed execution state."
    default_code = "invalid_backup_execution_state"


class BackupScopeNotAllowed(BackupEngineError):
    default_message = "The requested backup scope is not available for this business."
    default_code = "backup_scope_not_allowed"


class BackupTenantMismatch(BackupEngineError):
    default_message = "The backup record does not belong to the supplied business."
    default_code = "backup_tenant_mismatch"


class UnknownBackupComponent(BackupEngineError, LookupError):
    default_message = "The backup plan contains an unknown component."
    default_code = "unknown_backup_component"

    def __init__(self, component_key=None):
        self.component_key = str(component_key or "")
        super().__init__(self.default_message)


class MissingComponentDependency(BackupEngineError):
    default_message = "A registered backup component dependency is unavailable."
    default_code = "missing_component_dependency"

    def __init__(self, component_key=None, dependency_key=None):
        self.component_key = str(component_key or "")
        self.dependency_key = str(dependency_key or "")
        super().__init__(self.default_message)


class CircularComponentDependency(BackupEngineError):
    default_message = "The backup component registry contains a circular dependency."
    default_code = "circular_component_dependency"

    def __init__(self, component_keys=()):
        self.component_keys = tuple(sorted(str(key) for key in component_keys))
        super().__init__(self.default_message)


class UnsafeWorkspacePath(BackupEngineError):
    default_message = "The backup workspace path is not safe."
    default_code = "unsafe_backup_workspace_path"


class BackupLockUnavailable(BackupEngineError):
    default_message = "Another exclusive operation is active for this business."
    default_code = "backup_lock_unavailable"


class ManifestBuildError(BackupEngineError):
    default_message = "The backup manifest metadata could not be built safely."
    default_code = "backup_manifest_build_error"


class SnapshotEngineError(BackupEngineError):
    """Base for sanitized SQLite snapshot errors."""

    retryable = False


class UnsupportedSnapshotBackend(SnapshotEngineError):
    default_message = "The configured database backend cannot create this snapshot."
    default_code = "unsupported_snapshot_backend"


class UnsafeSnapshotSource(SnapshotEngineError):
    default_message = "The configured SQLite snapshot source is not safe."
    default_code = "unsafe_snapshot_source"


class SQLiteSnapshotPolicyError(SnapshotEngineError):
    default_message = "The SQLite runtime safety policy is not satisfied."
    default_code = "sqlite_snapshot_policy_mismatch"


class SnapshotWorkspaceUnavailable(SnapshotEngineError):
    default_message = "The private snapshot workspace is unavailable."
    default_code = "snapshot_workspace_unavailable"


class UnsafeStagingFilesystem(SnapshotEngineError):
    default_message = "The snapshot staging filesystem is not confirmed local."
    default_code = "unsafe_staging_filesystem"


class InsufficientSnapshotCapacity(SnapshotEngineError):
    default_message = "The private staging area has insufficient snapshot capacity."
    default_code = "insufficient_snapshot_capacity"


class SnapshotBusy(SnapshotEngineError):
    default_message = "The SQLite snapshot source is temporarily busy."
    default_code = "snapshot_busy"
    retryable = True


class SnapshotTimeout(SnapshotEngineError):
    default_message = "The SQLite snapshot exceeded its bounded deadline."
    default_code = "snapshot_timeout"
    retryable = True


class SnapshotCreationError(SnapshotEngineError):
    default_message = "The temporary SQLite snapshot could not be created."
    default_code = "snapshot_creation_failed"


class SnapshotValidationError(SnapshotEngineError):
    default_message = "The temporary SQLite snapshot failed structural validation."
    default_code = "snapshot_validation_failed"


class SnapshotNotFound(SnapshotEngineError, LookupError):
    default_message = "The opaque SQLite snapshot reference is unavailable."
    default_code = "snapshot_not_found"


class SnapshotCleanupError(SnapshotEngineError):
    default_message = "The temporary SQLite snapshot could not be cleaned safely."
    default_code = "snapshot_cleanup_failed"
