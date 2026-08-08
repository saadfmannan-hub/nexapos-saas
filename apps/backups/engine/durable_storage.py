"""Private local durable storage for authenticated Phase 2F artifacts."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .context import BackupExecutionContext
from .contracts import (
    DurableBackupStorageProvider,
    EncryptedArtifactReference,
    EncryptedArtifactResult,
    PersistedStoredObjectDescriptor,
    ReattestedStoredObjectResult,
    StoredBackupObjectReference,
    StoredBackupObjectRequest,
    StoredBackupObjectResult,
    StoredObjectDurabilityState,
    StoredObjectVerificationState,
)
from .durable_storage_exceptions import (
    DurableObjectCleanupError,
    DurableObjectCreationError,
    DurableObjectNotFound,
    DurableObjectValidationError,
    DurableStorageEngineError,
    DurableStoragePolicyError,
    DurableStorageTimeout,
    EncryptedStagingCleanupError,
    InsufficientDurableStorageCapacity,
    Phase2GCoordinationError,
    UnsafeDurableStorageRoot,
)
from .durable_storage_policy import (
    DurableStoragePolicy,
    validate_durable_storage_root,
)
from .encrypted_artifact import (
    ENCRYPTED_ARTIFACT_FORMAT_IDENTIFIER,
    ENCRYPTED_ARTIFACT_PROVIDER_IDENTIFIER,
    ENCRYPTION_ALGORITHM,
    EncryptedArtifactProvider,
)
from .encryption_exceptions import (
    EncryptedArtifactCleanupError,
    EncryptedArtifactNotFound,
    EncryptedArtifactValidationError,
)
from .snapshot_policy import LocalFilesystemInspector
from .workspace import (
    WorkspaceReference,
    path_has_link_like_component,
    path_is_link_like,
)

LOCAL_DURABLE_STORAGE_BACKEND_IDENTIFIER = "local-private-filesystem"
LOCAL_DURABLE_STORAGE_PROVIDER_IDENTIFIER = "local-private-durable-storage-v1"
STORED_OBJECT_SCHEMA_IDENTIFIER = "nexa.stored-backup-object.v1"
STORED_OBJECT_FILE_NAME = "artifact.nxb"
_SHA256_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class _StoredEvidence:
    context: BackupExecutionContext
    source: EncryptedArtifactResult
    result: StoredBackupObjectResult
    root_identity: tuple[int, int]
    objects_identity: tuple[int, int]
    tenant_identity: tuple[int, int]
    backup_identity: tuple[int, int]
    directory_identity: tuple[int, int]
    file_identity: tuple[int, int] | None


@dataclass(frozen=True, slots=True)
class _ReattestedEvidence:
    context: BackupExecutionContext
    descriptor: PersistedStoredObjectDescriptor
    result: ReattestedStoredObjectResult
    root_identity: tuple[int, int]
    objects_identity: tuple[int, int]
    tenant_identity: tuple[int, int]
    backup_identity: tuple[int, int]
    directory_identity: tuple[int, int]
    file_identity: tuple[int, int]


class _OpaqueStoredObjectReader:
    __slots__ = ("__file",)

    def __init__(self, file_object):
        self.__file = file_object

    def read(self, size=-1):
        return self.__file.read(size)

    def seek(self, offset, whence=io.SEEK_SET):
        return self.__file.seek(offset, whence)

    def tell(self):
        return self.__file.tell()

    def close(self):
        return self.__file.close()

    @property
    def closed(self):
        return self.__file.closed


def _identity(metadata):
    return metadata.st_dev, metadata.st_ino


def _is_aware(value):
    return (
        type(value) is datetime
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _safe_sha256(value, *, error_type):
    if (
        type(value) is not str
        or len(value) != 64
        or not set(value).issubset(_SHA256_HEX)
    ):
        raise error_type()
    return value


def _contained(root, candidate, *, allow_root=False, error_type):
    try:
        lexical_root = Path(os.path.abspath(root))
        lexical_candidate = Path(os.path.abspath(candidate))
        if path_has_link_like_component(lexical_root):
            raise error_type()
        try:
            lexical_candidate.relative_to(lexical_root)
        except ValueError:
            raise error_type() from None
        if lexical_candidate == lexical_root and not allow_root:
            raise error_type()
        current = lexical_candidate
        while True:
            if os.path.lexists(current) and path_is_link_like(current):
                raise error_type()
            if current == lexical_root:
                break
            current = current.parent
        resolved_root = lexical_root.resolve(strict=False)
        resolved_candidate = lexical_candidate.resolve(strict=False)
        try:
            resolved_candidate.relative_to(resolved_root)
        except ValueError:
            raise error_type() from None
        if resolved_candidate == resolved_root and not allow_root:
            raise error_type()
        return resolved_candidate
    except DurableStorageEngineError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise error_type() from None


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
    except DurableStorageEngineError:
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
    except DurableStorageEngineError:
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


def _fsync_directory(path, *, error_type):
    if os.name == "nt":
        return
    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
    except OSError:
        raise error_type() from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


class LocalPrivateDurableStorageProvider(DurableBackupStorageProvider):
    """Durably retain ciphertext in a private engine-owned local root."""

    def __init__(
        self,
        *,
        encrypted_artifact_provider,
        policy=None,
        reference_factory=None,
        clock=None,
        monotonic=None,
        disk_usage_provider=None,
        filesystem_inspector=None,
        failure_hook=None,
    ):
        if type(encrypted_artifact_provider) is not EncryptedArtifactProvider:
            raise Phase2GCoordinationError()
        selected_policy = policy or DurableStoragePolicy.from_settings()
        if type(selected_policy) is not DurableStoragePolicy:
            raise DurableStoragePolicyError()
        self.policy = selected_policy.validated()
        self.filesystem_inspector = filesystem_inspector or LocalFilesystemInspector()
        self.root = validate_durable_storage_root(
            self.policy.root,
            staging_root=encrypted_artifact_provider.workspace_manager.root,
            media_root=getattr(settings, "MEDIA_ROOT", None),
            static_root=getattr(settings, "STATIC_ROOT", None),
            require_local=self.policy.require_local,
            filesystem_inspector=self.filesystem_inspector,
        )
        self.encrypted_artifact_provider = encrypted_artifact_provider
        self.reference_factory = reference_factory or (
            lambda: StoredBackupObjectReference(uuid.uuid4())
        )
        self.clock = clock or timezone.now
        self.monotonic = monotonic or time.monotonic
        self.disk_usage_provider = disk_usage_provider or shutil.disk_usage
        self.failure_hook = failure_hook
        self._stored = {}
        self._deleted = {}
        self._by_source = {}
        self._reattested = {}
        self._state_lock = threading.RLock()

    def __repr__(self):
        return (
            "LocalPrivateDurableStorageProvider("
            f"provider_identifier={LOCAL_DURABLE_STORAGE_PROVIDER_IDENTIFIER!r})"
        )

    def _run_hook(self, stage):
        if self.failure_hook is not None:
            self.failure_hook(stage)

    def _check_deadline(self, deadline, *, error_type=DurableStorageTimeout):
        try:
            if self.monotonic() > deadline:
                raise error_type()
        except DurableStorageEngineError:
            raise
        except Exception:
            raise error_type() from None

    def _new_reference(self):
        try:
            reference = self.reference_factory()
            if type(reference) is uuid.UUID:
                reference = StoredBackupObjectReference(reference)
            if (
                type(reference) is not StoredBackupObjectReference
                or type(reference.identifier) is not uuid.UUID
            ):
                raise TypeError
            return reference
        except (AttributeError, TypeError, ValueError):
            raise DurableObjectCreationError() from None

    @staticmethod
    def _state_key(context, reference, *, error_type):
        if (
            type(context) is not BackupExecutionContext
            or type(context.workspace_reference) is not WorkspaceReference
            or type(reference) is not StoredBackupObjectReference
            or type(reference.identifier) is not uuid.UUID
        ):
            raise error_type()
        return context.workspace_reference.identifier, reference.identifier

    @staticmethod
    def _source_key(context, source, *, error_type):
        if (
            type(context) is not BackupExecutionContext
            or type(context.workspace_reference) is not WorkspaceReference
            or type(source) is not EncryptedArtifactResult
            or type(source.reference) is not EncryptedArtifactReference
        ):
            raise error_type()
        return context.workspace_reference.identifier, source.reference.identifier

    def _validate_request(self, request):
        if type(request) is not StoredBackupObjectRequest:
            raise Phase2GCoordinationError()
        context = request.context
        source = request.encrypted_artifact
        if (
            type(context) is not BackupExecutionContext
            or type(context.workspace_reference) is not WorkspaceReference
            or type(context.backup_public_id) is not uuid.UUID
            or type(context.business_public_id) is not uuid.UUID
            or type(source) is not EncryptedArtifactResult
            or type(source.reference) is not EncryptedArtifactReference
            or source.format_identifier != ENCRYPTED_ARTIFACT_FORMAT_IDENTIFIER
            or source.provider_identifier != ENCRYPTED_ARTIFACT_PROVIDER_IDENTIFIER
            or source.encryption_algorithm != ENCRYPTION_ALGORITHM
            or source.plaintext_cleanup_incomplete is not False
            or type(source.encrypted_byte_count) is not int
            or not 1 <= source.encrypted_byte_count <= self.policy.maximum_object_bytes
            or type(source.plaintext_byte_count) is not int
            or source.plaintext_byte_count <= 0
            or not _is_aware(source.created_at)
        ):
            raise Phase2GCoordinationError()
        _safe_sha256(source.ciphertext_sha256, error_type=Phase2GCoordinationError)
        _safe_sha256(source.plaintext_sha256, error_type=Phase2GCoordinationError)
        _safe_sha256(source.header_sha256, error_type=Phase2GCoordinationError)
        try:
            self.encrypted_artifact_provider.validate_owned_encrypted_artifact(
                context=context,
                result=source,
            )
        except EncryptedArtifactValidationError:
            raise Phase2GCoordinationError() from None
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception:
            raise Phase2GCoordinationError() from None
        return context, source

    def _ensure_directory(self, path, *, parent_state=None):
        existed = os.path.lexists(path)
        if existed and path_is_link_like(path):
            raise UnsafeDurableStorageRoot()
        try:
            path.mkdir(mode=0o700, exist_ok=True)
        except OSError:
            raise UnsafeDurableStorageRoot() from None
        current = _directory_state(path, error_type=UnsafeDurableStorageRoot)
        if parent_state is not None and current.st_dev != parent_state.st_dev:
            raise UnsafeDurableStorageRoot()
        _apply_private_mode(path, 0o700, error_type=UnsafeDurableStorageRoot)
        if existed and _identity(
            _directory_state(path, error_type=UnsafeDurableStorageRoot)
        ) != _identity(current):
            raise UnsafeDurableStorageRoot()
        return _directory_state(path, error_type=UnsafeDurableStorageRoot)

    def _prepare_hierarchy(self, context):
        self.root = validate_durable_storage_root(
            self.root,
            staging_root=self.encrypted_artifact_provider.workspace_manager.root,
            media_root=getattr(settings, "MEDIA_ROOT", None),
            static_root=getattr(settings, "STATIC_ROOT", None),
            require_local=self.policy.require_local,
            filesystem_inspector=self.filesystem_inspector,
        )
        if not os.path.lexists(self.root):
            try:
                self.root.mkdir(mode=0o700, parents=True, exist_ok=False)
            except OSError:
                raise UnsafeDurableStorageRoot() from None
        root_state = self._ensure_directory(self.root)
        if self.policy.require_local:
            try:
                assessment = self.filesystem_inspector.assess(self.root)
            except Exception:
                raise UnsafeDurableStorageRoot() from None
            if assessment.confirmed_local is not True:
                raise UnsafeDurableStorageRoot()
        objects = _contained(
            self.root,
            self.root / "objects",
            error_type=UnsafeDurableStorageRoot,
        )
        objects_state = self._ensure_directory(objects, parent_state=root_state)
        tenant = _contained(
            self.root,
            objects / context.business_public_id.hex,
            error_type=UnsafeDurableStorageRoot,
        )
        tenant_state = self._ensure_directory(tenant, parent_state=objects_state)
        backup = _contained(
            self.root,
            tenant / context.backup_public_id.hex,
            error_type=UnsafeDurableStorageRoot,
        )
        backup_state = self._ensure_directory(backup, parent_state=tenant_state)
        return (
            root_state,
            objects,
            objects_state,
            tenant,
            tenant_state,
            backup,
            backup_state,
        )

    def _object_paths(self, context, reference, *, error_type):
        if type(reference.identifier) is not uuid.UUID:
            raise error_type()
        objects = _contained(
            self.root,
            self.root / "objects",
            error_type=error_type,
        )
        tenant = _contained(
            self.root,
            objects / context.business_public_id.hex,
            error_type=error_type,
        )
        backup = _contained(
            self.root,
            tenant / context.backup_public_id.hex,
            error_type=error_type,
        )
        directory = _contained(
            self.root,
            backup / reference.identifier.hex,
            error_type=error_type,
        )
        final = _contained(
            self.root,
            directory / STORED_OBJECT_FILE_NAME,
            error_type=error_type,
        )
        return objects, tenant, backup, directory, final

    def _capacity_check(self, source):
        if source.encrypted_byte_count > self.policy.maximum_object_bytes:
            raise DurableStoragePolicyError()
        try:
            free = self.disk_usage_provider(self.root).free
        except Exception:
            raise InsufficientDurableStorageCapacity() from None
        required = max(
            self.policy.minimum_free_bytes,
            int(source.encrypted_byte_count * self.policy.headroom_multiplier),
        )
        if type(free) is not int or free < required:
            raise InsufficientDurableStorageCapacity()

    @staticmethod
    def _write_all(descriptor, value):
        try:
            offset = 0
            while offset < len(value):
                written = os.write(descriptor, value[offset:])
                if type(written) is not int or written <= 0:
                    raise DurableObjectCreationError()
                offset += written
        except DurableObjectCreationError:
            raise
        except (OSError, TypeError, ValueError):
            raise DurableObjectCreationError() from None

    def _hash_path(self, path, *, expected_identity, deadline, error_type):
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
                self._check_deadline(deadline, error_type=DurableStorageTimeout)
                chunk = os.read(descriptor, self.policy.chunk_bytes)
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > self.policy.maximum_object_bytes:
                    raise error_type()
                digest.update(chunk)
        except DurableStorageEngineError:
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

    def _validate_path(self, evidence, *, error_type):
        context = evidence.context
        result = evidence.result
        _objects, _tenant, _backup, directory, path = self._object_paths(
            context,
            result.reference,
            error_type=error_type,
        )
        root_state = _directory_state(self.root, error_type=error_type)
        objects_state = _directory_state(_objects, error_type=error_type)
        tenant_state = _directory_state(_tenant, error_type=error_type)
        backup_state = _directory_state(_backup, error_type=error_type)
        directory_state = _directory_state(directory, error_type=error_type)
        if (
            _identity(root_state) != evidence.root_identity
            or _identity(objects_state) != evidence.objects_identity
            or _identity(tenant_state) != evidence.tenant_identity
            or _identity(backup_state) != evidence.backup_identity
            or _identity(directory_state) != evidence.directory_identity
            or len(
                {
                    root_state.st_dev,
                    objects_state.st_dev,
                    tenant_state.st_dev,
                    backup_state.st_dev,
                    directory_state.st_dev,
                }
            )
            != 1
            or evidence.file_identity is None
        ):
            raise error_type()
        for directory_path in (self.root, _objects, _tenant, _backup, directory):
            _assert_private_mode(directory_path, 0o700, error_type=error_type)
        with os.scandir(directory) as contents:
            if {entry.name for entry in contents} != {STORED_OBJECT_FILE_NAME}:
                raise error_type()
        current = _regular_file_state(path, error_type=error_type)
        if (
            _identity(current) != evidence.file_identity
            or current.st_dev != evidence.directory_identity[0]
            or current.st_size != result.byte_count
        ):
            raise error_type()
        _assert_private_mode(path, 0o600, error_type=error_type)
        deadline = self.monotonic() + self.policy.timeout_seconds
        byte_count, sha256 = self._hash_path(
            path,
            expected_identity=evidence.file_identity,
            deadline=deadline,
            error_type=error_type,
        )
        if (
            byte_count != result.byte_count
            or sha256 != result.sha256
            or byte_count != evidence.source.encrypted_byte_count
            or sha256 != evidence.source.ciphertext_sha256
        ):
            raise error_type()
        descriptor = None
        raw_file = None
        opaque = None
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
                or opened.st_size != result.byte_count
            ):
                raise error_type()
            raw_file = os.fdopen(descriptor, "rb", closefd=True)
            descriptor = None
            opaque = _OpaqueStoredObjectReader(raw_file)
            self.encrypted_artifact_provider.validate_external_encrypted_artifact_stream(
                context=context,
                result=evidence.source,
                reader=opaque,
            )
            opaque.close()
            opaque = None
            raw_file = None
        except (EncryptedArtifactValidationError, OSError):
            raise error_type() from None
        finally:
            target = opaque or raw_file
            if target is not None:
                try:
                    target.close()
                except Exception:
                    pass
            elif descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        return directory, path

    def _validate_reattested_path(self, evidence, *, error_type):
        context = evidence.context
        result = evidence.result
        objects, tenant, backup, directory, path = self._object_paths(
            context,
            result.reference,
            error_type=error_type,
        )
        root_state = _directory_state(self.root, error_type=error_type)
        objects_state = _directory_state(objects, error_type=error_type)
        tenant_state = _directory_state(tenant, error_type=error_type)
        backup_state = _directory_state(backup, error_type=error_type)
        directory_state = _directory_state(directory, error_type=error_type)
        if (
            _identity(root_state) != evidence.root_identity
            or _identity(objects_state) != evidence.objects_identity
            or _identity(tenant_state) != evidence.tenant_identity
            or _identity(backup_state) != evidence.backup_identity
            or _identity(directory_state) != evidence.directory_identity
            or len(
                {
                    root_state.st_dev,
                    objects_state.st_dev,
                    tenant_state.st_dev,
                    backup_state.st_dev,
                    directory_state.st_dev,
                }
            )
            != 1
        ):
            raise error_type()
        for directory_path in (self.root, objects, tenant, backup, directory):
            _assert_private_mode(directory_path, 0o700, error_type=error_type)
        try:
            with os.scandir(directory) as contents:
                if {entry.name for entry in contents} != {STORED_OBJECT_FILE_NAME}:
                    raise error_type()
        except DurableStorageEngineError:
            raise
        except OSError:
            raise error_type() from None
        current = _regular_file_state(path, error_type=error_type)
        if (
            _identity(current) != evidence.file_identity
            or current.st_dev != evidence.directory_identity[0]
            or current.st_size != result.byte_count
        ):
            raise error_type()
        _assert_private_mode(path, 0o600, error_type=error_type)
        byte_count, sha256 = self._hash_path(
            path,
            expected_identity=evidence.file_identity,
            deadline=self.monotonic() + self.policy.timeout_seconds,
            error_type=error_type,
        )
        if byte_count != result.byte_count or sha256 != result.sha256:
            raise error_type()
        return directory, path

    def reattest_stored_object(self, *, context, descriptor):
        """Re-establish exact process-local ownership from persisted opaque evidence.

        The lookup is deterministic and provider-owned. It never accepts or scans
        a caller-supplied path.
        """

        if (
            type(descriptor) is not PersistedStoredObjectDescriptor
            or type(descriptor.reference) is not StoredBackupObjectReference
            or descriptor.backend_identifier
            != LOCAL_DURABLE_STORAGE_BACKEND_IDENTIFIER
            or descriptor.backup_public_id != getattr(context, "backup_public_id", None)
            or descriptor.tenant_public_id
            != getattr(context, "business_public_id", None)
            or type(descriptor.byte_count) is not int
            or not 1 <= descriptor.byte_count <= self.policy.maximum_object_bytes
        ):
            raise DurableObjectValidationError()
        _safe_sha256(descriptor.sha256, error_type=DurableObjectValidationError)
        key = self._state_key(
            context,
            descriptor.reference,
            error_type=DurableObjectValidationError,
        )
        with self._state_lock:
            existing = self._reattested.get(key)
        if existing is not None:
            if existing.context != context or existing.descriptor != descriptor:
                raise DurableObjectValidationError()
            self._validate_reattested_path(
                existing,
                error_type=DurableObjectValidationError,
            )
            return existing.result

        self.root = validate_durable_storage_root(
            self.root,
            staging_root=self.encrypted_artifact_provider.workspace_manager.root,
            media_root=getattr(settings, "MEDIA_ROOT", None),
            static_root=getattr(settings, "STATIC_ROOT", None),
            require_local=self.policy.require_local,
            filesystem_inspector=self.filesystem_inspector,
        )
        objects, tenant, backup, directory, path = self._object_paths(
            context,
            descriptor.reference,
            error_type=DurableObjectNotFound,
        )
        root_state = _directory_state(self.root, error_type=DurableObjectNotFound)
        objects_state = _directory_state(objects, error_type=DurableObjectNotFound)
        tenant_state = _directory_state(tenant, error_type=DurableObjectNotFound)
        backup_state = _directory_state(backup, error_type=DurableObjectNotFound)
        directory_state = _directory_state(directory, error_type=DurableObjectNotFound)
        file_state = _regular_file_state(path, error_type=DurableObjectNotFound)
        if (
            len(
                {
                    root_state.st_dev,
                    objects_state.st_dev,
                    tenant_state.st_dev,
                    backup_state.st_dev,
                    directory_state.st_dev,
                    file_state.st_dev,
                }
            )
            != 1
            or file_state.st_size != descriptor.byte_count
        ):
            raise DurableObjectValidationError()
        for directory_path in (self.root, objects, tenant, backup, directory):
            _assert_private_mode(
                directory_path,
                0o700,
                error_type=DurableObjectValidationError,
            )
        _assert_private_mode(path, 0o600, error_type=DurableObjectValidationError)
        try:
            with os.scandir(directory) as contents:
                if {entry.name for entry in contents} != {STORED_OBJECT_FILE_NAME}:
                    raise DurableObjectValidationError()
        except DurableStorageEngineError:
            raise
        except OSError:
            raise DurableObjectValidationError() from None
        byte_count, sha256 = self._hash_path(
            path,
            expected_identity=_identity(file_state),
            deadline=self.monotonic() + self.policy.timeout_seconds,
            error_type=DurableObjectValidationError,
        )
        if byte_count != descriptor.byte_count or sha256 != descriptor.sha256:
            raise DurableObjectValidationError()
        attested_at = self.clock()
        if not _is_aware(attested_at):
            raise DurableObjectValidationError()
        result = ReattestedStoredObjectResult(
            reference=descriptor.reference,
            backend_identifier=descriptor.backend_identifier,
            object_schema_identifier=STORED_OBJECT_SCHEMA_IDENTIFIER,
            byte_count=byte_count,
            sha256=sha256,
            backup_public_id=context.backup_public_id,
            tenant_public_id=context.business_public_id,
            provider_identifier=LOCAL_DURABLE_STORAGE_PROVIDER_IDENTIFIER,
            attested_at=attested_at.astimezone(UTC),
        )
        evidence = _ReattestedEvidence(
            context=context,
            descriptor=descriptor,
            result=result,
            root_identity=_identity(root_state),
            objects_identity=_identity(objects_state),
            tenant_identity=_identity(tenant_state),
            backup_identity=_identity(backup_state),
            directory_identity=_identity(directory_state),
            file_identity=_identity(file_state),
        )
        self._validate_reattested_path(
            evidence,
            error_type=DurableObjectValidationError,
        )
        with self._state_lock:
            current = self._reattested.get(key)
            if current is not None and current != evidence:
                raise DurableObjectValidationError()
            self._reattested[key] = evidence
        return result

    def validate_reattested_object(self, *, context, result):
        if (
            type(result) is not ReattestedStoredObjectResult
            or type(result.reference) is not StoredBackupObjectReference
        ):
            raise DurableObjectValidationError()
        key = self._state_key(
            context,
            result.reference,
            error_type=DurableObjectValidationError,
        )
        with self._state_lock:
            evidence = self._reattested.get(key)
        if evidence is None or evidence.context != context or evidence.result != result:
            raise DurableObjectValidationError()
        self._validate_reattested_path(
            evidence,
            error_type=DurableObjectValidationError,
        )
        return True

    @contextmanager
    def open_reattested_object(self, *, context, result):
        self.validate_reattested_object(context=context, result=result)
        key = self._state_key(
            context,
            result.reference,
            error_type=DurableObjectNotFound,
        )
        with self._state_lock:
            evidence = self._reattested.get(key)
        if evidence is None:
            raise DurableObjectNotFound()
        directory, path = self._validate_reattested_path(
            evidence,
            error_type=DurableObjectNotFound,
        )
        descriptor = None
        raw_file = None
        opaque = None
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
                or opened.st_size != result.byte_count
            ):
                raise DurableObjectNotFound()
            raw_file = os.fdopen(descriptor, "rb", closefd=True)
            descriptor = None
            opaque = _OpaqueStoredObjectReader(raw_file)
            yield opaque
        except DurableObjectNotFound:
            raise
        except OSError:
            raise DurableObjectNotFound() from None
        finally:
            active_exception = sys.exc_info()[0] is not None
            close_error = None
            target = opaque or raw_file
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
                    _identity(_directory_state(directory, error_type=DurableObjectNotFound))
                    != evidence.directory_identity
                ):
                    raise DurableObjectNotFound()
                self._validate_reattested_path(
                    evidence,
                    error_type=DurableObjectNotFound,
                )
            except BaseException as exc:
                if not active_exception:
                    close_error = exc
            if close_error is not None and not active_exception:
                if isinstance(close_error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    raise close_error.with_traceback(close_error.__traceback__)
                raise DurableObjectNotFound() from None

    def release_reattested_object(self, *, context, result):
        if (
            type(result) is not ReattestedStoredObjectResult
            or type(result.reference) is not StoredBackupObjectReference
        ):
            raise DurableObjectValidationError()
        key = self._state_key(
            context,
            result.reference,
            error_type=DurableObjectValidationError,
        )
        with self._state_lock:
            evidence = self._reattested.get(key)
            if evidence is None or evidence.context != context or evidence.result != result:
                raise DurableObjectValidationError()
            del self._reattested[key]
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
                raise DurableObjectCleanupError() from None
            if (
                path_is_link_like(path)
                or not stat.S_ISREG(current.st_mode)
                or _identity(current) != expected_identity
                or current.st_nlink != remaining_links
            ):
                raise DurableObjectCleanupError()
            os.unlink(path)
            if os.path.lexists(path):
                raise DurableObjectCleanupError()
            remaining_links -= 1
        return bool(owned)

    @staticmethod
    def _remove_empty_directory(directory, *, expected_identity):
        if directory is None or not os.path.lexists(directory):
            return False
        current = _directory_state(directory, error_type=DurableObjectCleanupError)
        if _identity(current) != expected_identity:
            raise DurableObjectCleanupError()
        with os.scandir(directory) as contents:
            if next(contents, None) is not None:
                return False
        os.rmdir(directory)
        if os.path.lexists(directory):
            raise DurableObjectCleanupError()
        return True

    @staticmethod
    def _safe_error(exc):
        if isinstance(
            exc,
            (
                DurableStoragePolicyError,
                UnsafeDurableStorageRoot,
                InsufficientDurableStorageCapacity,
                DurableStorageTimeout,
                DurableObjectCreationError,
                DurableObjectValidationError,
                Phase2GCoordinationError,
            ),
        ):
            return exc
        if isinstance(exc, DurableStorageEngineError):
            return DurableObjectCreationError(
                cleanup_incomplete=getattr(exc, "cleanup_incomplete", False)
            )
        return DurableObjectCreationError()

    def store_encrypted_artifact(self, request):
        if type(request) is StoredBackupObjectRequest:
            source_key = self._source_key(
                request.context,
                request.encrypted_artifact,
                error_type=Phase2GCoordinationError,
            )
            with self._state_lock:
                existing_key = self._by_source.get(source_key)
                existing = self._stored.get(existing_key) if existing_key else None
            if existing is not None:
                if (
                    existing.context != request.context
                    or existing.source != request.encrypted_artifact
                ):
                    raise Phase2GCoordinationError()
                self._validate_path(existing, error_type=DurableObjectValidationError)
                return existing.result
        directory = None
        directory_identity = None
        part_path = None
        final_path = None
        file_identity = None
        descriptor = None
        published_evidence = None
        result = None
        safe_error = None
        abort_error = None
        abort_traceback = None
        cleanup_incomplete = False
        deadline = self.monotonic() + self.policy.timeout_seconds
        try:
            context, source = self._validate_request(request)
            source_key = self._source_key(
                context,
                source,
                error_type=Phase2GCoordinationError,
            )
            reference = self._new_reference()
            key = self._state_key(
                context,
                reference,
                error_type=DurableObjectCreationError,
            )
            with self._state_lock:
                if key in self._stored or key in self._deleted or source_key in self._by_source:
                    raise DurableObjectCreationError()
            stored_at = self.clock()
            if not _is_aware(stored_at):
                raise DurableObjectCreationError()
            (
                root_state,
                _objects,
                objects_state,
                _tenant,
                tenant_state,
                _backup,
                backup_state,
            ) = self._prepare_hierarchy(context)
            self._capacity_check(source)
            _objects, _tenant, _backup, directory, final_path = self._object_paths(
                context,
                reference,
                error_type=DurableObjectCreationError,
            )
            if os.path.lexists(directory):
                raise DurableObjectCreationError()
            directory.mkdir(mode=0o700, exist_ok=False)
            directory_state = _directory_state(
                directory,
                error_type=DurableObjectCreationError,
            )
            directory_identity = _identity(directory_state)
            if directory_state.st_dev != backup_state.st_dev:
                raise DurableObjectCreationError()
            _apply_private_mode(
                directory,
                0o700,
                error_type=DurableObjectCreationError,
            )
            self._run_hook("after_durable_directory_creation")
            part_path = _contained(
                self.root,
                directory / f".{STORED_OBJECT_FILE_NAME}.{uuid.uuid4().hex}.part",
                error_type=DurableObjectCreationError,
            )
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
                raise DurableObjectCreationError()
            file_identity = _identity(opened)
            _apply_private_descriptor_mode(
                descriptor,
                part_path,
                0o600,
                error_type=DurableObjectCreationError,
            )
            digest = hashlib.sha256()
            byte_count = 0
            try:
                with self.encrypted_artifact_provider.open_encrypted_artifact(
                    context=context,
                    reference=source.reference,
                ) as reader:
                    while True:
                        self._check_deadline(deadline)
                        chunk = reader.read(self.policy.chunk_bytes)
                        if type(chunk) is not bytes or len(chunk) > self.policy.chunk_bytes:
                            raise DurableObjectCreationError()
                        if not chunk:
                            break
                        byte_count += len(chunk)
                        if byte_count > self.policy.maximum_object_bytes:
                            raise DurableStoragePolicyError()
                        digest.update(chunk)
                        self._write_all(descriptor, chunk)
            except (EncryptedArtifactNotFound, EncryptedArtifactValidationError):
                raise DurableObjectValidationError() from None
            if (
                byte_count != source.encrypted_byte_count
                or digest.hexdigest() != source.ciphertext_sha256
            ):
                raise DurableObjectValidationError()
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            current = _regular_file_state(
                part_path,
                error_type=DurableObjectCreationError,
            )
            if (
                _identity(current) != file_identity
                or current.st_size != source.encrypted_byte_count
            ):
                raise DurableObjectCreationError()
            self._run_hook("before_durable_publication")
            os.link(part_path, final_path, follow_symlinks=False)
            for path in (part_path, final_path):
                linked = os.stat(path, follow_symlinks=False)
                if (
                    _identity(linked) != file_identity
                    or linked.st_nlink != 2
                    or not stat.S_ISREG(linked.st_mode)
                ):
                    raise DurableObjectCreationError()
            self._run_hook("after_durable_publication_link")
            os.unlink(part_path)
            part_path = None
            final = _regular_file_state(
                final_path,
                error_type=DurableObjectCreationError,
            )
            if (
                _identity(final) != file_identity
                or final.st_size != source.encrypted_byte_count
            ):
                raise DurableObjectCreationError()
            _fsync_directory(directory, error_type=DurableObjectCreationError)
            self._run_hook("after_durable_publication")
            candidate = StoredBackupObjectResult(
                reference=reference,
                backend_identifier=LOCAL_DURABLE_STORAGE_BACKEND_IDENTIFIER,
                object_schema_identifier=STORED_OBJECT_SCHEMA_IDENTIFIER,
                byte_count=source.encrypted_byte_count,
                sha256=source.ciphertext_sha256,
                source_encrypted_artifact_sha256=source.ciphertext_sha256,
                backup_public_id=context.backup_public_id,
                tenant_public_id=context.business_public_id,
                stored_at=stored_at.astimezone(UTC),
                provider_identifier=LOCAL_DURABLE_STORAGE_PROVIDER_IDENTIFIER,
                durability_state=StoredObjectDurabilityState.STORED,
                verification_state=StoredObjectVerificationState.STORED_AND_VERIFIED,
                encrypted_format_identifier=source.format_identifier,
                encryption_algorithm=source.encryption_algorithm,
                kek_provider_identifier=source.kek_provider_identifier,
                kek_key_identifier=source.kek_key_identifier,
                kek_version=source.kek_version,
                encrypted_staging_cleanup_incomplete=True,
            )
            evidence = _StoredEvidence(
                context=context,
                source=source,
                result=candidate,
                root_identity=_identity(root_state),
                objects_identity=_identity(objects_state),
                tenant_identity=_identity(tenant_state),
                backup_identity=_identity(backup_state),
                directory_identity=directory_identity,
                file_identity=file_identity,
            )
            self._validate_path(evidence, error_type=DurableObjectValidationError)
            with self._state_lock:
                if key in self._stored or key in self._deleted or source_key in self._by_source:
                    raise DurableObjectCreationError()
                self._stored[key] = evidence
                self._by_source[source_key] = key
            published_evidence = evidence
            result = candidate
            self._run_hook("before_encrypted_staging_cleanup")
            try:
                self.encrypted_artifact_provider.cleanup_encrypted_artifact(
                    context=context,
                    reference=source.reference,
                )
            except EncryptedArtifactCleanupError:
                return result
            completed = replace(
                result,
                encrypted_staging_cleanup_incomplete=False,
            )
            updated = replace(evidence, result=completed)
            with self._state_lock:
                if self._stored.get(key) != evidence:
                    raise EncryptedStagingCleanupError(
                        encrypted_staging_cleanup_incomplete=True
                    )
                self._stored[key] = updated
            result = completed
            published_evidence = updated
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
                        raise DurableObjectCleanupError()
                except BaseException:
                    cleanup_incomplete = True
                if directory is not None and directory_identity is not None:
                    try:
                        removed = self._remove_empty_directory(
                            directory,
                            expected_identity=directory_identity,
                        )
                        if not removed and os.path.lexists(directory):
                            cleanup_incomplete = True
                    except BaseException:
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
            raise DurableObjectCreationError(cleanup_incomplete=cleanup_incomplete)
        return result

    def validate_stored_object(self, *, context, result):
        if (
            type(result) is not StoredBackupObjectResult
            or type(result.reference) is not StoredBackupObjectReference
        ):
            raise DurableObjectValidationError()
        key = self._state_key(
            context,
            result.reference,
            error_type=DurableObjectValidationError,
        )
        with self._state_lock:
            evidence = self._stored.get(key)
        if (
            evidence is None
            or evidence.context != context
            or evidence.result != result
        ):
            raise DurableObjectValidationError()
        self._validate_path(evidence, error_type=DurableObjectValidationError)
        return True

    def owns_stored_object_reference(self, *, context, reference):
        try:
            key = self._state_key(
                context,
                reference,
                error_type=DurableObjectValidationError,
            )
        except DurableObjectValidationError:
            return False
        with self._state_lock:
            evidence = self._stored.get(key)
        return evidence is not None and evidence.context == context

    def owns_stored_object_result(self, *, context, result):
        if (
            type(result) is not StoredBackupObjectResult
            or type(result.reference) is not StoredBackupObjectReference
        ):
            return False
        try:
            key = self._state_key(
                context,
                result.reference,
                error_type=DurableObjectValidationError,
            )
        except DurableObjectValidationError:
            return False
        with self._state_lock:
            evidence = self._stored.get(key)
        return (
            evidence is not None
            and evidence.context == context
            and evidence.result == result
        )

    def confirm_stored_object_absent(self, *, context, reference):
        try:
            key = self._state_key(
                context,
                reference,
                error_type=DurableObjectValidationError,
            )
        except DurableObjectValidationError:
            return False
        with self._state_lock:
            evidence = self._deleted.get(key)
        if evidence is None or evidence.context != context:
            return False
        try:
            _objects, _tenant, _backup, directory, _path = self._object_paths(
                context,
                reference,
                error_type=DurableObjectValidationError,
            )
            return not os.path.lexists(directory)
        except DurableObjectValidationError:
            return False

    def retry_encrypted_staging_cleanup(self, request, result):
        if (
            type(request) is not StoredBackupObjectRequest
            or type(result) is not StoredBackupObjectResult
            or result.encrypted_staging_cleanup_incomplete is not True
        ):
            raise EncryptedStagingCleanupError()
        context = request.context
        source = request.encrypted_artifact
        key = self._state_key(
            context,
            result.reference,
            error_type=EncryptedStagingCleanupError,
        )
        with self._state_lock:
            evidence = self._stored.get(key)
        if (
            evidence is None
            or evidence.context != context
            or evidence.source != source
            or evidence.result != result
        ):
            raise EncryptedStagingCleanupError()
        self._validate_path(evidence, error_type=EncryptedStagingCleanupError)
        try:
            self.encrypted_artifact_provider.cleanup_encrypted_artifact(
                context=context,
                reference=source.reference,
            )
        except EncryptedArtifactCleanupError:
            raise EncryptedStagingCleanupError(
                encrypted_staging_cleanup_incomplete=True
            ) from None
        completed = replace(result, encrypted_staging_cleanup_incomplete=False)
        updated = replace(evidence, result=completed)
        with self._state_lock:
            if self._stored.get(key) != evidence:
                raise EncryptedStagingCleanupError(
                    encrypted_staging_cleanup_incomplete=True
                )
            self._stored[key] = updated
        return completed

    @contextmanager
    def open_stored_object(self, *, context, reference):
        key = self._state_key(
            context,
            reference,
            error_type=DurableObjectNotFound,
        )
        with self._state_lock:
            evidence = self._stored.get(key)
        if evidence is None or evidence.file_identity is None:
            raise DurableObjectNotFound()
        directory, path = self._validate_path(evidence, error_type=DurableObjectNotFound)
        descriptor = None
        raw_file = None
        opaque = None
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
                or opened.st_size != evidence.result.byte_count
            ):
                raise DurableObjectNotFound()
            raw_file = os.fdopen(descriptor, "rb", closefd=True)
            descriptor = None
            opaque = _OpaqueStoredObjectReader(raw_file)
            yield opaque
        except DurableObjectNotFound:
            raise
        except OSError:
            raise DurableObjectNotFound() from None
        finally:
            active_exception = sys.exc_info()[0] is not None
            close_error = None
            target = opaque or raw_file
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
                    _identity(_directory_state(directory, error_type=DurableObjectNotFound))
                    != evidence.directory_identity
                ):
                    raise DurableObjectNotFound()
                self._validate_path(evidence, error_type=DurableObjectNotFound)
            except BaseException as exc:
                if not active_exception:
                    close_error = exc
            if close_error is not None and not active_exception:
                if isinstance(close_error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    raise close_error.with_traceback(close_error.__traceback__)
                raise DurableObjectNotFound() from None

    def delete_stored_object(self, *, context, reference):
        key = self._state_key(
            context,
            reference,
            error_type=DurableObjectCleanupError,
        )
        with self._state_lock:
            deleted = self._deleted.get(key)
            if deleted is not None:
                if deleted.context != context:
                    raise DurableObjectCleanupError()
                return True
            evidence = self._stored.get(key)
        if evidence is None or evidence.context != context:
            raise DurableObjectCleanupError()
        try:
            if evidence.file_identity is not None:
                directory, path = self._validate_path(
                    evidence,
                    error_type=DurableObjectCleanupError,
                )
                self._run_hook("before_durable_delete_unlink")
                abort = None
                abort_traceback = None
                try:
                    os.unlink(path)
                except BaseException as exc:
                    if os.path.lexists(path):
                        raise
                    if not isinstance(exc, Exception):
                        abort = exc
                        abort_traceback = exc.__traceback__
                if os.path.lexists(path):
                    raise DurableObjectCleanupError()
                updated = replace(evidence, file_identity=None)
                with self._state_lock:
                    if self._stored.get(key) != evidence:
                        raise DurableObjectCleanupError()
                    self._stored[key] = updated
                evidence = updated
                if abort is not None:
                    raise abort.with_traceback(abort_traceback)
            else:
                _objects, _tenant, _backup, directory, path = self._object_paths(
                    context,
                    reference,
                    error_type=DurableObjectCleanupError,
                )
                if not os.path.lexists(directory):
                    source_key = self._source_key(
                        context,
                        evidence.source,
                        error_type=DurableObjectCleanupError,
                    )
                    with self._state_lock:
                        if self._stored.get(key) != evidence:
                            raise DurableObjectCleanupError()
                        self._stored.pop(key, None)
                        if self._by_source.get(source_key) == key:
                            self._by_source.pop(source_key, None)
                        self._deleted[key] = evidence
                    return True
                if os.path.lexists(path):
                    raise DurableObjectCleanupError()
            if _identity(
                _directory_state(directory, error_type=DurableObjectCleanupError)
            ) != evidence.directory_identity:
                raise DurableObjectCleanupError()
            with os.scandir(directory) as contents:
                if next(contents, None) is not None:
                    raise DurableObjectCleanupError()
            self._run_hook("before_durable_delete_directory")
            os.rmdir(directory)
            if os.path.lexists(directory):
                raise DurableObjectCleanupError()
            source_key = self._source_key(
                context,
                evidence.source,
                error_type=DurableObjectCleanupError,
            )
            with self._state_lock:
                if self._stored.get(key) != evidence:
                    raise DurableObjectCleanupError()
                self._stored.pop(key, None)
                if self._by_source.get(source_key) == key:
                    self._by_source.pop(source_key, None)
                self._deleted[key] = evidence
            return True
        except DurableObjectCleanupError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise DurableObjectCleanupError() from None


__all__ = [
    "LOCAL_DURABLE_STORAGE_BACKEND_IDENTIFIER",
    "LOCAL_DURABLE_STORAGE_PROVIDER_IDENTIFIER",
    "STORED_OBJECT_FILE_NAME",
    "STORED_OBJECT_SCHEMA_IDENTIFIER",
    "LocalPrivateDurableStorageProvider",
]
