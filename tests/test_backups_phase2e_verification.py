"""Focused security tests for Phase 2E independent package verification."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import uuid
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

from apps.backups.engine.availability import (
    INDEPENDENT_PACKAGE_VERIFIER_READY,
    OPERATIONAL_PROVIDER_STACK_READY,
    get_engine_capability,
    real_execution_available,
)
from apps.backups.engine.contracts import (
    PackageBuildRequest,
    PackageCompatibilityStatus,
    PackageVerificationRequest,
    VerificationReference,
)
from apps.backups.engine.deterministic_package import (
    DETERMINISTIC_ZIP_TIMESTAMP,
    PACKAGE_FILE_NAME,
)
from apps.backups.engine.logical_serialization import encode_canonical_document
from apps.backups.engine.package_exceptions import PackageNotFound
from apps.backups.engine.package_verification import (
    INDEPENDENT_PACKAGE_VERIFIER_IDENTIFIER,
    VERIFICATION_FILE_NAME,
    VERIFICATION_SCHEMA_IDENTIFIER,
    IndependentPackageVerifier,
    _component_content_sha256,
    _payload_set_sha256,
)
from apps.backups.engine.verification_exceptions import (
    VerificationCleanupError,
    VerificationEvidenceNotFound,
    VerificationProviderStateError,
    VerificationPublicationError,
)
from apps.backups.engine.workspace import WorkspaceArea

from . import test_backups_phase2d2_package as phase2d2_tests


class IndependentPackageVerifierTests(phase2d2_tests.DeterministicPackageProviderTests):
    def setUp(self):
        super().setUp()
        self.verification_cleanup = []

    def tearDown(self):
        for verifier, context, reference in reversed(self.verification_cleanup):
            try:
                verifier.cleanup_verification_evidence(
                    context=context,
                    reference=reference,
                )
            except Exception:
                pass
        super().tearDown()

    def _build_verification_fixture(self, **verifier_changes):
        fixture = self._build_phase2d1()
        package_provider = self._package_provider(fixture)
        package = package_provider.build_package(
            PackageBuildRequest(
                context=fixture["context"],
                phase2d1_result=fixture["phase_result"],
            )
        )
        self.package_cleanup.append((package_provider, fixture["context"], package.reference))
        values = {
            "package_provider": package_provider,
            "workspace_manager": self.manager,
        }
        values.update(verifier_changes)
        verifier = IndependentPackageVerifier(**values)
        return fixture, package_provider, package, verifier

    @staticmethod
    def _verify(verifier, context, package):
        return verifier.verify(
            PackageVerificationRequest(
                context=context,
                package=package,
            )
        )

    def _package_path(self, package):
        return (
            self.workspace.path
            / WorkspaceArea.PACKAGE.value
            / package.reference.identifier.hex
            / PACKAGE_FILE_NAME
        )

    def _replace_owned_package(self, provider, context, package, raw, **changes):
        path = self._package_path(package)
        with path.open("r+b") as output:
            output.seek(0)
            output.write(raw)
            output.truncate()
            output.flush()
            os.fsync(output.fileno())
        updated = replace(
            package,
            byte_count=len(raw),
            plaintext_sha256=hashlib.sha256(raw).hexdigest(),
            **changes,
        )
        key = (
            context.workspace_reference.identifier,
            package.reference.identifier,
        )
        evidence = provider._published[key]
        provider._published[key] = replace(evidence, result=updated)
        return updated

    @staticmethod
    def _deterministic_zip(entries):
        destination = io.BytesIO()
        with zipfile.ZipFile(
            destination,
            mode="w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
            strict_timestamps=True,
        ) as archive:
            for name, payload in entries:
                info = zipfile.ZipInfo(name, date_time=DETERMINISTIC_ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                info.internal_attr = 0
                info.comment = b""
                info.extra = b""
                with archive.open(info, mode="w", force_zip64=True) as output:
                    output.write(payload)
        return destination.getvalue()

    @staticmethod
    def _package_entries(provider, context, package):
        with provider.open_package(
            context=context,
            reference=package.reference,
        ) as reader:
            raw = b""
            while True:
                chunk = reader.read(1024**2)
                if not chunk:
                    break
                raw += chunk
        with zipfile.ZipFile(io.BytesIO(raw), mode="r") as archive:
            return [(info.filename, archive.read(info)) for info in archive.infolist()]

    def test_real_phase2d1_to_phase2d2_to_phase2e_happy_path(self):
        fixture, provider, package, verifier = self._build_verification_fixture()
        result = self._verify(verifier, fixture["context"], package)
        self.assertTrue(result.verified)
        self.assertTrue(result.restore_ready)
        self.assertEqual(result.issues, ())
        self.assertEqual(
            result.compatibility_status,
            PackageCompatibilityStatus.COMPATIBLE,
        )
        self.assertEqual(
            result.provider_identifier,
            INDEPENDENT_PACKAGE_VERIFIER_IDENTIFIER,
        )
        self.assertEqual(result.verification_schema, VERIFICATION_SCHEMA_IDENTIFIER)
        self.assertEqual(result.package_byte_count, package.byte_count)
        self.assertEqual(result.plaintext_sha256, package.plaintext_sha256)
        self.assertEqual(result.entry_count, package.entry_count)
        self.assertEqual(result.payload_set_sha256, package.payload_set_sha256)
        self.assertTrue(
            verifier.validate_verification_evidence(
                context=fixture["context"],
                package=package,
                result=result,
            )
        )
        self.verification_cleanup.append((verifier, fixture["context"], result.reference))
        with verifier.open_verification_evidence(
            context=fixture["context"],
            reference=result.reference,
        ) as reader:
            raw = reader.read()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result.evidence_sha256)
        self.assertEqual(len(raw), result.evidence_byte_count)
        self.assertEqual(raw, encode_canonical_document(json.loads(raw), trailing_lf=True))
        evidence = json.loads(raw)
        self.assertEqual(evidence["verified"], True)
        self.assertEqual(evidence["restore_ready"], True)
        self.assertNotIn(str(self.workspace.path), raw.decode("utf-8"))
        self.assertNotIn(str(fixture["context"].business_id), evidence)
        with provider.open_package(
            context=fixture["context"],
            reference=package.reference,
        ) as reader:
            self.assertTrue(reader.read(1))

    def test_verification_evidence_cleanup_is_exact_and_idempotent(self):
        fixture, _provider, package, verifier = self._build_verification_fixture()
        result = self._verify(verifier, fixture["context"], package)
        self.assertTrue(result.verified)
        self.assertTrue(
            verifier.cleanup_verification_evidence(
                context=fixture["context"],
                reference=result.reference,
            )
        )
        self.assertTrue(
            verifier.cleanup_verification_evidence(
                context=fixture["context"],
                reference=result.reference,
            )
        )
        with self.assertRaises(VerificationEvidenceNotFound):
            with verifier.open_verification_evidence(
                context=fixture["context"],
                reference=result.reference,
            ):
                pass

    def test_forged_context_reference_and_build_metadata_are_rejected(self):
        fixture, _provider, package, verifier = self._build_verification_fixture()
        forged_context = replace(
            fixture["context"],
            backup_public_id=uuid.uuid4(),
        )
        forged_context_result = self._verify(verifier, forged_context, package)
        self.assertFalse(forged_context_result.verified)
        self.assertEqual(
            forged_context_result.issues[0].code,
            "package_evidence_rejected",
        )
        forged_reference = replace(
            package,
            reference=replace(package.reference, identifier=uuid.uuid4()),
        )
        self.assertFalse(self._verify(verifier, fixture["context"], forged_reference).verified)
        forged_metadata = replace(package, byte_count=package.byte_count + 1)
        self.assertFalse(self._verify(verifier, fixture["context"], forged_metadata).verified)

    def test_mutation_truncation_and_trailing_bytes_fail_and_keep_package(self):
        fixture, provider, package, verifier = self._build_verification_fixture()
        path = self._package_path(package)
        original = path.read_bytes()
        for mutation in ("one-byte", "truncated", "trailing"):
            with self.subTest(mutation=mutation):
                if mutation == "one-byte":
                    changed = bytearray(original)
                    changed[len(changed) // 2] ^= 1
                    changed = bytes(changed)
                elif mutation == "truncated":
                    changed = original[:-1]
                else:
                    changed = original + b"trailing"
                with path.open("r+b") as output:
                    output.write(changed)
                    output.truncate()
                result = self._verify(verifier, fixture["context"], package)
                self.assertFalse(result.verified)
                self.assertTrue(path.exists())
                self.assertEqual(path.read_bytes(), changed)
                with self.assertRaises(PackageNotFound):
                    with provider.open_package(
                        context=fixture["context"],
                        reference=package.reference,
                    ):
                        pass
        with path.open("r+b") as output:
            output.write(original)
            output.truncate()

    def test_structural_archive_variants_are_rejected(self):
        fixture, provider, package, verifier = self._build_verification_fixture()
        entries = self._package_entries(provider, fixture["context"], package)
        variants = {
            "missing": entries[:-1],
            "extra": [*entries, ("extra.bin", b"x")],
            "reordered": [entries[0], entries[2], entries[1], *entries[3:]],
            "duplicate": [*entries, entries[-1]],
            "case-fold-collision": [*entries, ("MANIFEST.JSON", b"x")],
            "absolute": [("/manifest.json", entries[0][1]), *entries[1:]],
            "traversal": [("../manifest.json", entries[0][1]), *entries[1:]],
            "backslash": [("manifest.json\\x", entries[0][1]), *entries[1:]],
        }
        for name, variant in variants.items():
            with self.subTest(name=name):
                raw = self._deterministic_zip(variant)
                updated = self._replace_owned_package(
                    provider,
                    fixture["context"],
                    package,
                    raw,
                    entry_count=len(variant),
                )
                result = self._verify(verifier, fixture["context"], updated)
                self.assertFalse(result.verified)
                self.assertIn(
                    result.issues[0].code,
                    {"zip_structure_invalid", "manifest_invalid"},
                )
                package = updated

    def test_raw_central_directory_trailing_and_device_metadata_are_rejected(self):
        fixture, provider, package, verifier = self._build_verification_fixture()
        entries = self._package_entries(provider, fixture["context"], package)
        valid = self._deterministic_zip(entries)
        variants = [("trailing", valid + b"x")]

        invalid_central = bytearray(valid)
        central_offset = invalid_central.index(b"PK\x01\x02")
        invalid_central[central_offset] ^= 1
        variants.append(("central-signature", bytes(invalid_central)))

        device_metadata = bytearray(valid)
        central_offset = device_metadata.index(b"PK\x01\x02")
        device_metadata[central_offset + 38 : central_offset + 42] = (
            (stat.S_IFCHR | 0o600) << 16
        ).to_bytes(4, "little")
        variants.append(("device-metadata", bytes(device_metadata)))

        for name, raw in variants:
            with self.subTest(name=name):
                package = self._replace_owned_package(
                    provider,
                    fixture["context"],
                    package,
                    raw,
                )
                result = self._verify(verifier, fixture["context"], package)
                self.assertFalse(result.verified)
                self.assertEqual(result.issues[0].code, "zip_structure_invalid")

    def test_compression_encryption_and_metadata_variants_are_rejected(self):
        fixture, provider, package, verifier = self._build_verification_fixture()
        entries = self._package_entries(provider, fixture["context"], package)

        destination = io.BytesIO()
        with zipfile.ZipFile(
            destination,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for name, payload in entries:
                archive.writestr(name, payload)
        compressed = self._replace_owned_package(
            provider,
            fixture["context"],
            package,
            destination.getvalue(),
        )
        self.assertFalse(self._verify(verifier, fixture["context"], compressed).verified)

        raw = bytearray(self._deterministic_zip(entries))
        raw[6:8] = (1).to_bytes(2, "little")
        central = raw.index(b"PK\x01\x02")
        raw[central + 8 : central + 10] = (1).to_bytes(2, "little")
        encrypted = self._replace_owned_package(
            provider,
            fixture["context"],
            compressed,
            bytes(raw),
        )
        self.assertFalse(self._verify(verifier, fixture["context"], encrypted).verified)

        raw = bytearray(self._deterministic_zip(entries))
        raw[10:12] = (1).to_bytes(2, "little")
        central = raw.index(b"PK\x01\x02")
        raw[central + 12 : central + 14] = (1).to_bytes(2, "little")
        nondeterministic = self._replace_owned_package(
            provider,
            fixture["context"],
            encrypted,
            bytes(raw),
        )
        self.assertFalse(self._verify(verifier, fixture["context"], nondeterministic).verified)

    def test_manifest_canonical_and_metadata_variants_are_rejected(self):
        fixture, provider, package, verifier = self._build_verification_fixture()
        entries = self._package_entries(provider, fixture["context"], package)
        manifest = entries[0][1]
        variants = {
            "bom": b"\xef\xbb\xbf" + manifest,
            "crlf": manifest[:-1] + b"\r\n",
            "duplicate-key": manifest.replace(
                b'{"backup":',
                b'{"schema":"duplicate","backup":',
                1,
            ),
            "float": manifest.replace(b'"missing_media_count":0', b'"missing_media_count":0.0'),
            "wrong-schema": manifest.replace(
                b'"nexa.backup-manifest.v1"',
                b'"nexa.backup-manifest.v9"',
                1,
            ),
            "package-hash": manifest[:-2] + b',"package_sha256":"' + (b"0" * 64) + b'"}\n',
        }
        wrong_tenant = json.loads(manifest)
        wrong_tenant["backup"]["tenant_public_id"] = str(uuid.uuid4())
        variants["wrong-tenant"] = encode_canonical_document(
            wrong_tenant,
            trailing_lf=True,
        )
        wrong_component = json.loads(manifest)
        wrong_component["components"][0]["records"]["package_path"] = (
            "components/9999/records.ndjson"
        )
        variants["wrong-component-path"] = encode_canonical_document(
            wrong_component,
            trailing_lf=True,
        )
        for name, changed_manifest in variants.items():
            with self.subTest(name=name):
                changed_entries = [(entries[0][0], changed_manifest), *entries[1:]]
                raw = self._deterministic_zip(changed_entries)
                package = self._replace_owned_package(
                    provider,
                    fixture["context"],
                    package,
                    raw,
                )
                result = self._verify(verifier, fixture["context"], package)
                self.assertFalse(result.verified)
                self.assertEqual(result.issues[0].code, "manifest_invalid")

    def test_payload_hash_count_totals_and_media_source_variants_are_rejected(self):
        fixture, provider, package, verifier = self._build_verification_fixture()
        entries = self._package_entries(provider, fixture["context"], package)
        manifest = json.loads(entries[0][1])
        variants = []

        wrong_component = json.loads(json.dumps(manifest))
        wrong_component["components"][0]["records"]["sha256"] = "0" * 64
        variants.append(("component-hash", wrong_component, entries[1:]))

        wrong_payload = json.loads(json.dumps(manifest))
        wrong_payload["payload_set_sha256"] = "0" * 64
        variants.append(("payload-set", wrong_payload, entries[1:]))

        wrong_totals = json.loads(json.dumps(manifest))
        wrong_totals["totals"]["record_count"] += 1
        variants.append(("totals", wrong_totals, entries[1:]))

        if manifest["media"]:
            duplicate_source = json.loads(json.dumps(manifest))
            duplicate_source["media"][0]["sources"].append(
                duplicate_source["media"][0]["sources"][0]
            )
            duplicate_source["media"][0]["source_reference_count"] += 1
            duplicate_source["totals"]["media_reference_count"] += 1
            variants.append(("duplicate-source", duplicate_source, entries[1:]))

        for name, document, payload_entries in variants:
            with self.subTest(name=name):
                raw_manifest = encode_canonical_document(document, trailing_lf=True)
                raw = self._deterministic_zip([("manifest.json", raw_manifest), *payload_entries])
                package = self._replace_owned_package(
                    provider,
                    fixture["context"],
                    package,
                    raw,
                )
                result = self._verify(verifier, fixture["context"], package)
                self.assertFalse(result.verified)

    def test_registry_component_version_incompatibility_is_not_restore_ready(self):
        fixture, provider, package, verifier = self._build_verification_fixture()
        entries = self._package_entries(provider, fixture["context"], package)
        document = json.loads(entries[0][1])
        component = document["components"][0]
        component["component_version"] = "999.0.0"
        records_name, records_raw = entries[1]
        rewritten_records = []
        for line in records_raw.splitlines():
            payload = json.loads(line)
            payload["component_version"] = "999.0.0"
            rewritten_records.append(encode_canonical_document(payload, trailing_lf=True))
        new_records = b"".join(rewritten_records)
        component["records"]["byte_count"] = len(new_records)
        component["records"]["sha256"] = hashlib.sha256(new_records).hexdigest()
        component["component_content_sha256"] = _component_content_sha256(component)
        document["totals"]["component_records_bytes"] += len(new_records) - len(records_raw)
        document["totals"]["planned_payload_bytes"] += len(new_records) - len(records_raw)
        document["payload_set_sha256"] = _payload_set_sha256(document)
        rewritten_entries = [
            (
                "manifest.json",
                encode_canonical_document(document, trailing_lf=True),
            ),
            (records_name, new_records),
            *entries[2:],
        ]
        raw = self._deterministic_zip(rewritten_entries)
        package = self._replace_owned_package(
            provider,
            fixture["context"],
            package,
            raw,
            payload_set_sha256=document["payload_set_sha256"],
        )
        result = self._verify(verifier, fixture["context"], package)
        self.assertTrue(result.verified)
        self.assertFalse(result.restore_ready)
        self.assertEqual(
            result.compatibility_status,
            PackageCompatibilityStatus.INCOMPATIBLE,
        )
        self.assertEqual(result.issues[0].code, "compatibility_incompatible")
        self.verification_cleanup.append((verifier, fixture["context"], result.reference))

    def test_minimum_restore_version_incompatible_and_not_proven_states(self):
        fixture, provider, package, verifier = self._build_verification_fixture()
        entries = self._package_entries(
            provider,
            fixture["context"],
            package,
        )
        for application, minimum, expected in (
            ("1.0.0", "999.0.0", PackageCompatibilityStatus.INCOMPATIBLE),
            (
                "source-release",
                "future-release",
                PackageCompatibilityStatus.NOT_PROVEN,
            ),
        ):
            with self.subTest(expected=expected.value):
                document = json.loads(entries[0][1])
                document["backup"]["application_version"] = application
                document["backup"]["minimum_restore_version"] = minimum
                document["compatibility"]["minimum_restore_version"] = minimum
                raw = self._deterministic_zip(
                    [
                        (
                            "manifest.json",
                            encode_canonical_document(
                                document,
                                trailing_lf=True,
                            ),
                        ),
                        *entries[1:],
                    ]
                )
                context = replace(
                    fixture["context"],
                    application_version=application,
                    minimum_restore_version=minimum,
                )
                package = self._replace_owned_package(
                    provider,
                    context,
                    package,
                    raw,
                )
                key = (
                    context.workspace_reference.identifier,
                    package.reference.identifier,
                )
                provider._published[key] = replace(
                    provider._published[key],
                    context=context,
                )
                self.package_cleanup[-1] = (
                    provider,
                    context,
                    package.reference,
                )
                result = self._verify(verifier, context, package)
                self.assertTrue(result.verified)
                self.assertFalse(result.restore_ready)
                self.assertEqual(result.compatibility_status, expected)
                self.verification_cleanup.append((verifier, context, result.reference))

    def test_publication_cleanup_failure_abort_and_unowned_hardlink_behavior(self):
        fixture, package_provider, package, _verifier = self._build_verification_fixture()
        for abort_type in (KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(abort_type=abort_type.__name__):
                verifier = IndependentPackageVerifier(
                    package_provider=package_provider,
                    workspace_manager=self.manager,
                    failure_hook=lambda stage, abort_type=abort_type: (
                        (_ for _ in ()).throw(abort_type())
                        if stage == "before_verification_publication"
                        else None
                    ),
                )
                with self.assertRaises(abort_type):
                    self._verify(verifier, fixture["context"], package)

        identifier = uuid.uuid4()
        alias = None

        def create_alias(stage):
            nonlocal alias
            if stage != "after_verification_publication_link":
                return
            directory = self.workspace.path / WorkspaceArea.VERIFICATION.value / identifier.hex
            final = directory / VERIFICATION_FILE_NAME
            alias = directory / "unowned-alias.json"
            os.link(final, alias, follow_symlinks=False)
            raise OSError("private evidence path")

        verifier = IndependentPackageVerifier(
            package_provider=package_provider,
            workspace_manager=self.manager,
            reference_factory=lambda: VerificationReference(identifier),
            failure_hook=create_alias,
        )
        with self.assertRaises(VerificationPublicationError) as raised:
            self._verify(verifier, fixture["context"], package)
        self.assertTrue(raised.exception.cleanup_incomplete)
        self.assertNotIn("private evidence path", str(raised.exception))
        self.assertTrue(alias.exists())
        for path in tuple(alias.parent.iterdir()):
            path.unlink()
        alias.parent.rmdir()

    def test_cleanup_failure_is_retryable_and_sanitized(self):
        fixture, _provider, package, verifier = self._build_verification_fixture()
        result = self._verify(verifier, fixture["context"], package)
        original_rmdir = os.rmdir
        failed = {"done": False}

        def fail_once(path, *args, **kwargs):
            if Path(path).name == result.reference.identifier.hex and not failed["done"]:
                failed["done"] = True
                raise OSError("private evidence cleanup path")
            return original_rmdir(path, *args, **kwargs)

        with mock.patch(
            "apps.backups.engine.package_verification.os.rmdir",
            side_effect=fail_once,
        ):
            with self.assertRaises(VerificationCleanupError) as raised:
                verifier.cleanup_verification_evidence(
                    context=fixture["context"],
                    reference=result.reference,
                )
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("private evidence cleanup path", str(raised.exception))
        self.assertTrue(
            verifier.cleanup_verification_evidence(
                context=fixture["context"],
                reference=result.reference,
            )
        )

    def test_cleanup_preserves_process_abort_and_retry_state(self):
        fixture, _provider, package, verifier = self._build_verification_fixture()
        original_unlink = os.unlink
        for abort_type in (KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(abort_type=abort_type.__name__):
                result = self._verify(verifier, fixture["context"], package)

                def unlink_then_abort(path, *args, abort_type=abort_type, **kwargs):
                    original_unlink(path, *args, **kwargs)
                    raise abort_type()

                with mock.patch(
                    "apps.backups.engine.package_verification.os.unlink",
                    side_effect=unlink_then_abort,
                ):
                    with self.assertRaises(abort_type):
                        verifier.cleanup_verification_evidence(
                            context=fixture["context"],
                            reference=result.reference,
                        )
                self.assertTrue(
                    verifier.cleanup_verification_evidence(
                        context=fixture["context"],
                        reference=result.reference,
                    )
                )

    def test_capability_and_runtime_surfaces_remain_fail_closed(self):
        self.assertIs(INDEPENDENT_PACKAGE_VERIFIER_READY, True)
        self.assertIs(OPERATIONAL_PROVIDER_STACK_READY, False)
        self.assertIs(real_execution_available(), False)
        capability = get_engine_capability()
        self.assertIs(capability.independent_package_verifier_ready, True)
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
                self.assertNotIn("IndependentPackageVerifier", source)
                self.assertNotIn("PackageVerificationRequest", source)

    def test_exact_provider_type_and_forged_verification_reference_are_rejected(self):
        fixture, provider, package, verifier = self._build_verification_fixture()

        class PackageProviderSubclass(type(provider)):
            pass

        with self.assertRaises(VerificationProviderStateError):
            IndependentPackageVerifier(
                package_provider=PackageProviderSubclass(
                    component_exporter=provider.component_exporter,
                    media_capture_provider=provider.media_capture_provider,
                    manifest_provider=provider.manifest_provider,
                    workspace_manager=self.manager,
                )
            )
        result = self._verify(verifier, fixture["context"], package)
        with self.assertRaises(VerificationEvidenceNotFound):
            with verifier.open_verification_evidence(
                context=fixture["context"],
                reference=VerificationReference(uuid.uuid4()),
            ):
                pass
        self.verification_cleanup.append((verifier, fixture["context"], result.reference))
