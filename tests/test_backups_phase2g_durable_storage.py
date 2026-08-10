"""Focused security tests for Phase 2G private durable encrypted storage."""

from __future__ import annotations

import hashlib
import os
import unittest
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import override_settings

from apps.backups.engine.availability import (
    DURABLE_STORAGE_PROVIDER_READY,
    OPERATIONAL_PROVIDER_STACK_READY,
    get_engine_capability,
    real_execution_available,
)
from apps.backups.engine.checks import (
    check_durable_storage_policy_settings,
    check_durable_storage_root,
)
from apps.backups.engine.contracts import (
    StoredBackupObjectReference,
    StoredBackupObjectRequest,
    StoredObjectDurabilityState,
    StoredObjectVerificationState,
)
from apps.backups.engine.durable_storage import (
    LOCAL_DURABLE_STORAGE_PROVIDER_IDENTIFIER,
    STORED_OBJECT_FILE_NAME,
    STORED_OBJECT_SCHEMA_IDENTIFIER,
    LocalPrivateDurableStorageProvider,
)
from apps.backups.engine.durable_storage_exceptions import (
    DurableObjectCleanupError,
    DurableObjectCreationError,
    DurableObjectValidationError,
    DurableStoragePolicyError,
    DurableStorageTimeout,
    InsufficientDurableStorageCapacity,
    Phase2GCoordinationError,
    UnsafeDurableStorageRoot,
)
from apps.backups.engine.durable_storage_policy import (
    DurableStoragePolicy,
    validate_durable_storage_root,
)
from apps.backups.engine.encrypted_artifact import ARTIFACT_MAGIC
from apps.backups.engine.encryption_exceptions import (
    EncryptedArtifactCleanupError,
    EncryptedArtifactNotFound,
)

from . import test_backups_phase2f_encryption as phase2f_tests
from .test_backups_phase2b_snapshot import _StaticFilesystemInspector


class DurableStorageProviderTests(phase2f_tests.EncryptedArtifactProviderTests):
    def setUp(self):
        super().setUp()
        self.durable_cleanup = []

    def tearDown(self):
        for provider, context, reference in reversed(self.durable_cleanup):
            try:
                provider.delete_stored_object(
                    context=context,
                    reference=reference,
                )
            except Exception:
                pass
        super().tearDown()

    def _durable_policy(self, **changes):
        values = {
            "root": self.root / "private-durable",
            "chunk_bytes": 4096,
            "maximum_object_bytes": 65 * 1024**2,
            "timeout_seconds": 60.0,
            "minimum_free_bytes": 0,
            "headroom_multiplier": 1.0,
            "require_local": True,
        }
        values.update(changes)
        return DurableStoragePolicy(**values)

    def _durable_fixture(self, **provider_changes):
        state = self._encryption_fixture()
        encrypted = self._encrypt(state)
        source_raw = self._read_artifact(state, encrypted)
        values = {
            "encrypted_artifact_provider": state["provider"],
            "policy": self._durable_policy(),
            "filesystem_inspector": _StaticFilesystemInspector(),
            "disk_usage_provider": lambda _path: SimpleNamespace(free=10**12),
        }
        values.update(provider_changes)
        durable_provider = LocalPrivateDurableStorageProvider(**values)
        request = StoredBackupObjectRequest(
            context=state["fixture"]["context"],
            encrypted_artifact=encrypted,
        )
        state.update(
            {
                "encrypted": encrypted,
                "source_raw": source_raw,
                "durable_provider": durable_provider,
                "storage_request": request,
            }
        )
        return state

    def _new_durable_provider(self, state, **changes):
        values = {
            "encrypted_artifact_provider": state["provider"],
            "policy": self._durable_policy(),
            "filesystem_inspector": _StaticFilesystemInspector(),
            "disk_usage_provider": lambda _path: SimpleNamespace(free=10**12),
        }
        values.update(changes)
        return LocalPrivateDurableStorageProvider(**values)

    def _store(self, state):
        result = state["durable_provider"].store_encrypted_artifact(
            state["storage_request"]
        )
        self.durable_cleanup.append(
            (
                state["durable_provider"],
                state["fixture"]["context"],
                result.reference,
            )
        )
        return result

    @staticmethod
    def _object_path(state, result):
        context = state["fixture"]["context"]
        return (
            state["durable_provider"].root
            / "objects"
            / context.business_public_id.hex
            / context.backup_public_id.hex
            / result.reference.identifier.hex
            / STORED_OBJECT_FILE_NAME
        )

    def _read_stored(self, state, result):
        with state["durable_provider"].open_stored_object(
            context=state["fixture"]["context"],
            reference=result.reference,
        ) as reader:
            return self._read_stream(reader)

    @contextmanager
    def _temporary_object_bytes(self, state, result, raw):
        path = self._object_path(state, result)
        original = path.read_bytes()
        try:
            with path.open("r+b") as output:
                output.write(raw)
                output.truncate()
                output.flush()
                os.fsync(output.fileno())
            yield
        finally:
            with path.open("r+b") as output:
                output.write(original)
                output.truncate()
                output.flush()
                os.fsync(output.fileno())

    def test_real_pipeline_stores_verifies_and_cleans_encrypted_staging(self):
        state = self._durable_fixture()
        result = self._store(state)
        context = state["fixture"]["context"]
        raw = self._read_stored(state, result)

        self.assertEqual(raw, state["source_raw"])
        self.assertEqual(raw[:8], ARTIFACT_MAGIC)
        self.assertNotEqual(raw[:2], b"PK")
        self.assertEqual(result.byte_count, len(raw))
        self.assertEqual(result.sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(result.sha256, state["encrypted"].ciphertext_sha256)
        self.assertEqual(result.backup_public_id, context.backup_public_id)
        self.assertEqual(result.tenant_public_id, context.business_public_id)
        self.assertEqual(result.object_schema_identifier, STORED_OBJECT_SCHEMA_IDENTIFIER)
        self.assertEqual(
            result.provider_identifier,
            LOCAL_DURABLE_STORAGE_PROVIDER_IDENTIFIER,
        )
        self.assertEqual(result.durability_state, StoredObjectDurabilityState.STORED)
        self.assertEqual(
            result.verification_state,
            StoredObjectVerificationState.STORED_AND_VERIFIED,
        )
        self.assertFalse(result.encrypted_staging_cleanup_incomplete)
        self.assertTrue(
            state["durable_provider"].validate_stored_object(
                context=context,
                result=result,
            )
        )
        self.assertTrue(
            state["durable_provider"].owns_stored_object_reference(
                context=context,
                reference=result.reference,
            )
        )
        self.assertTrue(self._object_path(state, result).exists())
        with self.assertRaises(EncryptedArtifactNotFound):
            with state["provider"].open_encrypted_artifact(
                context=context,
                reference=state["encrypted"].reference,
            ):
                pass

    def test_result_and_opaque_reader_expose_no_path_or_file_descriptor(self):
        state = self._durable_fixture()
        result = self._store(state)
        rendered = repr(result)
        self.assertNotIn(str(state["durable_provider"].root), rendered)
        self.assertNotIn("path", rendered.lower())
        with state["durable_provider"].open_stored_object(
            context=state["fixture"]["context"],
            reference=result.reference,
        ) as reader:
            self.assertFalse(hasattr(reader, "name"))
            self.assertFalse(hasattr(reader, "fileno"))
            self.assertEqual(reader.read(8), ARTIFACT_MAGIC)

    def test_completed_request_is_idempotent_without_duplicate_copy(self):
        state = self._durable_fixture()
        first = self._store(state)
        second = state["durable_provider"].store_encrypted_artifact(
            state["storage_request"]
        )
        self.assertEqual(first, second)
        backup_directory = self._object_path(state, first).parent.parent
        self.assertEqual(tuple(backup_directory.iterdir()), (self._object_path(state, first).parent,))

    def test_durable_byte_mutation_truncation_append_and_framing_are_rejected(self):
        state = self._durable_fixture()
        result = self._store(state)
        raw = state["source_raw"]
        cases = {
            "mutation": raw[:-17] + bytes([raw[-17] ^ 1]) + raw[-16:],
            "truncated": raw[:-1],
            "appended": raw + b"x",
            "framing": bytes([raw[0] ^ 1]) + raw[1:],
        }
        for label, changed in cases.items():
            with self.subTest(label=label):
                with self._temporary_object_bytes(state, result, changed):
                    with self.assertRaises(DurableObjectValidationError):
                        state["durable_provider"].validate_stored_object(
                            context=state["fixture"]["context"],
                            result=result,
                        )

    def test_forged_context_result_and_reference_are_rejected(self):
        state = self._durable_fixture()
        result = self._store(state)
        context = state["fixture"]["context"]
        forged_contexts = (
            replace(context, backup_public_id=uuid.uuid4()),
            replace(context, business_public_id=uuid.uuid4()),
        )
        for forged in forged_contexts:
            with self.subTest(forged=forged):
                with self.assertRaises(DurableObjectValidationError):
                    state["durable_provider"].validate_stored_object(
                        context=forged,
                        result=result,
                    )
        forged_results = (
            replace(result, sha256="0" * 64),
            replace(result, byte_count=result.byte_count + 1),
            replace(result, backup_public_id=uuid.uuid4()),
            replace(result, tenant_public_id=uuid.uuid4()),
            replace(result, reference=StoredBackupObjectReference(uuid.uuid4())),
        )
        for forged in forged_results:
            with self.subTest(forged=forged):
                with self.assertRaises(DurableObjectValidationError):
                    state["durable_provider"].validate_stored_object(
                        context=context,
                        result=forged,
                    )
        copied_source = replace(
            state["encrypted"],
            reference=replace(
                state["encrypted"].reference,
                identifier=uuid.uuid4(),
            ),
        )
        with self.assertRaises(Phase2GCoordinationError):
            state["durable_provider"].store_encrypted_artifact(
                replace(state["storage_request"], encrypted_artifact=copied_source)
            )

    def test_failure_before_and_after_publication_preserves_encrypted_staging(self):
        state = self._durable_fixture()
        for stage in ("before_durable_publication", "after_durable_publication"):
            with self.subTest(stage=stage):
                provider = self._new_durable_provider(
                    state,
                    failure_hook=lambda current, stage=stage: (
                        (_ for _ in ()).throw(OSError("private durable object path"))
                        if current == stage
                        else None
                    )
                )
                with self.assertRaises(DurableObjectCreationError) as raised:
                    provider.store_encrypted_artifact(state["storage_request"])
                self.assertNotIn("private durable object path", str(raised.exception))
                self.assertTrue(
                    state["provider"].validate_owned_encrypted_artifact(
                        context=state["fixture"]["context"],
                        result=state["encrypted"],
                    )
                )

    def test_capacity_timeout_and_object_size_bounds_fail_closed(self):
        state = self._durable_fixture()
        no_capacity = LocalPrivateDurableStorageProvider(
            encrypted_artifact_provider=state["provider"],
            policy=self._durable_policy(),
            filesystem_inspector=_StaticFilesystemInspector(),
            disk_usage_provider=lambda _path: SimpleNamespace(free=0),
        )
        with self.assertRaises(InsufficientDurableStorageCapacity):
            no_capacity.store_encrypted_artifact(state["storage_request"])

        too_small = LocalPrivateDurableStorageProvider(
            encrypted_artifact_provider=state["provider"],
            policy=self._durable_policy(
                maximum_object_bytes=state["encrypted"].encrypted_byte_count - 1,
            ),
            filesystem_inspector=_StaticFilesystemInspector(),
        )
        with self.assertRaises(Phase2GCoordinationError):
            too_small.store_encrypted_artifact(state["storage_request"])

        calls = {"count": 0}

        def expired():
            calls["count"] += 1
            return 0.0 if calls["count"] == 1 else 61.0

        timed_out = LocalPrivateDurableStorageProvider(
            encrypted_artifact_provider=state["provider"],
            policy=self._durable_policy(),
            filesystem_inspector=_StaticFilesystemInspector(),
            monotonic=expired,
        )
        with self.assertRaises(DurableStorageTimeout):
            timed_out.store_encrypted_artifact(state["storage_request"])
        self.assertTrue(
            state["provider"].validate_owned_encrypted_artifact(
                context=state["fixture"]["context"],
                result=state["encrypted"],
            )
        )

    def test_policy_and_unsafe_root_overlap_checks(self):
        invalid = (
            replace(self._durable_policy(), chunk_bytes=1),
            replace(self._durable_policy(), timeout_seconds=0),
            replace(self._durable_policy(), headroom_multiplier=float("nan")),
            replace(self._durable_policy(), require_local="yes"),
        )
        for policy in invalid:
            with self.subTest(policy=policy):
                with self.assertRaises(DurableStoragePolicyError):
                    policy.validated()
        for root, kwargs in (
            (Path("relative"), {}),
            (self.staging_root / "inside", {"staging_root": self.staging_root}),
            (self.root / "media" / "inside", {"media_root": self.root / "media"}),
            (
                self.root / "durable-parent",
                {"static_root": self.root / "durable-parent" / "public"},
            ),
        ):
            with self.subTest(root=root):
                with self.assertRaises(UnsafeDurableStorageRoot):
                    validate_durable_storage_root(
                        root,
                        require_local=False,
                        **kwargs,
                    )
        with mock.patch(
            "apps.backups.engine.durable_storage_policy.path_has_link_like_component",
            return_value=True,
        ):
            with self.assertRaises(UnsafeDurableStorageRoot):
                validate_durable_storage_root(
                    self.root / "linked-durable",
                    require_local=False,
                )

    def test_system_checks_are_nonmutating_and_fail_closed(self):
        durable = self.root / "check-durable"
        with override_settings(
            BACKUP_DURABLE_STORAGE_ROOT=durable,
            BACKUP_DURABLE_STORAGE_REQUIRE_LOCAL=False,
        ):
            self.assertEqual(check_durable_storage_policy_settings(None), [])
            self.assertEqual(check_durable_storage_root(None), [])
            self.assertFalse(durable.exists())
        with override_settings(BACKUP_DURABLE_STORAGE_CHUNK_BYTES=1):
            errors = check_durable_storage_policy_settings(None)
            self.assertEqual([error.id for error in errors], ["backups.E029"])
        with override_settings(
            BACKUP_EXECUTION_ENGINE_ENABLED=True,
            BACKUP_DURABLE_STORAGE_ROOT=self.staging_root / "inside",
            BACKUP_DURABLE_STORAGE_REQUIRE_LOCAL=False,
            BACKUP_STAGING_ROOT=self.staging_root,
        ):
            errors = check_durable_storage_root(None)
            self.assertEqual([error.id for error in errors], ["backups.E030"])

    def test_encrypted_staging_cleanup_failure_keeps_durable_and_retries(self):
        state = self._durable_fixture()
        with mock.patch.object(
            state["provider"],
            "cleanup_encrypted_artifact",
            side_effect=EncryptedArtifactCleanupError(),
        ):
            result = self._store(state)
        self.assertTrue(result.encrypted_staging_cleanup_incomplete)
        self.assertTrue(self._object_path(state, result).exists())
        self.assertTrue(
            state["durable_provider"].validate_stored_object(
                context=state["fixture"]["context"],
                result=result,
            )
        )
        completed = state["durable_provider"].retry_encrypted_staging_cleanup(
            state["storage_request"],
            result,
        )
        self.assertFalse(completed.encrypted_staging_cleanup_incomplete)
        self.assertTrue(self._object_path(state, completed).exists())
        with self.assertRaises(EncryptedArtifactNotFound):
            with state["provider"].open_encrypted_artifact(
                context=state["fixture"]["context"],
                reference=state["encrypted"].reference,
            ):
                pass

    def test_delete_is_exact_idempotent_and_rejects_hardlink_and_forgery(self):
        state = self._durable_fixture()
        result = self._store(state)
        context = state["fixture"]["context"]
        with self.assertRaises(DurableObjectCleanupError):
            state["durable_provider"].delete_stored_object(
                context=replace(context, backup_public_id=uuid.uuid4()),
                reference=result.reference,
            )
        path = self._object_path(state, result)
        alias = path.parent.parent / "unowned-alias.nxb"
        os.link(path, alias, follow_symlinks=False)
        try:
            with self.assertRaises(DurableObjectCleanupError):
                state["durable_provider"].delete_stored_object(
                    context=context,
                    reference=result.reference,
                )
            self.assertTrue(path.exists())
        finally:
            alias.unlink()
        self.assertTrue(
            state["durable_provider"].delete_stored_object(
                context=context,
                reference=result.reference,
            )
        )

    def test_nonregular_replacement_and_cleanup_failure_are_sanitized(self):
        state = self._durable_fixture()
        result = self._store(state)
        context = state["fixture"]["context"]
        path = self._object_path(state, result)
        saved = path.parent / "saved-owned-object"
        path.rename(saved)
        path.mkdir()
        try:
            with self.assertRaises(DurableObjectValidationError):
                state["durable_provider"].validate_stored_object(
                    context=context,
                    result=result,
                )
        finally:
            path.rmdir()
            saved.rename(path)

        original_unlink = os.unlink
        failed = {"done": False}

        def fail_once(target, *args, **kwargs):
            if Path(target) == path and not failed["done"]:
                failed["done"] = True
                raise OSError("private durable cleanup path")
            return original_unlink(target, *args, **kwargs)

        with mock.patch(
            "apps.backups.engine.durable_storage.os.unlink",
            side_effect=fail_once,
        ):
            with self.assertRaises(DurableObjectCleanupError) as raised:
                state["durable_provider"].delete_stored_object(
                    context=context,
                    reference=result.reference,
                )
        self.assertNotIn("private durable cleanup path", str(raised.exception))
        self.assertTrue(path.exists())
        self.assertTrue(
            state["durable_provider"].delete_stored_object(
                context=context,
                reference=result.reference,
            )
        )

    def test_partial_write_failure_cleans_owned_output_and_keeps_staging(self):
        state = self._durable_fixture()
        original_write = os.write
        calls = {"count": 0}

        def fail_write(descriptor, value):
            calls["count"] += 1
            if calls["count"] == 1:
                return original_write(descriptor, value[: max(1, len(value) // 2)])
            raise OSError("private partial object")

        with mock.patch(
            "apps.backups.engine.durable_storage.os.write",
            side_effect=fail_write,
        ):
            with self.assertRaises(DurableObjectCreationError) as raised:
                state["durable_provider"].store_encrypted_artifact(
                    state["storage_request"]
                )
        self.assertNotIn("private partial object", str(raised.exception))
        self.assertTrue(
            state["provider"].validate_owned_encrypted_artifact(
                context=state["fixture"]["context"],
                result=state["encrypted"],
            )
        )
        context = state["fixture"]["context"]
        backup_directory = (
            state["durable_provider"].root
            / "objects"
            / context.business_public_id.hex
            / context.backup_public_id.hex
        )
        self.assertEqual(tuple(backup_directory.iterdir()), ())

    def test_delete_preserves_abort_after_unlink_and_retries(self):
        state = self._durable_fixture()
        result = self._store(state)
        context = state["fixture"]["context"]
        original_unlink = os.unlink

        def unlink_then_abort(path, *args, **kwargs):
            original_unlink(path, *args, **kwargs)
            raise KeyboardInterrupt()

        with mock.patch(
            "apps.backups.engine.durable_storage.os.unlink",
            side_effect=unlink_then_abort,
        ):
            with self.assertRaises(KeyboardInterrupt):
                state["durable_provider"].delete_stored_object(
                    context=context,
                    reference=result.reference,
                )
        self.assertTrue(
            state["durable_provider"].delete_stored_object(
                context=context,
                reference=result.reference,
            )
        )
        self.assertTrue(
            state["durable_provider"].delete_stored_object(
                context=context,
                reference=result.reference,
            )
        )

    def test_publication_no_clobber_and_hardlink_ambiguity_preserve_unowned_files(self):
        state = self._durable_fixture()
        for mode in ("no-clobber", "hardlink"):
            with self.subTest(mode=mode):
                identifier = uuid.uuid4()
                alias = None

                def interfere(
                    stage,
                    *,
                    selected_state=state,
                    selected_identifier=identifier,
                    selected_mode=mode,
                ):
                    nonlocal alias
                    context = selected_state["fixture"]["context"]
                    directory = (
                        selected_state["durable_provider"].root
                        / "objects"
                        / context.business_public_id.hex
                        / context.backup_public_id.hex
                        / selected_identifier.hex
                    )
                    final = directory / STORED_OBJECT_FILE_NAME
                    if (
                        selected_mode == "no-clobber"
                        and stage == "before_durable_publication"
                    ):
                        final.write_bytes(b"unowned")
                    if (
                        selected_mode == "hardlink"
                        and stage == "after_durable_publication_link"
                    ):
                        alias = directory / "unowned-alias.nxb"
                        os.link(final, alias, follow_symlinks=False)
                        raise OSError("private alias")

                provider = self._new_durable_provider(
                    state,
                    reference_factory=lambda selected_identifier=identifier: (
                        selected_identifier
                    ),
                    failure_hook=interfere,
                )
                state["durable_provider"] = provider
                with self.assertRaises(DurableObjectCreationError) as raised:
                    provider.store_encrypted_artifact(state["storage_request"])
                self.assertTrue(raised.exception.cleanup_incomplete)
                directory = (
                    state["durable_provider"].root
                    / "objects"
                    / state["fixture"]["context"].business_public_id.hex
                    / state["fixture"]["context"].backup_public_id.hex
                    / identifier.hex
                )
                self.assertTrue(directory.exists())
                self.assertTrue(any(directory.iterdir()))
                for path in tuple(directory.iterdir()):
                    path.unlink()
                directory.rmdir()

    def test_abort_signals_preserved_and_errors_evidence_contain_no_paths(self):
        state = self._durable_fixture()
        for abort_type in (KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(abort_type=abort_type.__name__):
                provider = self._new_durable_provider(
                    state,
                    failure_hook=lambda stage, abort_type=abort_type: (
                        (_ for _ in ()).throw(abort_type())
                        if stage == "before_durable_publication"
                        else None
                    )
                )
                with self.assertRaises(abort_type):
                    provider.store_encrypted_artifact(state["storage_request"])
                self.assertTrue(
                    state["provider"].validate_owned_encrypted_artifact(
                        context=state["fixture"]["context"],
                        result=state["encrypted"],
                    )
                )
                self.assertNotIn(
                    str(provider.root),
                    repr(provider),
                )

    def test_capability_and_runtime_surfaces_remain_fail_closed(self):
        self.assertIs(DURABLE_STORAGE_PROVIDER_READY, True)
        self.assertIs(OPERATIONAL_PROVIDER_STACK_READY, False)
        self.assertIs(real_execution_available(), False)
        capability = get_engine_capability()
        self.assertIs(capability.durable_storage_provider_ready, True)
        self.assertIs(capability.provider_stack_ready, False)
        repository_root = Path(__file__).resolve().parents[1]
        for relative in (
            "apps/backups/views.py",
            "apps/backups/platform_views.py",
            "apps/backups/services.py",
            "apps/backups/tasks.py",
            "apps/backups/urls.py",
            "apps/backups/admin.py",
            "apps/backups/apps.py",
            "apps/backups/forms.py",
            "apps/backups/signals.py",
            "config/urls.py",
            "config/celery.py",
        ):
            path = repository_root / relative
            if path.exists():
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("LocalPrivateDurableStorageProvider", source)
                self.assertNotIn("StoredBackupObjectRequest", source)


def load_tests(loader, standard_tests, pattern):
    """Run Phase 2G cases; predecessor regressions have dedicated commands."""

    del loader, standard_tests, pattern
    names = sorted(
        name
        for name, value in DurableStorageProviderTests.__dict__.items()
        if name.startswith("test_") and callable(value)
    )
    return unittest.TestSuite(DurableStorageProviderTests(name) for name in names)
