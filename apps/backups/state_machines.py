"""Explicit lifecycle transition rules.

Persistence services must call these validators before changing state.  The
maps are immutable so no request or worker can extend a lifecycle at runtime.
"""

from types import MappingProxyType

from django.core.exceptions import ValidationError

from .enums import BackupStatus, IntegrityStatus, RestoreStatus


class InvalidStateTransition(ValidationError):
    """Raised when a lifecycle is asked to make a non-declared transition."""


def _freeze(transitions):
    return MappingProxyType(
        {state: frozenset(targets) for state, targets in transitions.items()}
    )


BACKUP_TRANSITIONS = _freeze(
    {
        BackupStatus.QUEUED: {
            BackupStatus.PREPARING,
            BackupStatus.FAILED,
            BackupStatus.CANCELLED,
        },
        BackupStatus.PREPARING: {
            BackupStatus.SNAPSHOTTING,
            BackupStatus.FAILED,
            BackupStatus.CANCELLED,
        },
        BackupStatus.SNAPSHOTTING: {
            BackupStatus.PACKAGING,
            BackupStatus.FAILED,
            BackupStatus.CANCELLED,
        },
        BackupStatus.PACKAGING: {
            BackupStatus.UPLOADING,
            BackupStatus.FAILED,
            BackupStatus.CANCELLED,
        },
        BackupStatus.UPLOADING: {
            BackupStatus.VERIFYING,
            BackupStatus.FAILED,
            BackupStatus.CANCELLED,
        },
        BackupStatus.VERIFYING: {
            BackupStatus.SUCCEEDED,
            BackupStatus.FAILED,
            BackupStatus.CANCELLED,
        },
        BackupStatus.SUCCEEDED: {BackupStatus.DELETION_PENDING},
        BackupStatus.FAILED: {BackupStatus.DELETION_PENDING},
        BackupStatus.CANCELLED: {BackupStatus.DELETION_PENDING},
        BackupStatus.DELETION_PENDING: {BackupStatus.DELETED},
        BackupStatus.DELETED: set(),
    }
)


INTEGRITY_TRANSITIONS = _freeze(
    {
        IntegrityStatus.NOT_CHECKED: {
            IntegrityStatus.VERIFYING,
            IntegrityStatus.FAILED,
            IntegrityStatus.CORRUPTED,
        },
        IntegrityStatus.VERIFYING: {
            IntegrityStatus.VERIFIED,
            IntegrityStatus.FAILED,
            IntegrityStatus.CORRUPTED,
        },
        IntegrityStatus.VERIFIED: {IntegrityStatus.CORRUPTED},
        IntegrityStatus.FAILED: {
            IntegrityStatus.VERIFYING,
            IntegrityStatus.CORRUPTED,
        },
        IntegrityStatus.CORRUPTED: set(),
    }
)


RESTORE_TRANSITIONS = _freeze(
    {
        RestoreStatus.QUEUED: {
            RestoreStatus.AUTHORIZING,
            RestoreStatus.FAILED,
        },
        RestoreStatus.AUTHORIZING: {
            RestoreStatus.LOCKING,
            RestoreStatus.FAILED,
        },
        RestoreStatus.LOCKING: {
            RestoreStatus.SAFETY_BACKUP,
            RestoreStatus.FAILED,
        },
        RestoreStatus.SAFETY_BACKUP: {
            RestoreStatus.VALIDATING,
            RestoreStatus.FAILED,
        },
        RestoreStatus.VALIDATING: {
            RestoreStatus.RESTORING,
            RestoreStatus.FAILED,
        },
        RestoreStatus.RESTORING: {
            RestoreStatus.VERIFYING,
            RestoreStatus.FAILED,
            RestoreStatus.ROLLING_BACK,
            RestoreStatus.INDETERMINATE,
        },
        RestoreStatus.VERIFYING: {
            RestoreStatus.SUCCEEDED,
            RestoreStatus.FAILED,
            RestoreStatus.ROLLING_BACK,
            RestoreStatus.INDETERMINATE,
        },
        RestoreStatus.FAILED: {
            RestoreStatus.ROLLING_BACK,
            RestoreStatus.INDETERMINATE,
        },
        RestoreStatus.ROLLING_BACK: {
            RestoreStatus.ROLLED_BACK,
            RestoreStatus.INDETERMINATE,
        },
        RestoreStatus.SUCCEEDED: set(),
        RestoreStatus.ROLLED_BACK: set(),
        RestoreStatus.INDETERMINATE: set(),
    }
)


def _value(state):
    return getattr(state, "value", state)


def can_transition(current, target, transitions) -> bool:
    """Return whether ``current -> target`` is explicitly declared.

    Same-state writes are not transitions and intentionally return ``False``.
    """

    current_value = _value(current)
    target_value = _value(target)
    return target_value in {_value(value) for value in transitions.get(current_value, ())}


def _validate(current, target, transitions, lifecycle):
    current_value = _value(current)
    target_value = _value(target)
    if not can_transition(current_value, target_value, transitions):
        raise InvalidStateTransition(
            f"Invalid {lifecycle} transition: {current_value} -> {target_value}.",
            code="invalid_state_transition",
        )
    return target_value


def validate_backup_transition(current, target):
    return _validate(current, target, BACKUP_TRANSITIONS, "backup")


def validate_integrity_transition(current, target):
    return _validate(current, target, INTEGRITY_TRANSITIONS, "integrity")


def validate_restore_transition(current, target):
    return _validate(current, target, RESTORE_TRANSITIONS, "restore")
