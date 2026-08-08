"""Typed provider contracts for later operational backup phases."""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .context import BackupExecutionContext
    from .manifest import BackupManifest
    from .pipeline import ComponentPlanItem


@dataclass(frozen=True, slots=True)
class SnapshotReference:
    identifier: uuid.UUID


@dataclass(frozen=True, slots=True)
class SnapshotRequest:
    context: "BackupExecutionContext"


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    reference: SnapshotReference
    created_at: datetime
    consistent: bool
    byte_count: int = 0
    page_count: int = 0
    page_size: int = 0
    schema_version: int = 0
    journal_mode: str = ""
    duration_ms: int = 0
    provider_identifier: str = ""
    consistency_cutoff_at: datetime | None = None


class SnapshotProvider(ABC):
    @abstractmethod
    def create_snapshot(self, request: SnapshotRequest) -> SnapshotResult:
        """Return an opaque snapshot reference without exposing a path."""


@dataclass(frozen=True, slots=True)
class ComponentExportReference:
    identifier: uuid.UUID


@dataclass(frozen=True, slots=True)
class ComponentExportRequest:
    context: "BackupExecutionContext"
    component: "ComponentPlanItem"
    snapshot: SnapshotReference
    component_plan: tuple["ComponentPlanItem", ...] = ()


@dataclass(frozen=True, slots=True)
class ComponentExportResult:
    component_key: str
    reference: ComponentExportReference
    row_count: int
    media_count: int
    deterministic_ordering_version: str
    model_counts: tuple[tuple[str, int], ...] = ()
    byte_count: int = 0
    media_index_byte_count: int = 0
    component_version: str = ""
    record_schema_version: str = ""
    created_at: datetime | None = None
    duration_ms: int = 0
    provider_identifier: str = ""


class ComponentExporter(ABC):
    @abstractmethod
    def export_component(
        self,
        request: ComponentExportRequest,
    ) -> ComponentExportResult:
        """Export one explicitly registered component."""


@dataclass(frozen=True, slots=True)
class MediaCaptureReference:
    identifier: uuid.UUID


@dataclass(frozen=True, slots=True)
class MediaCaptureResult:
    reference: MediaCaptureReference
    logical_storage_name: str
    byte_count: int
    sha256: str
    source_reference_count: int
    captured_at: datetime
    duration_ms: int
    provider_identifier: str


@dataclass(frozen=True, slots=True)
class ManifestReference:
    identifier: uuid.UUID


@dataclass(frozen=True, slots=True)
class CanonicalManifestResult:
    reference: ManifestReference
    byte_count: int
    sha256: str
    component_count: int
    unique_media_object_count: int
    total_record_count: int
    total_media_bytes: int
    payload_set_sha256: str
    schema_identifier: str
    created_at: datetime
    provider_identifier: str


@dataclass(frozen=True, slots=True)
class Phase2D1Request:
    context: "BackupExecutionContext"
    snapshot_result: SnapshotResult
    component_plan: tuple["ComponentPlanItem", ...]
    component_exports: tuple[ComponentExportResult, ...]


@dataclass(frozen=True, slots=True)
class Phase2D1Result:
    component_exports: tuple[ComponentExportResult, ...]
    media_captures: tuple[MediaCaptureResult, ...]
    manifest: CanonicalManifestResult


class ManifestBuilder(ABC):
    @abstractmethod
    def build(
        self,
        *,
        context: "BackupExecutionContext",
        components: tuple["ComponentPlanItem", ...],
        created_at: datetime,
    ) -> "BackupManifest":
        """Build in-memory manifest metadata without claiming verification."""


@dataclass(frozen=True, slots=True)
class PackageReference:
    identifier: uuid.UUID


@dataclass(frozen=True, slots=True)
class PackageBuildRequest:
    context: "BackupExecutionContext"
    phase2d1_result: Phase2D1Result


@dataclass(frozen=True, slots=True)
class PackageBuildResult:
    reference: PackageReference
    byte_count: int
    plaintext_sha256: str
    entry_count: int
    payload_set_sha256: str
    format_identifier: str
    created_at: datetime
    provider_identifier: str


class PackageBuilder(ABC):
    @abstractmethod
    def build_package(self, request: PackageBuildRequest) -> PackageBuildResult:
        """Assemble a deterministic private plaintext package."""


@dataclass(frozen=True, slots=True)
class Phase2D2Request:
    context: "BackupExecutionContext"
    phase2d1_result: Phase2D1Result


@dataclass(frozen=True, slots=True)
class Phase2D2Result:
    package: PackageBuildResult


class PackageCompatibilityStatus(StrEnum):
    COMPATIBLE = "COMPATIBLE"
    INCOMPATIBLE = "INCOMPATIBLE"
    NOT_PROVEN = "NOT_PROVEN"


@dataclass(frozen=True, slots=True)
class VerificationReference:
    identifier: uuid.UUID


@dataclass(frozen=True, slots=True)
class VerificationIssue:
    code: str
    sanitized_message: str


@dataclass(frozen=True, slots=True)
class PackageVerificationRequest:
    context: "BackupExecutionContext"
    package: PackageBuildResult


@dataclass(frozen=True, slots=True)
class RestoreReadinessResult:
    restore_ready: bool
    compatibility_status: PackageCompatibilityStatus
    issue_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PackageVerificationResult:
    reference: VerificationReference | None
    verified: bool
    restore_ready: bool
    verified_at: datetime
    package_byte_count: int
    plaintext_sha256: str
    entry_count: int
    manifest_sha256: str
    payload_set_sha256: str
    compatibility_status: PackageCompatibilityStatus
    provider_identifier: str
    verification_schema: str
    issues: tuple[VerificationIssue, ...]
    restore_readiness: RestoreReadinessResult
    evidence_byte_count: int = 0
    evidence_sha256: str = ""


class VerificationProvider(ABC):
    @abstractmethod
    def verify(
        self,
        request: PackageVerificationRequest,
    ) -> PackageVerificationResult:
        """Independently verify a package and publish safe readiness evidence."""


# Compatibility aliases retained for callers that imported the Phase 1 names.
VerificationRequest = PackageVerificationRequest
VerificationResult = PackageVerificationResult


@dataclass(frozen=True, slots=True)
class EncryptedArtifactReference:
    identifier: uuid.UUID


@dataclass(frozen=True, slots=True)
class EncryptedArtifactRequest:
    context: "BackupExecutionContext"
    package: PackageBuildResult
    verification: PackageVerificationResult


@dataclass(frozen=True, slots=True)
class EncryptedArtifactResult:
    reference: EncryptedArtifactReference
    encrypted_byte_count: int
    ciphertext_sha256: str
    plaintext_byte_count: int
    plaintext_sha256: str
    header_sha256: str
    format_identifier: str
    encryption_algorithm: str
    kek_provider_identifier: str
    kek_key_identifier: str
    kek_version: str
    created_at: datetime
    provider_identifier: str
    plaintext_cleanup_incomplete: bool


class StoredObjectDurabilityState(StrEnum):
    STORED = "STORED"


class StoredObjectVerificationState(StrEnum):
    STORED_AND_VERIFIED = "STORED_AND_VERIFIED"


@dataclass(frozen=True, slots=True)
class StoredBackupObjectReference:
    identifier: uuid.UUID


@dataclass(frozen=True, slots=True)
class StoredBackupObjectRequest:
    context: "BackupExecutionContext"
    encrypted_artifact: EncryptedArtifactResult


@dataclass(frozen=True, slots=True)
class StoredBackupObjectResult:
    reference: StoredBackupObjectReference
    backend_identifier: str
    object_schema_identifier: str
    byte_count: int
    sha256: str
    source_encrypted_artifact_sha256: str
    backup_public_id: uuid.UUID
    tenant_public_id: uuid.UUID
    stored_at: datetime
    provider_identifier: str
    durability_state: StoredObjectDurabilityState
    verification_state: StoredObjectVerificationState
    encrypted_format_identifier: str
    encryption_algorithm: str
    kek_provider_identifier: str
    kek_key_identifier: str
    kek_version: str
    encrypted_staging_cleanup_incomplete: bool


@dataclass(frozen=True, slots=True)
class PersistedStoredObjectDescriptor:
    """DB-backed identity used only for provider-owned restart re-attestation."""

    reference: StoredBackupObjectReference
    backend_identifier: str
    byte_count: int
    sha256: str
    backup_public_id: uuid.UUID
    tenant_public_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class ReattestedStoredObjectResult:
    """Short-lived ownership evidence for one exact historical durable object."""

    reference: StoredBackupObjectReference
    backend_identifier: str
    object_schema_identifier: str
    byte_count: int
    sha256: str
    backup_public_id: uuid.UUID
    tenant_public_id: uuid.UUID
    provider_identifier: str
    attested_at: datetime


@dataclass(frozen=True, slots=True)
class RestoredPlaintextEvidence:
    """Authenticated Phase 2F header evidence for a restored plaintext stream."""

    plaintext_byte_count: int
    plaintext_sha256: str
    encrypted_byte_count: int
    ciphertext_sha256: str
    header_sha256: str
    encrypted_format_identifier: str
    encrypted_format_version: str
    encryption_algorithm: str
    verified_package_format: str
    backup_public_id: uuid.UUID
    tenant_public_id: uuid.UUID
    kek_provider_identifier: str
    kek_key_identifier: str
    kek_version: str
    verification_schema: str
    verification_version: str
    verification_provider: str
    created_at: datetime


class DurableBackupStorageProvider(ABC):
    @abstractmethod
    def store_encrypted_artifact(
        self,
        request: StoredBackupObjectRequest,
    ) -> StoredBackupObjectResult:
        """Durably store and independently verify an encrypted artifact."""

    @abstractmethod
    def validate_stored_object(
        self,
        *,
        context: "BackupExecutionContext",
        result: StoredBackupObjectResult,
    ) -> bool:
        """Validate exact provider-held durable object evidence."""

    @abstractmethod
    def open_stored_object(
        self,
        *,
        context: "BackupExecutionContext",
        reference: StoredBackupObjectReference,
    ):
        """Open an owned durable object through an opaque reader."""

    @abstractmethod
    def owns_stored_object_reference(
        self,
        *,
        context: "BackupExecutionContext",
        reference: StoredBackupObjectReference,
    ) -> bool:
        """Return whether the exact context owns the opaque reference."""

    @abstractmethod
    def owns_stored_object_result(
        self,
        *,
        context: "BackupExecutionContext",
        result: StoredBackupObjectResult,
    ) -> bool:
        """Return whether result metadata exactly matches provider evidence."""

    @abstractmethod
    def confirm_stored_object_absent(
        self,
        *,
        context: "BackupExecutionContext",
        reference: StoredBackupObjectReference,
    ) -> bool:
        """Confirm an exact provider deletion tombstone and absent object."""

    @abstractmethod
    def delete_stored_object(
        self,
        *,
        context: "BackupExecutionContext",
        reference: StoredBackupObjectReference,
    ) -> bool:
        """Delete only an exactly owned durable object."""


@dataclass(frozen=True, slots=True)
class StorageObjectReference:
    backend_identifier: str
    opaque_object_key: str


@dataclass(frozen=True, slots=True)
class StorageWriteRequest:
    context: "BackupExecutionContext"
    package: PackageReference


@dataclass(frozen=True, slots=True)
class StorageWriteResult:
    reference: StorageObjectReference
    stored_at: datetime


class StorageProvider(ABC):
    @abstractmethod
    def store(self, request: StorageWriteRequest) -> StorageWriteResult:
        """Store a private artifact in a future provider."""

    @abstractmethod
    def retrieve(self, reference: StorageObjectReference) -> PackageReference:
        """Return an opaque local package reference for a future restore."""

    @abstractmethod
    def delete(self, reference: StorageObjectReference) -> None:
        """Delete a future private artifact."""
