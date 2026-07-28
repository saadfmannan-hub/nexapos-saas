"""Secure empty-workspace primitives for future backup stages."""

import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from django.conf import settings

from .exceptions import UnsafeWorkspacePath


@dataclass(frozen=True, slots=True)
class WorkspaceReference:
    """Opaque reference safe to place in an execution context."""

    identifier: uuid.UUID

    @classmethod
    def new(cls):
        return cls(uuid.uuid4())

    @classmethod
    def parse(cls, value):
        if isinstance(value, cls):
            return value
        try:
            return cls(uuid.UUID(str(value)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise UnsafeWorkspacePath() from exc


class WorkspaceArea(StrEnum):
    SNAPSHOT = "snapshot"
    COMPONENTS = "components"
    MANIFEST = "manifest"
    PACKAGE = "package"
    VERIFICATION = "verification"


def _resolved(path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _is_relative_to(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def path_is_link_like(path) -> bool:
    """Detect symlinks and Windows reparse/junction targets without following."""

    candidate = Path(path)
    if candidate.is_symlink():
        return True
    is_junction = getattr(candidate, "is_junction", None)
    if callable(is_junction):
        try:
            if is_junction():
                return True
        except OSError:
            return True
    try:
        file_attributes = getattr(os.lstat(candidate), "st_file_attributes", 0)
    except (FileNotFoundError, OSError):
        return False
    return bool(file_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def path_has_link_like_component(path) -> bool:
    """Inspect every existing lexical component without resolving through it."""

    candidate = Path(os.path.abspath(Path(path).expanduser()))
    while True:
        if os.path.lexists(candidate) and path_is_link_like(candidate):
            return True
        if candidate == candidate.parent:
            return False
        candidate = candidate.parent


def contained_path(root, candidate, *, allow_root=False) -> Path:
    """Reject lexical escapes and every existing link-like path component."""

    lexical_root = Path(os.path.abspath(root))
    lexical_candidate = Path(os.path.abspath(candidate))
    if path_has_link_like_component(lexical_root):
        raise UnsafeWorkspacePath()
    if lexical_candidate != lexical_root and not _is_relative_to(lexical_candidate, lexical_root):
        raise UnsafeWorkspacePath()
    if lexical_candidate == lexical_root and not allow_root:
        raise UnsafeWorkspacePath()

    current = lexical_candidate
    while True:
        if os.path.lexists(current) and path_is_link_like(current):
            raise UnsafeWorkspacePath()
        if current == lexical_root:
            break
        current = current.parent

    resolved_root = lexical_root.resolve(strict=False)
    resolved_candidate = lexical_candidate.resolve(strict=False)
    if resolved_candidate != resolved_root and not _is_relative_to(
        resolved_candidate, resolved_root
    ):
        raise UnsafeWorkspacePath()
    if resolved_candidate == resolved_root and not allow_root:
        raise UnsafeWorkspacePath()
    return resolved_candidate


def _configured_public_roots():
    for setting_name in ("MEDIA_ROOT", "STATIC_ROOT"):
        value = getattr(settings, setting_name, None)
        if value:
            yield setting_name, _resolved(value)


def validate_staging_root(root) -> Path:
    candidate = Path(root).expanduser()
    if not candidate.is_absolute():
        raise UnsafeWorkspacePath()
    if path_has_link_like_component(candidate):
        raise UnsafeWorkspacePath()
    candidate = candidate.resolve(strict=False)
    for _setting_name, public_root in _configured_public_roots():
        if candidate == public_root or _is_relative_to(candidate, public_root):
            raise UnsafeWorkspacePath(
                "The backup staging root cannot be inside a publicly served directory."
            )
    return candidate


def _validate_cleanup_tree(path, *, expected_device):
    """Refuse links, reparse points, and nested filesystem boundaries."""

    pending = [Path(path)]
    while pending:
        directory = pending.pop()
        if path_is_link_like(directory):
            raise UnsafeWorkspacePath()
        try:
            directory_stat = os.stat(directory, follow_symlinks=False)
            if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_dev != expected_device:
                raise UnsafeWorkspacePath()
            with os.scandir(directory) as entries:
                for entry in entries:
                    entry_path = Path(entry.path)
                    if path_is_link_like(entry_path):
                        raise UnsafeWorkspacePath()
                    entry_stat = entry.stat(follow_symlinks=False)
                    if entry_stat.st_dev != expected_device:
                        raise UnsafeWorkspacePath()
                    if stat.S_ISDIR(entry_stat.st_mode):
                        pending.append(entry_path)
        except UnsafeWorkspacePath:
            raise
        except OSError:
            raise UnsafeWorkspacePath() from None


@dataclass(frozen=True, slots=True)
class BackupWorkspace:
    """Internal workspace handle; only ``reference`` belongs in durable context."""

    reference: WorkspaceReference
    _root: Path

    @property
    def path(self) -> Path:
        """Return the internal path for engine providers, never for UI metadata."""

        return self._contained(self._root / f"ws-{self.reference.identifier.hex}")

    def _contained(self, candidate) -> Path:
        return contained_path(self._root, candidate)

    def child_path(self, *system_segments) -> Path:
        """Resolve engine-owned child segments while rejecting traversal."""

        if not system_segments:
            raise UnsafeWorkspacePath()
        candidate = self.path
        for segment in system_segments:
            value = str(segment)
            parsed = Path(value)
            if (
                not value
                or value in {".", ".."}
                or parsed.is_absolute()
                or len(parsed.parts) != 1
                or parsed.name != value
            ):
                raise UnsafeWorkspacePath()
            candidate /= value
        return self._contained(candidate)

    def system_area_path(
        self,
        area: WorkspaceArea,
        *,
        generated_identifier: uuid.UUID | None = None,
    ) -> Path:
        normalized_area = WorkspaceArea(area)
        if generated_identifier is None:
            return self.child_path(normalized_area.value)
        identifier = uuid.UUID(str(generated_identifier))
        return self.child_path(normalized_area.value, identifier.hex)


class BackupWorkspaceManager:
    """Create and idempotently clean empty, application-controlled workspaces."""

    def __init__(self, root=None):
        configured_root = (
            root if root is not None else getattr(settings, "BACKUP_STAGING_ROOT", "")
        )
        self.root = validate_staging_root(configured_root)

    def handle(self, reference) -> BackupWorkspace:
        return BackupWorkspace(WorkspaceReference.parse(reference), self.root)

    def create(self, reference=None) -> BackupWorkspace:
        workspace = self.handle(reference or WorkspaceReference.new())
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        workspace.path.mkdir(mode=0o700, exist_ok=False)
        try:
            os.chmod(workspace.path, 0o700)
        except OSError:
            pass
        return workspace

    def cleanup(self, workspace_or_reference) -> bool:
        reference = (
            workspace_or_reference.reference
            if isinstance(workspace_or_reference, BackupWorkspace)
            else workspace_or_reference
        )
        # Reconstruct against this manager's configured root.  A caller cannot
        # smuggle in a handle whose private root points somewhere else.
        workspace = self.handle(reference)
        path = workspace.path
        if os.path.lexists(path) and path_is_link_like(path):
            raise UnsafeWorkspacePath()
        if not path.exists():
            return False
        try:
            root_device = os.stat(self.root, follow_symlinks=False).st_dev
            workspace_device = os.stat(path, follow_symlinks=False).st_dev
        except OSError:
            raise UnsafeWorkspacePath() from None
        if workspace_device != root_device:
            raise UnsafeWorkspacePath()
        _validate_cleanup_tree(path, expected_device=root_device)
        shutil.rmtree(path)
        return True
