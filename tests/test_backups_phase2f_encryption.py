"""Focused security tests for Phase 2F authenticated package encryption."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import unittest
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import override_settings

from apps.backups.engine.availability import (
    ENCRYPTED_ARTIFACT_PROVIDER_READY,
    OPERATIONAL_PROVIDER_STACK_READY,
    get_engine_capability,
    real_execution_available,
)
from apps.backups.engine.checks import (
    check_encryption_policy_settings,
    check_local_kek_configuration,
)
from apps.backups.engine.contracts import (
    EncryptedArtifactRequest,
    PackageBuildRequest,
    VerificationIssue,
    VerificationReference,
)
from apps.backups.engine.encrypted_artifact import (
    ARTIFACT_FILE_NAME,
    ARTIFACT_MAGIC,
    ENCRYPTED_ARTIFACT_FORMAT_IDENTIFIER,
    ENCRYPTED_ARTIFACT_PROVIDER_IDENTIFIER,
    ENCRYPTION_ALGORITHM,
    EncryptedArtifactProvider,
)
from apps.backups.engine.encryption_exceptions import (
    EncryptedArtifactCleanupError,
    EncryptedArtifactCreationError,
    EncryptedArtifactValidationError,
    EncryptionPolicyError,
    KeyProviderConfigurationError,
    Phase2FCoordinationError,
)
from apps.backups.engine.encryption_policy import EncryptionPolicy
from apps.backups.engine.key_management import LocalConfiguredKekProvider
from apps.backups.engine.logical_serialization import encode_canonical_document
from apps.backups.engine.package_exceptions import PackageCleanupError, PackageNotFound
from apps.backups.engine.workspace import WorkspaceArea

from . import test_backups_phase2e_verification as phase2e_tests

_PREFIX = struct.Struct(">8sI")


class EncryptedArtifactProviderTests(phase2e_tests.IndependentPackageVerifierTests):
    def setUp(self):
        super().setUp()
        self.encrypted_cleanup = []

    def tearDown(self):
        for provider, context, reference in reversed(self.encrypted_cleanup):
            try:
                provider.cleanup_encrypted_artifact(
                    context=context,
                    reference=reference,
                )
            except Exception:
                pass
        super().tearDown()

    @staticmethod
    def _policy(**changes):
        values = {
            "chunk_bytes": 4096,
            "maximum_plaintext_bytes": 64 * 1024**2,
            "maximum_artifact_bytes": 65 * 1024**2,
            "timeout_seconds": 60.0,
            "minimum_free_bytes": 0,
            "headroom_multiplier": 1.0,
            "maximum_header_bytes": 65_536,
        }
        values.update(changes)
        return EncryptionPolicy(**values)

    @staticmethod
    def _kek(fill=b"k", *, key_identifier="test-kek", key_version="v1"):
        return LocalConfiguredKekProvider(
            key_b64=base64.b64encode(fill * 32).decode("ascii"),
            key_identifier=key_identifier,
            key_version=key_version,
        )

    def _encryption_fixture(self, **provider_changes):
        fixture, package_provider, package, verifier = (
            self._build_verification_fixture()
        )
        verification = self._verify(verifier, fixture["context"], package)
        self.verification_cleanup.append(
            (verifier, fixture["context"], verification.reference)
        )
        values = {
            "package_provider": package_provider,
            "verification_provider": verifier,
            "kek_provider": self._kek(),
            "workspace_manager": self.manager,
            "policy": self._policy(),
        }
        values.update(provider_changes)
        encrypted_provider = EncryptedArtifactProvider(**values)
        request = EncryptedArtifactRequest(
            context=fixture["context"],
            package=package,
            verification=verification,
        )
        return {
            "fixture": fixture,
            "package_provider": package_provider,
            "package": package,
            "verifier": verifier,
            "verification": verification,
            "provider": encrypted_provider,
            "request": request,
        }

    @staticmethod
    def _read_stream(reader):
        chunks = []
        while True:
            chunk = reader.read(4096)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    def _read_package(self, state):
        with state["package_provider"].open_package(
            context=state["fixture"]["context"],
            reference=state["package"].reference,
        ) as reader:
            return self._read_stream(reader)

    def _artifact_path(self, state, result):
        return (
            self.workspace.path
            / WorkspaceArea.ENCRYPTED.value
            / result.reference.identifier.hex
            / ARTIFACT_FILE_NAME
        )

    def _read_artifact(self, state, result):
        with state["provider"].open_encrypted_artifact(
            context=state["fixture"]["context"],
            reference=result.reference,
        ) as reader:
            return self._read_stream(reader)

    @staticmethod
    def _split_artifact(raw):
        magic, header_size = _PREFIX.unpack(raw[: _PREFIX.size])
        header_start = _PREFIX.size
        header_end = header_start + header_size
        return magic, json.loads(raw[header_start:header_end]), raw[header_end:]

    @staticmethod
    def _reframe(document, body, *, magic=ARTIFACT_MAGIC):
        header = encode_canonical_document(document)
        return _PREFIX.pack(magic, len(header)) + header + body, header

    @contextmanager
    def _temporary_artifact_bytes(self, state, result, raw, **result_changes):
        provider = state["provider"]
        context = state["fixture"]["context"]
        path = self._artifact_path(state, result)
        original_raw = path.read_bytes()
        key = (context.workspace_reference.identifier, result.reference.identifier)
        original_evidence = provider._published[key]
        updated_result = replace(
            result,
            encrypted_byte_count=len(raw),
            ciphertext_sha256=hashlib.sha256(raw).hexdigest(),
            **result_changes,
        )
        try:
            with path.open("r+b") as output:
                output.write(raw)
                output.truncate()
                output.flush()
                os.fsync(output.fileno())
            provider._published[key] = replace(
                original_evidence,
                result=updated_result,
            )
            yield updated_result
        finally:
            with path.open("r+b") as output:
                output.write(original_raw)
                output.truncate()
                output.flush()
                os.fsync(output.fileno())
            provider._published[key] = original_evidence

    def _encrypt(self, state):
        result = state["provider"].encrypt_verified_package(state["request"])
        self.encrypted_cleanup.append(
            (state["provider"], state["fixture"]["context"], result.reference)
        )
        return result

    def test_real_pipeline_encrypts_authenticates_and_cleans_plaintext(self):
        state = self._encryption_fixture()
        plaintext = self._read_package(state)
        result = self._encrypt(state)

        self.assertEqual(result.format_identifier, ENCRYPTED_ARTIFACT_FORMAT_IDENTIFIER)
        self.assertEqual(result.provider_identifier, ENCRYPTED_ARTIFACT_PROVIDER_IDENTIFIER)
        self.assertEqual(result.encryption_algorithm, ENCRYPTION_ALGORITHM)
        self.assertFalse(result.plaintext_cleanup_incomplete)
        self.assertEqual(result.plaintext_byte_count, len(plaintext))
        self.assertEqual(result.plaintext_sha256, hashlib.sha256(plaintext).hexdigest())
        raw = self._read_artifact(state, result)
        self.assertEqual(result.encrypted_byte_count, len(raw))
        self.assertEqual(result.ciphertext_sha256, hashlib.sha256(raw).hexdigest())
        with state["provider"].open_decrypted_artifact(
            context=state["fixture"]["context"],
            result=result,
        ) as reader:
            self.assertEqual(self._read_stream(reader), plaintext)
        self.assertTrue(
            state["provider"].validate_encrypted_artifact_evidence(
                context=state["fixture"]["context"],
                package=state["package"],
                verification=state["verification"],
                result=result,
            )
        )
        with self.assertRaises(PackageNotFound):
            with state["package_provider"].open_package(
                context=state["fixture"]["context"],
                reference=state["package"].reference,
            ):
                pass
        self.assertTrue(
            state["verifier"].validate_verification_evidence(
                context=state["fixture"]["context"],
                package=state["package"],
                result=state["verification"],
            )
        )

    def test_fresh_dek_and_nonces_randomize_identical_plaintext(self):
        state = self._encryption_fixture()
        second_package = state["package_provider"].build_package(
            PackageBuildRequest(
                context=state["fixture"]["context"],
                phase2d1_result=state["fixture"]["phase_result"],
            )
        )
        self.package_cleanup.append(
            (
                state["package_provider"],
                state["fixture"]["context"],
                second_package.reference,
            )
        )
        second_verification = self._verify(
            state["verifier"],
            state["fixture"]["context"],
            second_package,
        )
        self.verification_cleanup.append(
            (
                state["verifier"],
                state["fixture"]["context"],
                second_verification.reference,
            )
        )
        second_request = EncryptedArtifactRequest(
            context=state["fixture"]["context"],
            package=second_package,
            verification=second_verification,
        )
        first_plaintext = self._read_package(state)
        with state["package_provider"].open_package(
            context=state["fixture"]["context"],
            reference=second_package.reference,
        ) as reader:
            second_plaintext = self._read_stream(reader)
        self.assertEqual(first_plaintext, second_plaintext)

        first = self._encrypt(state)
        second = state["provider"].encrypt_verified_package(second_request)
        self.encrypted_cleanup.append(
            (state["provider"], state["fixture"]["context"], second.reference)
        )
        first_raw = self._read_artifact(state, first)
        second_raw = self._read_artifact(state, second)
        self.assertNotEqual(first_raw, second_raw)
        _, first_header, _ = self._split_artifact(first_raw)
        _, second_header, _ = self._split_artifact(second_raw)
        self.assertNotEqual(first_header["nonce_b64"], second_header["nonce_b64"])
        self.assertNotEqual(
            first_header["wrapped_dek"]["nonce_b64"],
            second_header["wrapped_dek"]["nonce_b64"],
        )
        self.assertNotEqual(
            first_header["wrapped_dek"]["wrapped_key_b64"],
            second_header["wrapped_dek"]["wrapped_key_b64"],
        )
        self.assertEqual(len(state["provider"]._used_dek_digests), 2)
        self.assertEqual(len(state["provider"]._used_data_nonces), 2)
        self.assertEqual(len(state["provider"]._used_wrap_nonces), 2)

    def test_header_is_canonical_safe_and_authenticated_aad(self):
        state = self._encryption_fixture()
        result = self._encrypt(state)
        raw = self._read_artifact(state, result)
        magic, document, body = self._split_artifact(raw)
        self.assertEqual(magic, ARTIFACT_MAGIC)
        canonical = encode_canonical_document(document)
        self.assertEqual(result.header_sha256, hashlib.sha256(canonical).hexdigest())
        self.assertNotIn(str(self.workspace.path), canonical.decode())
        self.assertNotIn("database", document)
        self.assertNotIn("path", document)
        self.assertNotIn("table", document)
        self.assertEqual(document["encryption_algorithm"], "AES-256-GCM")

        mutations = {
            "tenant": ("tenant_public_id", str(uuid.uuid4())),
            "backup": ("backup_public_id", str(uuid.uuid4())),
            "size": ("plaintext_byte_count", document["plaintext_byte_count"] + 1),
            "hash": ("plaintext_sha256", "0" * 64),
            "algorithm": ("encryption_algorithm", "AES-128-GCM"),
            "nonce": (
                "nonce_b64",
                base64.b64encode(b"n" * 12).decode("ascii"),
            ),
            "verification": ("verification_version", "forged"),
        }
        for label, (field, value) in mutations.items():
            with self.subTest(label=label):
                changed = dict(document)
                changed[field] = value
                changed_raw, changed_header = self._reframe(changed, body)
                with self._temporary_artifact_bytes(
                    state,
                    result,
                    changed_raw,
                    header_sha256=hashlib.sha256(changed_header).hexdigest(),
                ) as changed_result:
                    with self.assertRaises(EncryptedArtifactValidationError):
                        state["provider"].validate_encrypted_artifact_evidence(
                            context=state["fixture"]["context"],
                            package=state["package"],
                            verification=state["verification"],
                            result=changed_result,
                        )

    def test_ciphertext_tag_wrapped_key_and_framing_mutations_are_rejected(self):
        state = self._encryption_fixture()
        result = self._encrypt(state)
        raw = self._read_artifact(state, result)
        _magic, document, body = self._split_artifact(raw)
        cases = {
            "ciphertext": raw[:-17] + bytes([raw[-17] ^ 1]) + raw[-16:],
            "tag": raw[:-1] + bytes([raw[-1] ^ 1]),
            "truncated": raw[:-1],
            "appended": raw + b"x",
            "magic": bytes([raw[0] ^ 1]) + raw[1:],
            "header_length": _PREFIX.pack(ARTIFACT_MAGIC, 65_537)
            + raw[_PREFIX.size :],
        }
        wrapped_changed = dict(document)
        wrapped_metadata = dict(document["wrapped_dek"])
        wrapped = bytearray(base64.b64decode(wrapped_metadata["wrapped_key_b64"]))
        wrapped[0] ^= 1
        wrapped_metadata["wrapped_key_b64"] = base64.b64encode(wrapped).decode("ascii")
        wrapped_changed["wrapped_dek"] = wrapped_metadata
        wrapped_raw, wrapped_header = self._reframe(wrapped_changed, body)
        cases["wrapped_dek"] = wrapped_raw

        for label, changed_raw in cases.items():
            with self.subTest(label=label):
                changes = {}
                if label == "wrapped_dek":
                    changes["header_sha256"] = hashlib.sha256(wrapped_header).hexdigest()
                with self._temporary_artifact_bytes(
                    state,
                    result,
                    changed_raw,
                    **changes,
                ) as changed_result:
                    with self.assertRaises(EncryptedArtifactValidationError):
                        state["provider"].validate_encrypted_artifact_evidence(
                            context=state["fixture"]["context"],
                            package=state["package"],
                            verification=state["verification"],
                            result=changed_result,
                        )

    def test_wrong_kek_identifier_version_and_raw_key_are_rejected(self):
        state = self._encryption_fixture()
        result = self._encrypt(state)
        original = state["provider"].kek_provider
        alternatives = (
            self._kek(b"z"),
            self._kek(key_identifier="other-kek"),
            self._kek(key_version="v2"),
        )
        for alternative in alternatives:
            with self.subTest(provider=repr(alternative)):
                state["provider"].kek_provider = alternative
                with self.assertRaises(EncryptedArtifactValidationError):
                    state["provider"].validate_encrypted_artifact_evidence(
                        context=state["fixture"]["context"],
                        package=state["package"],
                        verification=state["verification"],
                        result=result,
                    )
        state["provider"].kek_provider = original

    def test_forged_and_unready_inputs_are_rejected_without_plaintext_loss(self):
        state = self._encryption_fixture()
        context = state["fixture"]["context"]
        cases = (
            replace(state["request"], context=replace(context, backup_public_id=uuid.uuid4())),
            replace(
                state["request"],
                package=replace(state["package"], plaintext_sha256="0" * 64),
            ),
            replace(
                state["request"],
                verification=replace(
                    state["verification"],
                    reference=VerificationReference(uuid.uuid4()),
                ),
            ),
            replace(
                state["request"],
                verification=replace(
                    state["verification"],
                    restore_ready=False,
                ),
            ),
            replace(
                state["request"],
                verification=replace(
                    state["verification"],
                    issues=(VerificationIssue("forged", "Rejected."),),
                ),
            ),
        )
        for request in cases:
            with self.subTest(request=request):
                with self.assertRaises(Phase2FCoordinationError):
                    state["provider"].encrypt_verified_package(request)
        self.assertEqual(
            hashlib.sha256(self._read_package(state)).hexdigest(),
            state["package"].plaintext_sha256,
        )

    def test_policy_timeout_capacity_and_header_bounds_fail_closed(self):
        state = self._encryption_fixture()
        package_size = state["package"].byte_count
        invalid_policies = (
            replace(self._policy(), chunk_bytes=1),
            replace(self._policy(), maximum_header_bytes=511),
            replace(self._policy(), timeout_seconds=0),
            replace(self._policy(), headroom_multiplier=float("nan")),
        )
        for policy in invalid_policies:
            with self.subTest(policy=policy):
                with self.assertRaises(EncryptionPolicyError):
                    policy.validated()

        bounded = EncryptedArtifactProvider(
            package_provider=state["package_provider"],
            verification_provider=state["verifier"],
            kek_provider=self._kek(),
            policy=self._policy(
                maximum_plaintext_bytes=package_size,
                maximum_artifact_bytes=package_size + 1,
            ),
        )
        with self.assertRaises(EncryptionPolicyError):
            bounded.encrypt_verified_package(state["request"])

        plaintext_bounded = EncryptedArtifactProvider(
            package_provider=state["package_provider"],
            verification_provider=state["verifier"],
            kek_provider=self._kek(),
            policy=self._policy(
                maximum_plaintext_bytes=package_size - 1,
                maximum_artifact_bytes=package_size + 1024**2,
            ),
        )
        with self.assertRaises(Phase2FCoordinationError):
            plaintext_bounded.encrypt_verified_package(state["request"])

        header_bounded = EncryptedArtifactProvider(
            package_provider=state["package_provider"],
            verification_provider=state["verifier"],
            kek_provider=self._kek(),
            policy=self._policy(maximum_header_bytes=512),
        )
        with self.assertRaises(EncryptionPolicyError):
            header_bounded.encrypt_verified_package(state["request"])

        no_capacity = EncryptedArtifactProvider(
            package_provider=state["package_provider"],
            verification_provider=state["verifier"],
            kek_provider=self._kek(),
            policy=self._policy(),
            disk_usage_provider=lambda _path: SimpleNamespace(free=0),
        )
        with self.assertRaises(EncryptedArtifactCreationError):
            no_capacity.encrypt_verified_package(state["request"])

        calls = {"count": 0}

        def expired_clock():
            calls["count"] += 1
            return 0.0 if calls["count"] == 1 else 61.0

        expired = EncryptedArtifactProvider(
            package_provider=state["package_provider"],
            verification_provider=state["verifier"],
            kek_provider=self._kek(),
            policy=self._policy(),
            monotonic=expired_clock,
        )
        with self.assertRaises(EncryptedArtifactCreationError):
            expired.encrypt_verified_package(state["request"])
        self.assertEqual(
            hashlib.sha256(self._read_package(state)).hexdigest(),
            state["package"].plaintext_sha256,
        )

    def test_publication_failure_cleans_owned_output_and_preserves_plaintext(self):
        state = self._encryption_fixture()

        def fail_publication(stage):
            if stage == "before_encrypted_publication":
                raise OSError("private encrypted staging path")

        provider = EncryptedArtifactProvider(
            package_provider=state["package_provider"],
            verification_provider=state["verifier"],
            kek_provider=self._kek(),
            policy=self._policy(),
            failure_hook=fail_publication,
        )
        with self.assertRaises(EncryptedArtifactCreationError) as raised:
            provider.encrypt_verified_package(state["request"])
        self.assertNotIn("private encrypted staging path", str(raised.exception))
        parent = self.workspace.path / WorkspaceArea.ENCRYPTED.value
        self.assertEqual(tuple(parent.iterdir()), ())
        self.assertEqual(
            hashlib.sha256(self._read_package(state)).hexdigest(),
            state["package"].plaintext_sha256,
        )

    def test_plaintext_cleanup_failure_keeps_valid_artifact_and_retries_exactly(self):
        state = self._encryption_fixture()
        with mock.patch.object(
            state["package_provider"],
            "cleanup_package",
            side_effect=PackageCleanupError(),
        ):
            result = self._encrypt(state)
        self.assertTrue(result.plaintext_cleanup_incomplete)
        self.assertTrue(
            state["provider"].validate_encrypted_artifact_evidence(
                context=state["fixture"]["context"],
                package=state["package"],
                verification=state["verification"],
                result=result,
            )
        )
        self.assertEqual(
            hashlib.sha256(self._read_package(state)).hexdigest(),
            state["package"].plaintext_sha256,
        )
        completed = state["provider"].retry_plaintext_package_cleanup(
            state["request"],
            result,
        )
        self.assertFalse(completed.plaintext_cleanup_incomplete)
        with self.assertRaises(PackageNotFound):
            with state["package_provider"].open_package(
                context=state["fixture"]["context"],
                reference=state["package"].reference,
            ):
                pass

    def test_publication_hardlink_ambiguity_is_preserved_not_deleted(self):
        state = self._encryption_fixture()
        identifier = uuid.uuid4()
        alias = None

        def create_unowned_alias(stage):
            nonlocal alias
            if stage != "after_encrypted_publication_link":
                return
            directory = (
                self.workspace.path
                / WorkspaceArea.ENCRYPTED.value
                / identifier.hex
            )
            final = directory / ARTIFACT_FILE_NAME
            alias = directory / "unowned-alias.bin"
            os.link(final, alias, follow_symlinks=False)
            raise OSError("private encrypted artifact path")

        provider = EncryptedArtifactProvider(
            package_provider=state["package_provider"],
            verification_provider=state["verifier"],
            kek_provider=self._kek(),
            policy=self._policy(),
            reference_factory=lambda: identifier,
            failure_hook=create_unowned_alias,
        )
        with self.assertRaises(EncryptedArtifactCreationError) as raised:
            provider.encrypt_verified_package(state["request"])
        self.assertTrue(raised.exception.cleanup_incomplete)
        self.assertNotIn("private encrypted artifact path", str(raised.exception))
        self.assertIsNotNone(alias)
        self.assertTrue(alias.exists())
        self.assertEqual(
            hashlib.sha256(self._read_package(state)).hexdigest(),
            state["package"].plaintext_sha256,
        )
        for path in tuple(alias.parent.iterdir()):
            path.unlink()
        alias.parent.rmdir()

    def test_encrypted_cleanup_is_exact_idempotent_and_rejects_forgery(self):
        state = self._encryption_fixture()
        result = self._encrypt(state)
        context = state["fixture"]["context"]
        forged = replace(context, backup_public_id=uuid.uuid4())
        with self.assertRaises(EncryptedArtifactCleanupError):
            state["provider"].cleanup_encrypted_artifact(
                context=forged,
                reference=result.reference,
            )
        path = self._artifact_path(state, result)
        alias = path.parent.parent / "unowned-hardlink.bin"
        os.link(path, alias, follow_symlinks=False)
        try:
            with self.assertRaises(EncryptedArtifactCleanupError):
                state["provider"].cleanup_encrypted_artifact(
                    context=context,
                    reference=result.reference,
                )
            self.assertTrue(path.exists())
        finally:
            alias.unlink()
        self.assertTrue(
            state["provider"].cleanup_encrypted_artifact(
                context=context,
                reference=result.reference,
            )
        )
        self.assertTrue(
            state["provider"].cleanup_encrypted_artifact(
                context=context,
                reference=result.reference,
            )
        )

    def test_abort_signals_are_preserved_and_plaintext_remains(self):
        state = self._encryption_fixture()
        for abort_type in (KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(abort_type=abort_type.__name__):
                provider = EncryptedArtifactProvider(
                    package_provider=state["package_provider"],
                    verification_provider=state["verifier"],
                    kek_provider=self._kek(),
                    policy=self._policy(),
                    failure_hook=lambda stage, abort_type=abort_type: (
                        (_ for _ in ()).throw(abort_type())
                        if stage == "before_encrypted_publication"
                        else None
                    ),
                )
                with self.assertRaises(abort_type):
                    provider.encrypt_verified_package(state["request"])
        self.assertEqual(
            hashlib.sha256(self._read_package(state)).hexdigest(),
            state["package"].plaintext_sha256,
        )

    def test_kek_configuration_checks_and_secret_sanitization(self):
        raw_key = b"s" * 32
        encoded_key = base64.b64encode(raw_key).decode("ascii")
        with self.assertRaises(KeyProviderConfigurationError):
            LocalConfiguredKekProvider(
                key_b64="missing",
                key_identifier="test",
                key_version="v1",
            )
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        last_index = alphabet.index(encoded_key[-2])
        noncanonical = encoded_key[:-2] + alphabet[last_index + 1] + "="
        self.assertEqual(base64.b64decode(noncanonical), raw_key)
        with self.assertRaises(KeyProviderConfigurationError):
            LocalConfiguredKekProvider(
                key_b64=noncanonical,
                key_identifier="test",
                key_version="v1",
            )
        with override_settings(
            BACKUP_EXECUTION_ENGINE_ENABLED=False,
            BACKUP_ENGINE_ENABLED=False,
            BACKUP_LOCAL_KEK_B64="",
            BACKUP_LOCAL_KEK_ID="",
            BACKUP_LOCAL_KEK_VERSION="",
        ):
            self.assertEqual(check_local_kek_configuration(None), [])
        with override_settings(
            BACKUP_EXECUTION_ENGINE_ENABLED=True,
            BACKUP_ENGINE_ENABLED=False,
            BACKUP_LOCAL_KEK_B64="",
            BACKUP_LOCAL_KEK_ID="",
            BACKUP_LOCAL_KEK_VERSION="",
        ):
            errors = check_local_kek_configuration(None)
            self.assertEqual([error.id for error in errors], ["backups.E028"])
        with override_settings(
            BACKUP_LOCAL_KEK_B64="invalid",
            BACKUP_LOCAL_KEK_ID="test",
            BACKUP_LOCAL_KEK_VERSION="v1",
        ):
            errors = check_local_kek_configuration(None)
            self.assertEqual([error.id for error in errors], ["backups.E028"])
        with override_settings(BACKUP_ENCRYPTION_CHUNK_BYTES=1):
            errors = check_encryption_policy_settings(None)
            self.assertEqual([error.id for error in errors], ["backups.E027"])

        provider = LocalConfiguredKekProvider(
            key_b64=encoded_key,
            key_identifier="safe-id",
            key_version="v1",
        )
        self.assertNotIn(encoded_key, repr(provider))
        self.assertNotIn(raw_key.hex(), repr(provider))
        try:
            provider.wrap_dek(b"bad", nonce=b"bad")
        except Exception as exc:
            self.assertNotIn(encoded_key, str(exc))
            self.assertNotIn(raw_key.hex(), str(exc))

    def test_raw_dek_and_kek_are_absent_from_metadata_and_errors(self):
        material = iter((b"d" * 32, b"n" * 12, b"w" * 12))
        state = self._encryption_fixture(random_bytes=lambda _size: next(material))
        result = self._encrypt(state)
        raw = self._read_artifact(state, result)
        _magic, header, _body = self._split_artifact(raw)
        serialized = encode_canonical_document(header)
        raw_dek_b64 = base64.b64encode(b"d" * 32)
        raw_kek_b64 = base64.b64encode(b"k" * 32)
        self.assertNotIn(raw_dek_b64, serialized)
        self.assertNotIn(raw_kek_b64, serialized)
        self.assertNotIn(raw_dek_b64.decode(), repr(result))
        self.assertNotIn(raw_kek_b64.decode(), repr(result))
        self.assertNotEqual(header["nonce_b64"], header["wrapped_dek"]["nonce_b64"])

    def test_capability_and_runtime_surfaces_remain_fail_closed(self):
        self.assertIs(ENCRYPTED_ARTIFACT_PROVIDER_READY, True)
        self.assertIs(OPERATIONAL_PROVIDER_STACK_READY, False)
        self.assertIs(real_execution_available(), False)
        capability = get_engine_capability()
        self.assertIs(capability.encrypted_artifact_provider_ready, True)
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
                self.assertNotIn("EncryptedArtifactProvider", source)
                self.assertNotIn("EncryptedArtifactRequest", source)


def load_tests(loader, standard_tests, pattern):
    """Run only Phase 2F cases; predecessor regressions have dedicated commands."""

    del loader, standard_tests, pattern
    names = sorted(
        name
        for name, value in EncryptedArtifactProviderTests.__dict__.items()
        if name.startswith("test_") and callable(value)
    )
    return unittest.TestSuite(EncryptedArtifactProviderTests(name) for name in names)
