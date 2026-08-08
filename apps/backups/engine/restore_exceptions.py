"""Sanitized Phase 3A restore-preflight failures."""


class RestoreEngineError(Exception):
    default_message = "Restore preflight could not be completed safely."

    def __init__(self, sanitized_message=None, *, issue_code="restore_preflight_failed"):
        self.sanitized_message = str(sanitized_message or self.default_message)[:500]
        self.issue_code = str(issue_code or "restore_preflight_failed")[:80]
        super().__init__(self.sanitized_message)


class RestoreSelectionError(RestoreEngineError):
    default_message = "The selected backup is not eligible for restore preflight."


class RestoreTenantMismatch(RestoreSelectionError):
    default_message = "The selected backup is not available for this business."


class RestoreLockUnavailable(RestoreEngineError):
    default_message = "Another exclusive operation is active for this business."


class RestoreLockLost(RestoreEngineError):
    default_message = "The restore-preflight tenant lock could not be maintained."


class RestoreDurableObjectError(RestoreEngineError):
    default_message = "The durable backup object could not be validated."


class RestoreDecryptError(RestoreEngineError):
    default_message = "The encrypted backup could not be authenticated and decrypted."


class RestorePackageVerificationError(RestoreEngineError):
    default_message = "The restored package did not pass independent verification."


class RestoreCompatibilityError(RestoreEngineError):
    default_message = "Restore compatibility was not proven."


class RestoreExtractionError(RestoreEngineError):
    default_message = "The restored package could not be expanded safely."


class RestoreComponentPlanError(RestoreEngineError):
    default_message = "A safe restore component plan could not be built."


class RestoreRecordPreflightError(RestoreEngineError):
    default_message = "Restored records did not pass semantic preflight."


class RestoreMediaPreflightError(RestoreEngineError):
    default_message = "Restored media did not pass semantic preflight."


class RestorePreflightCleanupError(RestoreEngineError):
    default_message = "The private restore-preflight workspace could not be cleaned safely."


class Phase3ACoordinationError(RestoreEngineError):
    default_message = "Restore-preflight providers are not composed safely."


__all__ = [
    "Phase3ACoordinationError",
    "RestoreCompatibilityError",
    "RestoreComponentPlanError",
    "RestoreDecryptError",
    "RestoreDurableObjectError",
    "RestoreEngineError",
    "RestoreExtractionError",
    "RestoreLockLost",
    "RestoreLockUnavailable",
    "RestoreMediaPreflightError",
    "RestorePackageVerificationError",
    "RestorePreflightCleanupError",
    "RestoreRecordPreflightError",
    "RestoreSelectionError",
    "RestoreTenantMismatch",
]
