"""Sanitized durable-storage exceptions for Backup Engine Phase 2G."""

from .exceptions import BackupEngineError


class DurableStorageEngineError(BackupEngineError):
    retryable = False

    def __init__(
        self,
        message=None,
        *,
        code=None,
        cleanup_incomplete=False,
        encrypted_staging_cleanup_incomplete=False,
    ):
        self.cleanup_incomplete = bool(cleanup_incomplete)
        self.encrypted_staging_cleanup_incomplete = bool(
            encrypted_staging_cleanup_incomplete
        )
        super().__init__(message, code=code)


class DurableStoragePolicyError(DurableStorageEngineError):
    default_message = "The durable backup storage policy is invalid."
    default_code = "durable_storage_policy_invalid"


class UnsafeDurableStorageRoot(DurableStorageEngineError):
    default_message = "The durable backup storage root is not safe."
    default_code = "unsafe_durable_storage_root"


class InsufficientDurableStorageCapacity(DurableStorageEngineError):
    default_message = "Durable backup storage capacity is insufficient."
    default_code = "insufficient_durable_storage_capacity"


class DurableStorageTimeout(DurableStorageEngineError):
    default_message = "The durable backup storage operation exceeded its safe deadline."
    default_code = "durable_storage_timeout"


class DurableStorageUnavailable(DurableStorageEngineError):
    default_message = "The durable backup storage provider is temporarily unavailable."
    default_code = "durable_storage_unavailable"
    retryable = True


class DurableStorageAuthorizationError(DurableStorageEngineError):
    default_message = "The durable backup storage provider rejected the operation."
    default_code = "durable_storage_authorization_failed"


class DurableObjectCreationError(DurableStorageEngineError):
    default_message = "The durable encrypted backup object could not be created safely."
    default_code = "durable_object_creation_failed"


class DurableObjectValidationError(DurableStorageEngineError):
    default_message = "The durable encrypted backup object failed validation."
    default_code = "durable_object_validation_failed"


class DurableObjectNotFound(DurableStorageEngineError, LookupError):
    default_message = "The opaque durable backup object reference is unavailable."
    default_code = "durable_object_not_found"


class DurableObjectCleanupError(DurableStorageEngineError):
    default_message = "The durable backup object could not be cleaned safely."
    default_code = "durable_object_cleanup_failed"
    retryable = True


class EncryptedStagingCleanupError(DurableStorageEngineError):
    default_message = (
        "The durable object is verified, but encrypted staging cleanup is incomplete."
    )
    default_code = "encrypted_staging_cleanup_failed"
    retryable = True


class Phase2GCoordinationError(DurableStorageEngineError):
    default_message = "Durable encrypted storage coordination failed."
    default_code = "phase2g_coordination_failed"


__all__ = [
    "DurableObjectCleanupError",
    "DurableObjectCreationError",
    "DurableObjectNotFound",
    "DurableObjectValidationError",
    "DurableStorageEngineError",
    "DurableStoragePolicyError",
    "DurableStorageTimeout",
    "DurableStorageUnavailable",
    "DurableStorageAuthorizationError",
    "EncryptedStagingCleanupError",
    "InsufficientDurableStorageCapacity",
    "Phase2GCoordinationError",
    "UnsafeDurableStorageRoot",
]
