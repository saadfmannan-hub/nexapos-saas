"""Strict bounded policy for Phase 2F encrypted artifact construction."""

import math
from dataclasses import dataclass

from django.conf import settings

from .encryption_exceptions import EncryptionPolicyError


@dataclass(frozen=True, slots=True)
class EncryptionPolicy:
    chunk_bytes: int
    maximum_plaintext_bytes: int
    maximum_artifact_bytes: int
    timeout_seconds: float
    minimum_free_bytes: int
    headroom_multiplier: float
    maximum_header_bytes: int

    @classmethod
    def from_settings(cls):
        try:
            return cls(
                chunk_bytes=settings.BACKUP_ENCRYPTION_CHUNK_BYTES,
                maximum_plaintext_bytes=settings.BACKUP_ENCRYPTION_MAX_PLAINTEXT_BYTES,
                maximum_artifact_bytes=settings.BACKUP_ENCRYPTION_MAX_ARTIFACT_BYTES,
                timeout_seconds=settings.BACKUP_ENCRYPTION_TIMEOUT_SECONDS,
                minimum_free_bytes=settings.BACKUP_ENCRYPTION_MIN_FREE_BYTES,
                headroom_multiplier=settings.BACKUP_ENCRYPTION_HEADROOM_MULTIPLIER,
                maximum_header_bytes=settings.BACKUP_ENCRYPTION_MAX_HEADER_BYTES,
            ).validated()
        except (AttributeError, TypeError, ValueError):
            raise EncryptionPolicyError() from None

    def validated(self):
        integer_values = (
            self.chunk_bytes,
            self.maximum_plaintext_bytes,
            self.maximum_artifact_bytes,
            self.minimum_free_bytes,
            self.maximum_header_bytes,
        )
        if any(type(value) is not int for value in integer_values):
            raise EncryptionPolicyError()
        if (
            not 4096 <= self.chunk_bytes <= 16 * 1024**2
            or not 1 <= self.maximum_plaintext_bytes <= 10 * 1024**4
            or not self.maximum_plaintext_bytes
            < self.maximum_artifact_bytes
            <= 10 * 1024**4 + 16 * 1024**2
            or not 0 <= self.minimum_free_bytes <= 10 * 1024**4
            or not 512 <= self.maximum_header_bytes <= 1024**2
            or type(self.timeout_seconds) not in (int, float)
            or type(self.timeout_seconds) is bool
            or not math.isfinite(float(self.timeout_seconds))
            or not 1.0 <= float(self.timeout_seconds) <= 86_400.0
            or type(self.headroom_multiplier) not in (int, float)
            or type(self.headroom_multiplier) is bool
            or not math.isfinite(float(self.headroom_multiplier))
            or not 1.0 <= float(self.headroom_multiplier) <= 10.0
        ):
            raise EncryptionPolicyError()
        return self


__all__ = ["EncryptionPolicy"]
