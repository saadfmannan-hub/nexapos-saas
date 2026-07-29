"""Bounded policy for Phase 2D-1 local media capture."""

import math
from dataclasses import dataclass

from django.conf import settings

from .exceptions import MediaCapturePolicyError


def _bounded_integer(name, value, *, minimum, maximum):
    if type(value) is not int:
        raise MediaCapturePolicyError(f"The {name} setting must be an integer.")
    if not minimum <= value <= maximum:
        raise MediaCapturePolicyError(f"The {name} setting is outside safe bounds.")
    return value


def _bounded_float(name, value, *, minimum, maximum):
    if isinstance(value, bool):
        raise MediaCapturePolicyError(f"The {name} setting is invalid.")
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        raise MediaCapturePolicyError(f"The {name} setting is invalid.") from None
    if not math.isfinite(normalized) or not minimum <= normalized <= maximum:
        raise MediaCapturePolicyError(f"The {name} setting is outside safe bounds.")
    return normalized


@dataclass(frozen=True, slots=True)
class MediaCapturePolicy:
    chunk_bytes: int
    maximum_file_bytes: int
    maximum_total_bytes: int
    maximum_objects: int
    timeout_seconds: float
    minimum_free_bytes: int
    headroom_multiplier: float
    require_local_staging: bool
    media_index_maximum_line_bytes: int

    @classmethod
    def from_settings(cls):
        return cls(
            chunk_bytes=getattr(
                settings,
                "BACKUP_MEDIA_CAPTURE_CHUNK_BYTES",
                1_048_576,
            ),
            maximum_file_bytes=getattr(
                settings,
                "BACKUP_MEDIA_CAPTURE_MAX_FILE_BYTES",
                67_108_864,
            ),
            maximum_total_bytes=getattr(
                settings,
                "BACKUP_MEDIA_CAPTURE_MAX_TOTAL_BYTES",
                4_294_967_296,
            ),
            maximum_objects=getattr(
                settings,
                "BACKUP_MEDIA_CAPTURE_MAX_OBJECTS",
                100_000,
            ),
            timeout_seconds=getattr(
                settings,
                "BACKUP_MEDIA_CAPTURE_TIMEOUT_SECONDS",
                1800.0,
            ),
            minimum_free_bytes=getattr(
                settings,
                "BACKUP_MEDIA_CAPTURE_MIN_FREE_BYTES",
                1_073_741_824,
            ),
            headroom_multiplier=getattr(
                settings,
                "BACKUP_MEDIA_CAPTURE_HEADROOM_MULTIPLIER",
                1.25,
            ),
            require_local_staging=getattr(
                settings,
                "BACKUP_MEDIA_CAPTURE_REQUIRE_LOCAL_STAGING",
                True,
            ),
            media_index_maximum_line_bytes=getattr(
                settings,
                "BACKUP_MEDIA_INDEX_MAX_LINE_BYTES",
                65_536,
            ),
        ).validated()

    def validated(self):
        if type(self) is not MediaCapturePolicy:
            raise MediaCapturePolicyError()
        chunk_bytes = _bounded_integer(
            "media capture chunk bytes",
            self.chunk_bytes,
            minimum=4_096,
            maximum=8 * 1024**2,
        )
        maximum_file_bytes = _bounded_integer(
            "media capture per-file bytes",
            self.maximum_file_bytes,
            minimum=1,
            maximum=10 * 1024**3,
        )
        maximum_total_bytes = _bounded_integer(
            "media capture total bytes",
            self.maximum_total_bytes,
            minimum=1,
            maximum=10 * 1024**4,
        )
        maximum_objects = _bounded_integer(
            "media capture object count",
            self.maximum_objects,
            minimum=1,
            maximum=1_000_000,
        )
        timeout_seconds = _bounded_float(
            "media capture timeout",
            self.timeout_seconds,
            minimum=1.0,
            maximum=86_400.0,
        )
        minimum_free_bytes = _bounded_integer(
            "media capture minimum free bytes",
            self.minimum_free_bytes,
            minimum=1,
            maximum=10 * 1024**4,
        )
        headroom_multiplier = _bounded_float(
            "media capture headroom multiplier",
            self.headroom_multiplier,
            minimum=1.0,
            maximum=20.0,
        )
        media_index_maximum_line_bytes = _bounded_integer(
            "media index maximum line bytes",
            self.media_index_maximum_line_bytes,
            minimum=128,
            maximum=1024**2,
        )
        if maximum_total_bytes < maximum_file_bytes:
            raise MediaCapturePolicyError(
                "The media capture total limit cannot be below the per-file limit."
            )
        if type(self.require_local_staging) is not bool:
            raise MediaCapturePolicyError(
                "The media capture local-staging requirement must be boolean."
            )
        return type(self)(
            chunk_bytes=chunk_bytes,
            maximum_file_bytes=maximum_file_bytes,
            maximum_total_bytes=maximum_total_bytes,
            maximum_objects=maximum_objects,
            timeout_seconds=timeout_seconds,
            minimum_free_bytes=minimum_free_bytes,
            headroom_multiplier=headroom_multiplier,
            require_local_staging=self.require_local_staging,
            media_index_maximum_line_bytes=media_index_maximum_line_bytes,
        )


def required_media_staging_capacity(*, byte_count, policy):
    if type(byte_count) is not int or byte_count < 0:
        raise MediaCapturePolicyError()
    validated = policy.validated()
    return max(
        validated.minimum_free_bytes,
        math.ceil(byte_count * validated.headroom_multiplier),
    )


__all__ = [
    "MediaCapturePolicy",
    "required_media_staging_capacity",
]
