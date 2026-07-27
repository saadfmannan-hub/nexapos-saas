"""Authoritative execution-context and manifest metadata builder."""

import uuid

from apps.backups.enums import BackupScope, BackupStatus, BackupTrigger, ProductOwner
from apps.backups.registry import COMPONENT_REGISTRY
from apps.backups.versioning import current_version_metadata

from .context import ActorIdentitySnapshot, BackupExecutionContext
from .exceptions import (
    BackupTenantMismatch,
    InvalidBackupExecutionState,
    ManifestBuildError,
)
from .manifest import InMemoryManifestBuilder

ALLOWED_PLANNING_STATUSES = frozenset({BackupStatus.QUEUED})


class BackupMetadataBuilder:
    """Derive authoritative values from models, services, and the registry."""

    def __init__(
        self,
        *,
        registry=COMPONENT_REGISTRY,
        manifest_builder=None,
        using="default",
    ):
        self.registry = registry
        self.manifest_builder = manifest_builder or InMemoryManifestBuilder()
        self.using = using

    def _validate_identity(self, *, business, backup_record):
        if (
            backup_record.business_id != business.pk
            or backup_record.tenant_public_id_snapshot != business.public_id
        ):
            raise BackupTenantMismatch()

    def _validate_versions(self, backup_record):
        current = current_version_metadata(using=self.using)
        expected = {
            "format_version": backup_record.format_version,
            "application_version": backup_record.application_version,
            "schema_fingerprint": backup_record.schema_fingerprint,
            "minimum_restore_version": backup_record.minimum_restore_version,
        }
        if (
            any(not str(value or "").strip() for value in expected.values())
            or expected != current
        ):
            raise ManifestBuildError(
                "The backup record version metadata does not match this deployment."
            )
        return current

    def build_context(
        self,
        *,
        business,
        backup_record,
        actor,
        scope_resolution,
    ) -> BackupExecutionContext:
        self._validate_identity(business=business, backup_record=backup_record)
        if backup_record.status not in ALLOWED_PLANNING_STATUSES:
            raise InvalidBackupExecutionState()
        if not getattr(self.registry, "definitions", None):
            raise ManifestBuildError("The component registry is unavailable.")
        versions = self._validate_versions(backup_record)
        try:
            scope = BackupScope(backup_record.scope)
            trigger = BackupTrigger(backup_record.trigger)
            products = tuple(
                ProductOwner(product)
                for product in scope_resolution.included_products
            )
        except (TypeError, ValueError) as exc:
            raise ManifestBuildError() from exc
        if scope != scope_resolution.scope or not products:
            raise ManifestBuildError("The resolved backup scope is inconsistent.")

        correlation_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            (
                f"nexa-backup:{backup_record.public_id}:"
                f"{backup_record.idempotency_key}"
            ),
        )
        return BackupExecutionContext(
            backup_public_id=backup_record.public_id,
            business_id=business.pk,
            business_public_id=business.public_id,
            requested_scope=scope,
            resolved_products=products,
            trigger_type=trigger,
            actor_identity=ActorIdentitySnapshot.from_actor(
                actor,
                system_actor=backup_record.system_actor,
            ),
            application_version=versions["application_version"],
            backup_format_version=versions["format_version"],
            schema_migration_fingerprint=versions["schema_fingerprint"],
            minimum_restore_version=versions["minimum_restore_version"],
            idempotency_key=backup_record.idempotency_key,
            operation_correlation_id=correlation_id,
        )

    def build_manifest(self, *, context, component_plan, backup_record):
        self._validate_identity(
            business=backup_record.business,
            backup_record=backup_record,
        )
        created_at = backup_record.created_at or backup_record.queued_at
        return self.manifest_builder.build(
            context=context,
            components=component_plan.export_components,
            created_at=created_at,
        )
