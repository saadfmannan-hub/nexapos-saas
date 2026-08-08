"""Immutable bounded policy for Phase 2H daily-full retention."""

import math
from dataclasses import dataclass

from django.conf import settings

from .retention_exceptions import RetentionPolicyError

RETENTION_POLICY_IDENTIFIER = "nexa.daily-full-retention.v1"
RETENTION_POLICY_VERSION = "1.0.0"
DAILY_FULL_KEEP_COUNT = 5


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    daily_full_keep_count: int = DAILY_FULL_KEEP_COUNT
    maximum_delete_batch: int = 100
    timeout_seconds: float = 300.0

    @classmethod
    def from_settings(cls):
        try:
            return cls(
                daily_full_keep_count=settings.BACKUP_RETENTION_DAILY_FULL_KEEP_COUNT,
                maximum_delete_batch=settings.BACKUP_RETENTION_MAX_DELETE_BATCH,
                timeout_seconds=settings.BACKUP_RETENTION_TIMEOUT_SECONDS,
            ).validated()
        except (AttributeError, TypeError, ValueError):
            raise RetentionPolicyError() from None

    def validated(self):
        if (
            type(self.daily_full_keep_count) is not int
            or not 1 <= self.daily_full_keep_count <= 3650
            or type(self.maximum_delete_batch) is not int
            or not 1 <= self.maximum_delete_batch <= 1000
            or type(self.timeout_seconds) not in (int, float)
            or type(self.timeout_seconds) is bool
            or not math.isfinite(float(self.timeout_seconds))
            or not 1.0 <= float(self.timeout_seconds) <= 3600.0
        ):
            raise RetentionPolicyError()
        return type(self)(
            daily_full_keep_count=self.daily_full_keep_count,
            maximum_delete_batch=self.maximum_delete_batch,
            timeout_seconds=float(self.timeout_seconds),
        )


__all__ = [
    "DAILY_FULL_KEEP_COUNT",
    "RETENTION_POLICY_IDENTIFIER",
    "RETENTION_POLICY_VERSION",
    "RetentionPolicy",
]
