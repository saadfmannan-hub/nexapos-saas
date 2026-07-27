"""Non-operational Phase 2A backup planning orchestration."""

from django.utils import timezone

from apps.backups import services
from apps.backups.enums import ActivitySeverity, OperationKind, ProductOwner
from apps.backups.models import BackupRecord, TenantOperationLock
from apps.backups.registry import COMPONENT_REGISTRY

from .availability import get_engine_capability
from .events import (
    COMPONENT_PLAN_RESOLVED,
    ENGINE_DISABLED,
    EXECUTION_BLOCKED,
    EXECUTION_PLAN_CREATED,
    EXECUTION_PLAN_REQUESTED,
)
from .exceptions import (
    BackupEngineError,
    BackupLockUnavailable,
    BackupScopeNotAllowed,
    BackupTenantMismatch,
)
from .metadata import BackupMetadataBuilder
from .pipeline import (
    BackupExecutionPlan,
    ComponentDependencyPlan,
    planning_stage_reports,
    resolve_component_plan,
)


def _refetch_backup(*, business, backup_record):
    if backup_record.business_id != business.pk:
        raise BackupTenantMismatch()
    current = (
        BackupRecord.objects.for_business(business)
        .select_related("business", "created_by")
        .filter(public_id=backup_record.public_id)
        .first()
    )
    if (
        current is None
        or current.tenant_public_id_snapshot != business.public_id
    ):
        raise BackupTenantMismatch()
    return current


def _validate_lock_availability(*, business, backup_record):
    active_lock = (
        TenantOperationLock.objects.for_business(business)
        .filter(active=True, lease_expires_at__gt=timezone.now())
        .order_by("-acquired_at")
        .first()
    )
    if active_lock is None:
        return
    if (
        active_lock.operation_kind != OperationKind.BACKUP
        or active_lock.operation_public_id != backup_record.public_id
    ):
        raise BackupLockUnavailable()


def _activity(
    *,
    business,
    backup,
    actor,
    event_type,
    message,
    metadata=None,
    request=None,
    severity=ActivitySeverity.INFO,
):
    return services.create_backup_activity(
        business=business,
        backup=backup,
        actor=actor,
        event_type=event_type,
        sanitized_message=message,
        structured_metadata=metadata or {},
        request=request,
        severity=severity,
    )


def prepare_backup_execution(
    *,
    business,
    backup_record,
    actor,
    request=None,
    using="default",
    registry=COMPONENT_REGISTRY,
) -> BackupExecutionPlan:
    """Return a deterministic plan without running an operational stage."""

    current = _refetch_backup(business=business, backup_record=backup_record)
    try:
        resolution = services.resolve_requested_scope(business, current.scope)
    except services.ScopeNotAllowed as exc:
        raise BackupScopeNotAllowed(str(exc)) from exc

    # Reuse Phase 1 authorization so subscription and role truth is not
    # duplicated in the engine package.
    services.authorize_backup_action(
        actor=actor,
        business=business,
        products=resolution.included_products,
        permission_code="backups.create",
        request=request,
        system_actor=current.system_actor,
    )
    _activity(
        business=business,
        backup=current,
        actor=actor,
        event_type=EXECUTION_PLAN_REQUESTED,
        message="Backup execution-plan validation requested.",
        metadata={"scope": str(resolution.scope)},
        request=request,
    )

    try:
        _validate_lock_availability(business=business, backup_record=current)
        metadata_builder = BackupMetadataBuilder(registry=registry, using=using)
        context = metadata_builder.build_context(
            business=business,
            backup_record=current,
            actor=actor,
            scope_resolution=resolution,
        )
        component_plan = resolve_component_plan(
            scope=resolution.scope,
            enabled_products=resolution.included_products,
            registry=registry,
        )
        manifest = metadata_builder.build_manifest(
            context=context,
            component_plan=component_plan,
            backup_record=current,
        )
    except BackupEngineError as exc:
        _activity(
            business=business,
            backup=current,
            actor=actor,
            event_type=EXECUTION_BLOCKED,
            message=exc.sanitized_message,
            metadata={"error_code": exc.engine_code},
            request=request,
            severity=ActivitySeverity.WARNING,
        )
        raise

    _activity(
        business=business,
        backup=current,
        actor=actor,
        event_type=COMPONENT_PLAN_RESOLVED,
        message="Registered backup components and dependencies were resolved.",
        metadata={
            "scope": str(resolution.scope),
            "component_keys": component_plan.export_keys,
        },
        request=request,
    )

    capability = get_engine_capability()
    _activity(
        business=business,
        backup=current,
        actor=actor,
        event_type=ENGINE_DISABLED,
        message="Real backup execution remains disabled.",
        metadata={"real_execution_available": False},
        request=request,
        severity=ActivitySeverity.WARNING,
    )

    components = component_plan.export_components
    plan = BackupExecutionPlan(
        context=context,
        scope=resolution.scope,
        resolved_products=context.resolved_products,
        ordered_component_keys=component_plan.export_keys,
        import_ordered_component_keys=component_plan.import_keys,
        shared_component_keys=tuple(
            component.key
            for component in components
            if component.product_owner == ProductOwner.SHARED
        ),
        pos_component_keys=tuple(
            component.key
            for component in components
            if component.product_owner == ProductOwner.POS
        ),
        wms_component_keys=tuple(
            component.key
            for component in components
            if component.product_owner == ProductOwner.WMS
        ),
        component_plan=components,
        dependency_ordering=tuple(
            ComponentDependencyPlan(
                component_key=component.key,
                required_component_keys=component.required_component_keys,
            )
            for component in components
        ),
        future_required_stages=tuple(stage.stage for stage in planning_stage_reports()),
        stage_reports=planning_stage_reports(),
        compatibility_metadata=manifest.compatibility,
        manifest=manifest,
        real_execution_available=capability.real_execution_available,
        disabled_reason=capability.disabled_reason,
        operation_correlation_id=context.operation_correlation_id,
    )
    _activity(
        business=business,
        backup=current,
        actor=actor,
        event_type=EXECUTION_PLAN_CREATED,
        message="A non-operational backup execution plan was created.",
        metadata={
            "component_count": len(plan.ordered_component_keys),
            "real_execution_available": plan.real_execution_available,
        },
        request=request,
    )
    return plan


__all__ = ["prepare_backup_execution"]
