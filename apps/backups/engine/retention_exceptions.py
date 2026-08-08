"""Sanitized retention exceptions for Backup Engine Phase 2H."""

from .exceptions import BackupEngineError


class RetentionEngineError(BackupEngineError):
    retryable = False

    def __init__(self, message=None, *, code=None, deletion_incomplete=False):
        # Retention catches provider/OS failures at a destructive boundary. Do
        # not permit an upstream raw message or code to cross that boundary.
        del message, code
        self.deletion_incomplete = bool(deletion_incomplete)
        super().__init__(self.default_message, code=self.default_code)


class RetentionPolicyError(RetentionEngineError):
    default_message = "The backup retention policy is invalid."
    default_code = "retention_policy_invalid"


class RetentionPlanError(RetentionEngineError):
    default_message = "The backup retention plan could not be built safely."
    default_code = "retention_plan_failed"


class RetentionEligibilityError(RetentionEngineError):
    default_message = "Backup retention eligibility could not be proven."
    default_code = "retention_eligibility_failed"


class RetentionDeleteValidationError(RetentionEngineError):
    default_message = "The retention delete candidate failed immediate revalidation."
    default_code = "retention_delete_validation_failed"


class RetentionDeleteError(RetentionEngineError):
    default_message = "The exact durable retention object could not be deleted safely."
    default_code = "retention_delete_failed"
    retryable = True


class RetentionConcurrencyError(RetentionEngineError):
    default_message = "Another retention execution is active for this tenant."
    default_code = "retention_concurrency_conflict"
    retryable = True


class Phase2HCoordinationError(RetentionEngineError):
    default_message = "Durable retention coordination failed."
    default_code = "phase2h_coordination_failed"


__all__ = [
    "Phase2HCoordinationError",
    "RetentionConcurrencyError",
    "RetentionDeleteError",
    "RetentionDeleteValidationError",
    "RetentionEligibilityError",
    "RetentionEngineError",
    "RetentionPlanError",
    "RetentionPolicyError",
]
