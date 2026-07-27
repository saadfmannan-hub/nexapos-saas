"""Immutable execution identity for backup planning and future workers."""

import uuid
from dataclasses import dataclass, replace

from apps.backups.enums import BackupScope, BackupTrigger, ProductOwner

from .workspace import WorkspaceReference


@dataclass(frozen=True, slots=True)
class ActorIdentitySnapshot:
    public_id: str
    email: str
    full_name: str
    actor_type: str
    platform_staff: bool

    @classmethod
    def from_actor(cls, actor, *, system_actor=False):
        if actor is None:
            return cls(
                public_id="",
                email="",
                full_name="",
                actor_type="SYSTEM" if system_actor else "UNKNOWN",
                platform_staff=False,
            )
        return cls(
            public_id=str(getattr(actor, "public_id", "")),
            email=str(getattr(actor, "email", ""))[:254],
            full_name=str(getattr(actor, "full_name", ""))[:150],
            actor_type="PLATFORM" if getattr(actor, "is_platform_staff", False) else "TENANT",
            platform_staff=bool(getattr(actor, "is_platform_staff", False)),
        )


@dataclass(frozen=True, slots=True)
class BackupExecutionContext:
    backup_public_id: uuid.UUID
    business_id: int
    business_public_id: uuid.UUID
    requested_scope: BackupScope
    resolved_products: tuple[ProductOwner, ...]
    trigger_type: BackupTrigger
    actor_identity: ActorIdentitySnapshot
    application_version: str
    backup_format_version: str
    schema_migration_fingerprint: str
    minimum_restore_version: str
    idempotency_key: str
    operation_correlation_id: uuid.UUID
    workspace_reference: WorkspaceReference | None = None

    def with_workspace(self, reference) -> "BackupExecutionContext":
        """Return a new context; the original context remains unchanged."""

        return replace(
            self,
            workspace_reference=WorkspaceReference.parse(reference),
        )
