"""Two-phase, no-clobber tenant media restoration for Phase 3B."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings

from .logical_restore import PreparedLogicalRestore
from .restore_exceptions import RestoreMediaPublicationError
from .restore_preflight import RestorePreflightConsumption
from .restore_workspace import RestoredPackageProvider
from .workspace import (
    BackupWorkspaceManager,
    WorkspaceArea,
    WorkspaceReference,
    contained_path,
    path_is_link_like,
)

_CHUNK_BYTES = 1024 * 1024


def _identity(state):
    return state.st_dev, state.st_ino


def _file_state(path):
    try:
        state = os.stat(path, follow_symlinks=False)
    except OSError:
        raise RestoreMediaPublicationError(issue_code="restore_media_unavailable") from None
    if not stat.S_ISREG(state.st_mode) or state.st_nlink != 1:
        raise RestoreMediaPublicationError(issue_code="restore_media_unsafe")
    return state


def _directory_state(path):
    try:
        state = os.stat(path, follow_symlinks=False)
    except OSError:
        raise RestoreMediaPublicationError(issue_code="restore_media_root_invalid") from None
    if not stat.S_ISDIR(state.st_mode) or path_is_link_like(path):
        raise RestoreMediaPublicationError(issue_code="restore_media_root_invalid")
    return state


def _hash_file(path, *, expected_identity=None, maximum=None):
    descriptor = None
    digest = hashlib.sha256()
    count = 0
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or expected_identity is not None
            and _identity(opened) != expected_identity
        ):
            raise RestoreMediaPublicationError(issue_code="restore_media_unsafe")
        while True:
            chunk = os.read(descriptor, _CHUNK_BYTES)
            if not chunk:
                break
            count += len(chunk)
            if maximum is not None and count > maximum:
                raise RestoreMediaPublicationError(issue_code="restore_media_size_mismatch")
            digest.update(chunk)
    except RestoreMediaPublicationError:
        raise
    except OSError:
        raise RestoreMediaPublicationError(issue_code="restore_media_unavailable") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return count, digest.hexdigest()


@dataclass(frozen=True, slots=True)
class StagedMediaRestore:
    reference: uuid.UUID
    operation_public_id: uuid.UUID
    object_count: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class MediaPublicationResult:
    reference: uuid.UUID
    object_count: int
    created_count: int
    reused_count: int


@dataclass(slots=True)
class _StagedObject:
    storage_name: str
    byte_count: int
    sha256: str
    staging_path: Path
    staging_identity: tuple[int, int]
    target_path: Path
    reuse_existing: bool


@dataclass(slots=True)
class _MediaEvidence:
    result: StagedMediaRestore
    workspace_reference: WorkspaceReference
    workspace_identity: tuple[int, int]
    area_path: Path
    area_identity: tuple[int, int]
    objects: tuple[_StagedObject, ...]
    created_targets: list[tuple[Path, tuple[int, int]]] = field(default_factory=list)
    created_directories: list[tuple[Path, tuple[int, int]]] = field(default_factory=list)
    published: bool = False


class LocalFilesystemMediaRestoreProvider:
    """Stage verified media privately, then publish without overwriting live files."""

    provider_identifier = "local-media-restore-provider-v1"

    def __init__(self, *, workspace_manager=None):
        self.workspace_manager = workspace_manager or BackupWorkspaceManager()
        if type(self.workspace_manager) is not BackupWorkspaceManager:
            raise RestoreMediaPublicationError(issue_code="restore_media_provider_invalid")
        self._evidence = {}
        self._cleaned = set()

    @staticmethod
    def _media_root():
        value = getattr(settings, "MEDIA_ROOT", None)
        if not value:
            raise RestoreMediaPublicationError(issue_code="restore_media_root_invalid")
        root = Path(value)
        if not root.is_absolute() or not root.exists():
            raise RestoreMediaPublicationError(issue_code="restore_media_root_invalid")
        _directory_state(root)
        return root.resolve(strict=True)

    @staticmethod
    def _manifest_media(consumption):
        media = consumption.document.get("media")
        if type(media) is not list:
            raise RestoreMediaPublicationError(issue_code="restore_media_manifest_invalid")
        by_name = {}
        for item in media:
            if (
                type(item) is not dict
                or type(item.get("storage_name")) is not str
                or item["storage_name"] in by_name
                or type(item.get("byte_count")) is not int
                or item["byte_count"] < 0
                or type(item.get("sha256")) is not str
                or len(item["sha256"]) != 64
                or type(item.get("package_path")) is not str
            ):
                raise RestoreMediaPublicationError(issue_code="restore_media_manifest_invalid")
            by_name[item["storage_name"]] = item
        return by_name

    @staticmethod
    def _write_staged(*, reader, path, expected_bytes, expected_sha256):
        descriptor = None
        digest = hashlib.sha256()
        count = 0
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags, 0o600)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise RestoreMediaPublicationError(issue_code="restore_media_stage_failed")
            identity = _identity(opened)
            while True:
                chunk = reader.read(_CHUNK_BYTES)
                if type(chunk) is not bytes or len(chunk) > _CHUNK_BYTES:
                    raise RestoreMediaPublicationError(issue_code="restore_media_stage_failed")
                if not chunk:
                    break
                count += len(chunk)
                if count > expected_bytes:
                    raise RestoreMediaPublicationError(issue_code="restore_media_size_mismatch")
                offset = 0
                while offset < len(chunk):
                    written = os.write(descriptor, chunk[offset:])
                    if written <= 0:
                        raise RestoreMediaPublicationError(issue_code="restore_media_stage_failed")
                    offset += written
                digest.update(chunk)
            os.fsync(descriptor)
            if count != expected_bytes or digest.hexdigest() != expected_sha256:
                raise RestoreMediaPublicationError(issue_code="restore_media_hash_mismatch")
            return identity
        except RestoreMediaPublicationError:
            raise
        except OSError:
            raise RestoreMediaPublicationError(issue_code="restore_media_stage_failed") from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def stage(self, *, consumption, prepared, package_provider):
        if (
            type(consumption) is not RestorePreflightConsumption
            or type(prepared) is not PreparedLogicalRestore
            or type(package_provider) is not RestoredPackageProvider
        ):
            raise RestoreMediaPublicationError(issue_code="restore_media_request_invalid")
        manifest = self._manifest_media(consumption)
        if set(manifest) != set(prepared.media_storage_names):
            raise RestoreMediaPublicationError(issue_code="restore_media_reference_mismatch")
        reference = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"nexa-restore-media:{consumption.result.operation_reference}",
        )
        if reference in self._evidence or reference in self._cleaned:
            raise RestoreMediaPublicationError(issue_code="restore_media_idempotency_conflict")
        root = self._media_root()
        workspace_reference = WorkspaceReference(
            uuid.uuid5(reference, "private-media-staging")
        )
        workspace = self.workspace_manager.create(workspace_reference)
        workspace_state = _directory_state(workspace.path)
        area = workspace.system_area_path(WorkspaceArea.RESTORE_MUTATION)
        objects = []
        try:
            area.mkdir(mode=0o700, exist_ok=False)
            area_state = _directory_state(area)
            if area_state.st_dev != workspace_state.st_dev:
                raise RestoreMediaPublicationError(issue_code="restore_media_stage_failed")
            for ordinal, storage_name in enumerate(sorted(manifest), start=1):
                item = manifest[storage_name]
                target = contained_path(root, root / Path(*storage_name.split("/")))
                reuse = False
                if os.path.lexists(target):
                    state = _file_state(target)
                    count, digest = _hash_file(
                        target,
                        expected_identity=_identity(state),
                        maximum=item["byte_count"],
                    )
                    if count != item["byte_count"] or digest != item["sha256"]:
                        raise RestoreMediaPublicationError(issue_code="restore_media_collision")
                    reuse = True
                staging_path = contained_path(area, area / f"{ordinal:08d}.media")
                with package_provider.open_extracted_entry(
                    context=consumption.context,
                    package=consumption.package,
                    package_path=item["package_path"],
                    expected_byte_count=item["byte_count"],
                    expected_sha256=item["sha256"],
                ) as reader:
                    identity = self._write_staged(
                        reader=reader,
                        path=staging_path,
                        expected_bytes=item["byte_count"],
                        expected_sha256=item["sha256"],
                    )
                objects.append(
                    _StagedObject(
                        storage_name=storage_name,
                        byte_count=item["byte_count"],
                        sha256=item["sha256"],
                        staging_path=staging_path,
                        staging_identity=identity,
                        target_path=target,
                        reuse_existing=reuse,
                    )
                )
            result = StagedMediaRestore(
                reference=reference,
                operation_public_id=consumption.result.operation_reference,
                object_count=len(objects),
                total_bytes=sum(item.byte_count for item in objects),
            )
            self._evidence[reference] = _MediaEvidence(
                result=result,
                workspace_reference=workspace.reference,
                workspace_identity=_identity(workspace_state),
                area_path=area,
                area_identity=_identity(area_state),
                objects=tuple(objects),
            )
            return result
        except BaseException:
            for item in reversed(objects):
                try:
                    current = _file_state(item.staging_path)
                    if _identity(current) == item.staging_identity:
                        os.unlink(item.staging_path)
                except Exception:
                    pass
            try:
                if os.path.lexists(area):
                    with os.scandir(area) as entries:
                        empty = next(entries, None) is None
                    if empty:
                        os.rmdir(area)
                self.workspace_manager.cleanup(workspace.reference)
            except Exception:
                pass
            raise

    @staticmethod
    def _ensure_parents(root, parent, evidence):
        missing = []
        current = parent
        while current != root and not current.exists():
            missing.append(current)
            current = current.parent
        _directory_state(current)
        for directory in reversed(missing):
            try:
                directory.mkdir(mode=0o755, exist_ok=False)
            except OSError:
                raise RestoreMediaPublicationError(issue_code="restore_media_publish_failed") from None
            state = _directory_state(directory)
            evidence.created_directories.append((directory, _identity(state)))

    @staticmethod
    def _publish_one(item, evidence):
        if item.reuse_existing:
            state = _file_state(item.target_path)
            count, digest = _hash_file(
                item.target_path,
                expected_identity=_identity(state),
                maximum=item.byte_count,
            )
            if count != item.byte_count or digest != item.sha256:
                raise RestoreMediaPublicationError(issue_code="restore_media_collision")
            return False
        if os.path.lexists(item.target_path):
            raise RestoreMediaPublicationError(issue_code="restore_media_collision")
        root = LocalFilesystemMediaRestoreProvider._media_root()
        if contained_path(root, item.target_path) != item.target_path:
            raise RestoreMediaPublicationError(issue_code="restore_media_unsafe")
        LocalFilesystemMediaRestoreProvider._ensure_parents(
            root,
            item.target_path.parent,
            evidence,
        )
        if contained_path(root, item.target_path) != item.target_path:
            raise RestoreMediaPublicationError(issue_code="restore_media_unsafe")
        part = item.target_path.parent / f".nexa-restore-{uuid.uuid4().hex}.part"
        source = None
        target = None
        try:
            source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            source_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            source = os.open(item.staging_path, source_flags)
            source_state = os.fstat(source)
            if _identity(source_state) != item.staging_identity or source_state.st_nlink != 1:
                raise RestoreMediaPublicationError(issue_code="restore_media_stage_changed")
            target_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            target_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            target_flags |= getattr(os, "O_BINARY", 0)
            target = os.open(part, target_flags, 0o644)
            while True:
                chunk = os.read(source, _CHUNK_BYTES)
                if not chunk:
                    break
                offset = 0
                while offset < len(chunk):
                    written = os.write(target, chunk[offset:])
                    if written <= 0:
                        raise RestoreMediaPublicationError(issue_code="restore_media_publish_failed")
                    offset += written
            os.fsync(target)
            os.close(target)
            target = None
            part_state = _file_state(part)
            count, digest = _hash_file(
                part,
                expected_identity=_identity(part_state),
                maximum=item.byte_count,
            )
            if count != item.byte_count or digest != item.sha256:
                raise RestoreMediaPublicationError(issue_code="restore_media_publish_failed")
            os.link(part, item.target_path, follow_symlinks=False)
            evidence.created_targets.append(
                (item.target_path, _identity(part_state))
            )
            linked_state = os.stat(item.target_path, follow_symlinks=False)
            if (
                not stat.S_ISREG(linked_state.st_mode)
                or _identity(linked_state) != _identity(part_state)
                or linked_state.st_nlink != 2
            ):
                raise RestoreMediaPublicationError(issue_code="restore_media_publish_failed")
            os.unlink(part)
            final_state = _file_state(item.target_path)
            if _identity(final_state) != _identity(part_state):
                raise RestoreMediaPublicationError(issue_code="restore_media_publish_failed")
            return True
        except RestoreMediaPublicationError:
            raise
        except OSError:
            raise RestoreMediaPublicationError(issue_code="restore_media_publish_failed") from None
        finally:
            if source is not None:
                try:
                    os.close(source)
                except OSError:
                    pass
            if target is not None:
                try:
                    os.close(target)
                except OSError:
                    pass
            if os.path.lexists(part):
                try:
                    os.unlink(part)
                except OSError:
                    pass

    def publish(self, staged):
        if type(staged) is not StagedMediaRestore:
            raise RestoreMediaPublicationError(issue_code="restore_media_request_invalid")
        evidence = self._evidence.get(staged.reference)
        if evidence is None or evidence.result != staged or evidence.published:
            raise RestoreMediaPublicationError(issue_code="restore_media_evidence_invalid")
        created = 0
        for item in evidence.objects:
            if self._publish_one(item, evidence):
                created += 1
        evidence.published = True
        return MediaPublicationResult(
            reference=staged.reference,
            object_count=len(evidence.objects),
            created_count=created,
            reused_count=len(evidence.objects) - created,
        )

    def verify(self, staged):
        if type(staged) is not StagedMediaRestore:
            raise RestoreMediaPublicationError(issue_code="restore_media_request_invalid")
        evidence = self._evidence.get(staged.reference)
        if evidence is None or evidence.result != staged or not evidence.published:
            raise RestoreMediaPublicationError(issue_code="restore_media_evidence_invalid")
        for item in evidence.objects:
            state = _file_state(item.target_path)
            count, digest = _hash_file(
                item.target_path,
                expected_identity=_identity(state),
                maximum=item.byte_count,
            )
            if count != item.byte_count or digest != item.sha256:
                raise RestoreMediaPublicationError(issue_code="restore_media_hash_mismatch")
        return True

    def rollback(self, staged):
        if type(staged) is not StagedMediaRestore:
            raise RestoreMediaPublicationError(issue_code="restore_media_request_invalid")
        evidence = self._evidence.get(staged.reference)
        if evidence is None or evidence.result != staged:
            raise RestoreMediaPublicationError(issue_code="restore_media_evidence_invalid")
        try:
            for path, expected_identity in reversed(evidence.created_targets):
                current = _file_state(path)
                if _identity(current) != expected_identity:
                    raise RestoreMediaPublicationError(issue_code="restore_media_rollback_unsafe")
                os.unlink(path)
            evidence.created_targets.clear()
            for path, expected_identity in reversed(evidence.created_directories):
                current = _directory_state(path)
                if _identity(current) != expected_identity:
                    raise RestoreMediaPublicationError(issue_code="restore_media_rollback_unsafe")
                with os.scandir(path) as entries:
                    if next(entries, None) is not None:
                        raise RestoreMediaPublicationError(issue_code="restore_media_rollback_unsafe")
                os.rmdir(path)
            evidence.created_directories.clear()
            evidence.published = False
            return True
        except RestoreMediaPublicationError:
            raise
        except OSError:
            raise RestoreMediaPublicationError(issue_code="restore_media_rollback_failed") from None

    def cleanup(self, staged):
        if type(staged) is not StagedMediaRestore:
            raise RestoreMediaPublicationError(issue_code="restore_media_request_invalid")
        if staged.reference in self._cleaned:
            return False
        evidence = self._evidence.get(staged.reference)
        if evidence is None or evidence.result != staged:
            raise RestoreMediaPublicationError(issue_code="restore_media_evidence_invalid")
        try:
            for item in reversed(evidence.objects):
                current = _file_state(item.staging_path)
                if _identity(current) != item.staging_identity:
                    raise RestoreMediaPublicationError(issue_code="restore_media_cleanup_unsafe")
                os.unlink(item.staging_path)
            area_state = _directory_state(evidence.area_path)
            if _identity(area_state) != evidence.area_identity:
                raise RestoreMediaPublicationError(issue_code="restore_media_cleanup_unsafe")
            with os.scandir(evidence.area_path) as entries:
                if next(entries, None) is not None:
                    raise RestoreMediaPublicationError(issue_code="restore_media_cleanup_unsafe")
            os.rmdir(evidence.area_path)
            workspace = self.workspace_manager.handle(evidence.workspace_reference)
            workspace_state = _directory_state(workspace.path)
            if _identity(workspace_state) != evidence.workspace_identity:
                raise RestoreMediaPublicationError(issue_code="restore_media_cleanup_unsafe")
            self.workspace_manager.cleanup(evidence.workspace_reference)
        except RestoreMediaPublicationError:
            raise
        except OSError:
            raise RestoreMediaPublicationError(issue_code="restore_media_cleanup_failed") from None
        del self._evidence[staged.reference]
        self._cleaned.add(staged.reference)
        return True


__all__ = [
    "LocalFilesystemMediaRestoreProvider",
    "MediaPublicationResult",
    "StagedMediaRestore",
]
