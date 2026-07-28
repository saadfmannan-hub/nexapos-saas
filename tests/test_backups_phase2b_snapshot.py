"""Focused safety tests for the Phase 2B SQLite snapshot provider."""

import os
import sqlite3
import stat
import tempfile
import threading
import uuid
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.backups.engine.availability import (
    OPERATIONAL_PROVIDER_STACK_READY,
    SQLITE_SNAPSHOT_PROVIDER_READY,
    get_engine_capability,
    real_execution_available,
)
from apps.backups.engine.checks import check_sqlite_snapshot_policy_settings
from apps.backups.engine.context import (
    ActorIdentitySnapshot,
    BackupExecutionContext,
)
from apps.backups.engine.contracts import (
    SnapshotProvider,
    SnapshotReference,
    SnapshotRequest,
)
from apps.backups.engine.exceptions import (
    BackupEngineDisabled,
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
from apps.backups.engine.orchestration import prepare_backup_execution
from apps.backups.engine.pipeline import (
    PipelineStage,
    PipelineStageState,
    planning_stage_reports,
)
from apps.backups.engine.snapshot_policy import (
    LocalFilesystemInspector,
    SQLiteSnapshotPolicy,
    StorageAssessment,
    required_staging_capacity,
)
from apps.backups.engine.sqlite_snapshot import (
    SNAPSHOT_FILE_NAME,
    SNAPSHOT_SIDECAR_SUFFIXES,
    SQLITE_SNAPSHOT_PROVIDER_IDENTIFIER,
    SQLiteSnapshotProvider,
)
from apps.backups.engine.workspace import (
    BackupWorkspaceManager,
    WorkspaceArea,
    WorkspaceReference,
)
from apps.backups.enums import (
    BackupScope,
    BackupStatus,
    BackupTrigger,
    IntegrityStatus,
    ProductOwner,
)
from apps.backups.tasks import (
    check_backup_async_execution_configuration,
    execute_backup,
)

from .test_backups_phase1 import BackupPhase1TestCase


class _StaticFilesystemInspector:
    def __init__(self, assessment=None):
        self.assessment = assessment or StorageAssessment(
            True,
            "local-test",
            "test",
        )
        self.paths = []

    def assess(self, path):
        self.paths.append(Path(path))
        return self.assessment


class _ConnectionProxy:
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)


class _ForeignKeysOffConnection(_ConnectionProxy):
    def execute(self, sql, parameters=()):
        normalized = " ".join(str(sql).lower().split())
        if normalized == "pragma foreign_keys=on":
            return self.connection.execute("PRAGMA foreign_keys=OFF")
        if normalized == "pragma foreign_keys":
            return self.connection.execute("SELECT 0")
        return self.connection.execute(sql, parameters)


class _PragmaOverrideConnection(_ConnectionProxy):
    def __init__(self, connection, **overrides):
        super().__init__(connection)
        self.overrides = {
            " ".join(str(name).lower().split()): int(value) for name, value in overrides.items()
        }

    def execute(self, sql, parameters=()):
        normalized = " ".join(str(sql).lower().split())
        if normalized in self.overrides:
            return self.connection.execute(f"SELECT {self.overrides[normalized]}")
        return self.connection.execute(sql, parameters)


class _BackupFailureConnection(_ConnectionProxy):
    def __init__(self, connection, error):
        super().__init__(connection)
        self.error = error

    def backup(self, target, **kwargs):
        del target, kwargs
        raise self.error


class _CloseFailureConnection(_ConnectionProxy):
    def close(self):
        self.connection.close()
        raise sqlite3.OperationalError("private close failure")


class SQLiteSnapshotTestCase(SimpleTestCase):
    """Real file-backed SQLite fixture independent of Django's test database."""

    def setUp(self):
        super().setUp()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source_path = self.root / "source.sqlite3"
        self.staging_root = self.root / "private-staging"
        self.manager = BackupWorkspaceManager(root=self.staging_root)
        self.workspace = self.manager.create()
        self.context_without_workspace = BackupExecutionContext(
            backup_public_id=uuid.uuid4(),
            business_id=17,
            business_public_id=uuid.uuid4(),
            requested_scope=BackupScope.POS,
            resolved_products=(ProductOwner.POS,),
            trigger_type=BackupTrigger.MANUAL,
            actor_identity=ActorIdentitySnapshot(
                public_id="actor-opaque",
                email="owner@example.test",
                full_name="Sensitive Tenant Name",
                actor_type="TENANT",
                platform_staff=False,
            ),
            application_version="phase2b-test",
            backup_format_version="2b-test",
            schema_migration_fingerprint="opaque-fingerprint",
            minimum_restore_version="phase2b-test",
            idempotency_key="opaque-idempotency",
            operation_correlation_id=uuid.uuid4(),
        )
        self.context = self.context_without_workspace.with_workspace(self.workspace.reference)
        self.source = sqlite3.connect(
            self.source_path,
            isolation_level=None,
            check_same_thread=False,
        )
        self.assertEqual(
            self.source.execute("PRAGMA journal_mode=WAL").fetchone(),
            ("wal",),
        )
        self.source.execute("PRAGMA synchronous=FULL")
        self.source.execute("PRAGMA wal_autocheckpoint=0")
        self.source.execute("PRAGMA foreign_keys=ON")
        self.source.executescript("""
            CREATE TABLE parent (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE child (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER NOT NULL REFERENCES parent(id),
                value INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE marker (
                id INTEGER PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE pair_state (
                id INTEGER PRIMARY KEY,
                value INTEGER NOT NULL
            );
            INSERT INTO parent (id, name) VALUES (1, 'committed-wal-row');
            INSERT INTO child (id, parent_id, value) VALUES (1, 1, 0);
            INSERT INTO marker (id, value) VALUES (1, 'baseline');
            INSERT INTO pair_state (id, value) VALUES (1, 0), (2, 0);
            """)
        self.threads = []

    def tearDown(self):
        for thread in self.threads:
            if thread.is_alive():
                thread.join(timeout=5)
        if self.source is not None:
            self.source.close()
            self.source = None
        self.temporary_directory.cleanup()
        super().tearDown()

    def policy(self, **changes):
        values = {
            "required_journal_mode": "WAL",
            "required_synchronous": "FULL",
            "busy_timeout_seconds": 1.0,
            "pages_per_step": 4,
            "backup_sleep_seconds": 0.001,
            "snapshot_timeout_seconds": 30.0,
            "minimum_free_bytes": 1,
            "headroom_multiplier": 1.0,
            "require_local_staging": True,
        }
        values.update(changes)
        return SQLiteSnapshotPolicy(**values)

    def source_configuration(self, path=None, engine="django.db.backends.sqlite3"):
        return {
            "ENGINE": engine,
            "NAME": str(path or self.source_path),
        }

    def provider(self, **changes):
        values = {
            "workspace_manager": self.manager,
            "policy": self.policy(),
            "source_settings_resolver": (lambda using: self.source_configuration()),
            "filesystem_inspector": _StaticFilesystemInspector(),
            "disk_usage_provider": (lambda path: SimpleNamespace(free=10**12)),
        }
        values.update(changes)
        return SQLiteSnapshotProvider(**values)

    def create_snapshot(self, provider=None, context=None):
        selected_provider = provider or self.provider()
        selected_context = context or self.context
        result = selected_provider.create_snapshot(SnapshotRequest(context=selected_context))
        return selected_provider, selected_context, result

    def snapshot_directory(self, result, workspace=None):
        selected_workspace = workspace or self.workspace
        return (
            selected_workspace.path / WorkspaceArea.SNAPSHOT.value / result.reference.identifier.hex
        )

    def snapshot_path(self, result, workspace=None):
        return self.snapshot_directory(result, workspace) / SNAPSHOT_FILE_NAME

    def assert_no_snapshot_files(self):
        if not self.staging_root.exists():
            return
        snapshot_parent = self.workspace.path / WorkspaceArea.SNAPSHOT.value
        if snapshot_parent.exists():
            self.assertFalse(
                any(
                    child.is_dir()
                    and len(child.name) == 32
                    and all(character in "0123456789abcdef" for character in child.name.lower())
                    for child in snapshot_parent.iterdir()
                )
            )
        self.assertFalse(
            any(
                path.name == SNAPSHOT_FILE_NAME
                or any(path.name.endswith(suffix) for suffix in SNAPSHOT_SIDECAR_SUFFIXES)
                for path in self.staging_root.rglob("*")
            )
        )

    def assert_sanitized_exception(self, error):
        rendered = " ".join(
            (
                str(error),
                repr(error),
                " ".join(getattr(error, "messages", ())),
            )
        )
        self.assertNotIn(str(self.source_path), rendered)
        self.assertNotIn(str(self.staging_root), rendered)
        self.assertNotIn("private raw sqlite detail", rendered)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)


class SQLiteSnapshotContractTests(SQLiteSnapshotTestCase):
    def test_real_snapshot_result_is_opaque_frozen_and_schema_exact(self):
        source_schema = self.source.execute("PRAGMA schema_version").fetchone()[0]
        provider, context, result = self.create_snapshot()

        self.assertIsInstance(provider, SnapshotProvider)
        self.assertIsInstance(result.reference.identifier, uuid.UUID)
        self.assertTrue(result.consistent)
        self.assertEqual(result.schema_version, source_schema)
        self.assertEqual(
            result.provider_identifier,
            SQLITE_SNAPSHOT_PROVIDER_IDENTIFIER,
        )
        self.assertEqual(result.journal_mode, "wal")
        self.assertGreater(result.byte_count, 0)
        self.assertEqual(result.byte_count, result.page_count * result.page_size)
        self.assertGreaterEqual(result.duration_ms, 0)
        self.assertLessEqual(result.duration_ms, 3_600_000)
        self.assertIsNotNone(result.created_at.tzinfo)

        serialized = repr(asdict(result))
        self.assertNotIn(str(self.source_path), serialized)
        self.assertNotIn(str(self.staging_root), serialized)
        self.assertFalse(
            {"path", "filename", "source_path", "destination_path"} & set(asdict(result))
        )
        context_text = repr(asdict(context))
        self.assertNotIn(str(self.source_path), context_text)
        self.assertNotIn(str(self.staging_root), context_text)
        with self.assertRaises(FrozenInstanceError):
            result.consistent = False
        with self.assertRaises(FrozenInstanceError):
            result.reference.identifier = uuid.uuid4()

        self.assertTrue(
            provider.cleanup_snapshot(
                context=context,
                reference=result.reference,
            )
        )

    def test_consistency_is_never_returned_after_validation_failure(self):
        provider = self.provider(quick_check_runner=lambda reader: (("not ok",),))

        with self.assertRaises(SnapshotValidationError):
            provider.create_snapshot(SnapshotRequest(context=self.context))

        self.assert_no_snapshot_files()

    def test_online_backup_uses_no_shutil_copy_api(self):
        forbidden = AssertionError("raw filesystem copy was called")
        with (
            mock.patch("shutil.copy", side_effect=forbidden) as copy,
            mock.patch("shutil.copy2", side_effect=forbidden) as copy2,
            mock.patch("shutil.copyfile", side_effect=forbidden) as copyfile,
            mock.patch("shutil.copytree", side_effect=forbidden) as copytree,
        ):
            provider, context, result = self.create_snapshot()

        copy.assert_not_called()
        copy2.assert_not_called()
        copyfile.assert_not_called()
        copytree.assert_not_called()
        provider.cleanup_snapshot(context=context, reference=result.reference)


class SQLiteSnapshotWalConcurrencyTests(SQLiteSnapshotTestCase):
    def test_committed_wal_data_is_present_without_live_checkpoint(self):
        source_statements = []
        provider_connection_count = 0

        def traced_factory(database, **kwargs):
            nonlocal provider_connection_count
            connection = sqlite3.connect(database, **kwargs)
            if provider_connection_count == 0:
                connection.set_trace_callback(source_statements.append)
            provider_connection_count += 1
            return connection

        wal_path = Path(f"{self.source_path}-wal")
        self.assertTrue(wal_path.exists())
        wal_size_before = wal_path.stat().st_size
        provider, context, result = self.create_snapshot(
            self.provider(connection_factory=traced_factory)
        )
        wal_size_after = wal_path.stat().st_size

        with provider.open_snapshot(
            context=context,
            reference=result.reference,
        ) as reader:
            self.assertEqual(
                reader.query("SELECT name FROM parent WHERE id=1"),
                (("committed-wal-row",),),
            )
        self.source.execute("INSERT INTO marker (id, value) VALUES (2, 'after-snapshot')")
        self.assertEqual(
            self.source.execute("SELECT value FROM marker WHERE id=2").fetchone(),
            ("after-snapshot",),
        )
        normalized = "\n".join(source_statements).lower()
        self.assertTrue(source_statements)
        self.assertIn("pragma journal_mode", normalized)
        self.assertNotIn("wal_checkpoint", normalized)
        self.assertNotIn("vacuum", normalized)
        self.assertNotIn("journal_mode=", normalized.replace(" ", ""))
        self.assertNotIn("begin immediate", normalized)
        self.assertNotIn("begin exclusive", normalized)
        self.assertGreater(wal_size_before, 0)
        self.assertGreaterEqual(wal_size_after, wal_size_before)
        provider.cleanup_snapshot(context=context, reference=result.reference)

    def test_uncommitted_transaction_is_absent_and_related_updates_are_atomic(self):
        staged = threading.Event()
        release = threading.Event()
        thread_errors = []

        def hold_uncommitted_transaction():
            connection = None
            try:
                connection = sqlite3.connect(
                    self.source_path,
                    isolation_level=None,
                )
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("INSERT INTO marker (id, value) VALUES (99, 'uncommitted')")
                connection.execute("UPDATE pair_state SET value=9 WHERE id IN (1, 2)")
                staged.set()
                if not release.wait(timeout=10):
                    raise AssertionError("release event was not signalled")
                connection.execute("ROLLBACK")
            except BaseException as exc:  # test thread evidence
                thread_errors.append(exc)
                staged.set()
            finally:
                if connection is not None:
                    connection.close()

        thread = threading.Thread(target=hold_uncommitted_transaction)
        self.threads.append(thread)
        thread.start()
        self.assertTrue(staged.wait(timeout=10))

        provider, context, result = self.create_snapshot()
        with provider.open_snapshot(
            context=context,
            reference=result.reference,
        ) as reader:
            self.assertEqual(
                reader.scalar("SELECT count(*) FROM marker WHERE id=99"),
                0,
            )
            self.assertEqual(
                reader.query("SELECT value FROM pair_state ORDER BY id"),
                ((0,), (0,)),
            )

        release.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(thread_errors, [])
        provider.cleanup_snapshot(context=context, reference=result.reference)

    def test_concurrent_committed_writer_keeps_snapshot_structurally_valid(self):
        self.source.execute("CREATE TABLE filler (id INTEGER PRIMARY KEY, payload BLOB NOT NULL)")
        self.source.executemany(
            "INSERT INTO filler (payload) VALUES (?)",
            ((b"x" * 3000,) for _ in range(160)),
        )
        self.assertGreater(
            self.source.execute("PRAGMA page_count").fetchone()[0],
            100,
        )
        start_writer = threading.Event()
        writer_done = threading.Event()
        thread_errors = []

        def committed_writer():
            connection = None
            try:
                if not start_writer.wait(timeout=10):
                    raise AssertionError("writer was not released")
                connection = sqlite3.connect(
                    self.source_path,
                    isolation_level=None,
                )
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE pair_state SET value=1 WHERE id IN (1, 2)")
                connection.execute("INSERT INTO marker (id, value) VALUES (3, 'concurrent')")
                connection.execute("COMMIT")
            except BaseException as exc:  # test thread evidence
                thread_errors.append(exc)
            finally:
                if connection is not None:
                    connection.close()
                writer_done.set()

        thread = threading.Thread(target=committed_writer)
        self.threads.append(thread)
        thread.start()
        progress_seen = False

        def progress_hook(remaining, total):
            nonlocal progress_seen
            del remaining, total
            if not progress_seen:
                progress_seen = True
                start_writer.set()
                if not writer_done.wait(timeout=10):
                    raise AssertionError("writer did not commit")

        provider = self.provider(
            policy=self.policy(pages_per_step=1),
            progress_hook=progress_hook,
        )
        provider, context, result = self.create_snapshot(provider)
        thread.join(timeout=10)

        self.assertTrue(progress_seen)
        self.assertFalse(thread.is_alive())
        self.assertEqual(thread_errors, [])
        with provider.open_snapshot(
            context=context,
            reference=result.reference,
        ) as reader:
            pair = reader.query("SELECT value FROM pair_state ORDER BY id")
            self.assertIn(pair, (((0,), (0,)), ((1,), (1,))))
            self.assertEqual(reader.query("PRAGMA quick_check(1)"), (("ok",),))
            self.assertEqual(reader.query("PRAGMA foreign_key_check"), ())
        provider.cleanup_snapshot(context=context, reference=result.reference)


class SQLiteSnapshotPolicyTests(SQLiteSnapshotTestCase):
    def test_invalid_source_configurations_fail_closed(self):
        directory_source = self.root / "source-directory"
        directory_source.mkdir()
        cases = (
            (
                lambda using: self.source_configuration(engine="django.db.backends.postgresql"),
                UnsupportedSnapshotBackend,
            ),
            (
                lambda using: self.source_configuration(path=":memory:"),
                UnsafeSnapshotSource,
            ),
            (
                lambda using: self.source_configuration(path="file:memorydb?mode=memory"),
                UnsafeSnapshotSource,
            ),
            (
                lambda using: self.source_configuration(path=self.root / "missing.sqlite3"),
                UnsafeSnapshotSource,
            ),
            (
                lambda using: self.source_configuration(path=directory_source),
                UnsafeSnapshotSource,
            ),
            (
                lambda using: (_ for _ in ()).throw(KeyError("unknown")),
                UnsupportedSnapshotBackend,
            ),
        )
        for resolver, error_type in cases:
            with self.subTest(error_type=error_type.__name__):
                with self.assertRaises(error_type):
                    self.provider(source_settings_resolver=resolver).create_snapshot(
                        SnapshotRequest(context=self.context)
                    )

    def test_source_symlink_is_rejected(self):
        symlink_path = self.root / "source-link.sqlite3"
        try:
            os.symlink(self.source_path, symlink_path)
        except OSError:
            with mock.patch(
                "apps.backups.engine.sqlite_snapshot." "path_has_link_like_component",
                return_value=True,
            ):
                with self.assertRaises(UnsafeSnapshotSource):
                    self.provider().create_snapshot(SnapshotRequest(context=self.context))
        else:
            with self.assertRaises(UnsafeSnapshotSource):
                self.provider(
                    source_settings_resolver=(
                        lambda using: self.source_configuration(path=symlink_path)
                    )
                ).create_snapshot(SnapshotRequest(context=self.context))

    def test_journal_mismatch_is_rejected_without_reconfiguration(self):
        delete_path = self.root / "delete-mode.sqlite3"
        connection = sqlite3.connect(delete_path, isolation_level=None)
        try:
            connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
            self.assertEqual(
                connection.execute("PRAGMA journal_mode").fetchone(),
                ("delete",),
            )
        finally:
            connection.close()

        provider = self.provider(
            source_settings_resolver=(lambda using: self.source_configuration(path=delete_path))
        )
        with self.assertRaises(SQLiteSnapshotPolicyError):
            provider.create_snapshot(SnapshotRequest(context=self.context))

        connection = sqlite3.connect(delete_path)
        try:
            self.assertEqual(
                connection.execute("PRAGMA journal_mode").fetchone(),
                ("delete",),
            )
        finally:
            connection.close()

    def test_synchronous_policy_mismatch_fails_closed(self):
        for reported_level in (1, 999):
            connection_count = 0

            def connection_factory(database, _level=reported_level, **kwargs):
                nonlocal connection_count
                connection = sqlite3.connect(database, **kwargs)
                connection_count += 1
                if connection_count == 1:
                    return _PragmaOverrideConnection(
                        connection,
                        **{"pragma synchronous": _level},
                    )
                return connection

            with self.subTest(reported_level=reported_level):
                with self.assertRaises(SQLiteSnapshotPolicyError):
                    self.provider(connection_factory=connection_factory).create_snapshot(
                        SnapshotRequest(context=self.context)
                    )
                self.assert_no_snapshot_files()

    def test_invalid_page_size_fails_closed(self):
        connection_count = 0

        def connection_factory(database, **kwargs):
            nonlocal connection_count
            connection = sqlite3.connect(database, **kwargs)
            connection_count += 1
            if connection_count == 1:
                return _PragmaOverrideConnection(
                    connection,
                    **{"pragma page_size": 1000},
                )
            return connection

        with self.assertRaises(SQLiteSnapshotPolicyError):
            self.provider(connection_factory=connection_factory).create_snapshot(
                SnapshotRequest(context=self.context)
            )
        self.assert_no_snapshot_files()

    def test_foreign_key_enablement_failure_fails_closed(self):
        def connection_factory(database, **kwargs):
            return _ForeignKeysOffConnection(sqlite3.connect(database, **kwargs))

        with self.assertRaises(SQLiteSnapshotPolicyError):
            self.provider(connection_factory=connection_factory).create_snapshot(
                SnapshotRequest(context=self.context)
            )
        self.assert_no_snapshot_files()

    def test_invalid_numeric_policies_are_rejected(self):
        invalid_changes = (
            {"busy_timeout_seconds": 0},
            {"pages_per_step": 0},
            {"pages_per_step": 1.5},
            {"backup_sleep_seconds": -1},
            {"snapshot_timeout_seconds": 0},
            {"minimum_free_bytes": 0},
            {"headroom_multiplier": 0.5},
            {
                "busy_timeout_seconds": 2,
                "snapshot_timeout_seconds": 1,
            },
            {
                "backup_sleep_seconds": 2,
                "snapshot_timeout_seconds": 1,
            },
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes):
                with self.assertRaises(SQLiteSnapshotPolicyError):
                    self.policy(**changes).validated()

    @override_settings(BACKUP_SQLITE_BACKUP_PAGES_PER_STEP=0)
    def test_invalid_settings_have_a_stable_system_check(self):
        errors = check_sqlite_snapshot_policy_settings(None)
        self.assertEqual([error.id for error in errors], ["backups.E021"])

    def test_storage_inspector_never_claims_unknown_or_remote_is_local(self):
        fixed = LocalFilesystemInspector(
            platform_name="Windows",
            windows_drive_type=lambda root: 3,
        ).assess(self.root)
        remote = LocalFilesystemInspector(
            platform_name="Windows",
            windows_drive_type=lambda root: 4,
        ).assess(self.root)
        unknown = LocalFilesystemInspector(platform_name="UnknownOS").assess(self.root)

        self.assertTrue(fixed.confirmed_local)
        self.assertFalse(remote.confirmed_local)
        self.assertEqual(remote.classification, "network")
        self.assertFalse(unknown.confirmed_local)
        self.assertEqual(unknown.classification, "unknown")

    def test_capacity_formula_is_conservative_and_exact(self):
        policy = self.policy(
            minimum_free_bytes=1000,
            headroom_multiplier=2.5,
        ).validated()
        self.assertEqual(
            required_staging_capacity(
                page_count=2,
                page_size=512,
                wal_bytes=100,
                policy=policy,
            ),
            2810,
        )
        policy = replace(policy, minimum_free_bytes=5000)
        self.assertEqual(
            required_staging_capacity(
                page_count=2,
                page_size=512,
                wal_bytes=100,
                policy=policy,
            ),
            5000,
        )


class SQLiteSnapshotWorkspaceTests(SQLiteSnapshotTestCase):
    def test_missing_or_nonexistent_workspace_is_rejected(self):
        with self.assertRaises(SnapshotWorkspaceUnavailable):
            self.provider().create_snapshot(SnapshotRequest(context=self.context_without_workspace))
        with self.assertRaises(UnsafeWorkspacePath):
            self.context_without_workspace.with_workspace("../escape")
        nonexistent = self.context_without_workspace.with_workspace(WorkspaceReference.new())
        with self.assertRaises(SnapshotWorkspaceUnavailable):
            self.provider().create_snapshot(SnapshotRequest(context=nonexistent))

    def test_snapshot_location_and_names_are_engine_controlled(self):
        provider, context, result = self.create_snapshot()
        snapshot_path = self.snapshot_path(result)
        self.assertTrue(snapshot_path.is_file())
        self.assertEqual(snapshot_path.name, SNAPSHOT_FILE_NAME)
        self.assertEqual(snapshot_path.parent.name, result.reference.identifier.hex)
        self.assertEqual(
            snapshot_path.parent.parent.name,
            WorkspaceArea.SNAPSHOT.value,
        )
        rendered = str(snapshot_path).lower()
        self.assertNotIn("sensitive tenant name", rendered)
        self.assertNotIn("owner@example.test", rendered)
        self.assertTrue(snapshot_path.resolve().is_relative_to(self.workspace.path.resolve()))
        provider.cleanup_snapshot(context=context, reference=result.reference)

    def test_private_permission_policy_is_applied(self):
        applied = []

        def permission_applier(path, mode):
            applied.append((Path(path), mode))
            os.chmod(path, mode)

        provider, context, result = self.create_snapshot(
            self.provider(permission_applier=permission_applier)
        )
        snapshot_path = self.snapshot_path(result)
        snapshot_directory = snapshot_path.parent
        snapshot_parent = snapshot_directory.parent
        self.assertIn((snapshot_path, 0o600), applied)
        self.assertIn((snapshot_directory, 0o700), applied)
        self.assertIn((snapshot_parent, 0o700), applied)
        self.assertIn((self.workspace.path, 0o700), applied)
        self.assertIn((self.staging_root, 0o700), applied)
        if os.name != "nt":
            self.assertEqual(
                stat.S_IMODE(snapshot_path.stat().st_mode),
                0o600,
            )
            self.assertEqual(
                stat.S_IMODE(snapshot_directory.stat().st_mode),
                0o700,
            )
        provider.cleanup_snapshot(context=context, reference=result.reference)

    def test_existing_destination_is_never_overwritten(self):
        fixed_uuid = uuid.uuid4()
        reference = SnapshotReference(fixed_uuid)
        snapshot_parent = self.workspace.path / WorkspaceArea.SNAPSHOT.value
        snapshot_parent.mkdir(mode=0o700)
        destination_directory = snapshot_parent / fixed_uuid.hex
        destination_directory.mkdir(mode=0o700)
        destination = destination_directory / SNAPSHOT_FILE_NAME
        destination.write_bytes(b"sentinel")

        provider = self.provider(reference_factory=lambda: reference)
        with self.assertRaises(SnapshotCreationError):
            provider.create_snapshot(SnapshotRequest(context=self.context))

        self.assertEqual(destination.read_bytes(), b"sentinel")

    def test_exclusive_file_collision_preserves_unowned_destination(self):
        reference = SnapshotReference(uuid.uuid4())
        destination_directory = (
            self.workspace.path / WorkspaceArea.SNAPSHOT.value / reference.identifier.hex
        )
        destination = destination_directory / SNAPSHOT_FILE_NAME
        collision_created = False

        def permission_applier(path, mode):
            nonlocal collision_created
            os.chmod(path, mode)
            if Path(path) == destination_directory and not collision_created:
                collision_created = True
                destination.write_bytes(b"racing-sentinel")

        provider = self.provider(
            reference_factory=lambda: reference,
            permission_applier=permission_applier,
        )
        with self.assertRaises(SnapshotCreationError) as caught:
            provider.create_snapshot(SnapshotRequest(context=self.context))

        self.assertTrue(caught.exception.cleanup_incomplete)
        self.assertEqual(destination.read_bytes(), b"racing-sentinel")
        destination.unlink()
        destination_directory.rmdir()

    def test_snapshot_area_symlink_is_rejected(self):
        outside = self.root / "outside-area"
        outside.mkdir()
        snapshot_area = self.workspace.path / WorkspaceArea.SNAPSHOT.value
        try:
            os.symlink(outside, snapshot_area, target_is_directory=True)
        except OSError:
            with mock.patch(
                "apps.backups.engine.sqlite_snapshot.path_is_link_like",
                side_effect=lambda path: Path(path) == snapshot_area,
            ):
                with self.assertRaises(SnapshotWorkspaceUnavailable):
                    self.provider().create_snapshot(SnapshotRequest(context=self.context))
        else:
            with self.assertRaises(SnapshotWorkspaceUnavailable):
                self.provider().create_snapshot(SnapshotRequest(context=self.context))
        self.assertEqual(tuple(outside.iterdir()), ())

    def test_snapshot_file_symlink_is_rejected_for_read_and_cleanup(self):
        provider, context, result = self.create_snapshot()
        snapshot_path = self.snapshot_path(result)
        outside = self.root / "outside-file.sqlite3"
        outside.write_bytes(b"outside")
        snapshot_path.unlink()
        try:
            os.symlink(outside, snapshot_path)
        except OSError:
            snapshot_path.write_bytes(b"simulated-link")
            link_patch = mock.patch(
                "apps.backups.engine.sqlite_snapshot.path_is_link_like",
                side_effect=lambda path: Path(path) == snapshot_path,
            )
        else:
            link_patch = mock.patch(
                "apps.backups.engine.sqlite_snapshot.path_is_link_like",
                wraps=None,
            )

        if snapshot_path.is_symlink():
            with self.assertRaises(SnapshotValidationError):
                with provider.open_snapshot(
                    context=context,
                    reference=result.reference,
                ):
                    pass
            with self.assertRaises(SnapshotCleanupError):
                provider.cleanup_snapshot(
                    context=context,
                    reference=result.reference,
                )
        else:
            with link_patch:
                with self.assertRaises(SnapshotValidationError):
                    with provider.open_snapshot(
                        context=context,
                        reference=result.reference,
                    ):
                        pass
                with self.assertRaises(SnapshotCleanupError):
                    provider.cleanup_snapshot(
                        context=context,
                        reference=result.reference,
                    )
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_other_workspace_cannot_open_snapshot_by_uuid(self):
        provider, context, result = self.create_snapshot()
        other_workspace = self.manager.create()
        other_context = self.context_without_workspace.with_workspace(other_workspace.reference)
        with self.assertRaises(SnapshotEngineError):
            with provider.open_snapshot(
                context=other_context,
                reference=result.reference,
            ):
                pass
        provider.cleanup_snapshot(context=context, reference=result.reference)


class SQLiteSnapshotCapacityStorageTests(SQLiteSnapshotTestCase):
    def test_insufficient_capacity_leaves_no_partial_snapshot(self):
        provider = self.provider(disk_usage_provider=lambda path: SimpleNamespace(free=0))
        with self.assertRaises(InsufficientSnapshotCapacity):
            provider.create_snapshot(SnapshotRequest(context=self.context))
        self.assert_no_snapshot_files()

    def test_remote_and_unknown_storage_fail_closed(self):
        for classification in ("network", "unknown"):
            inspector = _StaticFilesystemInspector(
                StorageAssessment(False, classification, classification)
            )
            with self.subTest(classification=classification):
                with self.assertRaises(UnsafeStagingFilesystem):
                    self.provider(filesystem_inspector=inspector).create_snapshot(
                        SnapshotRequest(context=self.context)
                    )
                self.assertTrue(inspector.paths)
                self.assertEqual(
                    inspector.paths[-1].name,
                    WorkspaceArea.SNAPSHOT.value,
                )

    def test_confirmed_local_storage_passes_on_exact_snapshot_area(self):
        inspector = _StaticFilesystemInspector()
        provider, context, result = self.create_snapshot(
            self.provider(filesystem_inspector=inspector)
        )
        self.assertEqual(
            inspector.paths,
            [self.workspace.path / WorkspaceArea.SNAPSHOT.value],
        )
        provider.cleanup_snapshot(context=context, reference=result.reference)


class SQLiteSnapshotFailureTests(SQLiteSnapshotTestCase):
    def test_busy_and_locked_errors_are_retryable_and_sanitized(self):
        for code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raw_error = sqlite3.OperationalError(f"private raw sqlite detail {self.source_path}")
            raw_error.sqlite_errorcode = code
            connection_count = 0

            def connection_factory(database, _error=raw_error, **kwargs):
                nonlocal connection_count
                connection_count += 1
                connection = sqlite3.connect(database, **kwargs)
                if connection_count == 1:
                    return _BackupFailureConnection(connection, _error)
                return connection

            with self.subTest(code=code):
                with self.assertRaises(SnapshotBusy) as caught:
                    self.provider(connection_factory=connection_factory).create_snapshot(
                        SnapshotRequest(context=self.context)
                    )
                self.assertTrue(caught.exception.retryable)
                self.assert_sanitized_exception(caught.exception)
                self.assert_no_snapshot_files()

    def test_source_resolver_failure_is_sanitized_and_path_free(self):
        def failing_resolver(using):
            del using
            raise OSError(f"private raw sqlite detail {self.source_path} " f"{self.staging_root}")

        with self.assertRaises(UnsupportedSnapshotBackend) as caught:
            self.provider(source_settings_resolver=failing_resolver).create_snapshot(
                SnapshotRequest(context=self.context)
            )
        self.assert_sanitized_exception(caught.exception)

    def test_timeout_is_enforced_and_removes_partial_snapshot(self):
        expired = False

        def monotonic():
            return 2.0 if expired else 0.0

        def progress_hook(remaining, total):
            nonlocal expired
            del remaining, total
            expired = True

        provider = self.provider(
            policy=self.policy(
                busy_timeout_seconds=0.1,
                snapshot_timeout_seconds=1.0,
                pages_per_step=1,
            ),
            monotonic=monotonic,
            progress_hook=progress_hook,
        )
        with self.assertRaises(SnapshotTimeout) as caught:
            provider.create_snapshot(SnapshotRequest(context=self.context))
        self.assertTrue(caught.exception.retryable)
        self.assert_no_snapshot_files()

    def test_validation_timeout_is_enforced_and_removes_snapshot(self):
        expired = False

        def monotonic():
            return 2.0 if expired else 0.0

        def quick_check(reader):
            nonlocal expired
            rows = reader.query("PRAGMA quick_check(1)")
            expired = True
            return rows

        provider = self.provider(
            policy=self.policy(
                busy_timeout_seconds=0.1,
                snapshot_timeout_seconds=1.0,
            ),
            monotonic=monotonic,
            quick_check_runner=quick_check,
        )
        with self.assertRaises(SnapshotTimeout) as caught:
            provider.create_snapshot(SnapshotRequest(context=self.context))
        self.assertTrue(caught.exception.retryable)
        self.assert_no_snapshot_files()

    def test_failure_hooks_remove_every_owned_snapshot(self):
        stages = (
            "after_destination_creation",
            "after_backup",
            "before_validation",
            "after_validation",
        )
        for selected_stage in stages:

            def failure_hook(stage, _selected_stage=selected_stage):
                if stage == _selected_stage:
                    raise RuntimeError(f"private raw sqlite detail {self.staging_root}")

            with self.subTest(stage=selected_stage):
                with self.assertRaises(SnapshotEngineError) as caught:
                    self.provider(failure_hook=failure_hook).create_snapshot(
                        SnapshotRequest(context=self.context)
                    )
                self.assert_sanitized_exception(caught.exception)
                self.assert_no_snapshot_files()

    def test_permission_failure_after_exclusive_creation_leaks_nothing(self):
        failed = False

        def permission_applier(path, mode):
            nonlocal failed
            if mode == 0o600 and not failed:
                failed = True
                raise OSError(f"private raw sqlite detail {path}")
            os.chmod(path, mode)

        with self.assertRaises(SnapshotCreationError) as caught:
            self.provider(permission_applier=permission_applier).create_snapshot(
                SnapshotRequest(context=self.context)
            )
        self.assert_sanitized_exception(caught.exception)
        self.assert_no_snapshot_files()

    def test_validation_failure_removes_primary_and_exact_sidecars(self):
        def create_sidecars(stage):
            if stage != "before_validation":
                return
            snapshot_path = next(self.staging_root.rglob(SNAPSHOT_FILE_NAME))
            for suffix in SNAPSHOT_SIDECAR_SUFFIXES:
                Path(f"{snapshot_path}{suffix}").write_bytes(b"partial")

        with self.assertRaises(SnapshotValidationError):
            self.provider(failure_hook=create_sidecars).create_snapshot(
                SnapshotRequest(context=self.context)
            )
        self.assert_no_snapshot_files()

    def test_cleanup_failure_preserves_original_error_category(self):
        def failure_hook(stage):
            if stage == "after_backup":
                raise SnapshotTimeout()

        def failing_unlink(path):
            raise OSError(f"private raw sqlite detail {path}")

        provider = self.provider(
            failure_hook=failure_hook,
            unlinker=failing_unlink,
        )
        with self.assertRaises(SnapshotTimeout) as caught:
            provider.create_snapshot(SnapshotRequest(context=self.context))
        self.assertTrue(caught.exception.cleanup_incomplete)
        self.assert_sanitized_exception(caught.exception)
        provider.unlinker = os.unlink
        snapshot_path = next(self.staging_root.rglob(SNAPSHOT_FILE_NAME))
        reference = SnapshotReference(uuid.UUID(snapshot_path.parent.name))
        self.assertTrue(
            provider.cleanup_snapshot(
                context=self.context,
                reference=reference,
            )
        )


class SQLiteSnapshotValidationReaderTests(SQLiteSnapshotTestCase):
    def test_reader_is_query_only_path_opaque_and_closed_after_context(self):
        provider, context, result = self.create_snapshot()
        with provider.open_snapshot(
            context=context,
            reference=result.reference,
        ) as reader:
            self.assertEqual(reader.scalar("PRAGMA query_only"), 1)
            self.assertEqual(reader.scalar("PRAGMA foreign_keys"), 1)
            self.assertEqual(reader.scalar("PRAGMA journal_mode"), "delete")
            self.assertEqual(
                reader.query("SELECT value FROM marker WHERE id=1"),
                (("baseline",),),
            )
            forbidden_sql = (
                "INSERT INTO marker (id, value) VALUES (4, 'write')",
                "PRAGMA query_only=OFF",
                "ATTACH DATABASE ':memory:' AS extra",
                "PRAGMA database_list",
                "SELECT * FROM pragma_database_list",
            )
            for sql in forbidden_sql:
                with self.subTest(sql=sql):
                    with self.assertRaises(SnapshotValidationError):
                        reader.query(sql)
        with self.assertRaises(SnapshotValidationError):
            reader.query("SELECT 1")
        provider.cleanup_snapshot(context=context, reference=result.reference)

    def test_reader_close_failure_cannot_produce_success(self):
        provider, context, result = self.create_snapshot()

        def close_failure_factory(database, **kwargs):
            return _CloseFailureConnection(sqlite3.connect(database, **kwargs))

        provider.connection_factory = close_failure_factory
        with self.assertRaises(SnapshotValidationError):
            with provider.open_snapshot(
                context=context,
                reference=result.reference,
            ) as reader:
                self.assertEqual(reader.scalar("SELECT 1"), 1)
        provider.connection_factory = sqlite3.connect
        provider.cleanup_snapshot(context=context, reference=result.reference)

    def test_real_quick_check_and_foreign_key_check_are_successful(self):
        provider, context, result = self.create_snapshot()
        with provider.open_snapshot(
            context=context,
            reference=result.reference,
        ) as reader:
            self.assertEqual(reader.query("PRAGMA quick_check(1)"), (("ok",),))
            self.assertEqual(reader.query("PRAGMA foreign_key_check"), ())
        provider.cleanup_snapshot(context=context, reference=result.reference)

    def test_foreign_key_violation_is_rejected(self):
        self.source.execute("PRAGMA foreign_keys=OFF")
        self.source.execute("INSERT INTO child (id, parent_id, value) VALUES (2, 999, 0)")
        self.source.execute("PRAGMA foreign_keys=ON")
        with self.assertRaises(SnapshotValidationError):
            self.provider().create_snapshot(SnapshotRequest(context=self.context))
        self.assert_no_snapshot_files()

    def test_live_source_schema_race_is_rejected(self):
        def change_schema(stage):
            if stage == "before_validation":
                self.source.execute("CREATE TABLE schema_race (id INTEGER PRIMARY KEY)")

        with self.assertRaises(SnapshotValidationError):
            self.provider(failure_hook=change_schema).create_snapshot(
                SnapshotRequest(context=self.context)
            )
        self.assert_no_snapshot_files()

    def test_snapshot_schema_version_mismatch_is_rejected(self):
        source_schema = self.source.execute("PRAGMA schema_version").fetchone()[0]

        def change_snapshot_schema(stage):
            if stage != "before_validation":
                return
            snapshot_path = next(self.staging_root.rglob(SNAPSHOT_FILE_NAME))
            connection = sqlite3.connect(snapshot_path, isolation_level=None)
            try:
                connection.execute(f"PRAGMA schema_version={source_schema + 1}")
            finally:
                connection.close()

        with self.assertRaises(SnapshotValidationError):
            self.provider(failure_hook=change_snapshot_schema).create_snapshot(
                SnapshotRequest(context=self.context)
            )
        self.assert_no_snapshot_files()

    def test_success_has_exact_size_and_no_destination_sidecars(self):
        provider, context, result = self.create_snapshot()
        snapshot_path = self.snapshot_path(result)
        self.assertEqual(snapshot_path.stat().st_size, result.byte_count)
        self.assertEqual(result.byte_count, result.page_count * result.page_size)
        for suffix in SNAPSHOT_SIDECAR_SUFFIXES:
            self.assertFalse(Path(f"{snapshot_path}{suffix}").exists())
        provider.cleanup_snapshot(context=context, reference=result.reference)


class SQLiteSnapshotCleanupTests(SQLiteSnapshotTestCase):
    def test_explicit_cleanup_is_idempotent_and_boolean(self):
        provider, context, result = self.create_snapshot()
        self.assertTrue(
            provider.cleanup_snapshot(
                context=context,
                reference=result.reference,
            )
        )
        self.assertFalse(
            provider.cleanup_snapshot(
                context=context,
                reference=result.reference,
            )
        )

    def test_cleanup_requires_the_snapshot_directory_to_be_removed(self):
        provider, context, result = self.create_snapshot()
        snapshot_directory = self.snapshot_directory(result)
        provider.directory_remover = lambda path: None

        with self.assertRaises(SnapshotCleanupError):
            provider.cleanup_snapshot(
                context=context,
                reference=result.reference,
            )

        self.assertTrue(snapshot_directory.is_dir())
        provider.directory_remover = os.rmdir
        self.assertFalse(
            provider.cleanup_snapshot(
                context=context,
                reference=result.reference,
            )
        )
        self.assertFalse(os.path.lexists(snapshot_directory))
        self.assertFalse(self.snapshot_path(result).exists())

    def test_cleanup_accepts_only_an_opaque_reference(self):
        provider, context, result = self.create_snapshot()
        with self.assertRaises(SnapshotNotFound):
            provider.cleanup_snapshot(
                context=context,
                reference=str(self.snapshot_path(result)),
            )
        self.assertTrue(self.snapshot_path(result).exists())
        provider.cleanup_snapshot(context=context, reference=result.reference)

    def test_cleanup_preserves_unrelated_workspace_content(self):
        provider, context, result = self.create_snapshot()
        unrelated = self.snapshot_directory(result) / "unrelated.keep"
        unrelated.write_text("preserve", encoding="utf-8")

        cleaned = provider.cleanup_snapshot(
            context=context,
            reference=result.reference,
        )

        self.assertIs(cleaned, True)
        self.assertFalse(self.snapshot_path(result).exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "preserve")
        self.assertNotIn(str(self.staging_root), repr(cleaned))


class SQLiteSnapshotEngineBoundaryTests(BackupPhase1TestCase):
    @override_settings(
        BACKUP_EXECUTION_ENGINE_ENABLED=True,
        BACKUP_ENGINE_ENABLED=False,
        CELERY_BROKER_URL="redis://worker-only.example/0",
        CELERY_TASK_ALWAYS_EAGER=False,
    )
    def test_snapshot_capability_does_not_enable_full_execution(self):
        self.assertTrue(SQLITE_SNAPSHOT_PROVIDER_READY)
        self.assertFalse(OPERATIONAL_PROVIDER_STACK_READY)
        capability = get_engine_capability()
        self.assertTrue(capability.snapshot_provider_ready)
        self.assertIsNone(capability.runtime_snapshot_policy_ready)
        self.assertFalse(capability.provider_stack_ready)
        self.assertFalse(capability.real_execution_available)
        self.assertFalse(real_execution_available())
        errors = check_backup_async_execution_configuration(None)
        self.assertIn("backups.E012", [error.id for error in errors])

    def test_planning_marks_snapshot_planned_without_filesystem_work(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        backup = self.make_backup()
        with tempfile.TemporaryDirectory() as raw:
            staging_root = Path(raw) / "must-not-exist"
            with (
                override_settings(BACKUP_STAGING_ROOT=staging_root),
                mock.patch.object(
                    BackupWorkspaceManager,
                    "create",
                    side_effect=AssertionError("workspace creation was called"),
                ),
                mock.patch.object(
                    SQLiteSnapshotProvider,
                    "create_snapshot",
                    side_effect=AssertionError("snapshot creation was called"),
                ),
            ):
                plan = prepare_backup_execution(
                    business=backup.business,
                    backup_record=backup,
                    actor=backup.created_by,
                )
            self.assertFalse(staging_root.exists())

        states = {item.stage: item.state for item in plan.stage_reports}
        self.assertEqual(
            states[PipelineStage.PREPARE_SNAPSHOT],
            PipelineStageState.PLANNED,
        )
        for stage in (
            PipelineStage.EXPORT_COMPONENTS,
            PipelineStage.BUILD_PACKAGE,
            PipelineStage.VERIFY_ARTIFACT,
            PipelineStage.COMPLETE,
        ):
            self.assertEqual(states[stage], PipelineStageState.NOT_STARTED)
        self.assertIsNone(plan.context.workspace_reference)
        self.assertFalse(plan.real_execution_available)
        backup.refresh_from_db()
        self.assertEqual(backup.status, BackupStatus.QUEUED)
        self.assertEqual(backup.integrity_status, IntegrityStatus.NOT_CHECKED)
        self.assertEqual(
            {report.stage: report.state for report in planning_stage_reports()}[
                PipelineStage.PREPARE_SNAPSHOT
            ],
            PipelineStageState.PLANNED,
        )

    def test_disabled_task_creates_no_snapshot_or_artifact_metadata(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        backup = self.make_backup()
        with tempfile.TemporaryDirectory() as raw:
            staging_root = Path(raw) / "must-not-exist"
            with (
                override_settings(BACKUP_STAGING_ROOT=staging_root),
                mock.patch.object(
                    BackupWorkspaceManager,
                    "create",
                    side_effect=AssertionError("workspace creation was called"),
                ),
                mock.patch.object(
                    SQLiteSnapshotProvider,
                    "create_snapshot",
                    side_effect=AssertionError("snapshot creation was called"),
                ),
            ):
                with self.assertRaises(BackupEngineDisabled):
                    execute_backup(
                        backup.public_id,
                        self.business_a.public_id,
                    )
            self.assertFalse(staging_root.exists())

        backup.refresh_from_db()
        self.assertEqual(backup.status, BackupStatus.FAILED)
        self.assertNotEqual(backup.status, BackupStatus.SUCCEEDED)
        self.assertEqual(backup.integrity_status, IntegrityStatus.NOT_CHECKED)
        self.assertEqual(backup.storage_backend_identifier, "")
        self.assertEqual(backup.opaque_object_key, "")
        self.assertEqual(backup.encryption_key_identifier, "")
        self.assertEqual(backup.encrypted_data_key_envelope, "")
        self.assertEqual(backup.whole_artifact_hash, "")
        self.assertEqual(backup.backup_size_bytes, 0)
        self.assertFalse(hasattr(execute_backup, "delay"))
        self.assertFalse(hasattr(execute_backup, "apply_async"))
