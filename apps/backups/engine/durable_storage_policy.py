"""Strict policy and root validation for Phase 2G durable local storage."""

from __future__ import annotations

import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from .durable_storage_exceptions import (
    DurableStoragePolicyError,
    UnsafeDurableStorageRoot,
)
from .snapshot_policy import LocalFilesystemInspector
from .workspace import path_has_link_like_component, path_is_link_like


def _absolute(value):
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise ValueError
        return Path(os.path.abspath(candidate))
    except (OSError, TypeError, ValueError):
        raise UnsafeDurableStorageRoot() from None


def _relative_to(candidate, root):
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _overlaps(left, right):
    return left == right or _relative_to(left, right) or _relative_to(right, left)


def _nearest_existing_parent(candidate):
    current = candidate
    while not os.path.lexists(current):
        if current == current.parent:
            raise UnsafeDurableStorageRoot()
        current = current.parent
    return current


def validate_durable_storage_root(
    root,
    *,
    staging_root=None,
    media_root=None,
    static_root=None,
    require_local=True,
    filesystem_inspector=None,
):
    candidate = _absolute(root)
    if path_has_link_like_component(candidate):
        raise UnsafeDurableStorageRoot()
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        raise UnsafeDurableStorageRoot() from None
    if resolved != candidate:
        raise UnsafeDurableStorageRoot()
    for configured in (staging_root, media_root, static_root):
        if not configured:
            continue
        other = _absolute(configured).resolve(strict=False)
        if _overlaps(candidate, other):
            raise UnsafeDurableStorageRoot()
    existing = _nearest_existing_parent(candidate)
    try:
        current = os.stat(existing, follow_symlinks=False)
    except OSError:
        raise UnsafeDurableStorageRoot() from None
    if path_is_link_like(existing) or not stat.S_ISDIR(current.st_mode):
        raise UnsafeDurableStorageRoot()
    if require_local:
        inspector = filesystem_inspector or LocalFilesystemInspector()
        try:
            assessment = inspector.assess(existing)
        except Exception:
            raise UnsafeDurableStorageRoot() from None
        if assessment.confirmed_local is not True:
            raise UnsafeDurableStorageRoot()
    return candidate


@dataclass(frozen=True, slots=True)
class DurableStoragePolicy:
    root: Path
    chunk_bytes: int
    maximum_object_bytes: int
    timeout_seconds: float
    minimum_free_bytes: int
    headroom_multiplier: float
    require_local: bool

    @classmethod
    def from_settings(cls):
        try:
            return cls(
                root=settings.BACKUP_DURABLE_STORAGE_ROOT,
                chunk_bytes=settings.BACKUP_DURABLE_STORAGE_CHUNK_BYTES,
                maximum_object_bytes=settings.BACKUP_DURABLE_STORAGE_MAX_OBJECT_BYTES,
                timeout_seconds=settings.BACKUP_DURABLE_STORAGE_TIMEOUT_SECONDS,
                minimum_free_bytes=settings.BACKUP_DURABLE_STORAGE_MIN_FREE_BYTES,
                headroom_multiplier=(
                    settings.BACKUP_DURABLE_STORAGE_HEADROOM_MULTIPLIER
                ),
                require_local=settings.BACKUP_DURABLE_STORAGE_REQUIRE_LOCAL,
            ).validated()
        except (AttributeError, OSError, TypeError, ValueError):
            raise DurableStoragePolicyError() from None

    def validated(self):
        if (
            not isinstance(self.root, (str, Path))
            or type(self.chunk_bytes) is not int
            or not 4096 <= self.chunk_bytes <= 16 * 1024**2
            or type(self.maximum_object_bytes) is not int
            or not 1 <= self.maximum_object_bytes <= 10 * 1024**4 + 16 * 1024**2
            or type(self.minimum_free_bytes) is not int
            or not 0 <= self.minimum_free_bytes <= 10 * 1024**4
            or type(self.timeout_seconds) not in (int, float)
            or type(self.timeout_seconds) is bool
            or not math.isfinite(float(self.timeout_seconds))
            or not 1.0 <= float(self.timeout_seconds) <= 86_400.0
            or type(self.headroom_multiplier) not in (int, float)
            or type(self.headroom_multiplier) is bool
            or not math.isfinite(float(self.headroom_multiplier))
            or not 1.0 <= float(self.headroom_multiplier) <= 10.0
            or type(self.require_local) is not bool
        ):
            raise DurableStoragePolicyError()
        return type(self)(
            root=_absolute(self.root),
            chunk_bytes=self.chunk_bytes,
            maximum_object_bytes=self.maximum_object_bytes,
            timeout_seconds=float(self.timeout_seconds),
            minimum_free_bytes=self.minimum_free_bytes,
            headroom_multiplier=float(self.headroom_multiplier),
            require_local=self.require_local,
        )


__all__ = ["DurableStoragePolicy", "validate_durable_storage_root"]
