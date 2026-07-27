"""Versioned, deterministic in-memory manifest foundation."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from apps.backups.enums import (
    BackupScope,
    BackupTrigger,
    CompatibilityStatus,
    ProductOwner,
    RestoreBehavior,
)

from .contracts import ManifestBuilder
from .exceptions import ManifestBuildError


class ManifestVerificationState(StrEnum):
    NOT_VERIFIED = "NOT_VERIFIED"


def utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ManifestBuildError("Manifest timestamps must be timezone-aware.")
    normalized = value.astimezone(UTC)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ManifestCompatibilityMetadata:
    minimum_restore_version: str
    status: CompatibilityStatus
    database_engine_neutral: bool

    def to_ordered_dict(self):
        return {
            "minimum_restore_version": self.minimum_restore_version,
            "status": self.status.value,
            "database_engine_neutral": self.database_engine_neutral,
        }


@dataclass(frozen=True, slots=True)
class ManifestComponent:
    key: str
    product_owner: ProductOwner
    component_version: str
    restore_behavior: RestoreBehavior
    required_component_keys: tuple[str, ...]
    record_count: int | None = None
    media_count: int | None = None
    content_hash: str | None = None
    verification_state: ManifestVerificationState = (
        ManifestVerificationState.NOT_VERIFIED
    )

    def to_ordered_dict(self):
        return {
            "key": self.key,
            "product_owner": self.product_owner.value,
            "component_version": self.component_version,
            "restore_behavior": self.restore_behavior.value,
            "required_component_keys": list(self.required_component_keys),
            "record_count": self.record_count,
            "media_count": self.media_count,
            "content_hash": self.content_hash,
            "verification_state": self.verification_state.value,
        }


@dataclass(frozen=True, slots=True)
class BackupManifest:
    backup_format_version: str
    application_version: str
    schema_migration_fingerprint: str
    backup_public_id: str
    tenant_public_id: str
    scope: BackupScope
    included_products: tuple[ProductOwner, ...]
    included_component_keys: tuple[str, ...]
    components: tuple[ManifestComponent, ...]
    trigger_type: BackupTrigger
    created_timestamp: datetime
    compatibility: ManifestCompatibilityMetadata
    total_record_count: int | None
    total_media_count: int | None
    artifact_hash: str | None
    verification_state: ManifestVerificationState

    def to_ordered_dict(self):
        """Return stable field ordering without serializing an artifact."""

        return {
            "backup_format_version": self.backup_format_version,
            "application_version": self.application_version,
            "schema_migration_fingerprint": self.schema_migration_fingerprint,
            "backup_public_id": self.backup_public_id,
            "tenant_public_id": self.tenant_public_id,
            "scope": self.scope.value,
            "included_products": [product.value for product in self.included_products],
            "included_component_keys": list(self.included_component_keys),
            "components": [
                component.to_ordered_dict() for component in self.components
            ],
            "dependency_metadata": [
                {
                    "component_key": component.key,
                    "required_component_keys": list(
                        component.required_component_keys
                    ),
                }
                for component in self.components
            ],
            "trigger_type": self.trigger_type.value,
            "created_timestamp": utc_timestamp(self.created_timestamp),
            "compatibility": self.compatibility.to_ordered_dict(),
            "total_record_count": self.total_record_count,
            "total_media_count": self.total_media_count,
            "artifact_hash": self.artifact_hash,
            "verification_state": self.verification_state.value,
        }


class InMemoryManifestBuilder(ManifestBuilder):
    """Build metadata placeholders only; never serialize or verify an artifact."""

    def build(self, *, context, components, created_at):
        if not components:
            raise ManifestBuildError("A manifest requires registered components.")
        manifest_components = tuple(
            ManifestComponent(
                key=component.key,
                product_owner=component.product_owner,
                component_version=component.component_version,
                restore_behavior=component.restore_behavior,
                required_component_keys=component.required_component_keys,
            )
            for component in components
        )
        return BackupManifest(
            backup_format_version=context.backup_format_version,
            application_version=context.application_version,
            schema_migration_fingerprint=context.schema_migration_fingerprint,
            backup_public_id=str(context.backup_public_id),
            tenant_public_id=str(context.business_public_id),
            scope=context.requested_scope,
            included_products=context.resolved_products,
            included_component_keys=tuple(
                component.key for component in manifest_components
            ),
            components=manifest_components,
            trigger_type=context.trigger_type,
            created_timestamp=created_at,
            compatibility=ManifestCompatibilityMetadata(
                minimum_restore_version=context.minimum_restore_version,
                status=CompatibilityStatus.NOT_CHECKED,
                database_engine_neutral=True,
            ),
            total_record_count=None,
            total_media_count=None,
            artifact_hash=None,
            verification_state=ManifestVerificationState.NOT_VERIFIED,
        )
