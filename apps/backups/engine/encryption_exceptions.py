"""Sanitized encrypted-artifact exceptions for Backup Engine Phase 2F."""

from .exceptions import BackupEngineError


class Phase2FEngineError(BackupEngineError):
    retryable = False

    def __init__(
        self,
        message=None,
        *,
        code=None,
        cleanup_incomplete=False,
        plaintext_cleanup_incomplete=False,
    ):
        self.cleanup_incomplete = bool(cleanup_incomplete)
        self.plaintext_cleanup_incomplete = bool(plaintext_cleanup_incomplete)
        super().__init__(message, code=code)


class EncryptionPolicyError(Phase2FEngineError):
    default_message = "The encrypted-artifact safety policy is invalid."
    default_code = "encryption_policy_invalid"


class KeyProviderConfigurationError(Phase2FEngineError):
    default_message = "The configured key-encryption provider is not valid."
    default_code = "key_provider_configuration_invalid"


class KeyWrapError(Phase2FEngineError):
    default_message = "The artifact data-encryption key could not be handled safely."
    default_code = "key_wrap_failed"


class KeyProviderUnavailableError(KeyWrapError):
    default_message = "The key-encryption provider is temporarily unavailable."
    default_code = "key_provider_unavailable"
    retryable = True


class KeyRewrapError(Phase2FEngineError):
    default_message = "The artifact data-encryption key could not be rotated safely."
    default_code = "key_rewrap_failed"


class EncryptedArtifactCreationError(Phase2FEngineError):
    default_message = "The encrypted backup artifact could not be created safely."
    default_code = "encrypted_artifact_creation_failed"


class EncryptedArtifactValidationError(Phase2FEngineError):
    default_message = "The encrypted backup artifact failed authenticated validation."
    default_code = "encrypted_artifact_validation_failed"


class EncryptedArtifactNotFound(Phase2FEngineError, LookupError):
    default_message = "The opaque encrypted artifact reference is unavailable."
    default_code = "encrypted_artifact_not_found"


class EncryptedArtifactCleanupError(Phase2FEngineError):
    default_message = "The encrypted backup artifact could not be cleaned safely."
    default_code = "encrypted_artifact_cleanup_failed"
    retryable = True


class PlaintextPackageCleanupError(Phase2FEngineError):
    default_message = (
        "The validated encrypted artifact is retained, but plaintext cleanup "
        "is incomplete."
    )
    default_code = "plaintext_package_cleanup_failed"
    retryable = True


class Phase2FCoordinationError(Phase2FEngineError):
    default_message = "Encrypted artifact coordination failed."
    default_code = "phase2f_coordination_failed"


__all__ = [
    "EncryptedArtifactCleanupError",
    "EncryptedArtifactCreationError",
    "EncryptedArtifactNotFound",
    "EncryptedArtifactValidationError",
    "EncryptionPolicyError",
    "KeyProviderConfigurationError",
    "KeyProviderUnavailableError",
    "KeyRewrapError",
    "KeyWrapError",
    "Phase2FCoordinationError",
    "Phase2FEngineError",
    "PlaintextPackageCleanupError",
]
