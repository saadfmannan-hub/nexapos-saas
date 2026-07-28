"""Private SQLite online-backup provider for temporary full-platform snapshots."""

import math
import os
import shutil
import sqlite3
import stat
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from django.db import connections
from django.db.utils import ConnectionDoesNotExist
from django.utils import timezone

from .context import BackupExecutionContext
from .contracts import (
    SnapshotProvider,
    SnapshotReference,
    SnapshotRequest,
    SnapshotResult,
)
from .exceptions import (
    InsufficientSnapshotCapacity,
    SnapshotBusy,
    SnapshotCleanupError,
    SnapshotCreationError,
    SnapshotEngineError,
    SnapshotNotFound,
    SnapshotTimeout,
    SnapshotValidationError,
    SnapshotWorkspaceUnavailable,
    SQLiteSnapshotPolicyError,
    UnsafeSnapshotSource,
    UnsafeStagingFilesystem,
    UnsafeWorkspacePath,
    UnsupportedSnapshotBackend,
)
from .snapshot_policy import (
    SQLITE_SYNCHRONOUS_LEVELS,
    LocalFilesystemInspector,
    SQLiteSnapshotPolicy,
    assert_local_staging,
    assert_staging_capacity,
)
from .workspace import (
    BackupWorkspace,
    BackupWorkspaceManager,
    WorkspaceArea,
    contained_path,
    path_has_link_like_component,
    path_is_link_like,
)

SQLITE_SNAPSHOT_PROVIDER_IDENTIFIER = "sqlite-online-backup-v1"
SNAPSHOT_FILE_NAME = "snapshot.sqlite3"
SNAPSHOT_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_READER_ALLOWED_PRAGMAS = frozenset(
    {
        "foreign_key_check",
        "foreign_keys",
        "journal_mode",
        "page_count",
        "page_size",
        "query_only",
        "quick_check",
        "schema_version",
    }
)

_READER_DENIED_ACTIONS = frozenset(
    action
    for action in (
        getattr(sqlite3, "SQLITE_ATTACH", None),
        getattr(sqlite3, "SQLITE_DETACH", None),
        getattr(sqlite3, "SQLITE_INSERT", None),
        getattr(sqlite3, "SQLITE_UPDATE", None),
        getattr(sqlite3, "SQLITE_DELETE", None),
        getattr(sqlite3, "SQLITE_CREATE_INDEX", None),
        getattr(sqlite3, "SQLITE_CREATE_TABLE", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_INDEX", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_TABLE", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_VIEW", None),
        getattr(sqlite3, "SQLITE_CREATE_TRIGGER", None),
        getattr(sqlite3, "SQLITE_CREATE_VIEW", None),
        getattr(sqlite3, "SQLITE_CREATE_VTABLE", None),
        getattr(sqlite3, "SQLITE_DROP_INDEX", None),
        getattr(sqlite3, "SQLITE_DROP_TABLE", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_INDEX", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_TABLE", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_VIEW", None),
        getattr(sqlite3, "SQLITE_DROP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_DROP_VIEW", None),
        getattr(sqlite3, "SQLITE_DROP_VTABLE", None),
        getattr(sqlite3, "SQLITE_ALTER_TABLE", None),
        getattr(sqlite3, "SQLITE_REINDEX", None),
        getattr(sqlite3, "SQLITE_ANALYZE", None),
    )
    if action is not None
)


@dataclass(frozen=True, slots=True)
class _SourceMetadata:
    journal_mode: str
    synchronous_level: int
    schema_version: int
    page_count: int
    page_size: int
    wal_bytes: int


class SQLiteSnapshotReader:
    """Narrow read-only facade that does not expose its raw connection."""

    __slots__ = ("__connection", "__deadline_check")

    def __init__(self, connection, *, deadline_check=None):
        self.__connection = connection
        self.__deadline_check = deadline_check

    def _check_deadline(self):
        if self.__deadline_check is not None:
            self.__deadline_check()

    def query(self, sql, parameters=()):
        cursor = None
        try:
            self._check_deadline()
            cursor = self.__connection.execute(sql, tuple(parameters))
            rows = tuple(tuple(row) for row in cursor.fetchall())
            self._check_deadline()
            return rows
        except SnapshotTimeout:
            raise
        except sqlite3.Error:
            self._check_deadline()
            raise SnapshotValidationError(
                "The read-only snapshot operation was rejected."
            ) from None
        finally:
            if cursor is not None:
                active_exception = sys.exc_info()[0] is not None
                try:
                    cursor.close()
                except sqlite3.Error:
                    if not active_exception:
                        raise SnapshotValidationError() from None

    def first(self, sql, parameters=()):
        cursor = None
        try:
            self._check_deadline()
            cursor = self.__connection.execute(sql, tuple(parameters))
            row = cursor.fetchone()
            self._check_deadline()
            return None if row is None else tuple(row)
        except SnapshotTimeout:
            raise
        except sqlite3.Error:
            self._check_deadline()
            raise SnapshotValidationError(
                "The read-only snapshot operation was rejected."
            ) from None
        finally:
            if cursor is not None:
                active_exception = sys.exc_info()[0] is not None
                try:
                    cursor.close()
                except sqlite3.Error:
                    if not active_exception:
                        raise SnapshotValidationError() from None

    def scalar(self, sql, parameters=()):
        rows = self.query(sql, parameters)
        if len(rows) != 1 or len(rows[0]) != 1:
            raise SnapshotValidationError()
        return rows[0][0]


def _reader_authorizer(action, argument_1, argument_2, database_name, trigger_name):
    del database_name, trigger_name
    if action in _READER_DENIED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == getattr(sqlite3, "SQLITE_PRAGMA", -1):
        pragma_name = str(argument_1 or "").lower()
        if pragma_name not in _READER_ALLOWED_PRAGMAS:
            return sqlite3.SQLITE_DENY
        if argument_2 is not None and not (pragma_name == "quick_check" and str(argument_2) == "1"):
            return sqlite3.SQLITE_DENY
    if action == getattr(sqlite3, "SQLITE_READ", -1) and str(argument_1 or "").lower().startswith(
        "pragma_"
    ):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _django_source_settings(using):
    try:
        return dict(connections[using].settings_dict)
    except ConnectionDoesNotExist as exc:
        raise UnsupportedSnapshotBackend() from exc


def _fetchone(connection, sql):
    cursor = None
    try:
        cursor = connection.execute(sql)
        return cursor.fetchone()
    finally:
        if cursor is not None:
            cursor.close()


def _fetchall(connection, sql):
    cursor = None
    try:
        cursor = connection.execute(sql)
        return tuple(tuple(row) for row in cursor.fetchall())
    finally:
        if cursor is not None:
            cursor.close()


def _pragma_integer(connection, name):
    row = _fetchone(connection, f"PRAGMA {name}")
    if row is None or len(row) != 1:
        raise SQLiteSnapshotPolicyError()
    try:
        return int(row[0])
    except (TypeError, ValueError, OverflowError) as exc:
        raise SQLiteSnapshotPolicyError() from exc


def _is_regular_file(path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except (FileNotFoundError, OSError):
        return False


def _valid_sqlite_page_size(value) -> bool:
    return isinstance(value, int) and 512 <= value <= 65_536 and value & (value - 1) == 0


class SQLiteSnapshotProvider(SnapshotProvider):
    """Create, validate, read, and clean an opaque temporary SQLite snapshot."""

    def __init__(
        self,
        *,
        using="default",
        workspace_manager=None,
        policy=None,
        source_settings_resolver=None,
        connection_factory=None,
        monotonic=None,
        disk_usage_provider=None,
        filesystem_inspector=None,
        reference_factory=None,
        permission_applier=None,
        progress_hook=None,
        failure_hook=None,
        quick_check_runner=None,
        foreign_key_check_runner=None,
        unlinker=None,
        directory_remover=None,
    ):
        self.using = str(using)
        self.workspace_manager = workspace_manager or BackupWorkspaceManager()
        self.policy = policy
        self.source_settings_resolver = source_settings_resolver or _django_source_settings
        self.connection_factory = connection_factory or sqlite3.connect
        self.monotonic = monotonic or time.monotonic
        self.disk_usage_provider = disk_usage_provider or shutil.disk_usage
        self.filesystem_inspector = filesystem_inspector or LocalFilesystemInspector()
        self.reference_factory = reference_factory or (lambda: SnapshotReference(uuid.uuid4()))
        self.permission_applier = permission_applier or os.chmod
        self.progress_hook = progress_hook
        self.failure_hook = failure_hook
        self.quick_check_runner = quick_check_runner or (
            lambda reader: reader.query("PRAGMA quick_check(1)")
        )
        self.foreign_key_check_runner = foreign_key_check_runner or (
            lambda reader: reader.first("PRAGMA foreign_key_check")
        )
        self.unlinker = unlinker or os.unlink
        self.directory_remover = directory_remover or os.rmdir

    def create_snapshot(self, request: SnapshotRequest) -> SnapshotResult:
        policy = None
        context = None
        workspace = None
        source_path = None
        started = None
        deadline = None
        source_connection = None
        destination_connection = None
        reference = None
        snapshot_path = None
        snapshot_directory_owned = False
        snapshot_file_owned = False
        phase = "source"
        result = None
        safe_error = None
        abort_error = None
        abort_traceback = None
        cleanup_incomplete = False

        try:
            policy = (
                self.policy.validated()
                if self.policy is not None
                else SQLiteSnapshotPolicy.from_settings()
            )
            started = self._monotonic_value()
            deadline = started + policy.snapshot_timeout_seconds
            context = self._validated_context(request)
            workspace = self._existing_workspace(context)
            source_path = self._resolve_source_path()
            self._assert_before_deadline(deadline)
            source_identity = self._regular_file_identity(
                source_path,
                error_type=UnsafeSnapshotSource,
            )
            source_connection = self._open_connection(
                source_path,
                mode="ro",
                policy=policy,
            )
            self._assert_expected_identity(
                source_path,
                source_identity,
                error_type=UnsafeSnapshotSource,
            )
            source_metadata = self._assess_source(
                source_connection,
                source_path=source_path,
                expected_identity=source_identity,
                policy=policy,
            )
            self._assert_before_deadline(deadline)
            snapshot_parent = self._ensure_snapshot_parent(workspace)
            self._assert_same_storage_device(workspace.path, snapshot_parent)
            assert_local_staging(
                path=snapshot_parent,
                policy=policy,
                inspector=self.filesystem_inspector,
            )
            assert_staging_capacity(
                path=snapshot_parent,
                page_count=source_metadata.page_count,
                page_size=source_metadata.page_size,
                wal_bytes=source_metadata.wal_bytes,
                policy=policy,
                disk_usage_provider=self.disk_usage_provider,
            )
            self._assert_before_deadline(deadline)

            phase = "destination"
            reference = self._new_reference()
            snapshot_directory = self._snapshot_directory(
                workspace,
                reference,
                require_exists=False,
            )
            snapshot_directory.mkdir(mode=0o700, exist_ok=False)
            snapshot_directory_owned = True
            self._apply_private_mode(snapshot_directory, 0o700)
            self._assert_same_storage_device(snapshot_parent, snapshot_directory)
            snapshot_path = self._snapshot_file(
                workspace,
                reference,
                require_exists=False,
            )

            def mark_snapshot_file_owned():
                nonlocal snapshot_file_owned
                snapshot_file_owned = True

            initial_identity = self._reserve_snapshot_file(
                snapshot_path,
                on_created=mark_snapshot_file_owned,
            )
            self._apply_private_mode(snapshot_path, 0o600)
            self._assert_file_identity(snapshot_path, initial_identity)
            self._run_failure_hook("after_destination_creation")

            destination_connection = self._open_connection(
                snapshot_path,
                mode="rw",
                policy=policy,
            )
            self._assert_file_identity(snapshot_path, initial_identity)
            progress = self._progress_callback(deadline)
            source_connection.backup(
                destination_connection,
                pages=policy.pages_per_step,
                progress=progress,
                sleep=policy.backup_sleep_seconds,
            )
            self._assert_before_deadline(deadline)
            self._run_failure_hook("after_backup")
            self._normalize_destination_journal(destination_connection)
            self._normalize_destination_schema_version(
                destination_connection,
                source_metadata.schema_version,
            )
            self._assert_before_deadline(deadline)
            destination_connection.close()
            destination_connection = None
            self._apply_private_mode(snapshot_path, 0o600)
            self._assert_no_sidecars(snapshot_path)

            phase = "validation"
            self._run_failure_hook("before_validation")
            validation = self._validate_snapshot(
                context=context,
                reference=reference,
                expected_schema_version=source_metadata.schema_version,
                expected_identity=initial_identity,
                deadline=deadline,
            )
            self._assert_before_deadline(deadline)
            self._assert_expected_identity(
                source_path,
                source_identity,
                error_type=UnsafeSnapshotSource,
            )
            final_source_schema = _pragma_integer(
                source_connection,
                "schema_version",
            )
            if final_source_schema != source_metadata.schema_version:
                raise SnapshotValidationError(
                    "The live SQLite schema changed during snapshot acquisition."
                )
            self._assert_before_deadline(deadline)
            self._assert_no_sidecars(snapshot_path)
            self._run_failure_hook("after_validation")
            self._assert_before_deadline(deadline)
            self._assert_expected_identity(
                snapshot_path,
                initial_identity,
                error_type=SnapshotValidationError,
            )
            self._assert_no_sidecars(snapshot_path)

            source_connection.close()
            source_connection = None
            self._assert_before_deadline(deadline)
            duration_ms = self._duration_ms(started)
            result = SnapshotResult(
                reference=reference,
                created_at=timezone.now(),
                consistent=True,
                byte_count=validation["byte_count"],
                page_count=validation["page_count"],
                page_size=validation["page_size"],
                schema_version=validation["schema_version"],
                journal_mode=source_metadata.journal_mode,
                duration_ms=duration_ms,
                provider_identifier=SQLITE_SNAPSHOT_PROVIDER_IDENTIFIER,
            )
        except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
            abort_error = exc
            abort_traceback = exc.__traceback__
        except Exception as exc:
            safe_error = self._safe_error(exc, phase=phase)
        finally:
            self._close_connection(destination_connection)
            self._close_connection(source_connection)
            if (
                result is None
                and snapshot_directory_owned
                and snapshot_file_owned
                and context is not None
                and reference is not None
            ):
                try:
                    self._cleanup_owned_snapshot(
                        context=context,
                        reference=reference,
                        remove_primary=True,
                    )
                except SnapshotEngineError:
                    cleanup_incomplete = True
            elif (
                result is None
                and snapshot_directory_owned
                and context is not None
                and reference is not None
            ):
                try:
                    self._cleanup_owned_snapshot(
                        context=context,
                        reference=reference,
                        remove_primary=False,
                    )
                except SnapshotEngineError:
                    cleanup_incomplete = True

        if abort_error is not None:
            raise abort_error.with_traceback(abort_traceback)
        if safe_error is not None:
            safe_error.cleanup_incomplete = cleanup_incomplete
            safe_error.__cause__ = None
            safe_error.__context__ = None
            raise safe_error.with_traceback(None) from None
        if result is None:
            error = SnapshotCreationError()
            error.cleanup_incomplete = cleanup_incomplete
            raise error
        return result

    @contextmanager
    def open_snapshot(self, *, context, reference, _deadline=None):
        policy = (
            self.policy.validated()
            if self.policy is not None
            else SQLiteSnapshotPolicy.from_settings()
        )
        validated_context = self._validated_context(SnapshotRequest(context=context))
        validated_reference = self._validated_reference(reference)
        snapshot_path = self._snapshot_file(
            self._existing_workspace(validated_context),
            validated_reference,
            require_exists=True,
        )
        self._assert_no_sidecars(snapshot_path)
        snapshot_identity = self._regular_file_identity(
            snapshot_path,
            error_type=SnapshotValidationError,
        )
        connection = None
        try:
            if _deadline is not None:
                self._assert_before_deadline(_deadline)
            connection = self._open_connection(
                snapshot_path,
                mode="ro",
                policy=policy,
            )
            if _deadline is not None:

                def stop_after_deadline():
                    try:
                        self._assert_before_deadline(_deadline)
                    except SnapshotTimeout:
                        return 1
                    return 0

                connection.set_progress_handler(stop_after_deadline, 1000)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA foreign_keys=ON")
            if _pragma_integer(connection, "query_only") != 1:
                raise SnapshotValidationError()
            if _pragma_integer(connection, "foreign_keys") != 1:
                raise SnapshotValidationError()
            journal_row = _fetchone(connection, "PRAGMA journal_mode")
            if journal_row is None or str(journal_row[0]).lower() != "delete":
                raise SnapshotValidationError()
            try:
                connection.enable_load_extension(False)
            except AttributeError:
                pass
            connection.set_authorizer(_reader_authorizer)
            deadline_check = (
                None if _deadline is None else lambda: self._assert_before_deadline(_deadline)
            )
            yield SQLiteSnapshotReader(
                connection,
                deadline_check=deadline_check,
            )
        except SnapshotEngineError:
            raise
        except sqlite3.Error:
            if _deadline is not None:
                self._assert_before_deadline(_deadline)
            raise SnapshotValidationError() from None
        finally:
            active_exception = sys.exc_info()[0] is not None
            final_error = None
            if connection is not None:
                try:
                    if _deadline is not None:
                        connection.set_progress_handler(None, 0)
                    connection.close()
                except (sqlite3.Error, OSError):
                    final_error = SnapshotValidationError()
            try:
                self._assert_expected_identity(
                    snapshot_path,
                    snapshot_identity,
                    error_type=SnapshotValidationError,
                )
                self._assert_no_sidecars(snapshot_path)
            except SnapshotEngineError:
                try:
                    self._remove_exact_sidecars(snapshot_path)
                except SnapshotEngineError:
                    pass
                final_error = SnapshotValidationError()
            if final_error is not None and not active_exception:
                raise final_error from None

    def cleanup_snapshot(self, *, context, reference) -> bool:
        try:
            validated_context = self._validated_context(SnapshotRequest(context=context))
            validated_reference = self._validated_reference(reference)
            workspace = self._existing_workspace(validated_context)
            snapshot_parent = self._snapshot_parent(
                workspace,
                require_exists=False,
            )
            if not os.path.lexists(snapshot_parent):
                return False
            self._assert_safe_directory(snapshot_parent)
            self._assert_same_storage_device(workspace.path, snapshot_parent)
            snapshot_directory = self._snapshot_directory(
                workspace,
                validated_reference,
                require_exists=False,
            )
            if not os.path.lexists(snapshot_directory):
                return False
            self._assert_safe_directory(snapshot_directory)
            self._assert_same_storage_device(snapshot_parent, snapshot_directory)
            directory_identity = self._directory_identity(
                snapshot_directory,
                error_type=SnapshotCleanupError,
            )
            raw_snapshot_path = snapshot_directory / SNAPSHOT_FILE_NAME
            snapshot_path = contained_path(
                snapshot_directory,
                raw_snapshot_path,
            )
            targets = (snapshot_path, *self._sidecars(snapshot_path))
            existing = []
            for target in targets:
                if not os.path.lexists(target):
                    continue
                if path_is_link_like(target) or not _is_regular_file(target):
                    raise SnapshotCleanupError()
                contained_path(snapshot_directory, target)
                existing.append(target)
            exact_names = {target.name for target in targets}
            with os.scandir(snapshot_directory) as entries:
                unrelated_names = {entry.name for entry in entries if entry.name not in exact_names}
            for target in existing:
                self.unlinker(target)
            if not unrelated_names:
                self._assert_directory_identity(
                    snapshot_directory,
                    directory_identity,
                    error_type=SnapshotCleanupError,
                )
                self.directory_remover(snapshot_directory)
                if os.path.lexists(snapshot_directory):
                    raise SnapshotCleanupError()
            return bool(existing)
        except SnapshotNotFound:
            raise
        except SnapshotEngineError as exc:
            if isinstance(exc, SnapshotCleanupError):
                raise
            raise SnapshotCleanupError() from None
        except (OSError, RuntimeError, ValueError):
            raise SnapshotCleanupError() from None

    def _validated_context(self, request):
        if not isinstance(request, SnapshotRequest):
            raise SnapshotWorkspaceUnavailable()
        context = request.context
        if not isinstance(context, BackupExecutionContext) or context.workspace_reference is None:
            raise SnapshotWorkspaceUnavailable()
        return context

    @staticmethod
    def _validated_reference(reference):
        if not isinstance(reference, SnapshotReference) or not isinstance(
            reference.identifier, uuid.UUID
        ):
            raise SnapshotNotFound()
        return reference

    def _new_reference(self):
        try:
            reference = self.reference_factory()
            if isinstance(reference, uuid.UUID):
                reference = SnapshotReference(reference)
            return self._validated_reference(reference)
        except SnapshotEngineError:
            raise
        except (TypeError, ValueError, AttributeError):
            raise SnapshotCreationError() from None

    def _resolve_source_path(self):
        try:
            configuration = self.source_settings_resolver(self.using)
        except SnapshotEngineError:
            raise
        except (
            ConnectionDoesNotExist,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            sqlite3.Error,
        ):
            raise UnsupportedSnapshotBackend() from None
        if not isinstance(configuration, dict):
            raise UnsupportedSnapshotBackend()
        if configuration.get("ENGINE") != "django.db.backends.sqlite3":
            raise UnsupportedSnapshotBackend()
        name = str(configuration.get("NAME") or "").strip()
        lowered = name.lower()
        if (
            not name
            or lowered == ":memory:"
            or lowered.startswith("file:")
            or "mode=memory" in lowered
        ):
            raise UnsafeSnapshotSource()
        raw_path = Path(name).expanduser()
        if path_has_link_like_component(raw_path):
            raise UnsafeSnapshotSource()
        try:
            if not raw_path.exists() or not raw_path.is_file():
                raise UnsafeSnapshotSource()
            resolved = raw_path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise UnsafeSnapshotSource() from None
        if path_is_link_like(resolved) or not _is_regular_file(resolved):
            raise UnsafeSnapshotSource()
        return resolved

    def _existing_workspace(self, context) -> BackupWorkspace:
        try:
            self._assert_safe_directory(self.workspace_manager.root)
            self._apply_private_mode(self.workspace_manager.root, 0o700)
            workspace = self.workspace_manager.handle(context.workspace_reference)
            raw_path = self.workspace_manager.root / f"ws-{workspace.reference.identifier.hex}"
            if path_is_link_like(raw_path):
                raise SnapshotWorkspaceUnavailable()
            path = workspace.path
            if not path.exists() or not path.is_dir():
                raise SnapshotWorkspaceUnavailable()
            self._apply_private_mode(path, 0o700)
            return workspace
        except SnapshotEngineError:
            raise
        except (OSError, RuntimeError, ValueError, UnsafeWorkspacePath):
            raise SnapshotWorkspaceUnavailable() from None

    @staticmethod
    def _assert_same_storage_device(parent, child):
        try:
            parent_stat = os.stat(parent, follow_symlinks=False)
            child_stat = os.stat(child, follow_symlinks=False)
        except OSError:
            raise UnsafeStagingFilesystem() from None
        if parent_stat.st_dev != child_stat.st_dev:
            raise UnsafeStagingFilesystem()

    def _snapshot_parent(self, workspace, *, require_exists):
        raw_path = workspace.path / WorkspaceArea.SNAPSHOT.value
        if path_is_link_like(raw_path):
            raise SnapshotWorkspaceUnavailable()
        try:
            path = contained_path(workspace.path, raw_path)
        except UnsafeWorkspacePath:
            raise SnapshotWorkspaceUnavailable() from None
        if require_exists:
            self._assert_safe_directory(path)
        return path

    def _ensure_snapshot_parent(self, workspace):
        path = self._snapshot_parent(workspace, require_exists=False)
        try:
            path.mkdir(mode=0o700, exist_ok=True)
            self._assert_safe_directory(path)
            self._apply_private_mode(path, 0o700)
        except SnapshotEngineError:
            raise
        except (OSError, RuntimeError, ValueError):
            raise SnapshotWorkspaceUnavailable() from None
        return path

    def _snapshot_directory(self, workspace, reference, *, require_exists):
        parent = self._snapshot_parent(workspace, require_exists=require_exists)
        raw_path = parent / reference.identifier.hex
        if path_is_link_like(raw_path):
            raise SnapshotWorkspaceUnavailable()
        try:
            path = contained_path(parent, raw_path)
        except UnsafeWorkspacePath:
            raise SnapshotWorkspaceUnavailable() from None
        if require_exists:
            self._assert_safe_directory(path)
        return path

    def _snapshot_file(self, workspace, reference, *, require_exists):
        directory = self._snapshot_directory(
            workspace,
            reference,
            require_exists=require_exists,
        )
        raw_path = directory / SNAPSHOT_FILE_NAME
        if path_is_link_like(raw_path):
            raise SnapshotValidationError()
        try:
            path = contained_path(directory, raw_path)
        except UnsafeWorkspacePath:
            raise SnapshotValidationError() from None
        if require_exists:
            if not path.exists() or not _is_regular_file(path):
                raise SnapshotNotFound()
        return path

    @staticmethod
    def _assert_safe_directory(path):
        if not os.path.lexists(path) or path_is_link_like(path) or not Path(path).is_dir():
            raise SnapshotWorkspaceUnavailable()

    def _apply_private_mode(self, path, mode):
        try:
            self.permission_applier(path, mode)
            if os.name != "nt":
                actual = stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
                if actual != mode:
                    raise SnapshotCreationError()
        except SnapshotEngineError:
            raise
        except (OSError, RuntimeError, ValueError):
            raise SnapshotCreationError() from None

    def _reserve_snapshot_file(self, path, *, on_created):
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_BINARY", 0)
        descriptor = None
        try:
            descriptor = os.open(path, flags, 0o600)
            on_created()
            identity = os.fstat(descriptor)
            if not stat.S_ISREG(identity.st_mode):
                raise SnapshotCreationError()
            return identity.st_dev, identity.st_ino
        except SnapshotEngineError:
            raise
        except (FileExistsError, OSError, RuntimeError, ValueError):
            raise SnapshotCreationError() from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    if sys.exc_info()[0] is None:
                        raise SnapshotCreationError() from None

    @staticmethod
    def _regular_file_identity(path, *, error_type):
        try:
            current = os.stat(path, follow_symlinks=False)
        except OSError:
            raise error_type() from None
        if path_is_link_like(path) or not stat.S_ISREG(current.st_mode):
            raise error_type()
        return current.st_dev, current.st_ino

    @classmethod
    def _assert_expected_identity(cls, path, expected, *, error_type):
        if cls._regular_file_identity(path, error_type=error_type) != expected:
            raise error_type()

    @classmethod
    def _assert_file_identity(cls, path, expected):
        cls._assert_expected_identity(
            path,
            expected,
            error_type=SnapshotCreationError,
        )

    @staticmethod
    def _directory_identity(path, *, error_type):
        try:
            current = os.stat(path, follow_symlinks=False)
        except OSError:
            raise error_type() from None
        if path_is_link_like(path) or not stat.S_ISDIR(current.st_mode):
            raise error_type()
        return current.st_dev, current.st_ino

    @classmethod
    def _assert_directory_identity(cls, path, expected, *, error_type):
        if cls._directory_identity(path, error_type=error_type) != expected:
            raise error_type()

    def _open_connection(self, path, *, mode, policy):
        uri = f"{Path(path).as_uri()}?mode={mode}&cache=private"
        return self.connection_factory(
            uri,
            uri=True,
            timeout=policy.busy_timeout_seconds,
            isolation_level=None,
            check_same_thread=True,
        )

    def _assess_source(
        self,
        connection,
        *,
        source_path,
        expected_identity,
        policy,
    ):
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA foreign_keys=ON")
            if _pragma_integer(connection, "query_only") != 1:
                raise SQLiteSnapshotPolicyError()
            if _pragma_integer(connection, "foreign_keys") != 1:
                raise SQLiteSnapshotPolicyError()
            journal_row = _fetchone(connection, "PRAGMA journal_mode")
            journal_mode = str(journal_row[0] if journal_row else "").upper()
            if journal_mode != policy.required_journal_mode:
                raise SQLiteSnapshotPolicyError()
            synchronous_level = _pragma_integer(connection, "synchronous")
            if synchronous_level not in SQLITE_SYNCHRONOUS_LEVELS.values():
                raise SQLiteSnapshotPolicyError()
            required_level = SQLITE_SYNCHRONOUS_LEVELS[policy.required_synchronous]
            if synchronous_level < required_level:
                raise SQLiteSnapshotPolicyError()
            schema_version = _pragma_integer(connection, "schema_version")
            page_count = _pragma_integer(connection, "page_count")
            page_size = _pragma_integer(connection, "page_size")
            if schema_version < 0 or page_count <= 0 or not _valid_sqlite_page_size(page_size):
                raise SQLiteSnapshotPolicyError()
            databases = _fetchall(connection, "PRAGMA database_list")
            if len(databases) != 1 or str(databases[0][1]) != "main":
                raise SQLiteSnapshotPolicyError()
            configured_database = Path(str(databases[0][2] or ""))
            if not configured_database.is_absolute() or path_has_link_like_component(
                configured_database
            ):
                raise SQLiteSnapshotPolicyError()
            try:
                connected_source = configured_database.resolve(strict=True)
            except (OSError, RuntimeError):
                raise SQLiteSnapshotPolicyError() from None
            if connected_source != source_path:
                raise SQLiteSnapshotPolicyError()
            self._assert_expected_identity(
                connected_source,
                expected_identity,
                error_type=SQLiteSnapshotPolicyError,
            )
            wal_bytes = self._safe_wal_size(source_path)
            return _SourceMetadata(
                journal_mode=journal_mode.lower(),
                synchronous_level=synchronous_level,
                schema_version=schema_version,
                page_count=page_count,
                page_size=page_size,
                wal_bytes=wal_bytes,
            )
        except SnapshotEngineError:
            raise
        except (sqlite3.Error, KeyError, TypeError, ValueError):
            raise SQLiteSnapshotPolicyError() from None

    @staticmethod
    def _safe_wal_size(source_path):
        wal_path = Path(f"{source_path}-wal")
        if not os.path.lexists(wal_path):
            return 0
        if path_is_link_like(wal_path) or not _is_regular_file(wal_path):
            raise UnsafeSnapshotSource()
        try:
            return max(0, int(os.stat(wal_path, follow_symlinks=False).st_size))
        except OSError:
            raise UnsafeSnapshotSource() from None

    def _progress_callback(self, deadline):
        def progress(status, remaining, total):
            del status
            self._assert_before_deadline(deadline)
            if self.progress_hook is not None:
                self.progress_hook(int(remaining), int(total))
            self._assert_before_deadline(deadline)

        return progress

    def _assert_before_deadline(self, deadline):
        try:
            if self._monotonic_value() > deadline:
                raise SnapshotTimeout()
        except SnapshotTimeout:
            raise
        except (TypeError, ValueError, OverflowError):
            raise SnapshotTimeout() from None

    def _monotonic_value(self):
        try:
            value = float(self.monotonic())
        except (TypeError, ValueError, OverflowError):
            raise SnapshotTimeout() from None
        if not math.isfinite(value):
            raise SnapshotTimeout()
        return value

    @staticmethod
    def _normalize_destination_journal(connection):
        try:
            row = _fetchone(connection, "PRAGMA journal_mode=DELETE")
        except sqlite3.Error:
            raise SnapshotCreationError() from None
        if row is None or str(row[0]).lower() != "delete":
            raise SnapshotCreationError()

    @staticmethod
    def _normalize_destination_schema_version(connection, expected):
        """Restore the source schema cookie after SQLite invalidates the target."""

        try:
            schema_version = int(expected)
            if schema_version < 0:
                raise ValueError
            connection.execute(f"PRAGMA schema_version={schema_version}")
            if _pragma_integer(connection, "schema_version") != schema_version:
                raise SnapshotCreationError()
        except SnapshotEngineError:
            raise
        except (sqlite3.Error, TypeError, ValueError, OverflowError):
            raise SnapshotCreationError() from None

    def _validate_snapshot(
        self,
        *,
        context,
        reference,
        expected_schema_version,
        expected_identity,
        deadline,
    ):
        workspace = self._existing_workspace(context)
        path = self._snapshot_file(workspace, reference, require_exists=True)
        self._assert_expected_identity(
            path,
            expected_identity,
            error_type=SnapshotValidationError,
        )
        try:
            file_stat = os.stat(path, follow_symlinks=False)
        except OSError:
            raise SnapshotValidationError() from None
        if path_is_link_like(path) or not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size <= 0:
            raise SnapshotValidationError()
        self._apply_private_mode(path, 0o600)
        self._assert_before_deadline(deadline)

        with self.open_snapshot(
            context=context,
            reference=reference,
            _deadline=deadline,
        ) as reader:
            if int(reader.scalar("PRAGMA query_only")) != 1:
                raise SnapshotValidationError()
            if int(reader.scalar("PRAGMA foreign_keys")) != 1:
                raise SnapshotValidationError()
            if str(reader.scalar("PRAGMA journal_mode")).lower() != "delete":
                raise SnapshotValidationError()
            if tuple(self.quick_check_runner(reader)) != (("ok",),):
                raise SnapshotValidationError()
            self._assert_before_deadline(deadline)
            foreign_key_issue = self.foreign_key_check_runner(reader)
            if foreign_key_issue:
                raise SnapshotValidationError()
            self._assert_before_deadline(deadline)
            schema_rows = reader.query("SELECT count(*) FROM sqlite_schema")
            if len(schema_rows) != 1 or len(schema_rows[0]) != 1 or int(schema_rows[0][0]) < 0:
                raise SnapshotValidationError()
            schema_version = int(reader.scalar("PRAGMA schema_version"))
            page_count = int(reader.scalar("PRAGMA page_count"))
            page_size = int(reader.scalar("PRAGMA page_size"))
            if (
                schema_version != expected_schema_version
                or page_count <= 0
                or not _valid_sqlite_page_size(page_size)
            ):
                raise SnapshotValidationError()
            self._assert_before_deadline(deadline)
        self._assert_expected_identity(
            path,
            expected_identity,
            error_type=SnapshotValidationError,
        )
        self._assert_no_sidecars(path)
        try:
            final_stat = os.stat(path, follow_symlinks=False)
        except OSError:
            raise SnapshotValidationError() from None
        if (
            not stat.S_ISREG(final_stat.st_mode)
            or final_stat.st_size <= 0
            or final_stat.st_size != page_count * page_size
        ):
            raise SnapshotValidationError()
        self._assert_before_deadline(deadline)
        return {
            "byte_count": int(final_stat.st_size),
            "page_count": page_count,
            "page_size": page_size,
            "schema_version": schema_version,
        }

    @staticmethod
    def _sidecars(snapshot_path):
        return tuple(Path(f"{snapshot_path}{suffix}") for suffix in SNAPSHOT_SIDECAR_SUFFIXES)

    def _assert_no_sidecars(self, snapshot_path):
        for sidecar in self._sidecars(snapshot_path):
            if os.path.lexists(sidecar):
                raise SnapshotValidationError(
                    "The temporary SQLite snapshot has an unexpected sidecar."
                )

    def _remove_exact_sidecars(self, snapshot_path):
        directory = Path(snapshot_path).parent
        try:
            self._assert_safe_directory(directory)
            existing = []
            for sidecar in self._sidecars(snapshot_path):
                if not os.path.lexists(sidecar):
                    continue
                if path_is_link_like(sidecar) or not _is_regular_file(sidecar):
                    raise SnapshotCleanupError()
                contained_path(directory, sidecar)
                existing.append(sidecar)
            for sidecar in existing:
                self.unlinker(sidecar)
        except SnapshotEngineError as exc:
            if isinstance(exc, SnapshotCleanupError):
                raise
            raise SnapshotCleanupError() from None
        except (OSError, RuntimeError, ValueError):
            raise SnapshotCleanupError() from None

    def _cleanup_owned_snapshot(self, *, context, reference, remove_primary):
        try:
            workspace = self._existing_workspace(context)
            parent = self._snapshot_parent(workspace, require_exists=True)
            self._assert_same_storage_device(workspace.path, parent)
            directory = self._snapshot_directory(
                workspace,
                reference,
                require_exists=True,
            )
            self._assert_same_storage_device(parent, directory)
            directory_identity = self._directory_identity(
                directory,
                error_type=SnapshotCleanupError,
            )
            primary = self._snapshot_file(
                workspace,
                reference,
                require_exists=False,
            )
            targets = (primary, *self._sidecars(primary)) if remove_primary else ()
            existing = []
            for target in targets:
                if not os.path.lexists(target):
                    continue
                if path_is_link_like(target) or not _is_regular_file(target):
                    raise SnapshotCleanupError()
                contained_path(directory, target)
                existing.append(target)
            allowed_names = {target.name for target in targets}
            with os.scandir(directory) as entries:
                if any(entry.name not in allowed_names for entry in entries):
                    raise SnapshotCleanupError()
            for target in existing:
                self.unlinker(target)
            self._assert_directory_identity(
                directory,
                directory_identity,
                error_type=SnapshotCleanupError,
            )
            self.directory_remover(directory)
            if os.path.lexists(directory):
                raise SnapshotCleanupError()
        except SnapshotEngineError as exc:
            if isinstance(exc, SnapshotCleanupError):
                raise
            raise SnapshotCleanupError() from None
        except (OSError, RuntimeError, ValueError):
            raise SnapshotCleanupError() from None

    def _run_failure_hook(self, stage):
        if self.failure_hook is not None:
            self.failure_hook(stage)

    def _duration_ms(self, started):
        try:
            elapsed = max(0.0, self._monotonic_value() - started)
        except (TypeError, ValueError, OverflowError):
            return 0
        return min(int(elapsed * 1000), 3_600_000)

    @staticmethod
    def _close_connection(connection):
        if connection is None:
            return
        try:
            connection.close()
        except (sqlite3.Error, OSError):
            pass

    @staticmethod
    def _safe_error(exc, *, phase):
        if isinstance(exc, SnapshotEngineError):
            return exc
        if isinstance(exc, sqlite3.Error):
            code = getattr(exc, "sqlite_errorcode", None)
            if isinstance(code, int):
                base_code = code & 0xFF
                if base_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    return SnapshotBusy()
                if base_code == sqlite3.SQLITE_FULL:
                    return InsufficientSnapshotCapacity()
        if phase == "validation":
            return SnapshotValidationError()
        return SnapshotCreationError()
