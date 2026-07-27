"""Secure empty-workspace primitives for future backup stages."""

import os
import shutil
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


def _configured_public_roots():
    for setting_name in ("MEDIA_ROOT", "STATIC_ROOT"):
        value = getattr(settings, setting_name, None)
        if value:
            yield setting_name, _resolved(value)


def validate_staging_root(root) -> Path:
    candidate = Path(root).expanduser()
    if not candidate.is_absolute():
        raise UnsafeWorkspacePath()
    candidate = candidate.resolve(strict=False)
    for _setting_name, public_root in _configured_public_roots():
        if candidate == public_root or _is_relative_to(candidate, public_root):
            raise UnsafeWorkspacePath(
                "The backup staging root cannot be inside a publicly served directory."
            )
    return candidate


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
        root = self._root.resolve(strict=False)
        resolved = Path(candidate).resolve(strict=False)
        if resolved == root or not _is_relative_to(resolved, root):
            raise UnsafeWorkspacePath()
        return resolved

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
        if not path.exists():
            return False
        if path.is_symlink():
            raise UnsafeWorkspacePath()
        shutil.rmtree(path)
        return True
