"""Atomic persistence boundary for verified DEK re-wrap metadata."""

from django.db import transaction
from django.utils import timezone

from apps.backups.models import BackupRecord

from .encrypted_artifact import RewrappedArtifactKeyResult
from .encryption_exceptions import KeyRewrapError


def publish_rewrapped_key_metadata(*, backup, result):
    """Compare-and-set verified wrapper metadata while preserving old evidence."""

    if (
        type(backup) is not BackupRecord
        or type(result) is not RewrappedArtifactKeyResult
        or result.previous_key_identifier != backup.encryption_key_identifier
        or result.previous_envelope != backup.encrypted_data_key_envelope
        or result.encrypted_byte_count != backup.backup_size_bytes
        or result.artifact_sha256 != backup.whole_artifact_hash
    ):
        raise KeyRewrapError()
    try:
        with transaction.atomic():
            changed = BackupRecord.objects.filter(
                pk=backup.pk,
                encryption_key_identifier=result.previous_key_identifier,
                encrypted_data_key_envelope=result.previous_envelope,
                backup_size_bytes=result.encrypted_byte_count,
                whole_artifact_hash=result.artifact_sha256,
            ).update(
                encryption_key_identifier=result.new_key_identifier,
                encrypted_data_key_envelope=result.new_envelope,
                updated_at=timezone.now(),
            )
            if changed != 1:
                raise KeyRewrapError()
    except KeyRewrapError:
        raise
    except Exception:
        raise KeyRewrapError() from None
    backup.refresh_from_db()
    return backup


__all__ = ["publish_rewrapped_key_metadata"]
