"""Typed provider contracts for later operational backup phases."""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
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


@dataclass(frozen=True, slots=True)
class VerificationIssue:
    code: str
    sanitized_message: str


@dataclass(frozen=True, slots=True)
class VerificationRequest:
    context: "BackupExecutionContext"
    package: PackageReference
    manifest: "BackupManifest"


@dataclass(frozen=True, slots=True)
class VerificationResult:
    verified: bool
    issues: tuple[VerificationIssue, ...]
    verified_at: datetime | None


class VerificationProvider(ABC):
    @abstractmethod
    def verify(self, request: VerificationRequest) -> VerificationResult:
        """Verify a future artifact and its restore readiness."""


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
