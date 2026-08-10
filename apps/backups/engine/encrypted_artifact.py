"""Authenticated envelope encryption for verified Phase 2E packages."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import shutil
import stat
import struct
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.utils import timezone

from .canonical_manifest import PACKAGE_FORMAT_IDENTIFIER
from .context import BackupExecutionContext
from .contracts import (
    EncryptedArtifactReference,
    EncryptedArtifactRequest,
    EncryptedArtifactResult,
    PackageBuildResult,
    PackageCompatibilityStatus,
    PackageReference,
    PackageVerificationResult,
    RestoredPlaintextEvidence,
    VerificationReference,
)
from .deterministic_package import DeterministicPackageProvider
from .encryption_exceptions import (
    EncryptedArtifactCleanupError,
    EncryptedArtifactCreationError,
    EncryptedArtifactNotFound,
    EncryptedArtifactValidationError,
    EncryptionPolicyError,
    KeyProviderConfigurationError,
    KeyRewrapError,
    KeyWrapError,
    Phase2FCoordinationError,
    Phase2FEngineError,
    PlaintextPackageCleanupError,
)
from .encryption_policy import EncryptionPolicy
from .key_management import (
    KeyEncryptionProvider,
    KeyEncryptionProviderRegistry,
    deserialize_wrapped_dek,
    serialize_wrapped_dek,
    wrapped_dek_document,
    wrapped_dek_from_document,
    wrapped_dek_key_identifier,
)
from .logical_serialization import encode_canonical_document
from .package_exceptions import PackageCleanupError, PackageNotFound, PackageValidationError
from .package_verification import (
    INDEPENDENT_PACKAGE_VERIFIER_IDENTIFIER,
    VERIFICATION_SCHEMA_IDENTIFIER,
    VERIFICATION_VERSION,
    IndependentPackageVerifier,
)
from .verification_exceptions import VerificationProviderStateError
from .workspace import (
    BackupWorkspaceManager,
    WorkspaceArea,
    WorkspaceReference,
    contained_path,
    path_is_link_like,
)

ENCRYPTED_ARTIFACT_PROVIDER_IDENTIFIER = "encrypted-artifact-provider-v1"
ENCRYPTED_ARTIFACT_FORMAT_IDENTIFIER = "nexa.encrypted-backup.v1"
ENCRYPTED_ARTIFACT_FORMAT_VERSION = "1.0.0"
ENCRYPTION_ALGORITHM = "AES-256-GCM"
ARTIFACT_FILE_NAME = "artifact.bin"
ARTIFACT_MAGIC = b"NEXA2F01"

_PREFIX = struct.Struct(">8sI")
_KEY_BYTES = 32
_NONCE_BYTES = 12
_TAG_BYTES = 16
_SHA256_HEX = frozenset("0123456789abcdef")
_HEADER_KEYS = frozenset(
    {
        "schema",
        "format_version",
        "encryption_algorithm",
        "nonce_b64",
        "wrapped_dek",
        "plaintext_byte_count",
        "plaintext_sha256",
        "ciphertext_byte_count",
        "verified_package_format",
        "backup_public_id",
        "tenant_public_id",
        "verification_schema",
        "verification_version",
        "verification_provider",
        "created_timestamp",
    }
)
_WRAPPED_KEYS = frozenset(
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
class _PublishedArtifact:
    context: BackupExecutionContext
    package: PackageBuildResult
    verification: PackageVerificationResult
    result: EncryptedArtifactResult
    directory_identity: tuple[int, int]
    file_identity: tuple[int, int] | None


@dataclass(frozen=True, slots=True)
class RewrappedArtifactKeyResult:
    previous_key_identifier: str
    new_key_identifier: str
    previous_envelope: str = field(repr=False)
    new_envelope: str = field(repr=False)
    encrypted_byte_count: int
    artifact_sha256: str


class _OpaqueArtifactReader:
    __slots__ = ("__file",)

    def __init__(self, file_object):
        self.__file = file_object

    def read(self, size=-1):
        return self.__file.read(size)

    def readline(self, size=-1):
        return self.__file.readline(size)

    def seek(self, offset, whence=io.SEEK_SET):
        return self.__file.seek(offset, whence)

    def tell(self):
        return self.__file.tell()

    def close(self):
        return self.__file.close()

    @property
    def closed(self):
        return self.__file.closed


class _DecryptingReader:
    __slots__ = (
        "__file",
        "__decryptor",
        "__remaining",
        "__chunk_bytes",
        "__expected_count",
        "__expected_sha256",
        "__count",
        "__digest",
        "__finalized",
        "__closed",
        "__deadline_check",
    )

    def __init__(
        self,
        *,
        file_object,
        decryptor,
        ciphertext_bytes,
        chunk_bytes,
        expected_count,
        expected_sha256,
        deadline_check,
    ):
        self.__file = file_object
        self.__decryptor = decryptor
        self.__remaining = ciphertext_bytes
        self.__chunk_bytes = chunk_bytes
        self.__expected_count = expected_count
        self.__expected_sha256 = expected_sha256
        self.__count = 0
        self.__digest = hashlib.sha256()
        self.__finalized = False
        self.__closed = False
        self.__deadline_check = deadline_check

    def _finalize(self):
        if self.__finalized:
            return
        try:
            self.__deadline_check()
            tail = self.__decryptor.finalize()
            if tail:
                self.__count += len(tail)
                self.__digest.update(tail)
            if (
                self.__count != self.__expected_count
                or self.__digest.hexdigest() != self.__expected_sha256
            ):
                raise EncryptedArtifactValidationError()
            self.__finalized = True
        except EncryptedArtifactValidationError:
            raise
        except (InvalidTag, ValueError):
            raise EncryptedArtifactValidationError() from None
        except Exception:
            raise EncryptedArtifactValidationError() from None

    def read(self, size=-1):
        if self.__closed:
            raise EncryptedArtifactValidationError()
        if self.__remaining == 0:
            self._finalize()
            return b""
        if type(size) is not int:
            raise EncryptedArtifactValidationError()
        requested = self.__chunk_bytes if size < 0 else min(size, self.__chunk_bytes)
        if requested <= 0:
            return b""
        requested = min(requested, self.__remaining)
        try:
            self.__deadline_check()
            ciphertext = self.__file.read(requested)
            if type(ciphertext) is not bytes or len(ciphertext) != requested:
                raise EncryptedArtifactValidationError()
            plaintext = self.__decryptor.update(ciphertext)
            if type(plaintext) is not bytes or len(plaintext) != len(ciphertext):
                raise EncryptedArtifactValidationError()
            self.__remaining -= len(ciphertext)
            self.__count += len(plaintext)
            if self.__count > self.__expected_count:
                raise EncryptedArtifactValidationError()
            self.__digest.update(plaintext)
            if self.__remaining == 0:
                self._finalize()
            return plaintext
        except EncryptedArtifactValidationError:
            raise
        except (InvalidTag, OSError, ValueError):
            raise EncryptedArtifactValidationError() from None

    def close(self):
        if self.__closed:
            return
        while not self.__finalized:
            self.read(self.__chunk_bytes)
        self.__closed = True

    @property
    def closed(self):
        return self.__closed


def _identity(current):
    return current.st_dev, current.st_ino


def _is_aware(value):
    return (
        type(value) is datetime
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _utc_timestamp(value, *, error_type):
    if not _is_aware(value):
        raise error_type()
    try:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
            "+00:00",
            "Z",
        )
    except (OverflowError, TypeError, ValueError):
        raise error_type() from None


def _parse_utc_timestamp(value, *, error_type):
    try:
        if type(value) is not str or not value.endswith("Z"):
            raise ValueError
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
        if not _is_aware(parsed) or _utc_timestamp(parsed, error_type=error_type) != value:
            raise ValueError
        return parsed.astimezone(UTC)
    except (OverflowError, TypeError, ValueError):
        raise error_type() from None


def _sha256(value, *, error_type):
    if (
        type(value) is not str
        or len(value) != 64
        or not set(value).issubset(_SHA256_HEX)
    ):
        raise error_type()
    return value


def _count(value, *, maximum, positive=False, error_type):
    if (
        type(value) is not int
        or value < (1 if positive else 0)
        or value > maximum
    ):
        raise error_type()
    return value


def _exact_keys(value, expected, *, error_type):
    if type(value) is not dict or frozenset(value) != frozenset(expected):
        raise error_type()
    return value


def _strict_b64(value, *, expected_bytes, error_type):
    try:
        if type(value) is not str or not value:
            raise ValueError
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
        if len(decoded) != expected_bytes or base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError
        return decoded
    except (UnicodeError, ValueError, binascii.Error):
        raise error_type() from None


def _b64(value):
    if type(value) is not bytes:
        raise EncryptedArtifactCreationError()
    return base64.b64encode(value).decode("ascii")


def _duplicate_rejecting_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_number(_value):
    raise ValueError


def _strict_header(raw, *, maximum_bytes):
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= maximum_bytes:
            raise ValueError
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
        if type(value) is not dict or encode_canonical_document(value) != raw:
            raise ValueError
        return value
    except (
        UnicodeError,
        json.JSONDecodeError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        raise EncryptedArtifactValidationError() from None


def _directory_state(path, *, error_type):
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        raise error_type() from None
    if path_is_link_like(path) or not stat.S_ISDIR(current.st_mode):
        raise error_type()
    return current


def _regular_file_state(path, *, error_type, require_single_link=True):
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        raise error_type() from None
    if (
        path_is_link_like(path)
        or not stat.S_ISREG(current.st_mode)
        or (require_single_link and current.st_nlink != 1)
    ):
        raise error_type()
    return current


def _apply_private_mode(path, mode, *, error_type):
    try:
        os.chmod(path, mode)
        current = os.stat(path, follow_symlinks=False)
        if os.name != "nt" and stat.S_IMODE(current.st_mode) != mode:
            raise error_type()
    except Phase2FEngineError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise error_type() from None


def _apply_private_descriptor_mode(descriptor, path, mode, *, error_type):
    try:
        before = os.fstat(descriptor)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        else:
            _apply_private_mode(path, mode, error_type=error_type)
        after = os.fstat(descriptor)
        if (
            _identity(before) != _identity(after)
            or not stat.S_ISREG(after.st_mode)
            or (os.name != "nt" and stat.S_IMODE(after.st_mode) != mode)
        ):
            raise error_type()
    except Phase2FEngineError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise error_type() from None


def _assert_private_mode(path, mode, *, error_type):
    if os.name == "nt":
        return
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        raise error_type() from None
    if stat.S_IMODE(current.st_mode) != mode:
        raise error_type()


class EncryptedArtifactProvider:
    """Create, authenticate, open, and exactly clean encrypted artifacts."""

    def __init__(
        self,
        *,
        package_provider,
        verification_provider,
        kek_provider,
        key_provider_registry=None,
        workspace_manager=None,
        policy=None,
        reference_factory=None,
        random_bytes=None,
        clock=None,
        monotonic=None,
        disk_usage_provider=None,
        failure_hook=None,
    ):
        if (
            type(package_provider) is not DeterministicPackageProvider
            or type(verification_provider) is not IndependentPackageVerifier
            or verification_provider.package_provider is not package_provider
            or not isinstance(kek_provider, KeyEncryptionProvider)
            or (
                key_provider_registry is not None
                and (
                    type(key_provider_registry) is not KeyEncryptionProviderRegistry
                    or key_provider_registry.active_provider is not kek_provider
                )
            )
        ):
            raise Phase2FCoordinationError()
        manager = workspace_manager or package_provider.workspace_manager
        if (
            type(manager) is not BackupWorkspaceManager
            or manager.root != package_provider.workspace_manager.root
            or manager.root != verification_provider.workspace_manager.root
        ):
            raise Phase2FCoordinationError()
        selected_policy = policy or EncryptionPolicy.from_settings()
        if type(selected_policy) is not EncryptionPolicy:
            raise EncryptionPolicyError()
        self.policy = selected_policy.validated()
        self.package_provider = package_provider
        self.verification_provider = verification_provider
        self.kek_provider = kek_provider
        self.key_provider_registry = key_provider_registry
        self.workspace_manager = manager
        self.reference_factory = reference_factory or (
            lambda: EncryptedArtifactReference(uuid.uuid4())
        )
        self.random_bytes = random_bytes or os.urandom
        self.clock = clock or timezone.now
        self.monotonic = monotonic or time.monotonic
        self.disk_usage_provider = disk_usage_provider or shutil.disk_usage
        self.failure_hook = failure_hook
        self._published = {}
        self._cleaned = {}
        self._used_data_nonces = set()
        self._used_wrap_nonces = set()
        self._used_dek_digests = set()
        self._state_lock = threading.RLock()

    def _provider_for_wrapped_dek(self, wrapped):
        try:
            if self.key_provider_registry is not None:
                return self.key_provider_registry.resolve(wrapped)
            if not self.kek_provider.can_unwrap(wrapped):
                raise KeyProviderConfigurationError()
            return self.kek_provider
        except (KeyProviderConfigurationError, KeyWrapError):
            raise EncryptedArtifactValidationError() from None

    @staticmethod
    def _wrapped_dek_from_header(document):
        try:
            return wrapped_dek_from_document(document)
        except KeyWrapError:
            raise EncryptedArtifactValidationError() from None

    def _run_hook(self, stage):
        if self.failure_hook is not None:
            self.failure_hook(stage)

    def _check_deadline(self, deadline, *, error_type):
        try:
            if self.monotonic() > deadline:
                raise error_type()
        except Phase2FEngineError:
            raise
        except Exception:
            raise error_type() from None

    def _new_reference(self):
        try:
            reference = self.reference_factory()
            if type(reference) is uuid.UUID:
                reference = EncryptedArtifactReference(reference)
            if (
                type(reference) is not EncryptedArtifactReference
                or type(reference.identifier) is not uuid.UUID
            ):
                raise TypeError
            return reference
        except (AttributeError, TypeError, ValueError):
            raise EncryptedArtifactCreationError() from None

    def _new_material(self):
        try:
            dek = self.random_bytes(_KEY_BYTES)
            data_nonce = self.random_bytes(_NONCE_BYTES)
            wrap_nonce = self.random_bytes(_NONCE_BYTES)
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception:
            raise EncryptedArtifactCreationError() from None
        if (
            type(dek) is not bytes
            or len(dek) != _KEY_BYTES
            or type(data_nonce) is not bytes
            or len(data_nonce) != _NONCE_BYTES
            or type(wrap_nonce) is not bytes
            or len(wrap_nonce) != _NONCE_BYTES
            or data_nonce == wrap_nonce
        ):
            raise EncryptedArtifactCreationError()
        dek_digest = hashlib.sha256(dek).digest()
        with self._state_lock:
            if (
                data_nonce in self._used_data_nonces
                or wrap_nonce in self._used_wrap_nonces
                or dek_digest in self._used_dek_digests
            ):
                raise EncryptedArtifactCreationError()
            self._used_data_nonces.add(data_nonce)
            self._used_wrap_nonces.add(wrap_nonce)
            self._used_dek_digests.add(dek_digest)
        return dek, data_nonce, wrap_nonce

    def _state_key(self, context, reference, *, error_type):
        if (
            type(context) is not BackupExecutionContext
            or type(context.workspace_reference) is not WorkspaceReference
            or type(reference) is not EncryptedArtifactReference
            or type(reference.identifier) is not uuid.UUID
        ):
            raise error_type()
        return context.workspace_reference.identifier, reference.identifier

    def _validate_request(self, request):
        if type(request) is not EncryptedArtifactRequest:
            raise Phase2FCoordinationError()
        context = request.context
        package = request.package
        verification = request.verification
        if (
            type(context) is not BackupExecutionContext
            or type(context.workspace_reference) is not WorkspaceReference
            or type(package) is not PackageBuildResult
            or type(package.reference) is not PackageReference
            or type(package.reference.identifier) is not uuid.UUID
            or type(verification) is not PackageVerificationResult
            or type(verification.reference) is not VerificationReference
            or type(verification.reference.identifier) is not uuid.UUID
            or verification.verified is not True
            or verification.restore_ready is not True
            or verification.issues != ()
            or verification.compatibility_status
            != PackageCompatibilityStatus.COMPATIBLE
            or verification.package_byte_count != package.byte_count
            or verification.plaintext_sha256 != package.plaintext_sha256
            or verification.entry_count != package.entry_count
            or verification.payload_set_sha256 != package.payload_set_sha256
            or verification.provider_identifier
            != INDEPENDENT_PACKAGE_VERIFIER_IDENTIFIER
            or verification.verification_schema != VERIFICATION_SCHEMA_IDENTIFIER
            or not _is_aware(verification.verified_at)
            or package.byte_count <= 0
            or package.byte_count > self.policy.maximum_plaintext_bytes
        ):
            raise Phase2FCoordinationError()
        _sha256(package.plaintext_sha256, error_type=Phase2FCoordinationError)
        _sha256(package.payload_set_sha256, error_type=Phase2FCoordinationError)
        try:
            self.package_provider.validate_package_evidence(
                context=context,
                result=package,
            )
            self.verification_provider.validate_verification_evidence(
                context=context,
                package=package,
                result=verification,
            )
        except (PackageNotFound, PackageValidationError, VerificationProviderStateError):
            raise Phase2FCoordinationError() from None
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception:
            raise Phase2FCoordinationError() from None
        return context, package, verification

    def _existing_workspace(self, context, *, error_type):
        try:
            root = self.workspace_manager.root
            root_state = _directory_state(root, error_type=error_type)
            workspace = self.workspace_manager.handle(context.workspace_reference)
            workspace_state = _directory_state(workspace.path, error_type=error_type)
            if root_state.st_dev != workspace_state.st_dev:
                raise error_type()
            _assert_private_mode(root, 0o700, error_type=error_type)
            _assert_private_mode(workspace.path, 0o700, error_type=error_type)
            return workspace
        except Phase2FEngineError:
            raise
        except Exception:
            raise error_type() from None

    def _artifact_parent(self, context, *, create, error_type):
        workspace = self._existing_workspace(context, error_type=error_type)
        try:
            parent = workspace.system_area_path(WorkspaceArea.ENCRYPTED)
            if os.path.lexists(parent) and path_is_link_like(parent):
                raise error_type()
            if create:
                parent.mkdir(mode=0o700, exist_ok=True)
            state = _directory_state(parent, error_type=error_type)
            if state.st_dev != _directory_state(
                workspace.path,
                error_type=error_type,
            ).st_dev:
                raise error_type()
            _apply_private_mode(parent, 0o700, error_type=error_type)
            if _identity(_directory_state(parent, error_type=error_type)) != _identity(
                state
            ):
                raise error_type()
            return workspace, parent
        except Phase2FEngineError:
            raise
        except Exception:
            raise error_type() from None

    def _artifact_directory(
        self,
        context,
        reference,
        *,
        require_exists,
        error_type,
    ):
        workspace, parent = self._artifact_parent(
            context,
            create=False,
            error_type=error_type,
        )
        try:
            directory = workspace.system_area_path(
                WorkspaceArea.ENCRYPTED,
                generated_identifier=reference.identifier,
            )
            if os.path.lexists(directory) and path_is_link_like(directory):
                raise error_type()
            if require_exists:
                state = _directory_state(directory, error_type=error_type)
                if state.st_dev != _directory_state(parent, error_type=error_type).st_dev:
                    raise error_type()
            return directory
        except Phase2FEngineError:
            raise
        except Exception:
            raise error_type() from None

    @staticmethod
    def _remove_empty_directory(directory, *, expected_identity, error_type):
        if directory is None or not os.path.lexists(directory):
            return False
        state = _directory_state(directory, error_type=error_type)
        if _identity(state) != expected_identity:
            raise error_type()
        with os.scandir(directory) as entries:
            if next(entries, None) is not None:
                return False
        os.rmdir(directory)
        if os.path.lexists(directory):
            raise error_type()
        return True

    @staticmethod
    def _cleanup_owned_files(paths, *, expected_identity):
        owned = []
        for path in paths:
            if path is not None and path not in owned and os.path.lexists(path):
                owned.append(path)
        remaining_links = len(owned)
        for path in owned:
            try:
                current = os.stat(path, follow_symlinks=False)
            except OSError:
                raise EncryptedArtifactCleanupError() from None
            if (
                path_is_link_like(path)
                or not stat.S_ISREG(current.st_mode)
                or _identity(current) != expected_identity
                or current.st_nlink != remaining_links
            ):
                raise EncryptedArtifactCleanupError()
            os.unlink(path)
            if os.path.lexists(path):
                raise EncryptedArtifactCleanupError()
            remaining_links -= 1
        return bool(owned)

    def _capacity_check(self, parent, package):
        planned = _PREFIX.size + self.policy.maximum_header_bytes + package.byte_count + _TAG_BYTES
        if planned > self.policy.maximum_artifact_bytes:
            raise EncryptionPolicyError()
        try:
            free = self.disk_usage_provider(parent).free
        except Exception:
            raise EncryptedArtifactCreationError() from None
        required = max(
            self.policy.minimum_free_bytes,
            int(planned * self.policy.headroom_multiplier),
        )
        if type(free) is not int or free < required:
            raise EncryptedArtifactCreationError()

    def _header_document(
        self,
        *,
        context,
        package,
        verification,
        data_nonce,
        wrapped,
        created_at,
    ):
        return {
            "schema": ENCRYPTED_ARTIFACT_FORMAT_IDENTIFIER,
            "format_version": ENCRYPTED_ARTIFACT_FORMAT_VERSION,
            "encryption_algorithm": ENCRYPTION_ALGORITHM,
            "nonce_b64": _b64(data_nonce),
            "wrapped_dek": wrapped_dek_document(wrapped),
            "plaintext_byte_count": package.byte_count,
            "plaintext_sha256": package.plaintext_sha256,
            "ciphertext_byte_count": package.byte_count,
            "verified_package_format": package.format_identifier,
            "backup_public_id": str(context.backup_public_id),
            "tenant_public_id": str(context.business_public_id),
            "verification_schema": verification.verification_schema,
            "verification_version": VERIFICATION_VERSION,
            "verification_provider": verification.provider_identifier,
            "created_timestamp": _utc_timestamp(
                created_at,
                error_type=EncryptedArtifactCreationError,
            ),
        }

    @staticmethod
    def _write_all(descriptor, value, *, artifact_digest, byte_counter):
        offset = 0
        while offset < len(value):
            written = os.write(descriptor, value[offset:])
            if type(written) is not int or written <= 0:
                raise EncryptedArtifactCreationError()
            fragment = value[offset : offset + written]
            artifact_digest.update(fragment)
            byte_counter[0] += written
            offset += written

    def _parse_header(
        self,
        file_object,
        *,
        file_size,
        context,
        package,
        verification,
        result,
    ):
        try:
            file_object.seek(0)
            prefix = file_object.read(_PREFIX.size)
            if type(prefix) is not bytes or len(prefix) != _PREFIX.size:
                raise EncryptedArtifactValidationError()
            magic, header_size = _PREFIX.unpack(prefix)
            if (
                magic != ARTIFACT_MAGIC
                or not 0 < header_size <= self.policy.maximum_header_bytes
            ):
                raise EncryptedArtifactValidationError()
            header_bytes = file_object.read(header_size)
            if type(header_bytes) is not bytes or len(header_bytes) != header_size:
                raise EncryptedArtifactValidationError()
            document = _strict_header(
                header_bytes,
                maximum_bytes=self.policy.maximum_header_bytes,
            )
            _exact_keys(
                document,
                _HEADER_KEYS,
                error_type=EncryptedArtifactValidationError,
            )
            wrapped_document = _exact_keys(
                document["wrapped_dek"],
                _WRAPPED_KEYS,
                error_type=EncryptedArtifactValidationError,
            )
            data_nonce = _strict_b64(
                document["nonce_b64"],
                expected_bytes=_NONCE_BYTES,
                error_type=EncryptedArtifactValidationError,
            )
            wrapped = self._wrapped_dek_from_header(wrapped_document)
            plaintext_count = _count(
                document["plaintext_byte_count"],
                maximum=self.policy.maximum_plaintext_bytes,
                positive=True,
                error_type=EncryptedArtifactValidationError,
            )
            ciphertext_count = _count(
                document["ciphertext_byte_count"],
                maximum=self.policy.maximum_plaintext_bytes,
                positive=True,
                error_type=EncryptedArtifactValidationError,
            )
            _sha256(
                document["plaintext_sha256"],
                error_type=EncryptedArtifactValidationError,
            )
            if (
                document["schema"] != ENCRYPTED_ARTIFACT_FORMAT_IDENTIFIER
                or document["format_version"] != ENCRYPTED_ARTIFACT_FORMAT_VERSION
                or document["encryption_algorithm"] != ENCRYPTION_ALGORITHM
                or plaintext_count != package.byte_count
                or ciphertext_count != package.byte_count
                or document["plaintext_sha256"] != package.plaintext_sha256
                or document["verified_package_format"] != package.format_identifier
                or document["backup_public_id"] != str(context.backup_public_id)
                or document["tenant_public_id"] != str(context.business_public_id)
                or document["verification_schema"] != verification.verification_schema
                or document["verification_version"] != VERIFICATION_VERSION
                or document["verification_provider"] != verification.provider_identifier
                or document["created_timestamp"]
                != _utc_timestamp(
                    result.created_at,
                    error_type=EncryptedArtifactValidationError,
                )
                or wrapped.provider_identifier != result.kek_provider_identifier
                or wrapped.key_identifier != result.kek_key_identifier
                or wrapped.key_version != result.kek_version
                or hashlib.sha256(header_bytes).hexdigest() != result.header_sha256
            ):
                raise EncryptedArtifactValidationError()
            expected_size = _PREFIX.size + header_size + ciphertext_count + _TAG_BYTES
            if file_size != expected_size or file_size != result.encrypted_byte_count:
                raise EncryptedArtifactValidationError()
            try:
                dek = self._provider_for_wrapped_dek(wrapped).unwrap_dek(wrapped)
            except (KeyProviderConfigurationError, KeyWrapError):
                raise EncryptedArtifactValidationError() from None
            ciphertext_offset = _PREFIX.size + header_size
            file_object.seek(ciphertext_offset + ciphertext_count)
            tag = file_object.read(_TAG_BYTES)
            if type(tag) is not bytes or len(tag) != _TAG_BYTES:
                raise EncryptedArtifactValidationError()
            file_object.seek(ciphertext_offset)
            try:
                decryptor = Cipher(
                    algorithms.AES(dek),
                    modes.GCM(data_nonce, tag, min_tag_length=_TAG_BYTES),
                ).decryptor()
                decryptor.authenticate_additional_data(header_bytes)
            except Exception:
                raise EncryptedArtifactValidationError() from None
            return document, header_bytes, decryptor, ciphertext_count
        except EncryptedArtifactValidationError:
            raise
        except (OSError, OverflowError, struct.error, TypeError, ValueError):
            raise EncryptedArtifactValidationError() from None

    def _parse_restored_header(
        self,
        file_object,
        *,
        file_size,
        context,
        expected_key_identifier,
        encrypted_data_key_envelope,
    ):
        """Parse historical framing inside the authoritative Phase 2F boundary."""

        try:
            if (
                type(context) is not BackupExecutionContext
                or type(context.workspace_reference) is not WorkspaceReference
                or type(expected_key_identifier) is not str
                or not expected_key_identifier
                or len(expected_key_identifier) > 255
                or type(encrypted_data_key_envelope) is not str
            ):
                raise EncryptedArtifactValidationError()
            file_object.seek(0)
            prefix = file_object.read(_PREFIX.size)
            if type(prefix) is not bytes or len(prefix) != _PREFIX.size:
                raise EncryptedArtifactValidationError()
            magic, header_size = _PREFIX.unpack(prefix)
            if (
                magic != ARTIFACT_MAGIC
                or not 0 < header_size <= self.policy.maximum_header_bytes
            ):
                raise EncryptedArtifactValidationError()
            header_bytes = file_object.read(header_size)
            if type(header_bytes) is not bytes or len(header_bytes) != header_size:
                raise EncryptedArtifactValidationError()
            document = _strict_header(
                header_bytes,
                maximum_bytes=self.policy.maximum_header_bytes,
            )
            _exact_keys(
                document,
                _HEADER_KEYS,
                error_type=EncryptedArtifactValidationError,
            )
            wrapped_document = _exact_keys(
                document["wrapped_dek"],
                _WRAPPED_KEYS,
                error_type=EncryptedArtifactValidationError,
            )
            data_nonce = _strict_b64(
                document["nonce_b64"],
                expected_bytes=_NONCE_BYTES,
                error_type=EncryptedArtifactValidationError,
            )
            embedded_wrapped = self._wrapped_dek_from_header(wrapped_document)
            try:
                wrapped = (
                    deserialize_wrapped_dek(encrypted_data_key_envelope)
                    if encrypted_data_key_envelope
                    else embedded_wrapped
                )
            except KeyWrapError:
                raise EncryptedArtifactValidationError() from None
            plaintext_count = _count(
                document["plaintext_byte_count"],
                maximum=self.policy.maximum_plaintext_bytes,
                positive=True,
                error_type=EncryptedArtifactValidationError,
            )
            ciphertext_count = _count(
                document["ciphertext_byte_count"],
                maximum=self.policy.maximum_plaintext_bytes,
                positive=True,
                error_type=EncryptedArtifactValidationError,
            )
            plaintext_sha256 = _sha256(
                document["plaintext_sha256"],
                error_type=EncryptedArtifactValidationError,
            )
            created_at = _parse_utc_timestamp(
                document["created_timestamp"],
                error_type=EncryptedArtifactValidationError,
            )
            try:
                persisted_key_identifier = wrapped_dek_key_identifier(wrapped)
            except (KeyProviderConfigurationError, KeyWrapError):
                raise EncryptedArtifactValidationError() from None
            if (
                document["schema"] != ENCRYPTED_ARTIFACT_FORMAT_IDENTIFIER
                or document["format_version"] != ENCRYPTED_ARTIFACT_FORMAT_VERSION
                or document["encryption_algorithm"] != ENCRYPTION_ALGORITHM
                or plaintext_count != ciphertext_count
                or document["verified_package_format"] != PACKAGE_FORMAT_IDENTIFIER
                or document["backup_public_id"] != str(context.backup_public_id)
                or document["tenant_public_id"] != str(context.business_public_id)
                or document["verification_schema"] != VERIFICATION_SCHEMA_IDENTIFIER
                or document["verification_version"] != VERIFICATION_VERSION
                or document["verification_provider"]
                != INDEPENDENT_PACKAGE_VERIFIER_IDENTIFIER
                or persisted_key_identifier != expected_key_identifier
            ):
                raise EncryptedArtifactValidationError()
            expected_size = _PREFIX.size + header_size + ciphertext_count + _TAG_BYTES
            if file_size != expected_size:
                raise EncryptedArtifactValidationError()
            try:
                dek = self._provider_for_wrapped_dek(wrapped).unwrap_dek(wrapped)
            except (KeyProviderConfigurationError, KeyWrapError):
                raise EncryptedArtifactValidationError() from None
            ciphertext_offset = _PREFIX.size + header_size
            file_object.seek(ciphertext_offset + ciphertext_count)
            tag = file_object.read(_TAG_BYTES)
            if type(tag) is not bytes or len(tag) != _TAG_BYTES:
                raise EncryptedArtifactValidationError()
            file_object.seek(ciphertext_offset)
            try:
                decryptor = Cipher(
                    algorithms.AES(dek),
                    modes.GCM(data_nonce, tag, min_tag_length=_TAG_BYTES),
                ).decryptor()
                decryptor.authenticate_additional_data(header_bytes)
            except Exception:
                raise EncryptedArtifactValidationError() from None
            return (
                RestoredPlaintextEvidence(
                    plaintext_byte_count=plaintext_count,
                    plaintext_sha256=plaintext_sha256,
                    encrypted_byte_count=file_size,
                    ciphertext_sha256="",
                    header_sha256=hashlib.sha256(header_bytes).hexdigest(),
                    encrypted_format_identifier=document["schema"],
                    encrypted_format_version=document["format_version"],
                    encryption_algorithm=document["encryption_algorithm"],
                    verified_package_format=document["verified_package_format"],
                    backup_public_id=context.backup_public_id,
                    tenant_public_id=context.business_public_id,
                    kek_provider_identifier=wrapped.provider_identifier,
                    kek_key_identifier=wrapped.key_identifier,
                    kek_version=wrapped.key_version,
                    verification_schema=document["verification_schema"],
                    verification_version=document["verification_version"],
                    verification_provider=document["verification_provider"],
                    created_at=created_at,
                ),
                decryptor,
                ciphertext_count,
            )
        except EncryptedArtifactValidationError:
            raise
        except (OSError, OverflowError, struct.error, TypeError, ValueError):
            raise EncryptedArtifactValidationError() from None

    def _hash_file(self, path, *, expected_identity, deadline, error_type):
        descriptor = None
        digest = hashlib.sha256()
        byte_count = 0
        try:
            flags = os.O_RDONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if (
                _identity(opened) != expected_identity
                or opened.st_nlink != 1
                or not stat.S_ISREG(opened.st_mode)
            ):
                raise error_type()
            while True:
                self._check_deadline(deadline, error_type=error_type)
                chunk = os.read(descriptor, self.policy.chunk_bytes)
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > self.policy.maximum_artifact_bytes:
                    raise error_type()
                digest.update(chunk)
        except Phase2FEngineError:
            raise
        except OSError:
            raise error_type() from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        return byte_count, digest.hexdigest()

    def _authenticate_path(
        self,
        path,
        *,
        expected_identity,
        context,
        package,
        verification,
        result,
        deadline,
        error_type,
    ):
        descriptor = None
        raw_file = None
        reader = None
        try:
            flags = os.O_RDONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if (
                _identity(opened) != expected_identity
                or opened.st_nlink != 1
                or opened.st_size != result.encrypted_byte_count
            ):
                raise error_type()
            raw_file = os.fdopen(descriptor, "rb", closefd=True)
            descriptor = None
            _document, _header, decryptor, ciphertext_count = self._parse_header(
                raw_file,
                file_size=opened.st_size,
                context=context,
                package=package,
                verification=verification,
                result=result,
            )
            reader = _DecryptingReader(
                file_object=raw_file,
                decryptor=decryptor,
                ciphertext_bytes=ciphertext_count,
                chunk_bytes=self.policy.chunk_bytes,
                expected_count=package.byte_count,
                expected_sha256=package.plaintext_sha256,
                deadline_check=lambda: self._check_deadline(
                    deadline,
                    error_type=error_type,
                ),
            )
            while reader.read(self.policy.chunk_bytes):
                pass
            reader.close()
            reader = None
            raw_file.close()
            raw_file = None
            return True
        except (EncryptedArtifactValidationError, EncryptionPolicyError):
            raise error_type() from None
        except Phase2FEngineError:
            raise
        except (OSError, ValueError):
            raise error_type() from None
        finally:
            if reader is not None:
                try:
                    reader.close()
                except Exception:
                    pass
            if raw_file is not None:
                try:
                    raw_file.close()
                except OSError:
                    pass
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _validate_published(self, *, context, reference, evidence, error_type):
        if (
            context != evidence.context
            or reference != evidence.result.reference
            or evidence.file_identity is None
        ):
            raise error_type()
        directory = self._artifact_directory(
            context,
            reference,
            require_exists=True,
            error_type=error_type,
        )
        state = _directory_state(directory, error_type=error_type)
        if _identity(state) != evidence.directory_identity:
            raise error_type()
        _assert_private_mode(directory, 0o700, error_type=error_type)
        with os.scandir(directory) as contents:
            if {entry.name for entry in contents} != {ARTIFACT_FILE_NAME}:
                raise error_type()
        path = contained_path(directory, directory / ARTIFACT_FILE_NAME)
        current = _regular_file_state(path, error_type=error_type)
        if (
            _identity(current) != evidence.file_identity
            or current.st_dev != evidence.directory_identity[0]
            or current.st_size != evidence.result.encrypted_byte_count
        ):
            raise error_type()
        _assert_private_mode(path, 0o600, error_type=error_type)
        deadline = self.monotonic() + self.policy.timeout_seconds
        byte_count, digest = self._hash_file(
            path,
            expected_identity=evidence.file_identity,
            deadline=deadline,
            error_type=error_type,
        )
        if (
            byte_count != evidence.result.encrypted_byte_count
            or digest != evidence.result.ciphertext_sha256
        ):
            raise error_type()
        self._authenticate_path(
            path,
            expected_identity=evidence.file_identity,
            context=context,
            package=evidence.package,
            verification=evidence.verification,
            result=evidence.result,
            deadline=deadline,
            error_type=error_type,
        )
        return directory, path

    @staticmethod
    def _safe_error(exc):
        if isinstance(
            exc,
            (
                Phase2FCoordinationError,
                EncryptionPolicyError,
                KeyProviderConfigurationError,
                KeyWrapError,
                EncryptedArtifactCreationError,
                EncryptedArtifactValidationError,
            ),
        ):
            return exc
        if isinstance(exc, Phase2FEngineError):
            return EncryptedArtifactCreationError(
                cleanup_incomplete=getattr(exc, "cleanup_incomplete", False)
            )
        return EncryptedArtifactCreationError()

    def encrypt_verified_package(self, request):
        directory = None
        directory_identity = None
        directory_created = False
        part_path = None
        final_path = None
        file_identity = None
        descriptor = None
        result = None
        safe_error = None
        abort_error = None
        abort_traceback = None
        cleanup_incomplete = False
        published_evidence = None
        deadline = self.monotonic() + self.policy.timeout_seconds
        try:
            context, package, verification = self._validate_request(request)
            created_at = self.clock()
            if not _is_aware(created_at):
                raise EncryptedArtifactCreationError()
            dek, data_nonce, wrap_nonce = self._new_material()
            wrapped = self.kek_provider.wrap_dek(dek, nonce=wrap_nonce)
            header_document = self._header_document(
                context=context,
                package=package,
                verification=verification,
                data_nonce=data_nonce,
                wrapped=wrapped,
                created_at=created_at,
            )
            header_bytes = encode_canonical_document(header_document)
            if not 0 < len(header_bytes) <= self.policy.maximum_header_bytes:
                raise EncryptionPolicyError()
            prefix = _PREFIX.pack(ARTIFACT_MAGIC, len(header_bytes))
            expected_artifact_bytes = (
                len(prefix) + len(header_bytes) + package.byte_count + _TAG_BYTES
            )
            if expected_artifact_bytes > self.policy.maximum_artifact_bytes:
                raise EncryptionPolicyError()
            reference = self._new_reference()
            key = self._state_key(
                context,
                reference,
                error_type=EncryptedArtifactCreationError,
            )
            with self._state_lock:
                if key in self._published or key in self._cleaned:
                    raise EncryptedArtifactCreationError()
            _workspace, parent = self._artifact_parent(
                context,
                create=True,
                error_type=EncryptedArtifactCreationError,
            )
            self._capacity_check(parent, package)
            directory = self._artifact_directory(
                context,
                reference,
                require_exists=False,
                error_type=EncryptedArtifactCreationError,
            )
            if os.path.lexists(directory):
                raise EncryptedArtifactCreationError()
            directory.mkdir(mode=0o700, exist_ok=False)
            directory_created = True
            directory_state = _directory_state(
                directory,
                error_type=EncryptedArtifactCreationError,
            )
            directory_identity = _identity(directory_state)
            if directory_state.st_dev != _directory_state(
                parent,
                error_type=EncryptedArtifactCreationError,
            ).st_dev:
                raise EncryptedArtifactCreationError()
            _apply_private_mode(
                directory,
                0o700,
                error_type=EncryptedArtifactCreationError,
            )
            self._run_hook("after_encrypted_directory_creation")
            part_path = contained_path(
                directory,
                directory / f".{ARTIFACT_FILE_NAME}.{uuid.uuid4().hex}.part",
            )
            final_path = contained_path(directory, directory / ARTIFACT_FILE_NAME)
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(part_path, flags, 0o600)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_dev != directory_state.st_dev
            ):
                raise EncryptedArtifactCreationError()
            file_identity = _identity(opened)
            _apply_private_descriptor_mode(
                descriptor,
                part_path,
                0o600,
                error_type=EncryptedArtifactCreationError,
            )
            artifact_digest = hashlib.sha256()
            artifact_bytes = [0]
            self._write_all(
                descriptor,
                prefix,
                artifact_digest=artifact_digest,
                byte_counter=artifact_bytes,
            )
            self._write_all(
                descriptor,
                header_bytes,
                artifact_digest=artifact_digest,
                byte_counter=artifact_bytes,
            )
            try:
                encryptor = Cipher(
                    algorithms.AES(dek),
                    modes.GCM(data_nonce),
                ).encryptor()
                encryptor.authenticate_additional_data(header_bytes)
            except Exception:
                raise EncryptedArtifactCreationError() from None
            plaintext_digest = hashlib.sha256()
            plaintext_bytes = 0
            try:
                with self.package_provider.open_package(
                    context=context,
                    reference=package.reference,
                ) as reader:
                    while True:
                        self._check_deadline(
                            deadline,
                            error_type=EncryptedArtifactCreationError,
                        )
                        chunk = reader.read(self.policy.chunk_bytes)
                        if type(chunk) is not bytes or len(chunk) > self.policy.chunk_bytes:
                            raise EncryptedArtifactCreationError()
                        if not chunk:
                            break
                        plaintext_bytes += len(chunk)
                        if plaintext_bytes > package.byte_count:
                            raise EncryptedArtifactValidationError()
                        plaintext_digest.update(chunk)
                        ciphertext = encryptor.update(chunk)
                        if type(ciphertext) is not bytes or len(ciphertext) != len(chunk):
                            raise EncryptedArtifactCreationError()
                        self._write_all(
                            descriptor,
                            ciphertext,
                            artifact_digest=artifact_digest,
                            byte_counter=artifact_bytes,
                        )
            except (PackageNotFound, PackageValidationError):
                raise EncryptedArtifactValidationError() from None
            if (
                plaintext_bytes != package.byte_count
                or plaintext_digest.hexdigest() != package.plaintext_sha256
            ):
                raise EncryptedArtifactValidationError()
            try:
                tail = encryptor.finalize()
                if tail:
                    self._write_all(
                        descriptor,
                        tail,
                        artifact_digest=artifact_digest,
                        byte_counter=artifact_bytes,
                    )
                tag = encryptor.tag
            except Exception:
                raise EncryptedArtifactCreationError() from None
            if type(tag) is not bytes or len(tag) != _TAG_BYTES:
                raise EncryptedArtifactCreationError()
            self._write_all(
                descriptor,
                tag,
                artifact_digest=artifact_digest,
                byte_counter=artifact_bytes,
            )
            if artifact_bytes[0] != expected_artifact_bytes:
                raise EncryptedArtifactCreationError()
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            current = _regular_file_state(
                part_path,
                error_type=EncryptedArtifactCreationError,
            )
            if (
                _identity(current) != file_identity
                or current.st_size != expected_artifact_bytes
            ):
                raise EncryptedArtifactCreationError()
            candidate = EncryptedArtifactResult(
                reference=reference,
                encrypted_byte_count=expected_artifact_bytes,
                ciphertext_sha256=artifact_digest.hexdigest(),
                plaintext_byte_count=package.byte_count,
                plaintext_sha256=package.plaintext_sha256,
                header_sha256=hashlib.sha256(header_bytes).hexdigest(),
                format_identifier=ENCRYPTED_ARTIFACT_FORMAT_IDENTIFIER,
                encryption_algorithm=ENCRYPTION_ALGORITHM,
                kek_provider_identifier=wrapped.provider_identifier,
                kek_key_identifier=wrapped.key_identifier,
                kek_version=wrapped.key_version,
                created_at=created_at,
                provider_identifier=ENCRYPTED_ARTIFACT_PROVIDER_IDENTIFIER,
                plaintext_cleanup_incomplete=True,
            )
            self._authenticate_path(
                part_path,
                expected_identity=file_identity,
                context=context,
                package=package,
                verification=verification,
                result=candidate,
                deadline=deadline,
                error_type=EncryptedArtifactValidationError,
            )
            self._run_hook("before_encrypted_publication")
            os.link(part_path, final_path, follow_symlinks=False)
            for path in (part_path, final_path):
                linked = os.stat(path, follow_symlinks=False)
                if (
                    _identity(linked) != file_identity
                    or linked.st_nlink != 2
                    or not stat.S_ISREG(linked.st_mode)
                ):
                    raise EncryptedArtifactCreationError()
            self._run_hook("after_encrypted_publication_link")
            os.unlink(part_path)
            part_path = None
            final = _regular_file_state(
                final_path,
                error_type=EncryptedArtifactCreationError,
            )
            if _identity(final) != file_identity or final.st_size != expected_artifact_bytes:
                raise EncryptedArtifactCreationError()
            evidence = _PublishedArtifact(
                context=context,
                package=package,
                verification=verification,
                result=candidate,
                directory_identity=directory_identity,
                file_identity=file_identity,
            )
            self._validate_published(
                context=context,
                reference=reference,
                evidence=evidence,
                error_type=EncryptedArtifactValidationError,
            )
            with self._state_lock:
                if key in self._published or key in self._cleaned:
                    raise EncryptedArtifactCreationError()
                self._published[key] = evidence
            published_evidence = evidence
            result = candidate
            self._run_hook("before_plaintext_package_cleanup")
            try:
                self.package_provider.cleanup_package(
                    context=context,
                    reference=package.reference,
                )
            except PackageCleanupError:
                return result
            completed = replace(result, plaintext_cleanup_incomplete=False)
            completed_evidence = replace(evidence, result=completed)
            with self._state_lock:
                if self._published.get(key) != evidence:
                    raise PlaintextPackageCleanupError(
                        plaintext_cleanup_incomplete=True
                    )
                self._published[key] = completed_evidence
            result = completed
            published_evidence = completed_evidence
        except BaseException as exc:
            if isinstance(exc, Exception):
                safe_error = self._safe_error(exc)
            else:
                abort_error = exc
                abort_traceback = exc.__traceback__
        finally:
            if published_evidence is None:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except BaseException:
                        cleanup_incomplete = True
                try:
                    if file_identity is not None:
                        self._cleanup_owned_files(
                            (part_path, final_path),
                            expected_identity=file_identity,
                        )
                    elif any(
                        path is not None and os.path.lexists(path)
                        for path in (part_path, final_path)
                    ):
                        raise EncryptedArtifactCleanupError()
                except BaseException:
                    cleanup_incomplete = True
                if directory is not None and directory_identity is not None:
                    try:
                        removed = self._remove_empty_directory(
                            directory,
                            expected_identity=directory_identity,
                            error_type=EncryptedArtifactCleanupError,
                        )
                        if not removed and os.path.lexists(directory):
                            cleanup_incomplete = True
                    except BaseException:
                        cleanup_incomplete = True
                elif directory_created and directory is not None and os.path.lexists(directory):
                    cleanup_incomplete = True
        if abort_error is not None:
            try:
                abort_error.cleanup_incomplete = bool(
                    cleanup_incomplete
                    or getattr(abort_error, "cleanup_incomplete", False)
                )
            except Exception:
                pass
            raise abort_error.with_traceback(abort_traceback)
        if safe_error is not None:
            safe_error.cleanup_incomplete = bool(
                cleanup_incomplete or getattr(safe_error, "cleanup_incomplete", False)
            )
            safe_error.__cause__ = None
            safe_error.__context__ = None
            raise safe_error.with_traceback(None) from None
        if result is None:
            raise EncryptedArtifactCreationError(cleanup_incomplete=cleanup_incomplete)
        return result

    def validate_encrypted_artifact_evidence(
        self,
        *,
        context,
        package,
        verification,
        result,
    ):
        if (
            type(package) is not PackageBuildResult
            or type(verification) is not PackageVerificationResult
            or type(result) is not EncryptedArtifactResult
            or type(result.reference) is not EncryptedArtifactReference
        ):
            raise EncryptedArtifactValidationError()
        key = self._state_key(
            context,
            result.reference,
            error_type=EncryptedArtifactValidationError,
        )
        with self._state_lock:
            evidence = self._published.get(key)
        if (
            evidence is None
            or evidence.context != context
            or evidence.package != package
            or evidence.verification != verification
            or evidence.result != result
        ):
            raise EncryptedArtifactValidationError()
        self._validate_published(
            context=context,
            reference=result.reference,
            evidence=evidence,
            error_type=EncryptedArtifactValidationError,
        )
        return True

    def validate_owned_encrypted_artifact(self, *, context, result):
        """Validate exact local encrypted evidence without exposing predecessors."""

        if (
            type(result) is not EncryptedArtifactResult
            or type(result.reference) is not EncryptedArtifactReference
        ):
            raise EncryptedArtifactValidationError()
        key = self._state_key(
            context,
            result.reference,
            error_type=EncryptedArtifactValidationError,
        )
        with self._state_lock:
            evidence = self._published.get(key)
        if (
            evidence is None
            or evidence.context != context
            or evidence.result != result
        ):
            raise EncryptedArtifactValidationError()
        self._validate_published(
            context=context,
            reference=result.reference,
            evidence=evidence,
            error_type=EncryptedArtifactValidationError,
        )
        return True

    def validate_external_encrypted_artifact_stream(
        self,
        *,
        context,
        result,
        reader,
    ):
        """Authenticate expected artifact bytes from an opaque external reader.

        This is the narrow validation bridge used by durable storage. It keeps
        package/verification evidence and KEK access inside the Phase 2F
        provider boundary and never accepts a filesystem path.
        """

        if (
            type(result) is not EncryptedArtifactResult
            or type(result.reference) is not EncryptedArtifactReference
            or not callable(getattr(reader, "read", None))
            or not callable(getattr(reader, "seek", None))
        ):
            raise EncryptedArtifactValidationError()
        key = self._state_key(
            context,
            result.reference,
            error_type=EncryptedArtifactValidationError,
        )
        with self._state_lock:
            evidence = self._published.get(key) or self._cleaned.get(key)
        if (
            evidence is None
            or evidence.context != context
            or evidence.result != result
        ):
            raise EncryptedArtifactValidationError()
        deadline = self.monotonic() + self.policy.timeout_seconds
        digest = hashlib.sha256()
        byte_count = 0
        decrypting_reader = None
        try:
            reader.seek(0)
            while True:
                self._check_deadline(
                    deadline,
                    error_type=EncryptedArtifactValidationError,
                )
                chunk = reader.read(self.policy.chunk_bytes)
                if type(chunk) is not bytes or len(chunk) > self.policy.chunk_bytes:
                    raise EncryptedArtifactValidationError()
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > self.policy.maximum_artifact_bytes:
                    raise EncryptedArtifactValidationError()
                digest.update(chunk)
            if (
                byte_count != result.encrypted_byte_count
                or digest.hexdigest() != result.ciphertext_sha256
            ):
                raise EncryptedArtifactValidationError()
            reader.seek(0)
            _document, _header, decryptor, ciphertext_count = self._parse_header(
                reader,
                file_size=byte_count,
                context=context,
                package=evidence.package,
                verification=evidence.verification,
                result=result,
            )
            decrypting_reader = _DecryptingReader(
                file_object=reader,
                decryptor=decryptor,
                ciphertext_bytes=ciphertext_count,
                chunk_bytes=self.policy.chunk_bytes,
                expected_count=evidence.package.byte_count,
                expected_sha256=evidence.package.plaintext_sha256,
                deadline_check=lambda: self._check_deadline(
                    deadline,
                    error_type=EncryptedArtifactValidationError,
                ),
            )
            while decrypting_reader.read(self.policy.chunk_bytes):
                pass
            decrypting_reader.close()
            decrypting_reader = None
            return True
        except EncryptedArtifactValidationError:
            raise
        except (OSError, OverflowError, TypeError, ValueError):
            raise EncryptedArtifactValidationError() from None
        finally:
            if decrypting_reader is not None:
                try:
                    decrypting_reader.close()
                except Exception:
                    pass

    def rewrap_encrypted_artifact_key(
        self,
        *,
        context,
        reader,
        encrypted_byte_count,
        ciphertext_sha256,
        encryption_key_identifier,
        encrypted_data_key_envelope,
        target_provider,
        publish_metadata,
    ):
        """Verify and publish a new DEK wrapper without changing artifact bytes."""

        if (
            type(context) is not BackupExecutionContext
            or type(context.workspace_reference) is not WorkspaceReference
            or not callable(getattr(reader, "read", None))
            or not callable(getattr(reader, "seek", None))
            or type(encrypted_byte_count) is not int
            or not 1 <= encrypted_byte_count <= self.policy.maximum_artifact_bytes
            or type(encryption_key_identifier) is not str
            or not encryption_key_identifier
            or type(encrypted_data_key_envelope) is not str
            or not isinstance(target_provider, KeyEncryptionProvider)
            or not callable(publish_metadata)
            or (
                self.key_provider_registry is not None
                and target_provider is not self.key_provider_registry.active_provider
            )
        ):
            raise KeyRewrapError()
        _sha256(ciphertext_sha256, error_type=KeyRewrapError)
        deadline = self.monotonic() + self.policy.timeout_seconds
        digest = hashlib.sha256()
        byte_count = 0
        current_dek = None
        verified_dek = None
        try:
            reader.seek(0)
            while True:
                self._check_deadline(deadline, error_type=KeyRewrapError)
                chunk = reader.read(self.policy.chunk_bytes)
                if type(chunk) is not bytes or len(chunk) > self.policy.chunk_bytes:
                    raise KeyRewrapError()
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > self.policy.maximum_artifact_bytes:
                    raise KeyRewrapError()
                digest.update(chunk)
            if (
                byte_count != encrypted_byte_count
                or digest.hexdigest() != ciphertext_sha256
            ):
                raise KeyRewrapError()

            reader.seek(0)
            prefix = reader.read(_PREFIX.size)
            if type(prefix) is not bytes or len(prefix) != _PREFIX.size:
                raise KeyRewrapError()
            magic, header_size = _PREFIX.unpack(prefix)
            if (
                magic != ARTIFACT_MAGIC
                or not 0 < header_size <= self.policy.maximum_header_bytes
            ):
                raise KeyRewrapError()
            header_bytes = reader.read(header_size)
            document = _strict_header(
                header_bytes,
                maximum_bytes=self.policy.maximum_header_bytes,
            )
            _exact_keys(document, _HEADER_KEYS, error_type=KeyRewrapError)
            embedded = wrapped_dek_from_document(
                _exact_keys(
                    document["wrapped_dek"],
                    _WRAPPED_KEYS,
                    error_type=KeyRewrapError,
                )
            )
            ciphertext_count = _count(
                document["ciphertext_byte_count"],
                maximum=self.policy.maximum_plaintext_bytes,
                positive=True,
                error_type=KeyRewrapError,
            )
            if (
                document["schema"] != ENCRYPTED_ARTIFACT_FORMAT_IDENTIFIER
                or document["format_version"] != ENCRYPTED_ARTIFACT_FORMAT_VERSION
                or document["encryption_algorithm"] != ENCRYPTION_ALGORITHM
                or document["backup_public_id"] != str(context.backup_public_id)
                or document["tenant_public_id"] != str(context.business_public_id)
                or _PREFIX.size + header_size + ciphertext_count + _TAG_BYTES
                != encrypted_byte_count
            ):
                raise KeyRewrapError()
            current_wrapped = (
                deserialize_wrapped_dek(encrypted_data_key_envelope)
                if encrypted_data_key_envelope
                else embedded
            )
            if wrapped_dek_key_identifier(current_wrapped) != encryption_key_identifier:
                raise KeyRewrapError()

            source_provider = self._provider_for_wrapped_dek(current_wrapped)
            current_dek = source_provider.unwrap_dek(current_wrapped)
            wrap_nonce = self.random_bytes(_NONCE_BYTES)
            if type(wrap_nonce) is not bytes or len(wrap_nonce) != _NONCE_BYTES:
                raise KeyRewrapError()
            new_wrapped = target_provider.wrap_dek(current_dek, nonce=wrap_nonce)
            if not target_provider.can_unwrap(new_wrapped):
                raise KeyRewrapError()
            verified_dek = target_provider.unwrap_dek(new_wrapped)
            if not hmac.compare_digest(current_dek, verified_dek):
                raise KeyRewrapError()
            result = RewrappedArtifactKeyResult(
                previous_key_identifier=encryption_key_identifier,
                new_key_identifier=wrapped_dek_key_identifier(new_wrapped),
                previous_envelope=encrypted_data_key_envelope,
                new_envelope=serialize_wrapped_dek(new_wrapped),
                encrypted_byte_count=encrypted_byte_count,
                artifact_sha256=ciphertext_sha256,
            )
            publish_metadata(result)
            return result
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except KeyRewrapError:
            raise
        except Exception:
            raise KeyRewrapError() from None
        finally:
            current_dek = None
            verified_dek = None

    @contextmanager
    def open_restored_plaintext(
        self,
        *,
        context,
        reader,
        encrypted_byte_count,
        ciphertext_sha256,
        encryption_key_identifier,
        encrypted_data_key_envelope="",
    ):
        """Authenticate and decrypt a restart-retrieved durable object.

        The caller supplies only opaque stream access plus DB-backed expected
        evidence. Framing, KEK access, AAD, tag verification, and plaintext
        evidence remain owned by Phase 2F.
        """

        if (
            type(context) is not BackupExecutionContext
            or type(context.workspace_reference) is not WorkspaceReference
            or not callable(getattr(reader, "read", None))
            or not callable(getattr(reader, "seek", None))
            or type(encrypted_byte_count) is not int
            or not 1 <= encrypted_byte_count <= self.policy.maximum_artifact_bytes
        ):
            raise EncryptedArtifactValidationError()
        _sha256(
            ciphertext_sha256,
            error_type=EncryptedArtifactValidationError,
        )
        deadline = self.monotonic() + self.policy.timeout_seconds
        decrypting_reader = None
        digest = hashlib.sha256()
        byte_count = 0
        try:
            reader.seek(0)
            while True:
                self._check_deadline(
                    deadline,
                    error_type=EncryptedArtifactValidationError,
                )
                chunk = reader.read(self.policy.chunk_bytes)
                if type(chunk) is not bytes or len(chunk) > self.policy.chunk_bytes:
                    raise EncryptedArtifactValidationError()
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > self.policy.maximum_artifact_bytes:
                    raise EncryptedArtifactValidationError()
                digest.update(chunk)
            if (
                byte_count != encrypted_byte_count
                or digest.hexdigest() != ciphertext_sha256
            ):
                raise EncryptedArtifactValidationError()
            reader.seek(0)
            evidence, decryptor, ciphertext_count = self._parse_restored_header(
                reader,
                file_size=byte_count,
                context=context,
                expected_key_identifier=encryption_key_identifier,
                encrypted_data_key_envelope=encrypted_data_key_envelope,
            )
            evidence = replace(evidence, ciphertext_sha256=ciphertext_sha256)
            decrypting_reader = _DecryptingReader(
                file_object=reader,
                decryptor=decryptor,
                ciphertext_bytes=ciphertext_count,
                chunk_bytes=self.policy.chunk_bytes,
                expected_count=evidence.plaintext_byte_count,
                expected_sha256=evidence.plaintext_sha256,
                deadline_check=lambda: self._check_deadline(
                    deadline,
                    error_type=EncryptedArtifactValidationError,
                ),
            )
            yield decrypting_reader, evidence
        except EncryptedArtifactValidationError:
            raise
        except (OSError, OverflowError, TypeError, ValueError):
            raise EncryptedArtifactValidationError() from None
        finally:
            if decrypting_reader is not None:
                active_exception = sys.exc_info()[0] is not None
                try:
                    decrypting_reader.close()
                except BaseException:
                    if not active_exception:
                        raise EncryptedArtifactValidationError() from None

    def retry_plaintext_package_cleanup(self, request, result):
        context, package, verification = self._validate_request_for_retry(request)
        if (
            type(result) is not EncryptedArtifactResult
            or result.plaintext_cleanup_incomplete is not True
        ):
            raise PlaintextPackageCleanupError()
        key = self._state_key(
            context,
            result.reference,
            error_type=PlaintextPackageCleanupError,
        )
        with self._state_lock:
            evidence = self._published.get(key)
        if (
            evidence is None
            or evidence.context != context
            or evidence.package != package
            or evidence.verification != verification
            or evidence.result != result
        ):
            raise PlaintextPackageCleanupError()
        self._validate_published(
            context=context,
            reference=result.reference,
            evidence=evidence,
            error_type=PlaintextPackageCleanupError,
        )
        try:
            self.package_provider.cleanup_package(
                context=context,
                reference=package.reference,
            )
        except PackageCleanupError:
            raise PlaintextPackageCleanupError(
                plaintext_cleanup_incomplete=True
            ) from None
        completed = replace(result, plaintext_cleanup_incomplete=False)
        updated = replace(evidence, result=completed)
        with self._state_lock:
            if self._published.get(key) != evidence:
                raise PlaintextPackageCleanupError(
                    plaintext_cleanup_incomplete=True
                )
            self._published[key] = updated
        return completed

    def _validate_request_for_retry(self, request):
        if type(request) is not EncryptedArtifactRequest:
            raise PlaintextPackageCleanupError()
        context = request.context
        package = request.package
        verification = request.verification
        if (
            type(context) is not BackupExecutionContext
            or type(package) is not PackageBuildResult
            or type(verification) is not PackageVerificationResult
        ):
            raise PlaintextPackageCleanupError()
        return context, package, verification

    @contextmanager
    def open_encrypted_artifact(self, *, context, reference):
        key = self._state_key(
            context,
            reference,
            error_type=EncryptedArtifactNotFound,
        )
        with self._state_lock:
            evidence = self._published.get(key)
        if evidence is None or evidence.file_identity is None:
            raise EncryptedArtifactNotFound()
        directory, path = self._validate_published(
            context=context,
            reference=reference,
            evidence=evidence,
            error_type=EncryptedArtifactNotFound,
        )
        descriptor = None
        raw_file = None
        reader = None
        try:
            flags = os.O_RDONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if (
                _identity(opened) != evidence.file_identity
                or opened.st_nlink != 1
                or opened.st_size != evidence.result.encrypted_byte_count
            ):
                raise EncryptedArtifactNotFound()
            raw_file = os.fdopen(descriptor, "rb", closefd=True)
            descriptor = None
            reader = _OpaqueArtifactReader(raw_file)
            yield reader
        except EncryptedArtifactNotFound:
            raise
        except OSError:
            raise EncryptedArtifactNotFound() from None
        finally:
            active_exception = sys.exc_info()[0] is not None
            close_error = None
            target = reader or raw_file
            if target is not None:
                try:
                    target.close()
                except BaseException as exc:
                    if not active_exception:
                        close_error = exc
            elif descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException as exc:
                    if not active_exception:
                        close_error = exc
            try:
                if (
                    _identity(
                        _directory_state(
                            directory,
                            error_type=EncryptedArtifactNotFound,
                        )
                    )
                    != evidence.directory_identity
                ):
                    raise EncryptedArtifactNotFound()
                self._validate_published(
                    context=context,
                    reference=reference,
                    evidence=evidence,
                    error_type=EncryptedArtifactNotFound,
                )
            except BaseException as exc:
                if not active_exception:
                    close_error = exc
            if close_error is not None and not active_exception:
                if isinstance(close_error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    raise close_error.with_traceback(close_error.__traceback__)
                raise EncryptedArtifactNotFound() from None

    @contextmanager
    def open_decrypted_artifact(self, *, context, result):
        if (
            type(result) is not EncryptedArtifactResult
            or type(result.reference) is not EncryptedArtifactReference
        ):
            raise EncryptedArtifactValidationError()
        key = self._state_key(
            context,
            result.reference,
            error_type=EncryptedArtifactValidationError,
        )
        with self._state_lock:
            evidence = self._published.get(key)
        if evidence is None or evidence.result != result or evidence.file_identity is None:
            raise EncryptedArtifactValidationError()
        _directory, path = self._validate_published(
            context=context,
            reference=result.reference,
            evidence=evidence,
            error_type=EncryptedArtifactValidationError,
        )
        descriptor = None
        raw_file = None
        reader = None
        deadline = self.monotonic() + self.policy.timeout_seconds
        try:
            flags = os.O_RDONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if (
                _identity(opened) != evidence.file_identity
                or opened.st_nlink != 1
                or opened.st_size != result.encrypted_byte_count
            ):
                raise EncryptedArtifactValidationError()
            raw_file = os.fdopen(descriptor, "rb", closefd=True)
            descriptor = None
            _document, _header, decryptor, ciphertext_count = self._parse_header(
                raw_file,
                file_size=opened.st_size,
                context=context,
                package=evidence.package,
                verification=evidence.verification,
                result=result,
            )
            reader = _DecryptingReader(
                file_object=raw_file,
                decryptor=decryptor,
                ciphertext_bytes=ciphertext_count,
                chunk_bytes=self.policy.chunk_bytes,
                expected_count=evidence.package.byte_count,
                expected_sha256=evidence.package.plaintext_sha256,
                deadline_check=lambda: self._check_deadline(
                    deadline,
                    error_type=EncryptedArtifactValidationError,
                ),
            )
            yield reader
        except EncryptedArtifactValidationError:
            raise
        except OSError:
            raise EncryptedArtifactValidationError() from None
        finally:
            active_exception = sys.exc_info()[0] is not None
            close_error = None
            if reader is not None:
                try:
                    reader.close()
                except BaseException as exc:
                    if not active_exception:
                        close_error = exc
            if raw_file is not None:
                try:
                    raw_file.close()
                except BaseException as exc:
                    if not active_exception and close_error is None:
                        close_error = exc
            elif descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException as exc:
                    if not active_exception and close_error is None:
                        close_error = exc
            if close_error is not None and not active_exception:
                if isinstance(close_error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    raise close_error.with_traceback(close_error.__traceback__)
                raise EncryptedArtifactValidationError() from None

    def cleanup_encrypted_artifact(self, *, context, reference):
        key = self._state_key(
            context,
            reference,
            error_type=EncryptedArtifactCleanupError,
        )
        with self._state_lock:
            if key in self._cleaned:
                if self._cleaned[key].context != context:
                    raise EncryptedArtifactCleanupError()
                return True
            evidence = self._published.get(key)
        if evidence is None or evidence.context != context:
            raise EncryptedArtifactCleanupError()
        try:
            if evidence.file_identity is not None:
                directory, path = self._validate_published(
                    context=context,
                    reference=reference,
                    evidence=evidence,
                    error_type=EncryptedArtifactCleanupError,
                )
                self._run_hook("before_encrypted_cleanup_unlink")
                unlink_abort = None
                unlink_abort_traceback = None
                try:
                    os.unlink(path)
                except BaseException as exc:
                    if os.path.lexists(path):
                        raise
                    if not isinstance(exc, Exception):
                        unlink_abort = exc
                        unlink_abort_traceback = exc.__traceback__
                if os.path.lexists(path):
                    raise EncryptedArtifactCleanupError()
                updated = replace(evidence, file_identity=None)
                with self._state_lock:
                    if self._published.get(key) != evidence:
                        raise EncryptedArtifactCleanupError()
                    self._published[key] = updated
                evidence = updated
                if unlink_abort is not None:
                    raise unlink_abort.with_traceback(unlink_abort_traceback)
            else:
                directory = self._artifact_directory(
                    context,
                    reference,
                    require_exists=False,
                    error_type=EncryptedArtifactCleanupError,
                )
                if not os.path.lexists(directory):
                    with self._state_lock:
                        if self._published.get(key) != evidence:
                            raise EncryptedArtifactCleanupError()
                        self._published.pop(key, None)
                        self._cleaned[key] = evidence
                    return True
                if _identity(
                    _directory_state(
                        directory,
                        error_type=EncryptedArtifactCleanupError,
                    )
                ) != evidence.directory_identity:
                    raise EncryptedArtifactCleanupError()
                path = contained_path(directory, directory / ARTIFACT_FILE_NAME)
                if os.path.lexists(path):
                    raise EncryptedArtifactCleanupError()
            if _identity(
                _directory_state(directory, error_type=EncryptedArtifactCleanupError)
            ) != evidence.directory_identity:
                raise EncryptedArtifactCleanupError()
            with os.scandir(directory) as contents:
                if next(contents, None) is not None:
                    raise EncryptedArtifactCleanupError()
            self._run_hook("before_encrypted_cleanup_directory_removal")
            directory_abort = None
            directory_abort_traceback = None
            try:
                os.rmdir(directory)
            except BaseException as exc:
                if os.path.lexists(directory):
                    raise
                if not isinstance(exc, Exception):
                    directory_abort = exc
                    directory_abort_traceback = exc.__traceback__
            if os.path.lexists(directory):
                raise EncryptedArtifactCleanupError()
            with self._state_lock:
                if self._published.get(key) != evidence:
                    raise EncryptedArtifactCleanupError()
                self._published.pop(key, None)
                self._cleaned[key] = evidence
            if directory_abort is not None:
                raise directory_abort.with_traceback(directory_abort_traceback)
            return True
        except EncryptedArtifactCleanupError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise EncryptedArtifactCleanupError() from None


__all__ = [
    "ARTIFACT_FILE_NAME",
    "ARTIFACT_MAGIC",
    "ENCRYPTED_ARTIFACT_FORMAT_IDENTIFIER",
    "ENCRYPTED_ARTIFACT_FORMAT_VERSION",
    "ENCRYPTED_ARTIFACT_PROVIDER_IDENTIFIER",
    "ENCRYPTION_ALGORITHM",
    "EncryptedArtifactProvider",
    "RewrappedArtifactKeyResult",
]
