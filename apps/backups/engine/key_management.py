"""Envelope-key boundary for Phase 2F encrypted backup artifacts."""

from __future__ import annotations

import base64
import binascii
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

from .encryption_exceptions import KeyProviderConfigurationError, KeyWrapError
from .logical_serialization import encode_canonical_document

LOCAL_KEK_PROVIDER_IDENTIFIER = "local-configured-kek-v1"
KEY_WRAP_ALGORITHM = "AES-256-GCM"
KEY_WRAP_AAD_SCHEMA = "nexa.dek-wrap-aad.v1"
_KEY_BYTES = 32
_NONCE_BYTES = 12
_TAG_BYTES = 16
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class WrappedDek:
    provider_identifier: str
    key_identifier: str
    key_version: str
    algorithm: str
    nonce: bytes = field(repr=False)
    wrapped_key: bytes = field(repr=False)
    tag: bytes = field(repr=False)


class KekProvider(ABC):
    @property
    @abstractmethod
    def provider_identifier(self):
        """Return a safe versioned provider identifier."""

    @property
    @abstractmethod
    def key_identifier(self):
        """Return a safe non-secret key identifier."""

    @property
    @abstractmethod
    def key_version(self):
        """Return a safe non-secret key version."""

    @abstractmethod
    def wrap_dek(self, dek, *, nonce):
        """Return authenticated wrapped-key metadata."""

    @abstractmethod
    def unwrap_dek(self, wrapped):
        """Return a transient raw DEK or raise a sanitized error."""


class LocalConfiguredKekProvider(KekProvider):
    """Development/test-only KEK supplied explicitly through configuration."""

    __slots__ = ("__key", "__key_identifier", "__key_version")

    def __init__(self, *, key_b64, key_identifier, key_version):
        try:
            if type(key_b64) is not str or not key_b64:
                raise ValueError
            key = base64.b64decode(
                key_b64.encode("ascii"),
                altchars=None,
                validate=True,
            )
            if (
                len(key) != _KEY_BYTES
                or base64.b64encode(key).decode("ascii") != key_b64
            ):
                raise ValueError
            if (
                type(key_identifier) is not str
                or _IDENTIFIER_PATTERN.fullmatch(key_identifier) is None
                or type(key_version) is not str
                or _IDENTIFIER_PATTERN.fullmatch(key_version) is None
            ):
                raise ValueError
        except (UnicodeError, ValueError, binascii.Error):
            raise KeyProviderConfigurationError() from None
        self.__key = key
        self.__key_identifier = key_identifier
        self.__key_version = key_version

    @classmethod
    def from_settings(cls):
        try:
            return cls(
                key_b64=settings.BACKUP_LOCAL_KEK_B64,
                key_identifier=settings.BACKUP_LOCAL_KEK_ID,
                key_version=settings.BACKUP_LOCAL_KEK_VERSION,
            )
        except (AttributeError, TypeError, ValueError):
            raise KeyProviderConfigurationError() from None

    @property
    def provider_identifier(self):
        return LOCAL_KEK_PROVIDER_IDENTIFIER

    @property
    def key_identifier(self):
        return self.__key_identifier

    @property
    def key_version(self):
        return self.__key_version

    def __repr__(self):
        return (
            "LocalConfiguredKekProvider("
            f"provider_identifier={self.provider_identifier!r}, "
            f"key_identifier={self.key_identifier!r}, "
            f"key_version={self.key_version!r})"
        )

    def _aad(self):
        try:
            return encode_canonical_document(
                {
                    "schema": KEY_WRAP_AAD_SCHEMA,
                    "kek_provider_identifier": self.provider_identifier,
                    "kek_key_identifier": self.key_identifier,
                    "kek_version": self.key_version,
                    "wrapping_algorithm": KEY_WRAP_ALGORITHM,
                }
            )
        except Exception:
            raise KeyProviderConfigurationError() from None

    def wrap_dek(self, dek, *, nonce):
        if (
            type(dek) is not bytes
            or len(dek) != _KEY_BYTES
            or type(nonce) is not bytes
            or len(nonce) != _NONCE_BYTES
        ):
            raise KeyWrapError()
        try:
            combined = AESGCM(self.__key).encrypt(nonce, dek, self._aad())
            if len(combined) != _KEY_BYTES + _TAG_BYTES:
                raise ValueError
            return WrappedDek(
                provider_identifier=self.provider_identifier,
                key_identifier=self.key_identifier,
                key_version=self.key_version,
                algorithm=KEY_WRAP_ALGORITHM,
                nonce=nonce,
                wrapped_key=combined[:-_TAG_BYTES],
                tag=combined[-_TAG_BYTES:],
            )
        except (KeyProviderConfigurationError, KeyWrapError):
            raise
        except Exception:
            raise KeyWrapError() from None

    def unwrap_dek(self, wrapped):
        if (
            type(wrapped) is not WrappedDek
            or wrapped.provider_identifier != self.provider_identifier
            or wrapped.key_identifier != self.key_identifier
            or wrapped.key_version != self.key_version
            or wrapped.algorithm != KEY_WRAP_ALGORITHM
            or type(wrapped.nonce) is not bytes
            or len(wrapped.nonce) != _NONCE_BYTES
            or type(wrapped.wrapped_key) is not bytes
            or len(wrapped.wrapped_key) != _KEY_BYTES
            or type(wrapped.tag) is not bytes
            or len(wrapped.tag) != _TAG_BYTES
        ):
            raise KeyWrapError()
        try:
            dek = AESGCM(self.__key).decrypt(
                wrapped.nonce,
                wrapped.wrapped_key + wrapped.tag,
                self._aad(),
            )
            if type(dek) is not bytes or len(dek) != _KEY_BYTES:
                raise ValueError
            return dek
        except (InvalidTag, ValueError):
            raise KeyWrapError() from None
        except KeyProviderConfigurationError:
            raise
        except Exception:
            raise KeyWrapError() from None


__all__ = [
    "KEY_WRAP_AAD_SCHEMA",
    "KEY_WRAP_ALGORITHM",
    "LOCAL_KEK_PROVIDER_IDENTIFIER",
    "KekProvider",
    "LocalConfiguredKekProvider",
    "WrappedDek",
]
