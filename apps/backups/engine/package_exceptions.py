"""Sanitized deterministic-package exceptions for Backup Engine Phase 2D-2."""

from .exceptions import BackupEngineError


class Phase2D2EngineError(BackupEngineError):
    """Base for package publication and successful-staging cleanup failures."""

    retryable = False

    def __init__(self, message=None, *, code=None, cleanup_incomplete=False):
        self.cleanup_incomplete = bool(cleanup_incomplete)
        super().__init__(message, code=code)


class PackageValidationError(Phase2D2EngineError):
    default_message = "Deterministic package inputs failed validation."
    default_code = "package_validation_failed"


class PackageContentMismatch(Phase2D2EngineError):
    default_message = "A staged payload does not match its canonical manifest evidence."
    default_code = "package_content_mismatch"


class PackageCreationError(Phase2D2EngineError):
    default_message = "The deterministic plaintext package could not be created safely."
    default_code = "package_creation_failed"


class PackageNotFound(Phase2D2EngineError, LookupError):
    default_message = "The opaque deterministic package reference is unavailable."
    default_code = "package_not_found"


class PackageCleanupError(Phase2D2EngineError):
    default_message = "The deterministic package could not be cleaned safely."
    default_code = "package_cleanup_failed"


class SuccessfulStagingCleanupError(Phase2D2EngineError):
    default_message = (
        "The package was published, but successful plaintext staging cleanup "
        "could not be proven complete."
    )
    default_code = "successful_staging_cleanup_failed"


class Phase2D2CoordinationError(Phase2D2EngineError):
    default_message = "Deterministic package coordination failed."
    default_code = "phase2d2_coordination_failed"


__all__ = [
    "PackageCleanupError",
    "PackageContentMismatch",
    "PackageCreationError",
    "PackageNotFound",
    "PackageValidationError",
    "Phase2D2CoordinationError",
    "Phase2D2EngineError",
    "SuccessfulStagingCleanupError",
]
