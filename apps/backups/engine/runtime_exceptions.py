"""Sanitized exceptions for the Phase 2I operational coordinator."""

from .exceptions import BackupEngineError


class RuntimeEngineError(BackupEngineError):
    retryable = False

    def __init__(self, message=None, *, code=None, durable_object_preserved=False):
        del message, code
        self.durable_object_preserved = bool(durable_object_preserved)
        super().__init__(self.default_message, code=self.default_code)


class RuntimeRequestError(RuntimeEngineError):
    default_message = "The backup execution request is invalid."
    default_code = "runtime_request_invalid"


class RuntimeStateError(RuntimeEngineError):
    default_message = "The backup is not in an allowed execution state."
    default_code = "execution_state_invalid"


class RuntimeAlreadyCompleted(RuntimeStateError):
    default_message = "The backup execution is already complete."
    default_code = "backup_already_completed"


class RuntimeLockUnavailable(RuntimeEngineError):
    default_message = "Another exclusive operation is active for this tenant."
    default_code = "lock_unavailable"
    retryable = True


class RuntimeLockLost(RuntimeEngineError):
    default_message = "The tenant execution lease could not be maintained."
    default_code = "lock_lost"
    retryable = True


class RuntimeProviderStackError(RuntimeEngineError):
    default_message = "The backup runtime provider stack is not safe."
    default_code = "runtime_provider_stack_invalid"


class RuntimeVerificationError(RuntimeEngineError):
    default_message = "The backup package did not pass required verification."
    default_code = "package_verification_failure"


class RuntimePersistenceError(RuntimeEngineError):
    default_message = "Durable backup evidence could not be finalized safely."
    default_code = "durable_finalization_failure"
    retryable = True


class RuntimeExecutionError(RuntimeEngineError):
    default_message = "The backup execution failed safely."
    default_code = "runtime_execution_failure"


class Phase2ICoordinationError(RuntimeEngineError):
    default_message = "Operational backup coordination failed."
    default_code = "phase2i_coordination_failed"


__all__ = [
    "Phase2ICoordinationError",
    "RuntimeAlreadyCompleted",
    "RuntimeEngineError",
    "RuntimeExecutionError",
    "RuntimeLockLost",
    "RuntimeLockUnavailable",
    "RuntimePersistenceError",
    "RuntimeProviderStackError",
    "RuntimeRequestError",
    "RuntimeStateError",
    "RuntimeVerificationError",
]
