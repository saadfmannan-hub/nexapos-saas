"""Secure local-filesystem media capture for Backup Engine Phase 2D-1."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from django.core.files.storage import FileSystemStorage, storages
from django.utils import timezone

from .context import BackupExecutionContext
from .contracts import (
    MediaCaptureReference,
    MediaCaptureResult,
    SnapshotReference,
    SnapshotResult,
)
from .exceptions import (
    InsufficientMediaCaptureCapacity,
    MediaCaptureCleanupError,
    MediaCaptureCreationError,
    MediaCaptureLimitExceeded,
    MediaCaptureTimeout,
    MediaObjectChanged,
    MediaObjectNotFound,
    MediaStorageAliasCollision,
    MediaStorageNameCollision,
    Phase2D1EngineError,
    SnapshotEngineError,
    UnsafeMediaStorageObject,
    UnsupportedMediaStorageBackend,
)
from .logical_serialization import validate_media_storage_name
from .media_capture_policy import (
    MediaCapturePolicy,
    required_media_staging_capacity,
)
from .snapshot_policy import LocalFilesystemInspector
from .sqlite_snapshot import (
    SQLITE_SNAPSHOT_PROVIDER_IDENTIFIER,
    SQLiteSnapshotProvider,
)
from .workspace import (
    BackupWorkspaceManager,
    WorkspaceArea,
    WorkspaceReference,
    contained_path,
    path_has_link_like_component,
    path_is_link_like,
)

LOCAL_FILESYSTEM_MEDIA_CAPTURE_PROVIDER_IDENTIFIER = (
    "django-filesystem-media-capture-v1"
)
LOCAL_MEDIA_CAPTURE_PROVIDER_IDENTIFIER = (
    LOCAL_FILESYSTEM_MEDIA_CAPTURE_PROVIDER_IDENTIFIER
)
MEDIA_CONTENT_FILE_NAME = "content.bin"
_MAXIMUM_MEDIA_NAME_LENGTH = 4096
_MAXIMUM_DURATION_MS = 86_400_000
_MAXIMUM_MOUNTINFO_BYTES = 8 * 1024**2
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _PublishedCapture:
    context: BackupExecutionContext
    directory_identity: tuple[int, int]
    file_identity: tuple[int, int] | None
    byte_count: int
    sha256: str
    logical_storage_name: str
    source_reference_count: int
    chunk_bytes: int


@dataclass(frozen=True, slots=True)
class _CaptureAccessPolicy:
    chunk_bytes: int
    require_local_staging: bool = False


class _OpaqueMediaReader:
    __slots__ = ("__file",)

    def __init__(self, file_object):
        self.__file = file_object

    def read(self, size=-1):
        return self.__file.read(size)

    def readline(self, size=-1):
        return self.__file.readline(size)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.__file)

    def close(self):
        return self.__file.close()

    @property
    def closed(self):
        return self.__file.closed


def media_storage_collision_key(storage_name) -> str:
    """Return a segment-preserving cross-platform comparison key."""

    if type(storage_name) is not str:
        raise MediaStorageNameCollision()
    try:
        return "/".join(
            unicodedata.normalize("NFKC", segment).casefold()
            for segment in storage_name.split("/")
        )
    except (TypeError, ValueError):
        raise MediaStorageNameCollision() from None


def _identity(value) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _stable_file_state(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        getattr(value, "st_mtime_ns", int(value.st_mtime * 1_000_000_000)),
        getattr(value, "st_ctime_ns", int(value.st_ctime * 1_000_000_000)),
    )


def _datetime_ns(value) -> int:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise MediaCaptureCreationError()
    try:
        delta = value.astimezone(UTC) - _EPOCH
        return (
            delta.days * 86_400_000_000_000
            + delta.seconds * 1_000_000_000
            + delta.microseconds * 1000
        )
    except (OverflowError, TypeError, ValueError):
        raise MediaCaptureCreationError() from None


def _linux_mount_boundaries(root):
    if not sys.platform.startswith("linux"):
        return frozenset()
    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open("/proc/self/mountinfo", flags)
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024**2)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAXIMUM_MOUNTINFO_BYTES:
                raise UnsupportedMediaStorageBackend()
            chunks.append(chunk)
        rendered = b"".join(chunks).decode("utf-8", errors="replace")
        boundaries = set()
        for line in rendered.splitlines():
            fields = line.split()
            if len(fields) < 5:
                raise UnsupportedMediaStorageBackend()
            value = fields[4]
            for encoded, decoded in (
                ("\\040", " "),
                ("\\011", "\t"),
                ("\\012", "\n"),
                ("\\134", "\\"),
            ):
                value = value.replace(encoded, decoded)
            mount_point = Path(value)
            try:
                mount_point.relative_to(root)
            except ValueError:
                continue
            if mount_point != root:
                boundaries.add(mount_point)
        return frozenset(boundaries)
    except Phase2D1EngineError:
        raise
    except Exception:
        raise UnsupportedMediaStorageBackend() from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _apply_private_mode(path, mode, *, error_type):
    try:
        os.chmod(path, mode, follow_symlinks=False)
        if os.name != "nt":
            current = os.stat(path, follow_symlinks=False)
            if stat.S_IMODE(current.st_mode) != mode:
                raise error_type()
    except Phase2D1EngineError:
        raise
    except (NotImplementedError, OSError, TypeError):
        if os.name != "nt":
            raise error_type() from None


def _assert_private_mode(path, mode, *, error_type):
    if os.name == "nt":
        return
    try:
        current = os.stat(path, follow_symlinks=False)
        if stat.S_IMODE(current.st_mode) != mode:
            raise error_type()
    except Phase2D1EngineError:
        raise
    except OSError:
        raise error_type() from None


def _directory_state(path, *, error_type):
    try:
        if path_is_link_like(path):
            raise error_type()
        current = os.stat(path, follow_symlinks=False)
        if not stat.S_ISDIR(current.st_mode):
            raise error_type()
        return current
    except Phase2D1EngineError:
        raise
    except OSError:
        raise error_type() from None


def _regular_state(path, *, missing_error, unsafe_error):
    try:
        if not os.path.lexists(path):
            raise missing_error()
        if path_is_link_like(path):
            raise unsafe_error()
        current = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise unsafe_error()
        return current
    except (MediaObjectNotFound, Phase2D1EngineError):
        raise
    except FileNotFoundError:
        raise missing_error() from None
    except OSError:
        raise unsafe_error() from None


class _AtomicMediaWriter:
    """A single private no-clobber media publication."""

    def __init__(
        self,
        *,
        directory,
        directory_identity,
        byte_limit,
        failure_hook,
    ):
        self.directory = Path(directory)
        self.directory_identity = directory_identity
        self.byte_limit = byte_limit
        self.failure_hook = failure_hook
        self.part_path = contained_path(
            self.directory,
            self.directory / f".media-{uuid.uuid4().hex}.part",
        )
        self.final_path = contained_path(
            self.directory,
            self.directory / MEDIA_CONTENT_FILE_NAME,
        )
        self.descriptor = None
        self.part_identity = None
        self.final_identity = None
        self.byte_count = 0
        self.digest = hashlib.sha256()
        self._create()

    def _run_hook(self, stage):
        if self.failure_hook is not None:
            self.failure_hook(stage)

    def _assert_directory(self, *, error_type):
        current = _directory_state(self.directory, error_type=error_type)
        if _identity(current) != self.directory_identity:
            raise error_type()

    def _create(self):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_BINARY", 0)
        try:
            self._assert_directory(error_type=MediaCaptureCreationError)
            self.descriptor = os.open(self.part_path, flags, 0o600)
            current = os.fstat(self.descriptor)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or current.st_dev != self.directory_identity[0]
            ):
                raise MediaCaptureCreationError()
            self.part_identity = _identity(current)
            try:
                os.fchmod(self.descriptor, 0o600)
            except (AttributeError, NotImplementedError, OSError):
                if os.name != "nt":
                    raise MediaCaptureCreationError() from None
            _apply_private_mode(
                self.part_path,
                0o600,
                error_type=MediaCaptureCreationError,
            )
            if (
                _identity(os.stat(self.part_path, follow_symlinks=False))
                != self.part_identity
            ):
                raise MediaCaptureCreationError()
            self._run_hook("after_media_part_creation")
        except BaseException as exc:
            cleanup_incomplete = False
            if self.descriptor is not None:
                try:
                    os.close(self.descriptor)
                except BaseException:
                    cleanup_incomplete = True
                self.descriptor = None
            if os.path.lexists(self.part_path):
                try:
                    current = os.stat(
                        self.part_path,
                        follow_symlinks=False,
                    )
                    if (
                        self.part_identity is None
                        or _identity(current) != self.part_identity
                        or not stat.S_ISREG(current.st_mode)
                        or current.st_nlink != 1
                    ):
                        raise MediaCaptureCleanupError()
                    os.unlink(self.part_path)
                except BaseException:
                    cleanup_incomplete = True
            if isinstance(exc, Phase2D1EngineError):
                exc.cleanup_incomplete = bool(
                    cleanup_incomplete
                    or getattr(exc, "cleanup_incomplete", False)
                )
                raise
            if isinstance(
                exc,
                (KeyboardInterrupt, SystemExit, GeneratorExit),
            ):
                try:
                    exc.cleanup_incomplete = cleanup_incomplete
                except Exception:
                    pass
                raise
            raise MediaCaptureCreationError(
                cleanup_incomplete=cleanup_incomplete
            ) from None

    def write(self, chunk):
        if type(chunk) is not bytes or not chunk:
            raise MediaCaptureCreationError()
        if self.descriptor is None:
            raise MediaCaptureCreationError()
        if self.byte_count + len(chunk) > self.byte_limit:
            raise MediaCaptureLimitExceeded()
        self._assert_directory(error_type=MediaCaptureCreationError)
        self._run_hook("before_media_destination_write")
        view = memoryview(chunk)
        written = 0
        try:
            while written < len(view):
                count = os.write(self.descriptor, view[written:])
                if type(count) is not int or count <= 0:
                    raise MediaCaptureCreationError()
                written += count
        except Phase2D1EngineError:
            raise
        except OSError:
            raise MediaCaptureCreationError() from None
        self.byte_count += len(chunk)
        self.digest.update(chunk)
        self._assert_directory(error_type=MediaCaptureCreationError)
        self._run_hook("after_media_destination_write")

    def finalize(self):
        if self.descriptor is None or self.part_identity is None:
            raise MediaCaptureCreationError()
        try:
            self._assert_directory(error_type=MediaCaptureCreationError)
            self._run_hook("before_media_flush")
            current = os.fstat(self.descriptor)
            if (
                _identity(current) != self.part_identity
                or current.st_size != self.byte_count
                or current.st_nlink != 1
            ):
                raise MediaCaptureCreationError()
            self._run_hook("after_media_flush")
            self._run_hook("before_media_fsync")
            os.fsync(self.descriptor)
            self._run_hook("after_media_fsync")
            os.close(self.descriptor)
            self.descriptor = None
            current = os.stat(self.part_path, follow_symlinks=False)
            if (
                _identity(current) != self.part_identity
                or current.st_size != self.byte_count
                or current.st_nlink != 1
            ):
                raise MediaCaptureCreationError()
            self._run_hook("before_media_publication")
            self._assert_directory(error_type=MediaCaptureCreationError)
            if os.path.lexists(self.final_path):
                raise MediaCaptureCreationError()
            os.link(
                self.part_path,
                self.final_path,
                follow_symlinks=False,
            )
            linked = os.stat(self.final_path, follow_symlinks=False)
            part = os.stat(self.part_path, follow_symlinks=False)
            if (
                _identity(linked) != self.part_identity
                or _identity(part) != self.part_identity
                or linked.st_nlink != 2
                or part.st_nlink != 2
            ):
                raise MediaCaptureCreationError()
            os.unlink(self.part_path)
            final = os.stat(self.final_path, follow_symlinks=False)
            if (
                _identity(final) != self.part_identity
                or final.st_nlink != 1
                or final.st_size != self.byte_count
            ):
                raise MediaCaptureCreationError()
            self.final_identity = self.part_identity
            self._assert_directory(error_type=MediaCaptureCreationError)
            _apply_private_mode(
                self.final_path,
                0o600,
                error_type=MediaCaptureCreationError,
            )
            self._run_hook("after_media_publication")
        except Phase2D1EngineError:
            raise
        except OSError:
            raise MediaCaptureCreationError() from None

    def cleanup_owned(self):
        close_failed = False
        if self.descriptor is not None:
            try:
                os.close(self.descriptor)
            except OSError:
                close_failed = True
            self.descriptor = None
        self._assert_directory(error_type=MediaCaptureCleanupError)
        paths = (
            (self.final_path, self.final_identity or self.part_identity),
            (self.part_path, self.part_identity),
        )
        existing = []
        for path, expected_identity in paths:
            if not os.path.lexists(path):
                continue
            try:
                current = os.stat(path, follow_symlinks=False)
                if (
                    expected_identity is None
                    or _identity(current) != expected_identity
                    or not stat.S_ISREG(current.st_mode)
                ):
                    raise MediaCaptureCleanupError()
                existing.append((path, expected_identity))
            except Phase2D1EngineError:
                raise
            except OSError:
                raise MediaCaptureCleanupError() from None
        expected_links = len(existing)
        for path, expected_identity in existing:
            try:
                current = os.stat(path, follow_symlinks=False)
                if (
                    _identity(current) != expected_identity
                    or current.st_nlink != expected_links
                ):
                    raise MediaCaptureCleanupError()
                os.unlink(path)
                expected_links -= 1
            except Phase2D1EngineError:
                raise
            except OSError:
                raise MediaCaptureCleanupError() from None
        if close_failed:
            raise MediaCaptureCleanupError()
        return True


class LocalFilesystemMediaCaptureProvider:
    """Capture exact local media names into private opaque workspace objects."""

    def __init__(
        self,
        *,
        snapshot_provider,
        workspace_manager=None,
        policy=None,
        storage_resolver=None,
        filesystem_inspector=None,
        disk_usage_provider=None,
        monotonic=None,
        reference_factory=None,
        failure_hook=None,
    ):
        self.workspace_manager = workspace_manager or BackupWorkspaceManager()
        if (
            type(self.workspace_manager) is not BackupWorkspaceManager
            or type(snapshot_provider) is not SQLiteSnapshotProvider
            or snapshot_provider.workspace_manager.root
            != self.workspace_manager.root
        ):
            raise UnsupportedMediaStorageBackend()
        self.snapshot_provider = snapshot_provider
        if policy is not None and type(policy) is not MediaCapturePolicy:
            raise UnsupportedMediaStorageBackend()
        self.policy = policy
        self.storage_resolver = storage_resolver or (
            lambda: storages["default"]
        )
        self.filesystem_inspector = (
            filesystem_inspector or LocalFilesystemInspector()
        )
        self.disk_usage_provider = disk_usage_provider or shutil.disk_usage
        self.monotonic = monotonic or time.monotonic
        self.reference_factory = reference_factory or (
            lambda: MediaCaptureReference(uuid.uuid4())
        )
        self.failure_hook = failure_hook
        self._published = {}
        self._cleaned = {}
        self._state_lock = threading.RLock()

    def _run_hook(self, stage):
        if self.failure_hook is not None:
            self.failure_hook(stage)

    def _monotonic_value(self):
        try:
            value = float(self.monotonic())
        except Exception:
            raise MediaCaptureCreationError() from None
        if value != value or value in {float("inf"), float("-inf")}:
            raise MediaCaptureCreationError()
        return value

    def _check_deadline(self, deadline):
        if self._monotonic_value() > deadline:
            raise MediaCaptureTimeout()

    def _duration_ms(self, started):
        try:
            return min(
                max(0, int((self._monotonic_value() - started) * 1000)),
                _MAXIMUM_DURATION_MS,
            )
        except Phase2D1EngineError:
            return 0

    def _new_reference(self):
        try:
            reference = self.reference_factory()
            if type(reference) is uuid.UUID:
                reference = MediaCaptureReference(reference)
            if (
                type(reference) is not MediaCaptureReference
                or type(reference.identifier) is not uuid.UUID
            ):
                raise TypeError
            return reference
        except Exception:
            raise MediaCaptureCreationError() from None

    @staticmethod
    def _validated_snapshot(snapshot_result):
        if (
            type(snapshot_result) is not SnapshotResult
            or type(snapshot_result.reference) is not SnapshotReference
            or type(snapshot_result.reference.identifier) is not uuid.UUID
            or snapshot_result.consistent is not True
            or snapshot_result.provider_identifier
            != SQLITE_SNAPSHOT_PROVIDER_IDENTIFIER
            or type(snapshot_result.created_at) is not datetime
            or snapshot_result.created_at.tzinfo is None
            or snapshot_result.created_at.utcoffset() is None
            or type(snapshot_result.consistency_cutoff_at) is not datetime
            or snapshot_result.consistency_cutoff_at.tzinfo is None
            or snapshot_result.consistency_cutoff_at.utcoffset() is None
            or snapshot_result.consistency_cutoff_at.utcoffset()
            != UTC.utcoffset(None)
            or snapshot_result.consistency_cutoff_at
            > snapshot_result.created_at
        ):
            raise MediaCaptureCreationError()
        return snapshot_result

    @staticmethod
    def _validated_context(context):
        if (
            type(context) is not BackupExecutionContext
            or type(context.workspace_reference) is not WorkspaceReference
            or type(context.workspace_reference.identifier) is not uuid.UUID
            or type(context.backup_public_id) is not uuid.UUID
            or type(context.business_public_id) is not uuid.UUID
            or type(context.business_id) is not int
            or context.business_id <= 0
        ):
            raise MediaCaptureCreationError()
        return context

    def _validated_media_root(self):
        try:
            configured = Path(getattr(settings, "MEDIA_ROOT", "")).expanduser()
            if not configured.is_absolute():
                raise UnsupportedMediaStorageBackend()
            lexical_root = Path(os.path.abspath(configured))
            if (
                path_has_link_like_component(lexical_root)
                or not os.path.lexists(lexical_root)
            ):
                raise UnsupportedMediaStorageBackend()
            root_state = os.stat(lexical_root, follow_symlinks=False)
            if not stat.S_ISDIR(root_state.st_mode):
                raise UnsupportedMediaStorageBackend()
            resolved_root = lexical_root.resolve(strict=True)
            if resolved_root != lexical_root:
                raise UnsupportedMediaStorageBackend()
            storage = self.storage_resolver()
            if type(storage) is not FileSystemStorage:
                raise UnsupportedMediaStorageBackend()
            location = Path(storage.location).expanduser()
            if not location.is_absolute():
                raise UnsupportedMediaStorageBackend()
            lexical_location = Path(os.path.abspath(location))
            if path_has_link_like_component(lexical_location):
                raise UnsupportedMediaStorageBackend()
            if lexical_location.resolve(strict=True) != resolved_root:
                raise UnsupportedMediaStorageBackend()
            if _identity(os.stat(resolved_root, follow_symlinks=False)) != _identity(
                root_state
            ):
                raise UnsupportedMediaStorageBackend()
            assessment = self.filesystem_inspector.assess(resolved_root)
            if getattr(assessment, "confirmed_local", None) is not True:
                raise UnsupportedMediaStorageBackend()
            mount_boundaries = _linux_mount_boundaries(resolved_root)
            return resolved_root, root_state, mount_boundaries
        except UnsupportedMediaStorageBackend:
            raise
        except Exception:
            raise UnsupportedMediaStorageBackend() from None

    def _existing_workspace(self, context, *, error_type):
        try:
            self._validated_context(context)
            root = self.workspace_manager.root
            root_state = _directory_state(root, error_type=error_type)
            workspace = self.workspace_manager.handle(
                context.workspace_reference
            )
            workspace_state = _directory_state(
                workspace.path,
                error_type=error_type,
            )
            if root_state.st_dev != workspace_state.st_dev:
                raise error_type()
            _apply_private_mode(root, 0o700, error_type=error_type)
            _apply_private_mode(
                workspace.path,
                0o700,
                error_type=error_type,
            )
            return workspace
        except Phase2D1EngineError:
            raise
        except Exception:
            raise error_type() from None

    def _media_parent(self, context, *, create, policy, error_type):
        workspace = self._existing_workspace(context, error_type=error_type)
        try:
            parent = workspace.system_area_path(WorkspaceArea.MEDIA)
            if os.path.lexists(parent) and path_is_link_like(parent):
                raise error_type()
            if create:
                parent.mkdir(mode=0o700, exist_ok=True)
            parent_state = _directory_state(parent, error_type=error_type)
            workspace_state = _directory_state(
                workspace.path,
                error_type=error_type,
            )
            if parent_state.st_dev != workspace_state.st_dev:
                raise error_type()
            _apply_private_mode(parent, 0o700, error_type=error_type)
            if policy.require_local_staging:
                assessment = self.filesystem_inspector.assess(parent)
                if getattr(assessment, "confirmed_local", None) is not True:
                    raise error_type()
            return workspace, parent, parent_state
        except Phase2D1EngineError:
            raise
        except Exception:
            raise error_type() from None

    @staticmethod
    def _source_path(root, root_state, storage_name, mount_boundaries):
        try:
            if (
                path_is_link_like(root)
                or _identity(os.stat(root, follow_symlinks=False))
                != _identity(root_state)
            ):
                raise UnsafeMediaStorageObject()
            storage_name = validate_media_storage_name(
                storage_name,
                maximum_length=_MAXIMUM_MEDIA_NAME_LENGTH,
            )
            candidate = Path(root)
            for segment in storage_name.split("/"):
                candidate /= segment
            candidate = Path(os.path.abspath(candidate))
            contained_path(root, candidate)
            current = Path(root)
            segments = storage_name.split("/")
            for index, segment in enumerate(segments):
                current /= segment
                if current in mount_boundaries or os.path.ismount(current):
                    raise UnsafeMediaStorageObject()
                if not os.path.lexists(current):
                    raise MediaObjectNotFound()
                if path_is_link_like(current):
                    raise UnsafeMediaStorageObject()
                state = os.stat(current, follow_symlinks=False)
                if state.st_dev != root_state.st_dev:
                    raise UnsafeMediaStorageObject()
                if index < len(segments) - 1:
                    if not stat.S_ISDIR(state.st_mode):
                        raise UnsafeMediaStorageObject()
                elif (
                    not stat.S_ISREG(state.st_mode)
                    or state.st_nlink != 1
                ):
                    raise UnsafeMediaStorageObject()
            resolved = candidate.resolve(strict=True)
            try:
                resolved.relative_to(root)
            except ValueError:
                raise UnsafeMediaStorageObject() from None
            state = _regular_state(
                candidate,
                missing_error=MediaObjectNotFound,
                unsafe_error=UnsafeMediaStorageObject,
            )
            if (
                state.st_dev != root_state.st_dev
                or _identity(os.stat(root, follow_symlinks=False))
                != _identity(root_state)
            ):
                raise UnsafeMediaStorageObject()
            return storage_name, candidate, state
        except (MediaObjectNotFound, Phase2D1EngineError):
            raise
        except Exception:
            raise UnsafeMediaStorageObject() from None

    @staticmethod
    def _validate_source_limits(
        state,
        *,
        cutoff_ns,
        policy,
        cumulative_bytes,
    ):
        if state.st_size > policy.maximum_file_bytes:
            raise MediaCaptureLimitExceeded()
        if cumulative_bytes + state.st_size > policy.maximum_total_bytes:
            raise MediaCaptureLimitExceeded()
        if getattr(
            state,
            "st_mtime_ns",
            int(state.st_mtime * 1_000_000_000),
        ) > cutoff_ns:
            raise MediaObjectChanged()

    def _capture_directory(self, context, reference, *, create, policy, error_type):
        workspace, parent, parent_state = self._media_parent(
            context,
            create=create,
            policy=policy,
            error_type=error_type,
        )
        try:
            directory = workspace.system_area_path(
                WorkspaceArea.MEDIA,
                generated_identifier=reference.identifier,
            )
            if os.path.lexists(directory) and path_is_link_like(directory):
                raise error_type()
            return directory, parent, parent_state
        except Phase2D1EngineError:
            raise
        except Exception:
            raise error_type() from None

    def _create_capture_directory(self, context, reference, *, policy):
        directory, parent, parent_state = self._capture_directory(
            context,
            reference,
            create=True,
            policy=policy,
            error_type=MediaCaptureCreationError,
        )
        absent_before_creation = not os.path.lexists(directory)
        directory_identity = None
        try:
            directory.mkdir(mode=0o700, exist_ok=False)
            current = _directory_state(
                directory,
                error_type=MediaCaptureCreationError,
            )
            if current.st_dev != parent_state.st_dev:
                raise MediaCaptureCreationError()
            directory_identity = _identity(current)
            _apply_private_mode(
                directory,
                0o700,
                error_type=MediaCaptureCreationError,
            )
            self._run_hook("after_media_directory_creation")
            return directory, directory_identity
        except BaseException as exc:
            cleanup_incomplete = False
            try:
                if os.path.lexists(directory):
                    if (
                        not absent_before_creation
                        or directory_identity is None
                    ):
                        raise MediaCaptureCleanupError()
                    current = _directory_state(
                        directory,
                        error_type=MediaCaptureCleanupError,
                    )
                    if _identity(current) != directory_identity:
                        raise MediaCaptureCleanupError()
                    with os.scandir(directory) as entries:
                        empty = next(entries, None) is None
                    if not empty:
                        raise MediaCaptureCleanupError()
                    os.rmdir(directory)
                    if os.path.lexists(directory):
                        raise MediaCaptureCleanupError()
            except BaseException:
                cleanup_incomplete = True
            if isinstance(exc, Phase2D1EngineError):
                exc.cleanup_incomplete = bool(
                    cleanup_incomplete
                    or getattr(exc, "cleanup_incomplete", False)
                )
                raise
            if isinstance(
                exc,
                (KeyboardInterrupt, SystemExit, GeneratorExit),
            ):
                try:
                    exc.cleanup_incomplete = cleanup_incomplete
                except Exception:
                    pass
                raise
            raise MediaCaptureCreationError(
                cleanup_incomplete=cleanup_incomplete
            ) from None

    @staticmethod
    def _remove_empty_directory(directory, expected_identity):
        if directory is None or not os.path.lexists(directory):
            return True
        current = _directory_state(
            directory,
            error_type=MediaCaptureCleanupError,
        )
        if _identity(current) != expected_identity:
            raise MediaCaptureCleanupError()
        with os.scandir(directory) as entries:
            if next(entries, None) is not None:
                return False
        os.rmdir(directory)
        return not os.path.lexists(directory)

    def _capture_one(
        self,
        *,
        context,
        snapshot_result,
        storage_name,
        source_reference_count,
        root,
        root_state,
        mount_boundaries,
        preflight_state,
        policy,
        deadline,
    ):
        started = self._monotonic_value()
        self._check_deadline(deadline)
        storage_name, source_path, current_state = self._source_path(
            root,
            root_state,
            storage_name,
            mount_boundaries,
        )
        if _stable_file_state(current_state) != _stable_file_state(
            preflight_state
        ):
            raise MediaObjectChanged()
        cutoff_ns = _datetime_ns(snapshot_result.consistency_cutoff_at)
        self._validate_source_limits(
            current_state,
            cutoff_ns=cutoff_ns,
            policy=policy,
            cumulative_bytes=0,
        )
        reference = self._new_reference()
        key = (
            context.workspace_reference.identifier,
            reference.identifier,
        )
        with self._state_lock:
            if key in self._published or key in self._cleaned:
                raise MediaCaptureCreationError()

        directory = None
        directory_identity = None
        writer = None
        source_descriptor = None
        result = None
        safe_error = None
        abort_error = None
        abort_traceback = None
        cleanup_abort = None
        cleanup_abort_traceback = None
        cleanup_incomplete = False
        try:
            directory, directory_identity = self._create_capture_directory(
                context,
                reference,
                policy=policy,
            )
            writer = _AtomicMediaWriter(
                directory=directory,
                directory_identity=directory_identity,
                byte_limit=policy.maximum_file_bytes,
                failure_hook=self.failure_hook,
            )
            flags = os.O_RDONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_NONBLOCK", 0)
            flags |= getattr(os, "O_BINARY", 0)
            self._run_hook("before_media_source_open")
            source_descriptor = os.open(source_path, flags)
            opened = os.fstat(source_descriptor)
            if (
                _identity(opened) != _identity(current_state)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_size != current_state.st_size
            ):
                raise MediaObjectChanged()
            bytes_read = 0
            digest = hashlib.sha256()
            while True:
                self._check_deadline(deadline)
                self._run_hook("before_media_source_read")
                chunk = os.read(source_descriptor, policy.chunk_bytes)
                if type(chunk) is not bytes or len(chunk) > policy.chunk_bytes:
                    raise MediaObjectChanged()
                if not chunk:
                    break
                bytes_read += len(chunk)
                if (
                    bytes_read > current_state.st_size
                    or bytes_read > policy.maximum_file_bytes
                ):
                    raise MediaObjectChanged()
                digest.update(chunk)
                writer.write(chunk)
                self._run_hook("after_media_source_chunk")
            if bytes_read != current_state.st_size:
                raise MediaObjectChanged()
            descriptor_final = os.fstat(source_descriptor)
            path_final_name, path_final, path_final_state = self._source_path(
                root,
                root_state,
                storage_name,
                mount_boundaries,
            )
            if (
                path_final_name != storage_name
                or path_final != source_path
                or _stable_file_state(descriptor_final)
                != _stable_file_state(opened)
                or _stable_file_state(path_final_state)
                != _stable_file_state(current_state)
                or _identity(descriptor_final)
                != _identity(path_final_state)
                or writer.byte_count != bytes_read
                or writer.digest.hexdigest() != digest.hexdigest()
            ):
                raise MediaObjectChanged()
            os.close(source_descriptor)
            source_descriptor = None
            writer.finalize()
            evidence = _PublishedCapture(
                context=context,
                directory_identity=directory_identity,
                file_identity=writer.final_identity,
                byte_count=bytes_read,
                sha256=digest.hexdigest(),
                logical_storage_name=storage_name,
                source_reference_count=source_reference_count,
                chunk_bytes=policy.chunk_bytes,
            )
            self._validate_published(
                context=context,
                reference=reference,
                evidence=evidence,
                policy=policy,
                error_type=MediaCaptureCreationError,
                deadline=deadline,
            )
            self._check_deadline(deadline)
            self._run_hook("before_media_result_return")
            self._validate_published(
                context=context,
                reference=reference,
                evidence=evidence,
                policy=policy,
                error_type=MediaCaptureCreationError,
                deadline=deadline,
            )
            self._check_deadline(deadline)
            with self._state_lock:
                if key in self._published or key in self._cleaned:
                    raise MediaCaptureCreationError()
                self._published[key] = evidence
            captured_at = timezone.now()
            if not timezone.is_aware(captured_at):
                raise MediaCaptureCreationError()
            result = MediaCaptureResult(
                reference=reference,
                logical_storage_name=storage_name,
                byte_count=bytes_read,
                sha256=digest.hexdigest(),
                source_reference_count=source_reference_count,
                captured_at=captured_at.astimezone(UTC),
                duration_ms=self._duration_ms(started),
                provider_identifier=(
                    LOCAL_FILESYSTEM_MEDIA_CAPTURE_PROVIDER_IDENTIFIER
                ),
            )
            return result, _identity(current_state)
        except BaseException as exc:
            if isinstance(exc, Exception):
                safe_error = self._safe_error(exc)
            else:
                abort_error = exc
                abort_traceback = exc.__traceback__
        finally:
            if source_descriptor is not None:
                try:
                    os.close(source_descriptor)
                except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
                    cleanup_incomplete = True
                    if cleanup_abort is None:
                        cleanup_abort = exc
                        cleanup_abort_traceback = exc.__traceback__
                except BaseException:
                    cleanup_incomplete = True
            if result is None:
                with self._state_lock:
                    self._published.pop(key, None)
                if writer is not None:
                    try:
                        writer.cleanup_owned()
                    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
                        cleanup_incomplete = True
                        if cleanup_abort is None:
                            cleanup_abort = exc
                            cleanup_abort_traceback = exc.__traceback__
                    except BaseException:
                        cleanup_incomplete = True
                if directory is not None and directory_identity is not None:
                    try:
                        if not self._remove_empty_directory(
                            directory,
                            directory_identity,
                        ):
                            cleanup_incomplete = True
                    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
                        cleanup_incomplete = True
                        if cleanup_abort is None:
                            cleanup_abort = exc
                            cleanup_abort_traceback = exc.__traceback__
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
        if cleanup_abort is not None:
            try:
                cleanup_abort.cleanup_incomplete = True
            except Exception:
                pass
            raise cleanup_abort.with_traceback(cleanup_abort_traceback)
        if safe_error is not None:
            safe_error.cleanup_incomplete = bool(
                cleanup_incomplete
                or getattr(safe_error, "cleanup_incomplete", False)
            )
            safe_error.__cause__ = None
            safe_error.__context__ = None
            raise safe_error.with_traceback(None) from None
        raise MediaCaptureCreationError(
            cleanup_incomplete=cleanup_incomplete
        )

    def capture_media(
        self,
        *,
        context,
        snapshot_result,
        media_sources,
    ) -> tuple[MediaCaptureResult, ...]:
        context = self._validated_context(context)
        snapshot_result = self._validated_snapshot(snapshot_result)
        try:
            self.snapshot_provider.validate_snapshot_evidence(
                context=context,
                snapshot_result=snapshot_result,
            )
        except SnapshotEngineError:
            raise MediaCaptureCreationError() from None
        policy = (
            self.policy.validated()
            if self.policy is not None
            else MediaCapturePolicy.from_settings()
        )
        started = self._monotonic_value()
        deadline = started + policy.timeout_seconds
        if type(media_sources) is not tuple:
            raise MediaCaptureCreationError()
        if len(media_sources) > policy.maximum_objects:
            raise MediaCaptureLimitExceeded()
        names = []
        counts = {}
        for item in media_sources:
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not int
                or item[1] <= 0
            ):
                raise MediaCaptureCreationError()
            name = validate_media_storage_name(
                item[0],
                maximum_length=_MAXIMUM_MEDIA_NAME_LENGTH,
            )
            if name in counts:
                raise MediaCaptureCreationError()
            names.append(name)
            counts[name] = item[1]
        if tuple(names) != tuple(sorted(names)):
            raise MediaCaptureCreationError()
        portable = {}
        for name in names:
            key = media_storage_collision_key(name)
            if key in portable and portable[key] != name:
                raise MediaStorageNameCollision()
            portable[key] = name

        self._check_deadline(deadline)
        root, root_state, mount_boundaries = self._validated_media_root()
        self._check_deadline(deadline)
        _workspace, parent, _parent_state = self._media_parent(
            context,
            create=True,
            policy=policy,
            error_type=MediaCaptureCreationError,
        )
        self._check_deadline(deadline)
        cutoff_ns = _datetime_ns(snapshot_result.consistency_cutoff_at)
        preflight = []
        seen_preflight_identities = {}
        total_bytes = 0
        for name in names:
            self._check_deadline(deadline)
            normalized, path, state = self._source_path(
                root,
                root_state,
                name,
                mount_boundaries,
            )
            self._validate_source_limits(
                state,
                cutoff_ns=cutoff_ns,
                policy=policy,
                cumulative_bytes=total_bytes,
            )
            source_identity = _identity(state)
            if (
                source_identity in seen_preflight_identities
                and seen_preflight_identities[source_identity] != name
            ):
                raise MediaStorageAliasCollision()
            seen_preflight_identities[source_identity] = name
            total_bytes += state.st_size
            preflight.append((normalized, path, state))
        if names:
            try:
                free = self.disk_usage_provider(parent).free
            except Exception:
                raise InsufficientMediaCaptureCapacity() from None
            required = required_media_staging_capacity(
                byte_count=total_bytes,
                policy=policy,
            )
            if type(free) is not int or free < required:
                raise InsufficientMediaCaptureCapacity()
        self._check_deadline(deadline)

        results = []
        actual_identities = {}
        safe_error = None
        abort_error = None
        abort_traceback = None
        cleanup_abort = None
        cleanup_abort_traceback = None
        cleanup_incomplete = False
        try:
            for name, _path, state in preflight:
                self._check_deadline(deadline)
                produced, source_identity = self._capture_one(
                    context=context,
                    snapshot_result=snapshot_result,
                    storage_name=name,
                    source_reference_count=counts[name],
                    root=root,
                    root_state=root_state,
                    mount_boundaries=mount_boundaries,
                    preflight_state=state,
                    policy=policy,
                    deadline=deadline,
                )
                if (
                    source_identity in actual_identities
                    and actual_identities[source_identity] != name
                ):
                    raise MediaStorageAliasCollision()
                actual_identities[source_identity] = name
                results.append(produced)
                self._run_hook("after_one_media_capture")
            self._check_deadline(deadline)
            self._run_hook("before_media_batch_result_return")
            for captured in results:
                key = (
                    context.workspace_reference.identifier,
                    captured.reference.identifier,
                )
                with self._state_lock:
                    evidence = self._published.get(key)
                if evidence is None:
                    raise MediaCaptureCreationError()
                self._validate_published(
                    context=context,
                    reference=captured.reference,
                    evidence=evidence,
                    policy=policy,
                    error_type=MediaCaptureCreationError,
                    deadline=deadline,
                )
                self._check_deadline(deadline)
            return tuple(results)
        except BaseException as exc:
            if isinstance(exc, Exception):
                safe_error = self._safe_error(exc)
            else:
                abort_error = exc
                abort_traceback = exc.__traceback__
        finally:
            if safe_error is not None or abort_error is not None:
                for captured in reversed(results):
                    try:
                        if (
                            self.cleanup_media_capture(
                                context=context,
                                reference=captured.reference,
                            )
                            is not True
                        ):
                            cleanup_incomplete = True
                    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
                        cleanup_incomplete = True
                        if cleanup_abort is None:
                            cleanup_abort = exc
                            cleanup_abort_traceback = exc.__traceback__
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
        if cleanup_abort is not None:
            try:
                cleanup_abort.cleanup_incomplete = True
            except Exception:
                pass
            raise cleanup_abort.with_traceback(cleanup_abort_traceback)
        if safe_error is not None:
            safe_error.cleanup_incomplete = bool(
                cleanup_incomplete
                or getattr(safe_error, "cleanup_incomplete", False)
            )
            safe_error.__cause__ = None
            safe_error.__context__ = None
            raise safe_error.with_traceback(None) from None
        raise MediaCaptureCreationError(
            cleanup_incomplete=cleanup_incomplete
        )

    def _state_key(self, context, reference, *, error_type):
        if (
            type(context) is not BackupExecutionContext
            or type(context.workspace_reference) is not WorkspaceReference
            or type(reference) is not MediaCaptureReference
            or type(reference.identifier) is not uuid.UUID
        ):
            raise error_type()
        return (
            context.workspace_reference.identifier,
            reference.identifier,
        )

    def _validate_published(
        self,
        *,
        context,
        reference,
        evidence,
        policy,
        error_type,
        deadline=None,
    ):
        if deadline is not None:
            self._check_deadline(deadline)
        if (
            context != evidence.context
        ):
            raise error_type()
        directory, _parent, _state = self._capture_directory(
            context,
            reference,
            create=False,
            policy=policy,
            error_type=error_type,
        )
        directory_state = _directory_state(directory, error_type=error_type)
        if _identity(directory_state) != evidence.directory_identity:
            raise error_type()
        _assert_private_mode(directory, 0o700, error_type=error_type)
        with os.scandir(directory) as entries:
            names = {entry.name for entry in entries}
        if names != {MEDIA_CONTENT_FILE_NAME}:
            raise error_type()
        path = contained_path(directory, directory / MEDIA_CONTENT_FILE_NAME)
        current = _regular_state(
            path,
            missing_error=error_type,
            unsafe_error=error_type,
        )
        if (
            evidence.file_identity is None
            or _identity(current) != evidence.file_identity
            or current.st_dev != evidence.directory_identity[0]
            or current.st_size != evidence.byte_count
        ):
            raise error_type()
        _assert_private_mode(path, 0o600, error_type=error_type)
        descriptor = None
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
                or opened.st_size != evidence.byte_count
            ):
                raise error_type()
            digest = hashlib.sha256()
            count = 0
            while True:
                if deadline is not None:
                    self._check_deadline(deadline)
                chunk = os.read(descriptor, policy.chunk_bytes)
                if type(chunk) is not bytes or len(chunk) > policy.chunk_bytes:
                    raise error_type()
                if not chunk:
                    break
                count += len(chunk)
                if count > evidence.byte_count:
                    raise error_type()
                digest.update(chunk)
            if count != evidence.byte_count or digest.hexdigest() != evidence.sha256:
                raise error_type()
        except Phase2D1EngineError:
            raise
        except OSError:
            raise error_type() from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        final = _regular_state(
            path,
            missing_error=error_type,
            unsafe_error=error_type,
        )
        if (
            _identity(final) != evidence.file_identity
            or final.st_size != evidence.byte_count
        ):
            raise error_type()
        if deadline is not None:
            self._check_deadline(deadline)
        return directory, path

    @contextmanager
    def open_media_capture(self, *, context, reference):
        key = self._state_key(
            context,
            reference,
            error_type=MediaObjectNotFound,
        )
        with self._state_lock:
            evidence = self._published.get(key)
        if evidence is None or evidence.file_identity is None:
            raise MediaObjectNotFound()
        policy = _CaptureAccessPolicy(
            chunk_bytes=evidence.chunk_bytes,
        )
        directory, path = self._validate_published(
            context=context,
            reference=reference,
            evidence=evidence,
            policy=policy,
            error_type=MediaObjectNotFound,
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
                or opened.st_size != evidence.byte_count
            ):
                raise MediaObjectNotFound()
            raw_file = os.fdopen(descriptor, "rb", closefd=True)
            descriptor = None
            reader = _OpaqueMediaReader(raw_file)
            yield reader
        except MediaObjectNotFound:
            raise
        except OSError:
            raise MediaObjectNotFound() from None
        finally:
            active_exception = sys.exc_info()[0] is not None
            close_error = None
            if reader is not None:
                try:
                    reader.close()
                except BaseException as exc:
                    if not active_exception:
                        close_error = exc
            elif raw_file is not None:
                try:
                    raw_file.close()
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
                self._validate_published(
                    context=context,
                    reference=reference,
                    evidence=evidence,
                    policy=policy,
                    error_type=MediaObjectNotFound,
                )
                if (
                    _identity(
                        _directory_state(
                            directory,
                            error_type=MediaObjectNotFound,
                        )
                    )
                    != evidence.directory_identity
                ):
                    raise MediaObjectNotFound()
            except BaseException as exc:
                if not active_exception:
                    close_error = exc
            if close_error is not None and not active_exception:
                if isinstance(
                    close_error,
                    (KeyboardInterrupt, SystemExit, GeneratorExit),
                ):
                    raise close_error.with_traceback(
                        close_error.__traceback__
                    )
                raise MediaObjectNotFound() from None

    def cleanup_media_capture(self, *, context, reference) -> bool:
        key = self._state_key(
            context,
            reference,
            error_type=MediaCaptureCleanupError,
        )
        with self._state_lock:
            if key in self._cleaned:
                if self._cleaned[key] != context:
                    raise MediaCaptureCleanupError()
                return True
            evidence = self._published.get(key)
        if evidence is None:
            raise MediaCaptureCleanupError()
        policy = _CaptureAccessPolicy(
            chunk_bytes=evidence.chunk_bytes,
        )
        try:
            if (
                context != evidence.context
            ):
                raise MediaCaptureCleanupError()
            if evidence.file_identity is not None:
                original_evidence = evidence
                directory, path = self._validate_published(
                    context=context,
                    reference=reference,
                    evidence=evidence,
                    policy=policy,
                    error_type=MediaCaptureCleanupError,
                )
                current = _regular_state(
                    path,
                    missing_error=MediaCaptureCleanupError,
                    unsafe_error=MediaCaptureCleanupError,
                )
                if _identity(current) != evidence.file_identity:
                    raise MediaCaptureCleanupError()
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
                    raise MediaCaptureCleanupError()
                evidence = replace(evidence, file_identity=None)
                with self._state_lock:
                    if self._published.get(key) != original_evidence:
                        raise MediaCaptureCleanupError()
                    self._published[key] = evidence
                if unlink_abort is not None:
                    raise unlink_abort.with_traceback(unlink_abort_traceback)
            else:
                directory, _parent, _parent_state = self._capture_directory(
                    context,
                    reference,
                    create=False,
                    policy=policy,
                    error_type=MediaCaptureCleanupError,
                )
                if (
                    _identity(
                        _directory_state(
                            directory,
                            error_type=MediaCaptureCleanupError,
                        )
                    )
                    != evidence.directory_identity
                ):
                    raise MediaCaptureCleanupError()
            with os.scandir(directory) as entries:
                if next(entries, None) is not None:
                    raise MediaCaptureCleanupError()
            if (
                _identity(
                    _directory_state(
                        directory,
                        error_type=MediaCaptureCleanupError,
                    )
                )
                != evidence.directory_identity
            ):
                raise MediaCaptureCleanupError()
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
                raise MediaCaptureCleanupError()
            with self._state_lock:
                self._published.pop(key, None)
                self._cleaned[key] = context
            if directory_abort is not None:
                raise directory_abort.with_traceback(
                    directory_abort_traceback
                )
            return True
        except MediaCaptureCleanupError:
            raise
        except Exception:
            raise MediaCaptureCleanupError() from None

    @staticmethod
    def _safe_error(exc):
        if isinstance(exc, Phase2D1EngineError):
            return exc
        if isinstance(exc, FileNotFoundError):
            return MediaObjectNotFound()
        if isinstance(exc, (OSError, RuntimeError, TypeError, ValueError)):
            return MediaCaptureCreationError()
        return MediaCaptureCreationError()


__all__ = [
    "LOCAL_FILESYSTEM_MEDIA_CAPTURE_PROVIDER_IDENTIFIER",
    "LOCAL_MEDIA_CAPTURE_PROVIDER_IDENTIFIER",
    "MEDIA_CONTENT_FILE_NAME",
    "LocalFilesystemMediaCaptureProvider",
    "media_storage_collision_key",
]
