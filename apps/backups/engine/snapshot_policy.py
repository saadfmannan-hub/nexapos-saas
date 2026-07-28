"""Fail-closed SQLite snapshot policy and local-storage assessment."""

import ctypes
import math
import platform
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from .exceptions import (
    InsufficientSnapshotCapacity,
    SQLiteSnapshotPolicyError,
    UnsafeStagingFilesystem,
)

SQLITE_SYNCHRONOUS_LEVELS = {
    "OFF": 0,
    "NORMAL": 1,
    "FULL": 2,
    "EXTRA": 3,
}

KNOWN_LOCAL_FILESYSTEMS = frozenset(
    {
        "apfs",
        "btrfs",
        "ext2",
        "ext3",
        "ext4",
        "exfat",
        "f2fs",
        "hfs",
        "hfsplus",
        "ntfs",
        "ntfs3",
        "refs",
        "ubifs",
        "vfat",
        "xfs",
        "zfs",
    }
)
KNOWN_NETWORK_FILESYSTEMS = frozenset(
    {
        "9p",
        "afs",
        "ceph",
        "cifs",
        "fuse.ceph",
        "fuse.glusterfs",
        "fuse.sshfs",
        "gcsfuse",
        "glusterfs",
        "lustre",
        "nfs",
        "nfs4",
        "smb2",
        "smb3",
        "sshfs",
    }
)


def _bounded_number(name, value, *, minimum, maximum, integer=False):
    if isinstance(value, bool):
        raise SQLiteSnapshotPolicyError(f"The {name} setting is invalid.")
    try:
        normalized = int(value) if integer else float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SQLiteSnapshotPolicyError(f"The {name} setting is invalid.") from exc
    if not math.isfinite(float(normalized)) or not minimum <= normalized <= maximum:
        raise SQLiteSnapshotPolicyError(f"The {name} setting is outside safe bounds.")
    if integer and normalized != value and not isinstance(value, str):
        raise SQLiteSnapshotPolicyError(f"The {name} setting must be an integer.")
    return normalized


@dataclass(frozen=True, slots=True)
class SQLiteSnapshotPolicy:
    required_journal_mode: str
    required_synchronous: str
    busy_timeout_seconds: float
    pages_per_step: int
    backup_sleep_seconds: float
    snapshot_timeout_seconds: float
    minimum_free_bytes: int
    headroom_multiplier: float
    require_local_staging: bool

    @classmethod
    def from_settings(cls):
        return cls(
            required_journal_mode=getattr(
                settings,
                "BACKUP_SQLITE_REQUIRED_JOURNAL_MODE",
                "WAL",
            ),
            required_synchronous=getattr(
                settings,
                "BACKUP_SQLITE_REQUIRED_SYNCHRONOUS",
                "FULL",
            ),
            busy_timeout_seconds=getattr(
                settings,
                "BACKUP_SQLITE_BUSY_TIMEOUT_SECONDS",
                5.0,
            ),
            pages_per_step=getattr(
                settings,
                "BACKUP_SQLITE_BACKUP_PAGES_PER_STEP",
                256,
            ),
            backup_sleep_seconds=getattr(
                settings,
                "BACKUP_SQLITE_BACKUP_SLEEP_SECONDS",
                0.05,
            ),
            snapshot_timeout_seconds=getattr(
                settings,
                "BACKUP_SQLITE_SNAPSHOT_TIMEOUT_SECONDS",
                300.0,
            ),
            minimum_free_bytes=getattr(
                settings,
                "BACKUP_SQLITE_MIN_FREE_BYTES",
                1_073_741_824,
            ),
            headroom_multiplier=getattr(
                settings,
                "BACKUP_SQLITE_HEADROOM_MULTIPLIER",
                3.0,
            ),
            require_local_staging=getattr(
                settings,
                "BACKUP_SQLITE_REQUIRE_LOCAL_STAGING",
                True,
            ),
        ).validated()

    def validated(self):
        journal_mode = str(self.required_journal_mode or "").strip().upper()
        if journal_mode != "WAL":
            raise SQLiteSnapshotPolicyError("The SQLite snapshot journal policy must require WAL.")
        synchronous = str(self.required_synchronous or "").strip().upper()
        if synchronous not in {"FULL", "EXTRA"}:
            raise SQLiteSnapshotPolicyError(
                "The SQLite snapshot durability policy must require FULL or EXTRA."
            )
        busy_timeout = _bounded_number(
            "SQLite busy timeout",
            self.busy_timeout_seconds,
            minimum=0.1,
            maximum=300.0,
        )
        pages = _bounded_number(
            "SQLite backup page step",
            self.pages_per_step,
            minimum=1,
            maximum=65_536,
            integer=True,
        )
        sleep = _bounded_number(
            "SQLite backup sleep",
            self.backup_sleep_seconds,
            minimum=0.001,
            maximum=10.0,
        )
        timeout = _bounded_number(
            "SQLite snapshot timeout",
            self.snapshot_timeout_seconds,
            minimum=1.0,
            maximum=3_600.0,
        )
        minimum_free = _bounded_number(
            "SQLite minimum free bytes",
            self.minimum_free_bytes,
            minimum=1,
            maximum=10 * 1024**4,
            integer=True,
        )
        multiplier = _bounded_number(
            "SQLite headroom multiplier",
            self.headroom_multiplier,
            minimum=1.0,
            maximum=20.0,
        )
        if busy_timeout > timeout:
            raise SQLiteSnapshotPolicyError(
                "The SQLite busy timeout cannot exceed the snapshot deadline."
            )
        if sleep >= timeout:
            raise SQLiteSnapshotPolicyError(
                "The SQLite backup sleep must remain below the snapshot deadline."
            )
        if not isinstance(self.require_local_staging, bool):
            raise SQLiteSnapshotPolicyError("The SQLite local-staging requirement must be boolean.")
        return type(self)(
            required_journal_mode=journal_mode,
            required_synchronous=synchronous,
            busy_timeout_seconds=busy_timeout,
            pages_per_step=pages,
            backup_sleep_seconds=sleep,
            snapshot_timeout_seconds=timeout,
            minimum_free_bytes=minimum_free,
            headroom_multiplier=multiplier,
            require_local_staging=self.require_local_staging,
        )


@dataclass(frozen=True, slots=True)
class StorageAssessment:
    confirmed_local: bool
    classification: str
    filesystem_type: str = ""


def _decode_mountinfo_path(value):
    return value.replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")


class LocalFilesystemInspector:
    """Use standard-library/OS facts without claiming unknown storage is local."""

    def __init__(
        self,
        *,
        platform_name=None,
        windows_drive_type=None,
        mountinfo_reader=None,
    ):
        self.platform_name = platform_name or platform.system()
        self.windows_drive_type = windows_drive_type or self._windows_drive_type
        self.mountinfo_reader = mountinfo_reader or self._read_mountinfo

    @staticmethod
    def _windows_drive_type(root):
        return int(ctypes.windll.kernel32.GetDriveTypeW(str(root)))

    @staticmethod
    def _read_mountinfo():
        return Path("/proc/self/mountinfo").read_text(
            encoding="utf-8",
            errors="replace",
        )

    def assess(self, path) -> StorageAssessment:
        candidate = Path(path)
        system = str(self.platform_name).lower()
        if system == "windows":
            rendered = str(candidate)
            if rendered.startswith(("\\\\", "//")):
                return StorageAssessment(False, "network", "unc")
            anchor = candidate.anchor
            if not anchor:
                return StorageAssessment(False, "unknown")
            try:
                drive_type = self.windows_drive_type(anchor)
            except (AttributeError, OSError, ValueError):
                return StorageAssessment(False, "unknown")
            # Win32 DRIVE_FIXED=3 and DRIVE_REMOTE=4.
            if drive_type == 3:
                return StorageAssessment(True, "local-fixed", "fixed")
            if drive_type == 4:
                return StorageAssessment(False, "network", "remote")
            return StorageAssessment(False, "unknown", f"drive-{drive_type}")
        if system == "linux":
            return self._assess_linux(candidate)
        return StorageAssessment(False, "unknown")

    def _assess_linux(self, path) -> StorageAssessment:
        try:
            candidate = path.resolve(strict=True)
            mountinfo = self.mountinfo_reader()
        except (OSError, RuntimeError, ValueError):
            return StorageAssessment(False, "unknown")
        best_match = None
        for line in str(mountinfo).splitlines():
            try:
                left, right = line.split(" - ", 1)
                left_fields = left.split()
                right_fields = right.split()
                mount_point = Path(_decode_mountinfo_path(left_fields[4]))
                filesystem_type = right_fields[0].lower()
                candidate.relative_to(mount_point)
            except (IndexError, ValueError):
                continue
            if best_match is None or len(mount_point.parts) > len(best_match[0].parts):
                best_match = (mount_point, filesystem_type)
        if best_match is None:
            return StorageAssessment(False, "unknown")
        filesystem_type = best_match[1]
        if filesystem_type in KNOWN_NETWORK_FILESYSTEMS:
            return StorageAssessment(False, "network", filesystem_type)
        if filesystem_type in KNOWN_LOCAL_FILESYSTEMS:
            return StorageAssessment(True, "local-block", filesystem_type)
        return StorageAssessment(False, "unknown", filesystem_type)


def required_staging_capacity(
    *,
    page_count,
    page_size,
    wal_bytes,
    policy: SQLiteSnapshotPolicy,
) -> int:
    if page_count <= 0 or page_size <= 0 or wal_bytes < 0:
        raise SQLiteSnapshotPolicyError("The SQLite capacity inputs are invalid.")
    estimated_bytes = page_count * page_size + wal_bytes
    reserve = math.ceil(estimated_bytes * policy.headroom_multiplier)
    return max(policy.minimum_free_bytes, reserve)


def assert_staging_capacity(
    *,
    path,
    page_count,
    page_size,
    wal_bytes,
    policy,
    disk_usage_provider,
) -> int:
    required = required_staging_capacity(
        page_count=page_count,
        page_size=page_size,
        wal_bytes=wal_bytes,
        policy=policy,
    )
    try:
        free_bytes = int(disk_usage_provider(path).free)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise InsufficientSnapshotCapacity() from exc
    if free_bytes < required:
        raise InsufficientSnapshotCapacity()
    return required


def assert_local_staging(*, path, policy, inspector) -> StorageAssessment:
    try:
        assessment = inspector.assess(path)
        if not isinstance(assessment, StorageAssessment):
            raise TypeError
        if policy.require_local_staging and not assessment.confirmed_local:
            raise UnsafeStagingFilesystem()
    except UnsafeStagingFilesystem:
        raise
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise UnsafeStagingFilesystem() from exc
    return assessment
