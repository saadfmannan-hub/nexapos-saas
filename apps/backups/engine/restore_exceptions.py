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


class RestoreMutationError(RestoreEngineError):
    default_message = "Restore mutation could not be completed safely."


class RestoreSafetyBackupError(RestoreMutationError):
    default_message = "The mandatory pre-restore safety backup was not proven durable."


class RestoreImportError(RestoreMutationError):
    default_message = "Tenant logical data could not be restored safely."


class RestoreRelationResolutionError(RestoreImportError):
    default_message = "A required restore relation could not be resolved safely."


class RestoreTenantDeletionError(RestoreImportError):
    default_message = "Tenant replacement rows could not be deleted safely."


class RestoreMediaPublicationError(RestoreMutationError):
    default_message = "Restored media could not be published safely."


class RestorePostVerificationError(RestoreMutationError):
    default_message = "The restored tenant state did not pass independent verification."


class RestoreRollbackError(RestoreMutationError):
    default_message = "Restore rollback could not be proven complete."


class RestoreRecoveryRequired(RestoreMutationError):
    default_message = "The restore requires controlled recovery from its safety backup."


class Phase3BCoordinationError(RestoreMutationError):
    default_message = "Restore-mutation providers are not composed safely."


__all__ = [
    "Phase3ACoordinationError",
    "Phase3BCoordinationError",
    "RestoreCompatibilityError",
    "RestoreComponentPlanError",
    "RestoreDecryptError",
    "RestoreDurableObjectError",
    "RestoreEngineError",
    "RestoreExtractionError",
    "RestoreLockLost",
    "RestoreLockUnavailable",
    "RestoreMediaPreflightError",
    "RestoreMediaPublicationError",
    "RestoreMutationError",
    "RestorePackageVerificationError",
    "RestorePreflightCleanupError",
    "RestoreRecordPreflightError",
    "RestoreImportError",
    "RestorePostVerificationError",
    "RestoreRecoveryRequired",
    "RestoreRelationResolutionError",
    "RestoreRollbackError",
    "RestoreSafetyBackupError",
    "RestoreSelectionError",
    "RestoreTenantMismatch",
    "RestoreTenantDeletionError",
]
