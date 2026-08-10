"""Key-encryption providers for authenticated backup envelope encryption."""

from __future__ import annotations

import base64
import binascii
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

from .encryption_exceptions import (
    KeyProviderConfigurationError,
    KeyProviderUnavailableError,
    KeyWrapError,
)
from .logical_serialization import encode_canonical_document

LOCAL_KEK_PROVIDER_IDENTIFIER = "local-configured-kek-v1"
AWS_KMS_PROVIDER_IDENTIFIER = "aws-kms-v1"
KEY_WRAP_ALGORITHM = "AES-256-GCM"
AWS_KMS_WRAP_ALGORITHM = "AWS-KMS-SYMMETRIC-DEFAULT"
KEY_WRAP_AAD_SCHEMA = "nexa.dek-wrap-aad.v1"
WRAPPED_DEK_ENVELOPE_SCHEMA = "nexa.wrapped-dek-envelope.v1"
SUPPORTED_KEY_PROVIDER_NAMES = frozenset({"local", "aws_kms"})
AWS_KMS_MAX_ATTEMPTS = 3

_KEY_BYTES = 32
_NONCE_BYTES = 12
_TAG_BYTES = 16
_MAX_KMS_CIPHERTEXT_BYTES = 8_192
_MAX_REFERENCE_LENGTH = 512
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+=,@-]{0,511}$")
_REGION_PATTERN = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z0-9-]+-\d$")
_RETRYABLE_KMS_CODES = frozenset(
    {
        "DependencyTimeoutException",
        "KMSInternalException",
        "ServiceUnavailableException",
        "Throttling",
        "ThrottlingException",
    }
)
_WRAPPED_DOCUMENT_KEYS = frozenset(
    {
        "kek_provider_identifier",
        "kek_key_identifier",
        "kek_version",
        "wrapping_algorithm",
        "nonce_b64",
        "wrapped_key_b64",
        "tag_b64",
    }
)


@dataclass(frozen=True, slots=True)
class WrappedDek:
    provider_identifier: str
    key_identifier: str
    key_version: str
    algorithm: str
    nonce: bytes = field(repr=False)
    wrapped_key: bytes = field(repr=False)
    tag: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class KeyProviderHealth:
    provider_identifier: str
    key_reference: str
    reachable: bool
    enabled: bool


class KeyEncryptionProvider(ABC):
    """Wrap DEKs without exposing raw KEK material to engine callers."""

    development_only = False

    @property
    @abstractmethod
    def provider_identifier(self):
        """Return a safe versioned provider identifier."""

    @property
    @abstractmethod
    def key_identifier(self):
        """Return the active safe non-secret key reference."""

    @property
    @abstractmethod
    def key_version(self):
        """Return the active safe non-secret key version/reference."""

    @abstractmethod
    def validate_configuration(self):
        """Validate configuration without requiring a network call."""

    @abstractmethod
    def wrap_dek(self, dek, *, nonce):
        """Return provider-owned wrapped-key metadata."""

    @abstractmethod
    def unwrap_dek(self, wrapped):
        """Return a transient raw DEK or raise a sanitized error."""

    @abstractmethod
    def health_check(self):
        """Perform one optional non-mutating provider attestation."""

    def can_unwrap(self, wrapped):
        return bool(
            type(wrapped) is WrappedDek
            and wrapped.provider_identifier == self.provider_identifier
            and wrapped.key_identifier == self.key_identifier
            and wrapped.key_version == self.key_version
        )


# Compatibility name retained for Phase 2F callers.
KekProvider = KeyEncryptionProvider


class LocalConfiguredKekProvider(KeyEncryptionProvider):
    """Development/test-only KEK supplied explicitly through configuration."""

    __slots__ = ("__key", "__key_identifier", "__key_version")
    development_only = True

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
                or not _valid_reference(key_identifier)
                or not _valid_reference(key_version)
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

    def validate_configuration(self):
        return True

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
        if not self.can_unwrap(wrapped) or not _valid_local_wrapped_dek(wrapped):
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

    def health_check(self):
        return KeyProviderHealth(
            provider_identifier=self.provider_identifier,
            key_reference=sanitize_key_reference(self.key_identifier),
            reachable=True,
            enabled=True,
        )


class AwsKmsKeyEncryptionProvider(KeyEncryptionProvider):
    """AWS KMS boundary; raw KMS key material never enters Django."""

    __slots__ = ("__client", "__client_factory", "__key_identifier", "__region")

    def __init__(self, *, key_identifier, region, client=None, client_factory=None):
        if not _valid_reference(key_identifier) or not _valid_region(region):
            raise KeyProviderConfigurationError()
        if client is not None and client_factory is not None:
            raise KeyProviderConfigurationError()
        if client is not None and not _valid_kms_client(client):
            raise KeyProviderConfigurationError()
        self.__key_identifier = key_identifier
        self.__region = region
        self.__client = client
        self.__client_factory = client_factory

    @classmethod
    def from_settings(cls, *, client=None, client_factory=None):
        try:
            return cls(
                key_identifier=settings.BACKUP_AWS_KMS_KEY_ID,
                region=settings.BACKUP_AWS_REGION,
                client=client,
                client_factory=client_factory,
            )
        except (AttributeError, TypeError, ValueError):
            raise KeyProviderConfigurationError() from None

    @property
    def provider_identifier(self):
        return AWS_KMS_PROVIDER_IDENTIFIER

    @property
    def key_identifier(self):
        return self.__key_identifier

    @property
    def key_version(self):
        # Encrypt returns the immutable provider KeyId stored on each artifact.
        return self.__key_identifier

    @property
    def region(self):
        return self.__region

    @property
    def sdk_max_attempts(self):
        return AWS_KMS_MAX_ATTEMPTS

    def __repr__(self):
        return (
            "AwsKmsKeyEncryptionProvider("
            f"provider_identifier={self.provider_identifier!r}, "
            f"key_identifier={sanitize_key_reference(self.key_identifier)!r}, "
            f"region={self.region!r})"
        )

    def validate_configuration(self):
        if not _valid_reference(self.key_identifier) or not _valid_region(self.region):
            raise KeyProviderConfigurationError()
        return True

    def _kms_client(self):
        if self.__client is not None:
            return self.__client
        try:
            if self.__client_factory is not None:
                client = self.__client_factory()
            else:
                import boto3
                from botocore.config import Config

                client = boto3.client(
                    "kms",
                    region_name=self.region,
                    config=Config(
                        retries={
                            "mode": "standard",
                            "total_max_attempts": AWS_KMS_MAX_ATTEMPTS,
                        }
                    ),
                )
            if not _valid_kms_client(client):
                raise ValueError
            self.__client = client
            return client
        except Exception:
            raise KeyProviderConfigurationError() from None

    def _encryption_context(self, key_identifier):
        if not _valid_reference(key_identifier):
            raise KeyWrapError()
        return {
            "nexa:purpose": "backup-dek",
            "nexa:provider": self.provider_identifier,
            "nexa:key-reference": key_identifier,
        }

    @staticmethod
    def _raise_sanitized(exc):
        code = ""
        try:
            code = str(exc.response["Error"]["Code"])
        except (AttributeError, KeyError, TypeError):
            pass
        if not code or code in _RETRYABLE_KMS_CODES:
            raise KeyProviderUnavailableError() from None
        raise KeyWrapError() from None

    def wrap_dek(self, dek, *, nonce):
        del nonce  # KMS owns wrapping randomness and ciphertext framing.
        if type(dek) is not bytes or len(dek) != _KEY_BYTES:
            raise KeyWrapError()
        try:
            response = self._kms_client().encrypt(
                KeyId=self.key_identifier,
                Plaintext=dek,
                EncryptionContext=self._encryption_context(self.key_identifier),
                EncryptionAlgorithm="SYMMETRIC_DEFAULT",
            )
            wrapped = WrappedDek(
                provider_identifier=self.provider_identifier,
                key_identifier=self.key_identifier,
                key_version=response["KeyId"],
                algorithm=AWS_KMS_WRAP_ALGORITHM,
                nonce=b"",
                wrapped_key=bytes(response["CiphertextBlob"]),
                tag=b"",
            )
            if not _valid_aws_wrapped_dek(wrapped):
                raise KeyWrapError()
            return wrapped
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except (KeyProviderConfigurationError, KeyWrapError):
            raise
        except Exception as exc:
            self._raise_sanitized(exc)

    def can_unwrap(self, wrapped):
        return bool(
            type(wrapped) is WrappedDek
            and wrapped.provider_identifier == self.provider_identifier
            and _valid_aws_wrapped_dek(wrapped)
        )

    def unwrap_dek(self, wrapped):
        if not self.can_unwrap(wrapped):
            raise KeyWrapError()
        try:
            response = self._kms_client().decrypt(
                CiphertextBlob=wrapped.wrapped_key,
                KeyId=wrapped.key_version,
                EncryptionContext=self._encryption_context(wrapped.key_identifier),
                EncryptionAlgorithm="SYMMETRIC_DEFAULT",
            )
            dek = response["Plaintext"]
            returned_key_id = response.get("KeyId", wrapped.key_version)
            if (
                type(dek) is not bytes
                or len(dek) != _KEY_BYTES
                or returned_key_id != wrapped.key_version
            ):
                raise KeyWrapError()
            return dek
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except (KeyProviderConfigurationError, KeyWrapError):
            raise
        except Exception as exc:
            self._raise_sanitized(exc)

    def health_check(self):
        try:
            response = self._kms_client().describe_key(KeyId=self.key_identifier)
            metadata = response["KeyMetadata"]
            enabled = bool(
                metadata.get("Enabled") is True
                and metadata.get("KeyState") == "Enabled"
                and metadata.get("KeyUsage") == "ENCRYPT_DECRYPT"
            )
            return KeyProviderHealth(
                provider_identifier=self.provider_identifier,
                key_reference=sanitize_key_reference(self.key_identifier),
                reachable=True,
                enabled=enabled,
            )
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except (KeyProviderConfigurationError, KeyWrapError):
            raise
        except Exception as exc:
            self._raise_sanitized(exc)


class KeyEncryptionProviderRegistry:
    """Explicit active/historical provider resolver with no fallback."""

    __slots__ = ("active_provider", "_providers")

    def __init__(self, *, active_provider, historical_providers=()):
        if not isinstance(active_provider, KeyEncryptionProvider):
            raise KeyProviderConfigurationError()
        try:
            providers = (active_provider, *tuple(historical_providers))
        except TypeError:
            raise KeyProviderConfigurationError() from None
        if not all(isinstance(provider, KeyEncryptionProvider) for provider in providers):
            raise KeyProviderConfigurationError()
        identities = [
            (
                provider.provider_identifier,
                provider.key_identifier,
                provider.key_version,
            )
            for provider in providers
        ]
        if len(identities) != len(set(identities)):
            raise KeyProviderConfigurationError()
        self.active_provider = active_provider
        self._providers = providers

    @property
    def providers(self):
        return self._providers

    def resolve(self, wrapped):
        validate_wrapped_dek(wrapped)
        exact = [
            provider
            for provider in self._providers
            if provider.provider_identifier == wrapped.provider_identifier
            and provider.key_identifier == wrapped.key_identifier
            and provider.key_version == wrapped.key_version
        ]
        candidates = exact or [
            provider
            for provider in self._providers
            if provider.provider_identifier == wrapped.provider_identifier
            and provider.can_unwrap(wrapped)
        ]
        if len(candidates) != 1:
            raise KeyProviderConfigurationError()
        return candidates[0]


def selected_key_provider_name():
    value = getattr(settings, "BACKUP_KEY_PROVIDER", "local")
    if type(value) is not str or value not in SUPPORTED_KEY_PROVIDER_NAMES:
        raise KeyProviderConfigurationError()
    return value


def validate_key_provider_settings():
    selected = selected_key_provider_name()
    if selected == "local":
        LocalConfiguredKekProvider.from_settings().validate_configuration()
    elif not (
        _valid_reference(getattr(settings, "BACKUP_AWS_KMS_KEY_ID", ""))
        and _valid_region(getattr(settings, "BACKUP_AWS_REGION", ""))
    ):
        raise KeyProviderConfigurationError()
    return selected


def build_key_provider_registry_from_settings(*, aws_client=None, aws_client_factory=None):
    selected = validate_key_provider_settings()
    if selected == "local":
        active = LocalConfiguredKekProvider.from_settings()
    else:
        active = AwsKmsKeyEncryptionProvider.from_settings(
            client=aws_client,
            client_factory=aws_client_factory,
        )
    active.validate_configuration()
    return KeyEncryptionProviderRegistry(active_provider=active)


def wrapped_dek_document(wrapped):
    validate_wrapped_dek(wrapped)
    return {
        "kek_provider_identifier": wrapped.provider_identifier,
        "kek_key_identifier": wrapped.key_identifier,
        "kek_version": wrapped.key_version,
        "wrapping_algorithm": wrapped.algorithm,
        "nonce_b64": _b64(wrapped.nonce),
        "wrapped_key_b64": _b64(wrapped.wrapped_key),
        "tag_b64": _b64(wrapped.tag),
    }


def wrapped_dek_from_document(document):
    try:
        if type(document) is not dict or frozenset(document) != _WRAPPED_DOCUMENT_KEYS:
            raise ValueError
        wrapped = WrappedDek(
            provider_identifier=document["kek_provider_identifier"],
            key_identifier=document["kek_key_identifier"],
            key_version=document["kek_version"],
            algorithm=document["wrapping_algorithm"],
            nonce=_strict_b64(document["nonce_b64"], maximum_bytes=_NONCE_BYTES),
            wrapped_key=_strict_b64(
                document["wrapped_key_b64"],
                maximum_bytes=_MAX_KMS_CIPHERTEXT_BYTES,
            ),
            tag=_strict_b64(document["tag_b64"], maximum_bytes=_TAG_BYTES),
        )
        return validate_wrapped_dek(wrapped)
    except KeyWrapError:
        raise
    except (UnicodeError, ValueError, TypeError, binascii.Error):
        raise KeyWrapError() from None


def serialize_wrapped_dek(wrapped):
    try:
        return encode_canonical_document(
            {
                "schema": WRAPPED_DEK_ENVELOPE_SCHEMA,
                "wrapped_dek": wrapped_dek_document(wrapped),
            }
        ).decode("utf-8")
    except KeyWrapError:
        raise
    except Exception:
        raise KeyWrapError() from None


def deserialize_wrapped_dek(value):
    try:
        if type(value) is not str or not value or len(value) > 32_768:
            raise ValueError
        document = json.loads(value)
        if (
            type(document) is not dict
            or frozenset(document) != {"schema", "wrapped_dek"}
            or document["schema"] != WRAPPED_DEK_ENVELOPE_SCHEMA
        ):
            raise ValueError
        wrapped = wrapped_dek_from_document(document["wrapped_dek"])
        if serialize_wrapped_dek(wrapped) != value:
            raise ValueError
        return wrapped
    except KeyWrapError:
        raise
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise KeyWrapError() from None


def wrapped_dek_key_identifier(wrapped):
    validate_wrapped_dek(wrapped)
    return key_metadata_identifier(
        wrapped.provider_identifier,
        wrapped.key_identifier,
        wrapped.key_version,
    )


def key_metadata_identifier(provider_identifier, key_identifier, key_version):
    if not all(
        _valid_reference(value)
        for value in (provider_identifier, key_identifier, key_version)
    ):
        raise KeyProviderConfigurationError()
    value = f"{provider_identifier}:{key_identifier}:{key_version}"
    if len(value) > 255:
        raise KeyProviderConfigurationError()
    return value


def sanitize_key_reference(value):
    if type(value) is not str or not value:
        return "unavailable"
    if value.startswith("alias/") and _valid_reference(value):
        return value
    suffix = value.rsplit("/", 1)[-1][-24:]
    return f".../{suffix}" if suffix else "unavailable"


def validate_wrapped_dek(wrapped):
    if (
        type(wrapped) is not WrappedDek
        or not _valid_reference(wrapped.provider_identifier)
        or not _valid_reference(wrapped.key_identifier)
        or not _valid_reference(wrapped.key_version)
        or not (
            (
                wrapped.provider_identifier == LOCAL_KEK_PROVIDER_IDENTIFIER
                and _valid_local_wrapped_dek(wrapped)
            )
            or (
                wrapped.provider_identifier == AWS_KMS_PROVIDER_IDENTIFIER
                and _valid_aws_wrapped_dek(wrapped)
            )
        )
    ):
        raise KeyWrapError()
    return wrapped


def _valid_reference(value):
    return bool(
        type(value) is str
        and len(value) <= _MAX_REFERENCE_LENGTH
        and _IDENTIFIER_PATTERN.fullmatch(value) is not None
    )


def _valid_region(value):
    return bool(
        type(value) is str
        and 3 <= len(value) <= 64
        and _REGION_PATTERN.fullmatch(value) is not None
    )


def _valid_kms_client(client):
    return all(
        callable(getattr(client, operation, None))
        for operation in ("encrypt", "decrypt", "describe_key")
    )


def _valid_local_wrapped_dek(wrapped):
    return bool(
        wrapped.algorithm == KEY_WRAP_ALGORITHM
        and type(wrapped.nonce) is bytes
        and len(wrapped.nonce) == _NONCE_BYTES
        and type(wrapped.wrapped_key) is bytes
        and len(wrapped.wrapped_key) == _KEY_BYTES
        and type(wrapped.tag) is bytes
        and len(wrapped.tag) == _TAG_BYTES
    )


def _valid_aws_wrapped_dek(wrapped):
    return bool(
        wrapped.algorithm == AWS_KMS_WRAP_ALGORITHM
        and wrapped.nonce == b""
        and type(wrapped.wrapped_key) is bytes
        and 1 <= len(wrapped.wrapped_key) <= _MAX_KMS_CIPHERTEXT_BYTES
        and wrapped.tag == b""
    )


def _b64(value):
    if type(value) is not bytes:
        raise KeyWrapError()
    return base64.b64encode(value).decode("ascii")


def _strict_b64(value, *, maximum_bytes):
    if type(value) is not str:
        raise ValueError
    decoded = base64.b64decode(value.encode("ascii"), validate=True)
    if (
        len(decoded) > maximum_bytes
        or base64.b64encode(decoded).decode("ascii") != value
    ):
        raise ValueError
    return decoded


__all__ = [
    "AWS_KMS_MAX_ATTEMPTS",
    "AWS_KMS_PROVIDER_IDENTIFIER",
    "AWS_KMS_WRAP_ALGORITHM",
    "AwsKmsKeyEncryptionProvider",
    "KEY_WRAP_AAD_SCHEMA",
    "KEY_WRAP_ALGORITHM",
    "KeyEncryptionProvider",
    "KeyEncryptionProviderRegistry",
    "KeyProviderHealth",
    "KekProvider",
    "LOCAL_KEK_PROVIDER_IDENTIFIER",
    "LocalConfiguredKekProvider",
    "SUPPORTED_KEY_PROVIDER_NAMES",
    "WRAPPED_DEK_ENVELOPE_SCHEMA",
    "WrappedDek",
    "build_key_provider_registry_from_settings",
    "deserialize_wrapped_dek",
    "key_metadata_identifier",
    "sanitize_key_reference",
    "selected_key_provider_name",
    "serialize_wrapped_dek",
    "validate_key_provider_settings",
    "validate_wrapped_dek",
    "wrapped_dek_document",
    "wrapped_dek_from_document",
    "wrapped_dek_key_identifier",
]
