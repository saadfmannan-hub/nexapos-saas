"""Bounded settings policy for Phase 2C tenant logical export."""

import math
from dataclasses import dataclass

from django.conf import settings

from .exceptions import LogicalExportPolicyError

MAXIMUM_LOGICAL_FETCH_PAYLOAD_BYTES = 16 * 1024**2
# Backward-compatible name retained for the Phase 2C test/report vocabulary.
MAXIMUM_LOGICAL_FETCH_MEMORY_BYTES = MAXIMUM_LOGICAL_FETCH_PAYLOAD_BYTES


def _bounded_number(name, value, *, minimum, maximum, integer=False):
    if isinstance(value, bool):
        raise LogicalExportPolicyError(f"The {name} setting is invalid.")
    if integer and type(value) is not int:
        raise LogicalExportPolicyError(f"The {name} setting must be an integer.")
    try:
        normalized = value if integer else float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LogicalExportPolicyError(f"The {name} setting is invalid.") from exc
    if not math.isfinite(float(normalized)) or not minimum <= normalized <= maximum:
        raise LogicalExportPolicyError(f"The {name} setting is outside safe bounds.")
    return normalized


@dataclass(frozen=True, slots=True)
class LogicalExportPolicy:
    fetch_batch_size: int
    component_timeout_seconds: float
    maximum_records_bytes: int
    maximum_media_index_bytes: int
    maximum_row_input_bytes: int
    maximum_json_depth: int
    maximum_media_name_length: int

    @classmethod
    def from_settings(cls):
        return cls(
            fetch_batch_size=getattr(
                settings,
                "BACKUP_LOGICAL_EXPORT_FETCH_BATCH_SIZE",
                200,
            ),
            component_timeout_seconds=getattr(
                settings,
                "BACKUP_LOGICAL_EXPORT_COMPONENT_TIMEOUT_SECONDS",
                120.0,
            ),
            maximum_records_bytes=getattr(
                settings,
                "BACKUP_LOGICAL_EXPORT_MAX_RECORDS_BYTES",
                536_870_912,
            ),
            maximum_media_index_bytes=getattr(
                settings,
                "BACKUP_LOGICAL_EXPORT_MAX_MEDIA_INDEX_BYTES",
                33_554_432,
            ),
            maximum_row_input_bytes=getattr(
                settings,
                "BACKUP_LOGICAL_EXPORT_MAX_ROW_INPUT_BYTES",
                65_536,
            ),
            maximum_json_depth=getattr(
                settings,
                "BACKUP_LOGICAL_EXPORT_MAX_JSON_DEPTH",
                20,
            ),
            maximum_media_name_length=getattr(
                settings,
                "BACKUP_LOGICAL_EXPORT_MAX_MEDIA_NAME_LENGTH",
                1024,
            ),
        ).validated()

    def validated(self):
        if type(self) is not LogicalExportPolicy:
            raise LogicalExportPolicyError("The logical export policy type is invalid.")
        validated = type(self)(
            fetch_batch_size=_bounded_number(
                "logical export fetch batch size",
                self.fetch_batch_size,
                minimum=1,
                maximum=10_000,
                integer=True,
            ),
            component_timeout_seconds=_bounded_number(
                "logical export component timeout",
                self.component_timeout_seconds,
                minimum=1.0,
                maximum=3_600.0,
            ),
            maximum_records_bytes=_bounded_number(
                "logical export records byte limit",
                self.maximum_records_bytes,
                minimum=1,
                maximum=10 * 1024**3,
                integer=True,
            ),
            maximum_media_index_bytes=_bounded_number(
                "logical export media-index byte limit",
                self.maximum_media_index_bytes,
                minimum=1,
                maximum=1024**3,
                integer=True,
            ),
            maximum_row_input_bytes=_bounded_number(
                "logical export row-input byte limit",
                self.maximum_row_input_bytes,
                minimum=1,
                maximum=8 * 1024**2,
                integer=True,
            ),
            maximum_json_depth=_bounded_number(
                "logical export JSON depth",
                self.maximum_json_depth,
                minimum=1,
                maximum=100,
                integer=True,
            ),
            maximum_media_name_length=_bounded_number(
                "logical export media-name length",
                self.maximum_media_name_length,
                minimum=1,
                maximum=4096,
                integer=True,
            ),
        )
        if (
            validated.fetch_batch_size * validated.maximum_row_input_bytes
            > MAXIMUM_LOGICAL_FETCH_PAYLOAD_BYTES
        ):
            raise LogicalExportPolicyError(
                "The logical export fetch memory bound is outside safe limits."
            )
        return validated


__all__ = [
    "MAXIMUM_LOGICAL_FETCH_MEMORY_BYTES",
    "MAXIMUM_LOGICAL_FETCH_PAYLOAD_BYTES",
    "LogicalExportPolicy",
]
