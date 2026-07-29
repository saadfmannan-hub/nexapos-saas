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


class LogicalExportEngineError(BackupEngineError):
    """Base for sanitized tenant logical-export failures."""

    retryable = False

    def __init__(self, message=None, *, code=None, cleanup_incomplete=False):
        self.cleanup_incomplete = bool(cleanup_incomplete)
        super().__init__(message, code=code)


class LogicalExportRegistryError(LogicalExportEngineError):
    default_message = "The logical export registry is not valid."
    default_code = "logical_export_registry_invalid"


class UnknownLogicalExportModel(LogicalExportRegistryError, LookupError):
    default_message = "The logical export model is not registered."
    default_code = "unknown_logical_export_model"


class UnsupportedLogicalExportField(LogicalExportRegistryError):
    default_message = "A logical export field has no safe serialization policy."
    default_code = "unsupported_logical_export_field"


class TenantIsolationViolation(LogicalExportEngineError):
    default_message = "Tenant isolation validation failed during logical export."
    default_code = "logical_export_tenant_isolation_failed"


class LogicalReferenceResolutionError(LogicalExportEngineError):
    default_message = "A logical export reference could not be resolved safely."
    default_code = "logical_export_reference_failed"


class UnsafeMediaReference(LogicalExportEngineError):
    default_message = "A logical media reference is not safe."
    default_code = "unsafe_logical_media_reference"


class LogicalExportPolicyError(LogicalExportEngineError):
    default_message = "The logical export policy configuration is invalid."
    default_code = "logical_export_policy_invalid"


class ComponentExportLimitExceeded(LogicalExportEngineError):
    default_message = "The logical component export exceeded a configured limit."
    default_code = "component_export_limit_exceeded"


class ComponentExportTimeout(LogicalExportEngineError):
    default_message = "The logical component export exceeded its deadline."
    default_code = "component_export_timeout"
    retryable = True


class ComponentExportCreationError(LogicalExportEngineError):
    default_message = "The logical component export could not be created safely."
    default_code = "component_export_creation_failed"


class ComponentExportValidationError(LogicalExportEngineError):
    default_message = "The logical component export request is not valid."
    default_code = "component_export_validation_failed"


class ComponentExportNotFound(LogicalExportEngineError, LookupError):
    default_message = "The opaque logical component export is unavailable."
    default_code = "component_export_not_found"


class ComponentExportCleanupError(LogicalExportEngineError):
    default_message = "The logical component export could not be cleaned safely."
    default_code = "component_export_cleanup_failed"


class SnapshotCleanupAfterExportError(LogicalExportEngineError):
    default_message = "The temporary snapshot could not be cleaned after logical export."
    default_code = "snapshot_cleanup_after_export_failed"
