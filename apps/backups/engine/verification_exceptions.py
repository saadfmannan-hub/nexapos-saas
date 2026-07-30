"""Sanitized independent-verification exceptions for Backup Engine Phase 2E."""

from .exceptions import BackupEngineError


class Phase2EEngineError(BackupEngineError):
    retryable = False

    def __init__(self, message=None, *, code=None, cleanup_incomplete=False):
        self.cleanup_incomplete = bool(cleanup_incomplete)
        super().__init__(message, code=code)


class VerificationProviderStateError(Phase2EEngineError):
    default_message = "The independent verification provider state is not valid."
    default_code = "verification_provider_state_invalid"


class VerificationPublicationError(Phase2EEngineError):
    default_message = "Verification evidence could not be published safely."
    default_code = "verification_publication_failed"


class VerificationEvidenceNotFound(Phase2EEngineError, LookupError):
    default_message = "The opaque verification evidence reference is unavailable."
    default_code = "verification_evidence_not_found"


class VerificationCleanupError(Phase2EEngineError):
    default_message = "Verification evidence could not be cleaned safely."
    default_code = "verification_cleanup_failed"
    retryable = True


__all__ = [
    "Phase2EEngineError",
    "VerificationCleanupError",
    "VerificationEvidenceNotFound",
    "VerificationProviderStateError",
    "VerificationPublicationError",
]
