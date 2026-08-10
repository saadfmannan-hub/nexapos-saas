"""Safe Phase 1 metadata services for backup and restore orchestration."""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.subscriptions.access import calculate_effective_modules, require_actor_access
from apps.subscriptions.models import Subscription

from .enums import (
    ActivitySeverity,
    BackupScope,
    BackupStatus,
    BackupTrigger,
    CompatibilityStatus,
    IntegrityStatus,
    OperationKind,
    ProductOwner,
    RestoreStatus,
)
from .models import (
    BackupActivity,
    BackupRecord,
    BackupSchedule,
    RestoreOperation,
    TenantOperationLock,
)
from .registry import ComponentDefinition, ScopeResolutionError, resolve_components
from .state_machines import (
    validate_backup_transition,
    validate_integrity_transition,
    validate_restore_transition,
)
from .versioning import assess_restore_compatibility, current_version_metadata

_PRODUCT_ORDER = (ProductOwner.POS, ProductOwner.WMS)
_EVENT_TYPE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_SENSITIVE_METADATA_TERMS = (
    "password",
    "secret",
    "token",
    "credential",
    "encryption_key",
    "data_key",
    "object_key",
    "object_version",
    "storage_bucket",
    "storage_key",
    "private_key",
)


class BackupServiceError(ValidationError):
    pass


class ScopeNotAllowed(BackupServiceError):
    pass


class IdempotencyConflict(BackupServiceError):
    pass


class TenantOperationLocked(BackupServiceError):
    pass


@dataclass(frozen=True, slots=True)
class ScopeResolution:
    scope: BackupScope
    included_products: tuple[ProductOwner, ...]
    component_definitions: tuple[ComponentDefinition, ...]

    @property
    def component_keys(self):
        return tuple(component.key for component in self.component_definitions)


def resolve_product_entitlements(business):
    """Resolve POS/WMS from the existing subscription module truth."""

    business_id = getattr(business, "pk", business)
    if not business_id:
        return ()
    subscription = (
        Subscription.objects.select_related("plan")
        .filter(business_id=business_id)
        .first()
    )
    if subscription is None:
        return ()
    modules = calculate_effective_modules(subscription.plan).effective_modules
    products = []
    if "pos_core" in modules:
        products.append(ProductOwner.POS)
    if "wms" in modules:
        products.append(ProductOwner.WMS)
    return tuple(products)


def available_backup_scopes(business):
    products = frozenset(resolve_product_entitlements(business))
    scopes = []
    if ProductOwner.POS in products:
        scopes.append(BackupScope.POS)
    if ProductOwner.WMS in products:
        scopes.append(BackupScope.WMS)
    if products:
        scopes.append(BackupScope.ALL_ENABLED)
    return tuple(scopes)


def resolve_requested_scope(business, scope):
    """Resolve a requested scope to current products and registry metadata."""

    try:
        normalized_scope = BackupScope(getattr(scope, "value", scope))
    except (TypeError, ValueError) as exc:
        raise ScopeNotAllowed("Unknown backup scope.") from exc
    products = tuple(resolve_product_entitlements(business))
    if normalized_scope == BackupScope.POS:
        included_products = (ProductOwner.POS,)
    elif normalized_scope == BackupScope.WMS:
        included_products = (ProductOwner.WMS,)
    else:
        included_products = tuple(
            product for product in _PRODUCT_ORDER if product in products
        )
    try:
        components = resolve_components(normalized_scope, products)
    except ScopeResolutionError as exc:
        raise ScopeNotAllowed(str(exc)) from exc
    return ScopeResolution(normalized_scope, included_products, components)


def generate_idempotency_key(namespace="operation", *stable_parts):
    """Return a namespaced random key, or deterministic key when parts are supplied."""

    normalized_namespace = re.sub(r"[^a-z0-9_-]", "-", str(namespace).lower()).strip("-")
    normalized_namespace = normalized_namespace[:30] or "operation"
    if stable_parts:
        canonical = json.dumps(
            [str(part) for part in stable_parts],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        suffix = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    else:
        suffix = uuid.uuid4().hex
    return f"{normalized_namespace}:{suffix}"[:128]


def _normalize_idempotency_key(value, namespace):
    key = str(value or generate_idempotency_key(namespace)).strip()
    if not key or len(key) > 128:
        raise ValidationError("The idempotency key must contain 1 to 128 characters.")
    return key


def _actor_snapshot(actor):
    if actor is None:
        return {}
    return {
        "public_id": str(getattr(actor, "public_id", "")),
        "email": str(getattr(actor, "email", ""))[:254],
        "full_name": str(getattr(actor, "full_name", ""))[:150],
        "platform_staff": bool(getattr(actor, "is_platform_staff", False)),
    }


def _request_ip(request):
    return audit.client_ip(request) if request is not None else None


def _support_actor_snapshot(request):
    return _actor_snapshot(getattr(request, "support_admin", None))


def _sanitize_text(value, limit=500):
    return " ".join(str(value or "").split())[:limit]


def _sanitize_metadata(value, *, key="", depth=0):
    if depth > 6:
        return "[TRUNCATED]"
    lowered = str(key).lower()
    if any(term in lowered for term in _SENSITIVE_METADATA_TERMS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key)[:100]: _sanitize_metadata(
                item_value,
                key=item_key,
                depth=depth + 1,
            )
            for item_key, item_value in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _sanitize_metadata(item, depth=depth + 1)
            for item in list(value)[:100]
        ]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)[:500]


def _module_keys_for_products(products):
    keys = []
    if ProductOwner.POS in products:
        keys.append("pos_core")
    if ProductOwner.WMS in products:
        keys.append("wms")
    return tuple(keys)


_PLATFORM_PERMISSION_FOR_ACTION = {
    "backups.view": "backups.platform_view_metadata",
    "backups.create": "backups.platform_manage_backups",
    "backups.schedule": "backups.platform_manage_backups",
    "backups.pin": "backups.platform_manage_backups",
    "backups.restore": "backups.platform_approve_restore",
}


def _authorize(
    *,
    actor,
    business,
    products,
    permission_code,
    request=None,
    system_actor=False,
):
    if system_actor:
        return
    if actor is None or not getattr(actor, "is_authenticated", False):
        raise PermissionDenied("An authenticated actor is required.")
    if getattr(actor, "is_platform_staff", False):
        platform_permission = _PLATFORM_PERMISSION_FOR_ACTION[permission_code]
        if not (
            getattr(actor, "is_superuser", False)
            or actor.has_perm(platform_permission)
        ):
            raise PermissionDenied("The platform capability is not assigned.")
        return
    module_keys = _module_keys_for_products(products)
    if not module_keys:
        raise PermissionDenied("No entitled backup product is available.")
    require_actor_access(
        actor,
        business,
        module_keys,
        permission_code=permission_code,
        action="write",
        request=request,
    )


def authorize_backup_action(
    *,
    actor,
    business,
    products,
    permission_code,
    request=None,
    system_actor=False,
):
    """Public authorization boundary reused by engine orchestration."""

    return _authorize(
        actor=actor,
        business=business,
        products=products,
        permission_code=permission_code,
        request=request,
        system_actor=system_actor,
    )


def _component_metadata(definitions):
    return [
        {
            "key": definition.key,
            "product_owner": str(definition.product_owner),
            "component_version": definition.component_version,
            "restore_behavior": str(definition.restore_behavior),
        }
        for definition in definitions
    ]


def _emit_summary_audit(
    action,
    *,
    business,
    actor,
    request,
    obj,
    description,
):
    audit.log(
        action,
        business=business,
        user=actor,
        request=request,
        module="backups",
        obj=obj,
        description=_sanitize_text(description, 400),
    )


def create_backup_activity(
    *,
    business,
    event_type,
    backup=None,
    restore=None,
    severity=ActivitySeverity.INFO,
    actor=None,
    request=None,
    reason="",
    sanitized_message="",
    structured_metadata=None,
    support_actor=None,
):
    """Append a sanitized evidence event; no update/delete interface is exposed."""

    event_type = str(event_type or "").strip().lower()
    if not _EVENT_TYPE_PATTERN.fullmatch(event_type):
        raise ValidationError("The backup activity event type is invalid.")
    try:
        normalized_severity = ActivitySeverity(getattr(severity, "value", severity))
    except (TypeError, ValueError) as exc:
        raise ValidationError("The backup activity severity is invalid.") from exc
    if backup is not None and backup.business_id != business.pk:
        raise ValidationError("The backup belongs to another business.")
    if restore is not None and restore.business_id != business.pk:
        raise ValidationError("The restore operation belongs to another business.")
    effective_support_actor = support_actor or getattr(request, "support_admin", None)
    activity = BackupActivity(
        business=business,
        backup=backup,
        restore=restore,
        event_type=event_type,
        severity=normalized_severity,
        actor=actor,
        actor_identity_snapshot=_actor_snapshot(actor),
        support_actor_identity_snapshot=_actor_snapshot(effective_support_actor),
        reason=_sanitize_text(reason),
        sanitized_message=_sanitize_text(sanitized_message),
        structured_metadata=_sanitize_metadata(structured_metadata or {}),
        request_ip=_request_ip(request),
        user_agent=(
            str(request.META.get("HTTP_USER_AGENT", ""))[:300]
            if request is not None
            else ""
        ),
    )
    activity.full_clean()
    activity.save()
    return activity


def _validate_existing_backup_idempotency(
    record,
    *,
    resolution,
    trigger,
    scheduled_local_date,
    parent_restore_operation,
):
    if (
        record.scope != resolution.scope
        or tuple(record.included_products or ()) != tuple(resolution.included_products)
        or record.trigger != trigger
        or record.scheduled_local_date != scheduled_local_date
        or record.parent_restore_operation_id
        != getattr(parent_restore_operation, "pk", None)
    ):
        raise IdempotencyConflict(
            "The idempotency key is already used by a different backup request."
        )
    return record


@transaction.atomic
def create_backup_request(
    *,
    business,
    scope,
    actor=None,
    trigger=BackupTrigger.MANUAL,
    scheduled_local_date=None,
    idempotency_key=None,
    parent_restore_operation=None,
    request=None,
    system_actor=False,
    using="default",
):
    """Create QUEUED metadata only; no task or artifact is created."""

    resolution = resolve_requested_scope(business, scope)
    try:
        normalized_trigger = BackupTrigger(getattr(trigger, "value", trigger))
    except (TypeError, ValueError) as exc:
        raise ValidationError("Unknown backup trigger.") from exc

    if normalized_trigger == BackupTrigger.SCHEDULED:
        if scheduled_local_date is None:
            raise ValidationError("Scheduled backup metadata requires a local date.")
        if resolution.scope != BackupScope.ALL_ENABLED:
            raise ValidationError("Daily schedules must use ALL_ENABLED scope.")
    elif scheduled_local_date is not None:
        raise ValidationError("Only scheduled backups may have a scheduled local date.")

    protected = normalized_trigger == BackupTrigger.PRE_RESTORE_SAFETY
    if protected:
        if parent_restore_operation is None:
            raise ValidationError("A safety backup must reference its restore operation.")
        if parent_restore_operation.business_id != business.pk:
            raise ValidationError("The restore operation belongs to another business.")
    if system_actor and normalized_trigger == BackupTrigger.MANUAL:
        raise ValidationError("A manual backup request requires a human actor.")

    _authorize(
        actor=actor,
        business=business,
        products=resolution.included_products,
        permission_code="backups.create",
        request=request,
        system_actor=system_actor,
    )
    key = _normalize_idempotency_key(idempotency_key, "backup")
    existing = BackupRecord.objects.for_business(business).filter(
        idempotency_key=key
    ).first()
    if existing is not None:
        return _validate_existing_backup_idempotency(
            existing,
            resolution=resolution,
            trigger=normalized_trigger,
            scheduled_local_date=scheduled_local_date,
            parent_restore_operation=parent_restore_operation,
        )

    versions = current_version_metadata(using=using)
    values = {
        "business": business,
        "tenant_public_id_snapshot": business.public_id,
        "scope": resolution.scope,
        "included_products": list(resolution.included_products),
        "included_components": _component_metadata(
            resolution.component_definitions
        ),
        "trigger": normalized_trigger,
        "scheduled_local_date": scheduled_local_date,
        "status": BackupStatus.QUEUED,
        "integrity_status": IntegrityStatus.NOT_CHECKED,
        "protected": protected,
        "retention_eligible": False,
        "created_by": actor,
        "creator_actor_snapshot": _actor_snapshot(actor),
        "system_actor": system_actor,
        "parent_restore_operation": parent_restore_operation,
        "idempotency_key": key,
        **versions,
    }
    try:
        with transaction.atomic():
            record = BackupRecord.objects.create(**values)
    except IntegrityError:
        record = BackupRecord.objects.for_business(business).filter(
            idempotency_key=key
        ).first()
        if record is None:
            raise
        return _validate_existing_backup_idempotency(
            record,
            resolution=resolution,
            trigger=normalized_trigger,
            scheduled_local_date=scheduled_local_date,
            parent_restore_operation=parent_restore_operation,
        )

    create_backup_activity(
        business=business,
        event_type="backup.requested",
        backup=record,
        actor=actor,
        request=request,
        reason=getattr(getattr(request, "support_session", None), "reason", ""),
        sanitized_message="Backup metadata request queued for guarded execution.",
        structured_metadata={
            "scope": str(record.scope),
            "trigger": str(record.trigger),
            "system_actor": system_actor,
        },
    )
    _emit_summary_audit(
        "backup.requested",
        business=business,
        actor=actor,
        request=request,
        obj=record,
        description="Backup metadata request queued; no inline artifact was created.",
    )
    return record


def _is_retention_eligible(record, *, status=None, integrity_status=None, pinned=None):
    return bool(
        (status or record.status) == BackupStatus.SUCCEEDED
        and (integrity_status or record.integrity_status) == IntegrityStatus.VERIFIED
        and record.trigger == BackupTrigger.SCHEDULED
        and record.scope == BackupScope.ALL_ENABLED
        and not (record.pinned if pinned is None else pinned)
        and not record.protected
    )


@transaction.atomic
def transition_backup(
    backup,
    target_status,
    *,
    failure_code="",
    failure_summary="",
):
    current = BackupRecord.objects.get(pk=backup.pk)
    target = validate_backup_transition(current.status, target_status)
    if (
        target == BackupStatus.SUCCEEDED
        and current.integrity_status != IntegrityStatus.VERIFIED
    ):
        raise ValidationError("A backup cannot succeed before integrity is verified.")

    now = timezone.now()
    updates = {"status": target, "updated_at": now}
    if target == BackupStatus.PREPARING and current.started_at is None:
        updates["started_at"] = now
    if target in {
        BackupStatus.SUCCEEDED,
        BackupStatus.FAILED,
        BackupStatus.CANCELLED,
    }:
        updates["completed_at"] = now
    if target == BackupStatus.FAILED:
        updates["failure_code"] = _sanitize_text(failure_code, 80)
        updates["sanitized_failure_summary"] = _sanitize_text(
            failure_summary,
            2000,
        )
    if target == BackupStatus.SUCCEEDED:
        updates["retention_eligible"] = _is_retention_eligible(
            current,
            status=target,
        )
    if target in {BackupStatus.DELETION_PENDING, BackupStatus.DELETED}:
        updates["retention_eligible"] = False
    if target == BackupStatus.DELETED:
        updates["deleted_at"] = now
    changed = BackupRecord.objects.filter(
        pk=current.pk,
        status=current.status,
    ).update(**updates)
    if changed != 1:
        raise BackupServiceError("The backup status changed concurrently.")
    current.refresh_from_db()
    return current


@transaction.atomic
def set_backup_integrity(backup, target_status):
    current = BackupRecord.objects.get(pk=backup.pk)
    target = validate_integrity_transition(current.integrity_status, target_status)
    now = timezone.now()
    updates = {"integrity_status": target, "updated_at": now}
    if target == IntegrityStatus.VERIFIED:
        updates["verified_at"] = now
    if target in {IntegrityStatus.FAILED, IntegrityStatus.CORRUPTED}:
        updates["retention_eligible"] = False
    changed = BackupRecord.objects.filter(
        pk=current.pk,
        integrity_status=current.integrity_status,
    ).update(**updates)
    if changed != 1:
        raise BackupServiceError("The integrity status changed concurrently.")
    current.refresh_from_db()
    return current


@transaction.atomic
def set_backup_pinned(
    *,
    business,
    backup,
    pinned,
    actor,
    request=None,
):
    if backup.business_id != business.pk:
        raise ValidationError("The backup belongs to another business.")
    resolution = resolve_requested_scope(business, backup.scope)
    if not set(backup.included_products or ()).issubset(resolution.included_products):
        raise ScopeNotAllowed("The backup contains a currently disabled product.")
    _authorize(
        actor=actor,
        business=business,
        products=tuple(ProductOwner(value) for value in backup.included_products),
        permission_code="backups.pin",
        request=request,
    )
    current = BackupRecord.objects.get(pk=backup.pk)
    normalized_pinned = bool(pinned)
    current.pinned = normalized_pinned
    current.retention_eligible = _is_retention_eligible(
        current,
        pinned=normalized_pinned,
    )
    current.save(update_fields=["pinned", "retention_eligible", "updated_at"])
    create_backup_activity(
        business=business,
        event_type="backup.pinned" if normalized_pinned else "backup.unpinned",
        backup=current,
        actor=actor,
        request=request,
        sanitized_message=(
            "Backup metadata pinned."
            if normalized_pinned
            else "Backup metadata unpinned."
        ),
    )
    _emit_summary_audit(
        "backup.pinned" if normalized_pinned else "backup.unpinned",
        business=business,
        actor=actor,
        request=request,
        obj=current,
        description=(
            "Backup metadata pinned."
            if normalized_pinned
            else "Backup metadata unpinned."
        ),
    )
    return current


def _validate_existing_restore_idempotency(
    restore,
    *,
    source_backup,
    requested_scope,
):
    if (
        restore.source_backup_id != source_backup.pk
        or restore.requested_scope != requested_scope
    ):
        raise IdempotencyConflict(
            "The idempotency key is already used by a different restore request."
        )
    return restore


@transaction.atomic
def create_restore_request(
    *,
    business,
    source_backup,
    requested_scope,
    actor,
    reason,
    idempotency_key=None,
    request=None,
    using="default",
):
    """Create restore-request metadata only; no lock or mutation is started."""

    if source_backup.business_id != business.pk:
        raise ValidationError("The source backup belongs to another business.")
    source = BackupRecord.objects.for_business(business).get(pk=source_backup.pk)
    resolution = resolve_requested_scope(business, requested_scope)
    source_products = frozenset(source.included_products or ())
    if not set(resolution.included_products).issubset(source_products):
        raise ScopeNotAllowed(
            "The requested scope is not fully contained in the source backup."
        )
    if (
        source.status != BackupStatus.SUCCEEDED
        or source.integrity_status != IntegrityStatus.VERIFIED
    ):
        raise ValidationError("Only a verified successful backup can be restored.")
    _authorize(
        actor=actor,
        business=business,
        products=resolution.included_products,
        permission_code="backups.restore",
        request=request,
    )
    normalized_reason = _sanitize_text(reason)
    if not normalized_reason:
        raise ValidationError("A restore reason is required.")
    compatibility = assess_restore_compatibility(
        format_version=source.format_version,
        minimum_restore_version=source.minimum_restore_version,
        schema_fingerprint=source.schema_fingerprint,
        using=using,
    )
    if compatibility.status != CompatibilityStatus.COMPATIBLE:
        raise ValidationError(compatibility.reason)

    key = _normalize_idempotency_key(idempotency_key, "restore")
    existing = RestoreOperation.objects.for_business(business).filter(
        idempotency_key=key
    ).first()
    if existing is not None:
        return _validate_existing_restore_idempotency(
            existing,
            source_backup=source,
            requested_scope=resolution.scope,
        )
    values = {
        "business": business,
        "source_backup": source,
        "requested_scope": resolution.scope,
        "requested_by": actor,
        "actor_identity_snapshot": _actor_snapshot(actor),
        "reason": normalized_reason,
        "compatibility_status": compatibility.status,
        "compatibility_reason": compatibility.reason,
        "idempotency_key": key,
    }
    try:
        with transaction.atomic():
            restore = RestoreOperation.objects.create(**values)
    except IntegrityError:
        restore = RestoreOperation.objects.for_business(business).filter(
            idempotency_key=key
        ).first()
        if restore is None:
            raise
        return _validate_existing_restore_idempotency(
            restore,
            source_backup=source,
            requested_scope=resolution.scope,
        )
    create_backup_activity(
        business=business,
        event_type="restore.requested",
        backup=source,
        restore=restore,
        actor=actor,
        request=request,
        reason=normalized_reason,
        sanitized_message="Restore metadata request queued; the engine is not enabled.",
        structured_metadata={"scope": str(resolution.scope)},
    )
    _emit_summary_audit(
        "restore.requested",
        business=business,
        actor=actor,
        request=request,
        obj=restore,
        description="Restore metadata request queued; no tenant data was changed.",
    )
    return restore


@transaction.atomic
def transition_restore(
    restore,
    target_status,
    *,
    failure_code="",
    failure_summary="",
    rollback_result="",
):
    current = RestoreOperation.objects.select_related("safety_backup").get(pk=restore.pk)
    target = validate_restore_transition(current.status, target_status)
    if (
        current.status == RestoreStatus.SAFETY_BACKUP
        and target == RestoreStatus.VALIDATING
    ):
        safety = current.safety_backup
        if (
            safety is None
            or safety.business_id != current.business_id
            or safety.parent_restore_operation_id != current.pk
            or safety.trigger != BackupTrigger.PRE_RESTORE_SAFETY
            or not safety.protected
            or safety.status != BackupStatus.SUCCEEDED
            or safety.integrity_status != IntegrityStatus.VERIFIED
        ):
            raise ValidationError(
                "Validation cannot start without this restore's protected, "
                "verified safety backup."
            )
    now = timezone.now()
    updates = {"status": target, "updated_at": now}
    if target == RestoreStatus.AUTHORIZING and current.started_at is None:
        updates["started_at"] = now
    if target in {
        RestoreStatus.SUCCEEDED,
        RestoreStatus.FAILED,
        RestoreStatus.ROLLED_BACK,
        RestoreStatus.INDETERMINATE,
    }:
        updates["completed_at"] = now
    if target in {RestoreStatus.FAILED, RestoreStatus.INDETERMINATE}:
        updates["failure_code"] = _sanitize_text(failure_code, 80)
        updates["sanitized_failure_summary"] = _sanitize_text(
            failure_summary,
            2000,
        )
    if target == RestoreStatus.ROLLING_BACK:
        updates["rollback_attempted"] = True
    if target == RestoreStatus.ROLLED_BACK:
        updates["rollback_attempted"] = True
        updates["rollback_result"] = _sanitize_text(rollback_result)
    changed = RestoreOperation.objects.filter(
        pk=current.pk,
        status=current.status,
    ).update(**updates)
    if changed != 1:
        raise BackupServiceError("The restore status changed concurrently.")
    current.refresh_from_db()
    return current


@transaction.atomic
def restart_failed_restore_before_mutation(restore):
    """Permit a controlled retry only for an explicitly pre-mutation failure."""

    current = RestoreOperation.objects.get(pk=restore.pk)
    if (
        current.status != RestoreStatus.FAILED
        or not current.failure_code.startswith("pre_mutation_")
        or current.rollback_attempted
    ):
        raise ValidationError("This restore operation is not safely retryable.")
    changed = RestoreOperation.objects.filter(
        pk=current.pk,
        status=RestoreStatus.FAILED,
        failure_code=current.failure_code,
        rollback_attempted=False,
    ).update(
        status=RestoreStatus.AUTHORIZING,
        failure_code="",
        sanitized_failure_summary="",
        completed_at=None,
        updated_at=timezone.now(),
    )
    if changed != 1:
        raise BackupServiceError("The restore status changed concurrently.")
    current.refresh_from_db()
    return current


@transaction.atomic
def set_restore_safety_backup(restore, safety_backup):
    current = RestoreOperation.objects.get(pk=restore.pk)
    safety = BackupRecord.objects.get(pk=safety_backup.pk)
    if safety.business_id != current.business_id:
        raise ValidationError("The safety backup belongs to another business.")
    if (
        safety.parent_restore_operation_id != current.pk
        or safety.trigger != BackupTrigger.PRE_RESTORE_SAFETY
        or not safety.protected
        or safety.status != BackupStatus.SUCCEEDED
        or safety.integrity_status != IntegrityStatus.VERIFIED
    ):
        raise ValidationError(
            "Restore requires its own fresh, protected, verified safety backup."
        )
    current.safety_backup = safety
    current.save(update_fields=["safety_backup", "updated_at"])
    return current


def acquire_tenant_operation_lock(
    *,
    business,
    operation_kind,
    operation_public_id,
    worker_task_identifier="",
    lease_seconds=300,
    now=None,
):
    """Atomically acquire one tenant lease without relying on row locks."""

    try:
        kind = OperationKind(getattr(operation_kind, "value", operation_kind))
        operation_id = uuid.UUID(str(operation_public_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("The operation lock identity is invalid.") from exc
    if not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 86400:
        raise ValidationError("The operation lock lease must be 1 to 86400 seconds.")
    current_time = now or timezone.now()
    expires_at = current_time + timedelta(seconds=lease_seconds)
    with transaction.atomic():
        TenantOperationLock.objects.for_business(business).filter(
            active=True,
            lease_expires_at__lte=current_time,
        ).update(active=False, released_at=current_time)
        try:
            with transaction.atomic():
                return TenantOperationLock.objects.create(
                    business=business,
                    operation_kind=kind,
                    operation_public_id=operation_id,
                    worker_task_identifier=_sanitize_text(
                        worker_task_identifier,
                        255,
                    ),
                    acquired_at=current_time,
                    lease_expires_at=expires_at,
                    heartbeat_at=current_time,
                    active=True,
                )
        except IntegrityError as exc:
            raise TenantOperationLocked(
                "Another exclusive operation is active for this business."
            ) from exc


def heartbeat_tenant_operation_lock(
    lock,
    *,
    lock_token,
    lease_seconds=300,
    now=None,
):
    if not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 86400:
        raise ValidationError("The operation lock lease must be 1 to 86400 seconds.")
    current_time = now or timezone.now()
    try:
        token = uuid.UUID(str(lock_token))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("The lock token is invalid.") from exc
    changed = TenantOperationLock.objects.filter(
        pk=lock.pk,
        lock_token=token,
        active=True,
        lease_expires_at__gt=current_time,
    ).update(
        heartbeat_at=current_time,
        lease_expires_at=current_time + timedelta(seconds=lease_seconds),
    )
    return changed == 1


def release_tenant_operation_lock(lock, *, lock_token, now=None):
    current_time = now or timezone.now()
    try:
        token = uuid.UUID(str(lock_token))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("The lock token is invalid.") from exc
    changed = TenantOperationLock.objects.filter(
        pk=lock.pk,
        lock_token=token,
        active=True,
    ).update(active=False, released_at=current_time)
    return changed == 1


@transaction.atomic
def upsert_backup_schedule(
    *,
    business,
    local_execution_time,
    enabled,
    actor,
    timezone_name=None,
    scope=BackupScope.ALL_ENABLED,
    next_run=None,
    request=None,
):
    """Create/update one tenant-local daily schedule configuration."""

    resolution = resolve_requested_scope(business, scope)
    if resolution.scope != BackupScope.ALL_ENABLED:
        raise ValidationError("The v1 daily schedule must use ALL_ENABLED scope.")
    _authorize(
        actor=actor,
        business=business,
        products=resolution.included_products,
        permission_code="backups.schedule",
        request=request,
    )
    effective_timezone = str(timezone_name or business.timezone or "UTC")
    try:
        ZoneInfo(effective_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValidationError("The schedule timezone is not recognized.") from exc
    if next_run is not None and timezone.is_naive(next_run):
        raise ValidationError("The next schedule run must be timezone-aware.")
    if enabled and next_run is None:
        from .scheduling import ScheduleDispatchError, next_daily_occurrence

        try:
            next_run = next_daily_occurrence(
                local_time=local_execution_time,
                timezone_name=effective_timezone,
                after=timezone.now(),
            )
        except ScheduleDispatchError:
            raise ValidationError(
                "The daily schedule time cannot be resolved safely."
            ) from None
    if not enabled:
        next_run = None

    schedule = BackupSchedule.objects.for_business(business).first()
    created = schedule is None
    if schedule is None:
        schedule = BackupSchedule(business=business, created_by=actor)
    schedule.enabled = bool(enabled)
    schedule.timezone_name = effective_timezone
    schedule.local_execution_time = local_execution_time
    schedule.next_run = next_run
    schedule.scope = BackupScope.ALL_ENABLED
    schedule.full_clean()
    schedule.save()
    create_backup_activity(
        business=business,
        event_type="backup.schedule_created" if created else "backup.schedule_updated",
        actor=actor,
        request=request,
        sanitized_message=(
            "Daily backup schedule configuration created."
            if created
            else "Daily backup schedule configuration updated."
        ),
        structured_metadata={
            "enabled": schedule.enabled,
            "scope": str(schedule.scope),
            "timezone": schedule.timezone_name,
        },
    )
    _emit_summary_audit(
        "backup.schedule_created" if created else "backup.schedule_updated",
        business=business,
        actor=actor,
        request=request,
        obj=schedule,
        description="Backup schedule configuration saved.",
    )
    return schedule


def compute_application_schema_metadata(*, using="default"):
    return current_version_metadata(using=using)
