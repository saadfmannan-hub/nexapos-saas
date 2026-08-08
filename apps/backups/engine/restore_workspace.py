"""Private restored-package publication, extraction, and exact cleanup."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import sys
import threading
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from .canonical_manifest import MANIFEST_FILE_NAME, PACKAGE_FORMAT_IDENTIFIER
from .context import BackupExecutionContext
from .contracts import (
    PackageBuildResult,
    PackageReference,
    PackageVerificationResult,
    RestoredPlaintextEvidence,
)
from .deterministic_package import PACKAGE_ACCESS_PROVIDER_SCHEMA
from .logical_serialization import encode_canonical_document
from .package_exceptions import PackageNotFound, PackageValidationError
from .restore_exceptions import (
    RestoreExtractionError,
    RestorePreflightCleanupError,
)
from .workspace import (
    BackupWorkspaceManager,
    WorkspaceArea,
    WorkspaceReference,
    contained_path,
    path_is_link_like,
)

RESTORED_PACKAGE_PROVIDER_IDENTIFIER = "restore-preflight-package-provider-v1"
RESTORE_PACKAGE_FILE_NAME = "package.zip"
RESTORE_EXTRACTED_DIRECTORY_NAME = "extracted"
RESTORE_PREFLIGHT_EVIDENCE_FILE_NAME = "preflight.json"

_PACKAGE_CHUNK_BYTES = 1024**2
_MAXIMUM_PACKAGE_BYTES = 10 * 1024**4
_MAXIMUM_PACKAGE_ENTRIES = 200_000
_MAXIMUM_MANIFEST_BYTES = 64 * 1024**2
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMPONENT_RECORDS_PATTERN = re.compile(r"^components/([0-9]{4})/records\.ndjson$")
_COMPONENT_MEDIA_PATTERN = re.compile(r"^components/([0-9]{4})/media-index\.ndjson$")
_MEDIA_PATTERN = re.compile(r"^media/([0-9]{8})\.bin$")


def _identity(current):
    return current.st_dev, current.st_ino


def _aware(value):
    return (
        type(value) is datetime
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _parse_timestamp(value):
    try:
        if type(value) is not str or not value.endswith("Z"):
            raise ValueError
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
        canonical = parsed.astimezone(UTC).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
        if not _aware(parsed) or canonical != value:
            raise ValueError
        return parsed.astimezone(UTC)
    except (OverflowError, TypeError, ValueError):
        raise PackageValidationError() from None


def _sha256(value):
    if type(value) is not str or not _SHA256_PATTERN.fullmatch(value):
        raise PackageValidationError()
    return value


def _duplicate_rejecting_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_number(_value):
    raise ValueError


def _strict_manifest(raw):
    try:
        if (
            type(raw) is not bytes
            or not raw.endswith(b"\n")
            or not 1 <= len(raw) <= _MAXIMUM_MANIFEST_BYTES
        ):
            raise ValueError
        document = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
        if (
            type(document) is not dict
            or encode_canonical_document(document, trailing_lf=True) != raw
        ):
            raise ValueError
        return document
    except (
        UnicodeError,
        json.JSONDecodeError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        raise PackageValidationError() from None


def _directory_state(path, *, error_type):
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        raise error_type() from None
    if path_is_link_like(path) or not stat.S_ISDIR(current.st_mode):
        raise error_type()
    return current


def _file_state(path, *, error_type):
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        raise error_type() from None
    if (
        path_is_link_like(path)
        or not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
    ):
        raise error_type()
    return current


def _apply_private_mode(path, mode, *, error_type):
    try:
        os.chmod(path, mode)
        current = os.stat(path, follow_symlinks=False)
        if os.name != "nt" and stat.S_IMODE(current.st_mode) != mode:
            raise error_type()
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
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
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


def _safe_archive_name(value):
    if (
        type(value) is not str
        or not value
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or any(segment in {"", ".", ".."} for segment in value.split("/"))
    ):
        raise PackageValidationError()
    if value == MANIFEST_FILE_NAME:
        return value
    if not any(
        pattern.fullmatch(value)
        for pattern in (
            _COMPONENT_RECORDS_PATTERN,
            _COMPONENT_MEDIA_PATTERN,
            _MEDIA_PATTERN,
        )
    ):
        raise PackageValidationError()
    return value


def _validate_zipinfo(info):
    mode = (info.external_attr >> 16) & 0xFFFF
    if (
        type(info.filename) is not str
        or info.is_dir()
        or stat.S_IFMT(mode) == stat.S_IFLNK
        or info.flag_bits & 0x1
        or info.file_size < 0
        or info.compress_size < 0
    ):
        raise PackageValidationError()
    _safe_archive_name(info.filename)


class _OpaqueRestorePackageReader:
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


@dataclass(frozen=True, slots=True)
class _RestoreWorkspaceEvidence:
    context: BackupExecutionContext
    result: PackageBuildResult
    workspace_identity: tuple[int, int]
    area_identity: tuple[int, int]
    package_identity: tuple[int, int] | None
    directory_identities: tuple[tuple[str, tuple[int, int]], ...] = ()
    file_identities: tuple[tuple[str, tuple[int, int]], ...] = ()
    preflight_evidence_identity: tuple[int, int] | None = None


class RestoredPackageProvider:
    """Own one short-lived plaintext package behind an opaque provider API."""

    package_access_provider_schema = PACKAGE_ACCESS_PROVIDER_SCHEMA
    package_result_provider_identifier = RESTORED_PACKAGE_PROVIDER_IDENTIFIER

    def __init__(self, *, workspace_manager=None):
        manager = workspace_manager or BackupWorkspaceManager()
        if type(manager) is not BackupWorkspaceManager:
            raise PackageValidationError()
        self.workspace_manager = manager
        self._published = {}
        self._cleaned = {}
        self._state_lock = threading.RLock()

    @staticmethod
    def _state_key(context, reference, *, error_type):
        if (
            type(context) is not BackupExecutionContext
            or type(context.workspace_reference) is not WorkspaceReference
            or type(reference) is not PackageReference
            or type(reference.identifier) is not uuid.UUID
        ):
            raise error_type()
        return context.workspace_reference.identifier, reference.identifier

    def _paths(self, context, *, error_type):
        try:
            workspace = self.workspace_manager.handle(context.workspace_reference)
            workspace_path = workspace.path
            area = workspace.system_area_path(WorkspaceArea.RESTORE_PREFLIGHT)
            package_path = contained_path(
                workspace_path,
                area / RESTORE_PACKAGE_FILE_NAME,
            )
            return workspace, workspace_path, area, package_path
        except Exception:
            raise error_type() from None

    @staticmethod
    def _hash_descriptor(descriptor, *, maximum, error_type):
        digest = hashlib.sha256()
        byte_count = 0
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            while True:
                chunk = os.read(descriptor, _PACKAGE_CHUNK_BYTES)
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > maximum:
                    raise error_type()
                digest.update(chunk)
            os.lseek(descriptor, 0, os.SEEK_SET)
            return byte_count, digest.hexdigest()
        except error_type:
            raise
        except OSError:
            raise error_type() from None

    def _inspect_package(self, path, *, expected_identity):
        descriptor = None
        raw_file = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if (
                _identity(opened) != expected_identity
                or opened.st_nlink != 1
                or not stat.S_ISREG(opened.st_mode)
            ):
                raise PackageValidationError()
            raw_file = os.fdopen(descriptor, "rb", closefd=True)
            descriptor = None
            with zipfile.ZipFile(raw_file, mode="r", allowZip64=True) as archive:
                if archive.comment != b"":
                    raise PackageValidationError()
                infos = archive.infolist()
                if not 1 <= len(infos) <= _MAXIMUM_PACKAGE_ENTRIES:
                    raise PackageValidationError()
                names = []
                for info in infos:
                    _validate_zipinfo(info)
                    names.append(info.filename)
                if len(names) != len(set(names)) or names[0] != MANIFEST_FILE_NAME:
                    raise PackageValidationError()
                manifest_info = infos[0]
                if not 1 <= manifest_info.file_size <= _MAXIMUM_MANIFEST_BYTES:
                    raise PackageValidationError()
                with archive.open(manifest_info, mode="r") as source:
                    manifest_raw = source.read(_MAXIMUM_MANIFEST_BYTES + 1)
                    if len(manifest_raw) != manifest_info.file_size:
                        raise PackageValidationError()
                document = _strict_manifest(manifest_raw)
                backup = document.get("backup")
                if type(backup) is not dict:
                    raise PackageValidationError()
                if document.get("package_format") != PACKAGE_FORMAT_IDENTIFIER:
                    raise PackageValidationError()
                return (
                    len(infos),
                    _sha256(document.get("payload_set_sha256")),
                    _parse_timestamp(backup.get("created_timestamp")),
                    document,
                    manifest_raw,
                )
        except PackageValidationError:
            raise
        except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile):
            raise PackageValidationError() from None
        finally:
            if raw_file is not None:
                try:
                    raw_file.close()
                except OSError:
                    pass
            elif descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _validate_published(self, evidence, *, error_type):
        _workspace, workspace_path, area, package_path = self._paths(
            evidence.context,
            error_type=error_type,
        )
        workspace_state = _directory_state(workspace_path, error_type=error_type)
        area_state = _directory_state(area, error_type=error_type)
        package_state = _file_state(package_path, error_type=error_type)
        if (
            evidence.package_identity is None
            or
            _identity(workspace_state) != evidence.workspace_identity
            or _identity(area_state) != evidence.area_identity
            or _identity(package_state) != evidence.package_identity
            or len({workspace_state.st_dev, area_state.st_dev, package_state.st_dev}) != 1
            or package_state.st_size != evidence.result.byte_count
        ):
            raise error_type()
        _assert_private_mode(workspace_path, 0o700, error_type=error_type)
        _assert_private_mode(area, 0o700, error_type=error_type)
        _assert_private_mode(package_path, 0o600, error_type=error_type)
        descriptor = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            descriptor = os.open(package_path, flags)
            opened = os.fstat(descriptor)
            if (
                _identity(opened) != evidence.package_identity
                or opened.st_nlink != 1
                or opened.st_size != evidence.result.byte_count
            ):
                raise error_type()
            count, digest = self._hash_descriptor(
                descriptor,
                maximum=_MAXIMUM_PACKAGE_BYTES,
                error_type=error_type,
            )
            if (
                count != evidence.result.byte_count
                or digest != evidence.result.plaintext_sha256
            ):
                raise error_type()
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        return package_path

    def publish_plaintext(self, *, context, reader, plaintext_evidence):
        if (
            type(context) is not BackupExecutionContext
            or type(context.workspace_reference) is not WorkspaceReference
            or type(plaintext_evidence) is not RestoredPlaintextEvidence
            or not callable(getattr(reader, "read", None))
            or plaintext_evidence.backup_public_id != context.backup_public_id
            or plaintext_evidence.tenant_public_id != context.business_public_id
            or plaintext_evidence.verified_package_format != PACKAGE_FORMAT_IDENTIFIER
        ):
            raise PackageValidationError()
        reference = PackageReference(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"nexa-restore-package:{context.workspace_reference.identifier}",
            )
        )
        key = self._state_key(context, reference, error_type=PackageValidationError)
        with self._state_lock:
            if key in self._published or key in self._cleaned:
                raise PackageValidationError()
        workspace, workspace_path, area, package_path = self._paths(
            context,
            error_type=PackageValidationError,
        )
        del workspace
        area_identity = None
        part_path = None
        file_identity = None
        descriptor = None
        published = False
        try:
            workspace_state = _directory_state(
                workspace_path,
                error_type=PackageValidationError,
            )
            _apply_private_mode(
                workspace_path,
                0o700,
                error_type=PackageValidationError,
            )
            if os.path.lexists(area):
                raise PackageValidationError()
            area.mkdir(mode=0o700, exist_ok=False)
            area_state = _directory_state(area, error_type=PackageValidationError)
            if area_state.st_dev != workspace_state.st_dev:
                raise PackageValidationError()
            area_identity = _identity(area_state)
            _apply_private_mode(area, 0o700, error_type=PackageValidationError)
            part_path = contained_path(
                workspace_path,
                area / f".{RESTORE_PACKAGE_FILE_NAME}.{uuid.uuid4().hex}.part",
            )
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(part_path, flags, 0o600)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_dev != area_state.st_dev
            ):
                raise PackageValidationError()
            file_identity = _identity(opened)
            _apply_private_mode(part_path, 0o600, error_type=PackageValidationError)
            digest = hashlib.sha256()
            byte_count = 0
            while True:
                chunk = reader.read(_PACKAGE_CHUNK_BYTES)
                if type(chunk) is not bytes or len(chunk) > _PACKAGE_CHUNK_BYTES:
                    raise PackageValidationError()
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > _MAXIMUM_PACKAGE_BYTES:
                    raise PackageValidationError()
                digest.update(chunk)
                offset = 0
                while offset < len(chunk):
                    written = os.write(descriptor, chunk[offset:])
                    if type(written) is not int or written <= 0:
                        raise PackageValidationError()
                    offset += written
            if (
                byte_count != plaintext_evidence.plaintext_byte_count
                or digest.hexdigest() != plaintext_evidence.plaintext_sha256
            ):
                raise PackageValidationError()
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            current = _file_state(part_path, error_type=PackageValidationError)
            if _identity(current) != file_identity or current.st_size != byte_count:
                raise PackageValidationError()
            os.link(part_path, package_path, follow_symlinks=False)
            for linked_path in (part_path, package_path):
                linked = os.stat(linked_path, follow_symlinks=False)
                if _identity(linked) != file_identity or linked.st_nlink != 2:
                    raise PackageValidationError()
            os.unlink(part_path)
            part_path = None
            _fsync_directory(area, error_type=PackageValidationError)
            final_state = _file_state(package_path, error_type=PackageValidationError)
            if _identity(final_state) != file_identity:
                raise PackageValidationError()
            entry_count, payload_set_sha256, created_at, _document, _raw = (
                self._inspect_package(package_path, expected_identity=file_identity)
            )
            result = PackageBuildResult(
                reference=reference,
                byte_count=byte_count,
                plaintext_sha256=plaintext_evidence.plaintext_sha256,
                entry_count=entry_count,
                payload_set_sha256=payload_set_sha256,
                format_identifier=PACKAGE_FORMAT_IDENTIFIER,
                created_at=created_at,
                provider_identifier=RESTORED_PACKAGE_PROVIDER_IDENTIFIER,
            )
            evidence = _RestoreWorkspaceEvidence(
                context=context,
                result=result,
                workspace_identity=_identity(workspace_state),
                area_identity=area_identity,
                package_identity=file_identity,
            )
            self._validate_published(evidence, error_type=PackageValidationError)
            with self._state_lock:
                if key in self._published or key in self._cleaned:
                    raise PackageValidationError()
                self._published[key] = evidence
            published = True
            return result
        except PackageValidationError:
            raise
        except (OSError, TypeError, ValueError):
            raise PackageValidationError() from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if not published:
                for candidate in (part_path, package_path):
                    if candidate is None or not os.path.lexists(candidate):
                        continue
                    try:
                        current = _file_state(candidate, error_type=PackageValidationError)
                        if file_identity is None or _identity(current) != file_identity:
                            continue
                        os.unlink(candidate)
                    except Exception:
                        pass
                if area_identity is not None and os.path.lexists(area):
                    try:
                        current = _directory_state(area, error_type=PackageValidationError)
                        with os.scandir(area) as contents:
                            empty = next(contents, None) is None
                        if _identity(current) == area_identity and empty:
                            os.rmdir(area)
                    except Exception:
                        pass

    def validate_package_evidence(self, *, context, result):
        if type(result) is not PackageBuildResult:
            raise PackageValidationError()
        key = self._state_key(context, result.reference, error_type=PackageValidationError)
        with self._state_lock:
            evidence = self._published.get(key)
        if evidence is None or evidence.context != context or evidence.result != result:
            raise PackageValidationError()
        self._validate_published(evidence, error_type=PackageValidationError)
        return True

    @contextmanager
    def open_package(self, *, context, reference):
        key = self._state_key(context, reference, error_type=PackageNotFound)
        with self._state_lock:
            evidence = self._published.get(key)
        if evidence is None or evidence.context != context:
            raise PackageNotFound()
        path = self._validate_published(evidence, error_type=PackageNotFound)
        descriptor = None
        raw_file = None
        opaque = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if (
                _identity(opened) != evidence.package_identity
                or opened.st_nlink != 1
                or opened.st_size != evidence.result.byte_count
            ):
                raise PackageNotFound()
            raw_file = os.fdopen(descriptor, "rb", closefd=True)
            descriptor = None
            opaque = _OpaqueRestorePackageReader(raw_file)
            yield opaque
        except PackageNotFound:
            raise
        except OSError:
            raise PackageNotFound() from None
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
                self._validate_published(evidence, error_type=PackageNotFound)
            except BaseException as exc:
                if not active_exception:
                    close_error = exc
            if close_error is not None and not active_exception:
                if isinstance(close_error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    raise close_error.with_traceback(close_error.__traceback__)
                raise PackageNotFound() from None

    @staticmethod
    def _expected_entries(document, *, manifest_sha256, manifest_size):
        expected = [(MANIFEST_FILE_NAME, manifest_size, _sha256(manifest_sha256))]
        components = document.get("components")
        media = document.get("media")
        if type(components) is not list or type(media) is not list:
            raise RestoreExtractionError()
        for ordinal, component in enumerate(components, start=1):
            if type(component) is not dict:
                raise RestoreExtractionError()
            records = component.get("records")
            media_index = component.get("media_index")
            if type(records) is not dict or type(media_index) is not dict:
                raise RestoreExtractionError()
            expected.extend(
                (
                    (
                        f"components/{ordinal:04d}/records.ndjson",
                        records.get("byte_count"),
                        _sha256(records.get("sha256")),
                    ),
                    (
                        f"components/{ordinal:04d}/media-index.ndjson",
                        media_index.get("byte_count"),
                        _sha256(media_index.get("sha256")),
                    ),
                )
            )
        for ordinal, item in enumerate(media, start=1):
            if type(item) is not dict:
                raise RestoreExtractionError()
            expected.append(
                (
                    f"media/{ordinal:08d}.bin",
                    item.get("byte_count"),
                    _sha256(item.get("sha256")),
                )
            )
        if any(type(size) is not int or size < 0 for _name, size, _digest in expected):
            raise RestoreExtractionError()
        return tuple(expected)

    def _create_private_directory(self, path, *, parent_state):
        try:
            path.mkdir(mode=0o700, exist_ok=False)
            current = _directory_state(path, error_type=RestoreExtractionError)
            if current.st_dev != parent_state.st_dev:
                raise RestoreExtractionError()
            _apply_private_mode(path, 0o700, error_type=RestoreExtractionError)
            _fsync_directory(path.parent, error_type=RestoreExtractionError)
            return current
        except RestoreExtractionError:
            raise
        except OSError:
            raise RestoreExtractionError() from None

    def _write_extracted_entry(self, *, archive, info, target, expected_size, expected_hash):
        descriptor = None
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(target, flags, 0o600)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise RestoreExtractionError()
            identity = _identity(opened)
            _apply_private_mode(target, 0o600, error_type=RestoreExtractionError)
            digest = hashlib.sha256()
            byte_count = 0
            with archive.open(info, mode="r") as source:
                while True:
                    chunk = source.read(_PACKAGE_CHUNK_BYTES)
                    if type(chunk) is not bytes or len(chunk) > _PACKAGE_CHUNK_BYTES:
                        raise RestoreExtractionError()
                    if not chunk:
                        break
                    byte_count += len(chunk)
                    if byte_count > expected_size:
                        raise RestoreExtractionError()
                    digest.update(chunk)
                    offset = 0
                    while offset < len(chunk):
                        written = os.write(descriptor, chunk[offset:])
                        if type(written) is not int or written <= 0:
                            raise RestoreExtractionError()
                        offset += written
            if byte_count != expected_size or digest.hexdigest() != expected_hash:
                raise RestoreExtractionError()
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            final = _file_state(target, error_type=RestoreExtractionError)
            if _identity(final) != identity or final.st_size != expected_size:
                raise RestoreExtractionError()
            _fsync_directory(target.parent, error_type=RestoreExtractionError)
            return identity
        except RestoreExtractionError:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile):
            raise RestoreExtractionError() from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    @staticmethod
    def _remove_created_files(created_files):
        for path, expected_identity in reversed(created_files):
            try:
                current = _file_state(path, error_type=RestoreExtractionError)
                if _identity(current) == expected_identity:
                    os.unlink(path)
            except Exception:
                pass

    @staticmethod
    def _remove_created_directories(created_directories):
        for path, expected_identity in reversed(created_directories):
            try:
                current = _directory_state(path, error_type=RestoreExtractionError)
                with os.scandir(path) as contents:
                    empty = next(contents, None) is None
                if _identity(current) == expected_identity and empty:
                    os.rmdir(path)
            except Exception:
                pass

    def extract_verified_package(self, *, context, package, verification):
        if (
            type(package) is not PackageBuildResult
            or type(verification) is not PackageVerificationResult
            or verification.verified is not True
            or verification.package_byte_count != package.byte_count
            or verification.plaintext_sha256 != package.plaintext_sha256
            or verification.entry_count != package.entry_count
            or verification.payload_set_sha256 != package.payload_set_sha256
        ):
            raise RestoreExtractionError()
        self.validate_package_evidence(context=context, result=package)
        key = self._state_key(context, package.reference, error_type=RestoreExtractionError)
        with self._state_lock:
            evidence = self._published.get(key)
        if evidence is None or evidence.file_identities or evidence.directory_identities:
            raise RestoreExtractionError()
        _workspace, workspace_path, area, package_path = self._paths(
            context,
            error_type=RestoreExtractionError,
        )
        del workspace_path
        created_directories = []
        created_files = []
        try:
            area_state = _directory_state(area, error_type=RestoreExtractionError)
            extracted = contained_path(
                self.workspace_manager.handle(context.workspace_reference).path,
                area / RESTORE_EXTRACTED_DIRECTORY_NAME,
            )
            extracted_state = self._create_private_directory(
                extracted,
                parent_state=area_state,
            )
            created_directories.append((extracted, _identity(extracted_state)))
            descriptor = None
            raw_file = None
            try:
                flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
                descriptor = os.open(package_path, flags)
                opened = os.fstat(descriptor)
                if _identity(opened) != evidence.package_identity or opened.st_nlink != 1:
                    raise RestoreExtractionError()
                raw_file = os.fdopen(descriptor, "rb", closefd=True)
                descriptor = None
                with zipfile.ZipFile(raw_file, mode="r", allowZip64=True) as archive:
                    infos = archive.infolist()
                    if len(infos) != package.entry_count:
                        raise RestoreExtractionError()
                    manifest_info = infos[0]
                    with archive.open(manifest_info, mode="r") as source:
                        manifest_raw = source.read(_MAXIMUM_MANIFEST_BYTES + 1)
                    if (
                        len(manifest_raw) != manifest_info.file_size
                        or hashlib.sha256(manifest_raw).hexdigest()
                        != verification.manifest_sha256
                    ):
                        raise RestoreExtractionError()
                    document = _strict_manifest(manifest_raw)
                    expected = self._expected_entries(
                        document,
                        manifest_sha256=verification.manifest_sha256,
                        manifest_size=len(manifest_raw),
                    )
                    if len(expected) != len(infos):
                        raise RestoreExtractionError()
                    if tuple(info.filename for info in infos) != tuple(
                        item[0] for item in expected
                    ):
                        raise RestoreExtractionError()

                    components = document["components"]
                    if components:
                        components_path = extracted / "components"
                        components_state = self._create_private_directory(
                            components_path,
                            parent_state=extracted_state,
                        )
                        created_directories.append(
                            (components_path, _identity(components_state))
                        )
                        for ordinal in range(1, len(components) + 1):
                            component_path = components_path / f"{ordinal:04d}"
                            component_state = self._create_private_directory(
                                component_path,
                                parent_state=components_state,
                            )
                            created_directories.append(
                                (component_path, _identity(component_state))
                            )
                    if document["media"]:
                        media_path = extracted / "media"
                        media_state = self._create_private_directory(
                            media_path,
                            parent_state=extracted_state,
                        )
                        created_directories.append((media_path, _identity(media_state)))

                    for info, (name, expected_size, expected_hash) in zip(
                        infos,
                        expected,
                        strict=True,
                    ):
                        _validate_zipinfo(info)
                        if info.filename != name or info.file_size != expected_size:
                            raise RestoreExtractionError()
                        target = contained_path(extracted, extracted / Path(*name.split("/")))
                        identity = self._write_extracted_entry(
                            archive=archive,
                            info=info,
                            target=target,
                            expected_size=expected_size,
                            expected_hash=expected_hash,
                        )
                        created_files.append((target, identity))
            finally:
                if raw_file is not None:
                    try:
                        raw_file.close()
                    except OSError:
                        pass
                elif descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

            directory_identities = tuple(
                (path.relative_to(area).as_posix(), identity)
                for path, identity in created_directories
            )
            file_identities = tuple(
                (path.relative_to(area).as_posix(), identity)
                for path, identity in created_files
            )
            updated = replace(
                evidence,
                directory_identities=directory_identities,
                file_identities=file_identities,
            )
            with self._state_lock:
                if self._published.get(key) != evidence:
                    raise RestoreExtractionError()
                self._published[key] = updated
            return document
        except RestoreExtractionError:
            self._remove_created_files(created_files)
            self._remove_created_directories(created_directories)
            raise
        except (OSError, TypeError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile):
            self._remove_created_files(created_files)
            self._remove_created_directories(created_directories)
            raise RestoreExtractionError() from None

    def publish_preflight_evidence(self, *, context, package, document):
        if type(document) is not dict:
            raise RestoreExtractionError()
        key = self._state_key(context, package.reference, error_type=RestoreExtractionError)
        with self._state_lock:
            evidence = self._published.get(key)
        if (
            evidence is None
            or evidence.context != context
            or evidence.result != package
            or not evidence.file_identities
            or evidence.preflight_evidence_identity is not None
        ):
            raise RestoreExtractionError()
        _workspace, _workspace_path, area, _package_path = self._paths(
            context,
            error_type=RestoreExtractionError,
        )
        path = contained_path(area, area / RESTORE_PREFLIGHT_EVIDENCE_FILE_NAME)
        raw = encode_canonical_document(document, trailing_lf=True)
        descriptor = None
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags, 0o600)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise RestoreExtractionError()
            identity = _identity(opened)
            _apply_private_mode(path, 0o600, error_type=RestoreExtractionError)
            offset = 0
            while offset < len(raw):
                written = os.write(descriptor, raw[offset:])
                if type(written) is not int or written <= 0:
                    raise RestoreExtractionError()
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            current = _file_state(path, error_type=RestoreExtractionError)
            if _identity(current) != identity or current.st_size != len(raw):
                raise RestoreExtractionError()
            _fsync_directory(area, error_type=RestoreExtractionError)
            updated = replace(evidence, preflight_evidence_identity=identity)
            with self._state_lock:
                if self._published.get(key) != evidence:
                    raise RestoreExtractionError()
                self._published[key] = updated
            return True
        except RestoreExtractionError:
            raise
        except (OSError, TypeError, ValueError):
            raise RestoreExtractionError() from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    @staticmethod
    def _enumerate_exact_tree(area):
        files = set()
        directories = set()
        pending = [area]
        while pending:
            directory = pending.pop()
            if path_is_link_like(directory):
                raise RestorePreflightCleanupError()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        path = Path(entry.path)
                        if path_is_link_like(path):
                            raise RestorePreflightCleanupError()
                        state = entry.stat(follow_symlinks=False)
                        relative = path.relative_to(area).as_posix()
                        if stat.S_ISDIR(state.st_mode):
                            directories.add(relative)
                            pending.append(path)
                        elif stat.S_ISREG(state.st_mode):
                            files.add(relative)
                        else:
                            raise RestorePreflightCleanupError()
            except RestorePreflightCleanupError:
                raise
            except OSError:
                raise RestorePreflightCleanupError() from None
        return files, directories

    def cleanup_workspace(self, *, context, package):
        if type(package) is not PackageBuildResult:
            raise RestorePreflightCleanupError()
        key = self._state_key(
            context,
            package.reference,
            error_type=RestorePreflightCleanupError,
        )
        with self._state_lock:
            evidence = self._published.get(key)
            cleaned = self._cleaned.get(key)
        if cleaned is not None:
            if cleaned.context != context or cleaned.result != package:
                raise RestorePreflightCleanupError()
            return False
        if evidence is None or evidence.context != context or evidence.result != package:
            raise RestorePreflightCleanupError()
        _workspace, _workspace_path, area, package_path = self._paths(
            context,
            error_type=RestorePreflightCleanupError,
        )
        if (
            not os.path.lexists(area)
            and evidence.package_identity is None
            and not evidence.file_identities
            and not evidence.directory_identities
            and evidence.preflight_evidence_identity is None
        ):
            with self._state_lock:
                if self._published.get(key) != evidence:
                    raise RestorePreflightCleanupError()
                del self._published[key]
                self._cleaned[key] = evidence
            return False
        if evidence.package_identity is not None:
            self._validate_published(evidence, error_type=RestorePreflightCleanupError)
        expected_files = set()
        if evidence.package_identity is not None:
            expected_files.add(RESTORE_PACKAGE_FILE_NAME)
        expected_files.update(path for path, _identity_value in evidence.file_identities)
        if evidence.preflight_evidence_identity is not None:
            expected_files.add(RESTORE_PREFLIGHT_EVIDENCE_FILE_NAME)
        expected_directories = {
            path for path, _identity_value in evidence.directory_identities
        }
        actual_files, actual_directories = self._enumerate_exact_tree(area)
        if actual_files != expected_files or actual_directories != expected_directories:
            raise RestorePreflightCleanupError()

        file_identities = dict(evidence.file_identities)
        if evidence.preflight_evidence_identity is not None:
            file_identities[RESTORE_PREFLIGHT_EVIDENCE_FILE_NAME] = (
                evidence.preflight_evidence_identity
            )
        if evidence.package_identity is not None:
            file_identities[RESTORE_PACKAGE_FILE_NAME] = evidence.package_identity
        for relative, expected_identity in sorted(
            file_identities.items(),
            key=lambda item: item[0].count("/"),
            reverse=True,
        ):
            path = contained_path(area, area / Path(*relative.split("/")))
            current = _file_state(path, error_type=RestorePreflightCleanupError)
            if _identity(current) != expected_identity or current.st_nlink != 1:
                raise RestorePreflightCleanupError()

        def commit(current_evidence, updated_evidence):
            with self._state_lock:
                if self._published.get(key) != current_evidence:
                    raise RestorePreflightCleanupError()
                self._published[key] = updated_evidence
            return updated_evidence

        non_package_files = {
            relative: identity
            for relative, identity in file_identities.items()
            if relative != RESTORE_PACKAGE_FILE_NAME
        }
        for relative, _expected_identity in sorted(
            non_package_files.items(),
            key=lambda item: item[0].count("/"),
            reverse=True,
        ):
            path = contained_path(area, area / Path(*relative.split("/")))
            unlink_error = None
            unlink_traceback = None
            try:
                os.unlink(path)
            except BaseException as exc:
                if os.path.lexists(path):
                    raise
                unlink_error = exc
                unlink_traceback = exc.__traceback__
            if relative == RESTORE_PREFLIGHT_EVIDENCE_FILE_NAME:
                updated = replace(evidence, preflight_evidence_identity=None)
            else:
                updated = replace(
                    evidence,
                    file_identities=tuple(
                        item for item in evidence.file_identities if item[0] != relative
                    ),
                )
            evidence = commit(evidence, updated)
            if unlink_error is not None:
                if isinstance(unlink_error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    raise unlink_error.with_traceback(unlink_traceback)
                raise RestorePreflightCleanupError() from None

        while evidence.directory_identities:
            relative, expected_identity = max(
                evidence.directory_identities,
                key=lambda item: item[0].count("/"),
            )
            path = contained_path(area, area / Path(*relative.split("/")))
            current = _directory_state(path, error_type=RestorePreflightCleanupError)
            if _identity(current) != expected_identity:
                raise RestorePreflightCleanupError()
            with os.scandir(path) as contents:
                if next(contents, None) is not None:
                    raise RestorePreflightCleanupError()
            removal_error = None
            removal_traceback = None
            try:
                os.rmdir(path)
            except BaseException as exc:
                if os.path.lexists(path):
                    raise
                removal_error = exc
                removal_traceback = exc.__traceback__
            updated = replace(
                evidence,
                directory_identities=tuple(
                    item for item in evidence.directory_identities if item[0] != relative
                ),
            )
            evidence = commit(evidence, updated)
            if removal_error is not None:
                if isinstance(removal_error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    raise removal_error.with_traceback(removal_traceback)
                raise RestorePreflightCleanupError() from None

        if evidence.package_identity is not None:
            current = _file_state(package_path, error_type=RestorePreflightCleanupError)
            if _identity(current) != evidence.package_identity or current.st_nlink != 1:
                raise RestorePreflightCleanupError()
            unlink_error = None
            unlink_traceback = None
            try:
                os.unlink(package_path)
            except BaseException as exc:
                if os.path.lexists(package_path):
                    raise
                unlink_error = exc
                unlink_traceback = exc.__traceback__
            updated = replace(evidence, package_identity=None)
            evidence = commit(evidence, updated)
            if unlink_error is not None:
                if isinstance(unlink_error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    raise unlink_error.with_traceback(unlink_traceback)
                raise RestorePreflightCleanupError() from None

        area_state = _directory_state(area, error_type=RestorePreflightCleanupError)
        if _identity(area_state) != evidence.area_identity:
            raise RestorePreflightCleanupError()
        with os.scandir(area) as contents:
            if next(contents, None) is not None:
                raise RestorePreflightCleanupError()
        removal_error = None
        removal_traceback = None
        try:
            os.rmdir(area)
        except BaseException as exc:
            if os.path.lexists(area):
                raise
            removal_error = exc
            removal_traceback = exc.__traceback__
        with self._state_lock:
            if self._published.get(key) != evidence:
                raise RestorePreflightCleanupError()
            del self._published[key]
            self._cleaned[key] = evidence
        if removal_error is not None:
            if isinstance(removal_error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise removal_error.with_traceback(removal_traceback)
            raise RestorePreflightCleanupError() from None
        return True


__all__ = [
    "RESTORED_PACKAGE_PROVIDER_IDENTIFIER",
    "RESTORE_EXTRACTED_DIRECTORY_NAME",
    "RESTORE_PACKAGE_FILE_NAME",
    "RESTORE_PREFLIGHT_EVIDENCE_FILE_NAME",
    "RestoredPackageProvider",
]
