"""Explicit durable-storage provider selection and historical resolution."""

from __future__ import annotations

import threading
import uuid

from django.conf import settings

from .contracts import DurableBackupStorageProvider, StoredBackupObjectReference
from .durable_storage import (
    LOCAL_DURABLE_STORAGE_BACKEND_IDENTIFIER,
    LocalPrivateDurableStorageProvider,
)
from .durable_storage_exceptions import (
    DurableObjectValidationError,
    DurableStoragePolicyError,
)
from .s3_storage import (
    S3_DURABLE_STORAGE_BACKEND_IDENTIFIER,
    S3CompatibleDurableStorageProvider,
    S3StorageConfiguration,
)

LOCAL_STORAGE_PROVIDER_NAME = "local"
S3_STORAGE_PROVIDER_NAME = "s3"


def selected_storage_provider_name():
    value = getattr(settings, "BACKUP_STORAGE_PROVIDER", LOCAL_STORAGE_PROVIDER_NAME)
    if type(value) is not str:
        raise DurableStoragePolicyError()
    selected = value.strip().lower()
    if selected not in {LOCAL_STORAGE_PROVIDER_NAME, S3_STORAGE_PROVIDER_NAME}:
        raise DurableStoragePolicyError()
    return selected


def validate_storage_provider_settings():
    selected = selected_storage_provider_name()
    if selected == S3_STORAGE_PROVIDER_NAME:
        S3StorageConfiguration.from_settings()
    return selected


def stored_reference_from_metadata(
    *,
    backend_identifier,
    opaque_object_key,
    bucket_identifier="",
    version_identifier="",
):
    """Decode only the exact persisted provider-specific reference shape."""

    if any(type(value) is not str for value in (
        backend_identifier,
        opaque_object_key,
        bucket_identifier,
        version_identifier,
    )):
        raise DurableObjectValidationError()
    if backend_identifier == LOCAL_DURABLE_STORAGE_BACKEND_IDENTIFIER:
        if bucket_identifier or version_identifier:
            raise DurableObjectValidationError()
        try:
            identifier = uuid.UUID(opaque_object_key)
        except (AttributeError, TypeError, ValueError):
            raise DurableObjectValidationError() from None
        if str(identifier) != opaque_object_key:
            raise DurableObjectValidationError()
        return StoredBackupObjectReference(identifier)
    if backend_identifier == S3_DURABLE_STORAGE_BACKEND_IDENTIFIER:
        if (
            not bucket_identifier
            or len(bucket_identifier) > 255
            or not opaque_object_key
            or len(opaque_object_key) > 500
            or len(version_identifier) > 1024
        ):
            raise DurableObjectValidationError()
        return StoredBackupObjectReference(
            opaque_object_key,
            bucket_identifier=bucket_identifier,
            version_identifier=version_identifier,
        )
    raise DurableObjectValidationError()


class DurableStorageProviderRegistry:
    """Resolve exact active and historical providers without fallback."""

    def __init__(self, *, active_provider, factories=None):
        if not isinstance(active_provider, DurableBackupStorageProvider):
            raise DurableStoragePolicyError()
        self.active_provider = active_provider
        self._providers = {
            self.backend_identifier_for(active_provider): active_provider,
        }
        self._factories = dict(factories or {})
        self._lock = threading.Lock()

    @staticmethod
    def backend_identifier_for(provider):
        if type(provider) is LocalPrivateDurableStorageProvider:
            return LOCAL_DURABLE_STORAGE_BACKEND_IDENTIFIER
        if type(provider) is S3CompatibleDurableStorageProvider:
            return S3_DURABLE_STORAGE_BACKEND_IDENTIFIER
        raise DurableStoragePolicyError()

    def resolve(self, backend_identifier):
        if type(backend_identifier) is not str or not backend_identifier:
            raise DurableObjectValidationError()
        provider = self._providers.get(backend_identifier)
        if provider is not None:
            return provider
        factory = self._factories.get(backend_identifier)
        if factory is None:
            raise DurableObjectValidationError()
        with self._lock:
            provider = self._providers.get(backend_identifier)
            if provider is None:
                try:
                    provider = factory()
                    if (
                        not isinstance(provider, DurableBackupStorageProvider)
                        or self.backend_identifier_for(provider) != backend_identifier
                    ):
                        raise DurableStoragePolicyError()
                except DurableStoragePolicyError:
                    raise
                except Exception:
                    raise DurableStoragePolicyError() from None
                self._providers[backend_identifier] = provider
        return provider


def build_storage_provider_registry(*, encrypted_artifact_provider):
    selected = validate_storage_provider_settings()

    def local_provider():
        return LocalPrivateDurableStorageProvider(
            encrypted_artifact_provider=encrypted_artifact_provider,
        )

    def s3_provider():
        return S3CompatibleDurableStorageProvider(
            encrypted_artifact_provider=encrypted_artifact_provider,
        )

    if selected == LOCAL_STORAGE_PROVIDER_NAME:
        active = local_provider()
        factories = {S3_DURABLE_STORAGE_BACKEND_IDENTIFIER: s3_provider}
    else:
        active = s3_provider()
        factories = {LOCAL_DURABLE_STORAGE_BACKEND_IDENTIFIER: local_provider}
    return DurableStorageProviderRegistry(
        active_provider=active,
        factories=factories,
    )


__all__ = [
    "DurableStorageProviderRegistry",
    "LOCAL_STORAGE_PROVIDER_NAME",
    "S3_STORAGE_PROVIDER_NAME",
    "build_storage_provider_registry",
    "selected_storage_provider_name",
    "stored_reference_from_metadata",
    "validate_storage_provider_settings",
]
