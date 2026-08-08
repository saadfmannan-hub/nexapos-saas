"""Independent post-mutation tenant-state verification boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from django.utils import timezone

from apps.tenants.models import Business

from .logical_restore import LogicalRestoreEngine, PreparedLogicalRestore
from .media_restore import LocalFilesystemMediaRestoreProvider, StagedMediaRestore
from .restore_exceptions import RestorePostVerificationError


class PostRestoreVerificationState(StrEnum):
    VERIFIED = "VERIFIED"


@dataclass(frozen=True, slots=True)
class PostRestoreVerificationResult:
    state: PostRestoreVerificationState
    component_count: int
    record_count: int
    media_count: int
    verified_at: datetime
    provider_identifier: str


class IndependentRestoreStateVerifier:
    """Compare live logical state and media bytes with the validated source."""

    provider_identifier = "independent-restore-state-verifier-v1"

    def __init__(self, *, logical_engine, media_provider, clock=None):
        if (
            type(logical_engine) is not LogicalRestoreEngine
            or type(media_provider) is not LocalFilesystemMediaRestoreProvider
        ):
            raise RestorePostVerificationError(issue_code="restore_verifier_invalid")
        self.logical_engine = logical_engine
        self.media_provider = media_provider
        self.clock = clock or timezone.now

    def verify(self, *, business, prepared, staged_media):
        if (
            type(business) is not Business
            or type(prepared) is not PreparedLogicalRestore
            or type(staged_media) is not StagedMediaRestore
        ):
            raise RestorePostVerificationError(issue_code="restore_verification_invalid")
        try:
            self.logical_engine.verify(business=business, prepared=prepared)
            self.media_provider.verify(staged_media)
            verified_at = self.clock()
            if verified_at.tzinfo is None or verified_at.utcoffset() is None:
                raise ValueError
        except RestorePostVerificationError:
            raise
        except Exception:
            raise RestorePostVerificationError(issue_code="restore_post_verify_failed") from None
        return PostRestoreVerificationResult(
            state=PostRestoreVerificationState.VERIFIED,
            component_count=len(prepared.component_keys),
            record_count=prepared.record_count,
            media_count=staged_media.object_count,
            verified_at=verified_at,
            provider_identifier=self.provider_identifier,
        )


__all__ = [
    "IndependentRestoreStateVerifier",
    "PostRestoreVerificationResult",
    "PostRestoreVerificationState",
]
