"""Deterministic component planning and future pipeline stage contracts."""

import heapq
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from apps.backups.enums import BackupScope, ProductOwner, RestoreBehavior
from apps.backups.registry import (
    COMPONENT_REGISTRY,
    ComponentDefinition,
    ComponentRegistryError,
    ScopeResolutionError,
    UnknownComponentError,
)

from .exceptions import (
    BackupScopeNotAllowed,
    CircularComponentDependency,
    ManifestBuildError,
    MissingComponentDependency,
    UnknownBackupComponent,
)

if TYPE_CHECKING:
    from .context import BackupExecutionContext
    from .manifest import BackupManifest, ManifestCompatibilityMetadata


class PipelineStage(StrEnum):
    AUTHORIZE = "AUTHORIZE"
    RESOLVE_SCOPE = "RESOLVE_SCOPE"
    ACQUIRE_LOCK = "ACQUIRE_LOCK"
    PREPARE_WORKSPACE = "PREPARE_WORKSPACE"
    PREPARE_SNAPSHOT = "PREPARE_SNAPSHOT"
    RESOLVE_COMPONENTS = "RESOLVE_COMPONENTS"
    EXPORT_COMPONENTS = "EXPORT_COMPONENTS"
    BUILD_MANIFEST = "BUILD_MANIFEST"
    BUILD_PACKAGE = "BUILD_PACKAGE"
    VERIFY_ARTIFACT = "VERIFY_ARTIFACT"
    FINALIZE_METADATA = "FINALIZE_METADATA"
    CLEANUP = "CLEANUP"
    COMPLETE = "COMPLETE"


PIPELINE_STAGE_ORDER = tuple(PipelineStage)


class PipelineStageState(StrEnum):
    VALIDATED = "VALIDATED"
    PLANNED = "PLANNED"
    NOT_STARTED = "NOT_STARTED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class PipelineStageReport:
    stage: PipelineStage
    state: PipelineStageState
    sanitized_detail: str


@dataclass(frozen=True, slots=True)
class ComponentPlanItem:
    key: str
    product_owner: ProductOwner
    component_version: str
    restore_behavior: RestoreBehavior
    required_component_keys: tuple[str, ...]
    export_order: int
    import_order: int

    @classmethod
    def from_definition(cls, definition: ComponentDefinition):
        return cls(
            key=definition.key,
            product_owner=ProductOwner(definition.product_owner),
            component_version=str(definition.component_version),
            restore_behavior=RestoreBehavior(definition.restore_behavior),
            required_component_keys=tuple(definition.required_component_keys),
            export_order=int(definition.export_order),
            import_order=int(definition.import_order),
        )


@dataclass(frozen=True, slots=True)
class ComponentDependencyPlan:
    component_key: str
    required_component_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedComponentPlan:
    export_components: tuple[ComponentPlanItem, ...]
    import_components: tuple[ComponentPlanItem, ...]

    @property
    def export_keys(self):
        return tuple(component.key for component in self.export_components)

    @property
    def import_keys(self):
        return tuple(component.key for component in self.import_components)


@dataclass(frozen=True, slots=True)
class BackupExecutionPlan:
    context: "BackupExecutionContext"
    scope: BackupScope
    resolved_products: tuple[ProductOwner, ...]
    ordered_component_keys: tuple[str, ...]
    import_ordered_component_keys: tuple[str, ...]
    shared_component_keys: tuple[str, ...]
    pos_component_keys: tuple[str, ...]
    wms_component_keys: tuple[str, ...]
    component_plan: tuple[ComponentPlanItem, ...]
    dependency_ordering: tuple[ComponentDependencyPlan, ...]
    future_required_stages: tuple[PipelineStage, ...]
    stage_reports: tuple[PipelineStageReport, ...]
    compatibility_metadata: "ManifestCompatibilityMetadata"
    manifest: "BackupManifest"
    real_execution_available: bool
    disabled_reason: str
    operation_correlation_id: uuid.UUID


def order_component_definitions(
    definitions,
    *,
    order_attribute="export_order",
) -> tuple[ComponentDefinition, ...]:
    """Topologically order definitions with a stable registry-order tie break."""

    if order_attribute not in {"export_order", "import_order"}:
        raise ValueError("Unsupported component ordering attribute.")
    by_key = {}
    for definition in definitions:
        if definition.key in by_key:
            raise ManifestBuildError("The component plan contains a duplicate key.")
        by_key[definition.key] = definition
    if not by_key:
        raise ManifestBuildError("The component registry resolved no eligible components.")

    indegree = {key: 0 for key in by_key}
    dependents = {key: [] for key in by_key}
    for definition in by_key.values():
        for dependency_key in definition.required_component_keys:
            if dependency_key not in by_key:
                raise MissingComponentDependency(definition.key, dependency_key)
            indegree[definition.key] += 1
            dependents[dependency_key].append(definition.key)

    ready = [
        (getattr(by_key[key], order_attribute), key)
        for key, dependency_count in indegree.items()
        if dependency_count == 0
    ]
    heapq.heapify(ready)
    ordered = []
    while ready:
        _declared_order, key = heapq.heappop(ready)
        ordered.append(by_key[key])
        for dependent_key in sorted(dependents[key]):
            indegree[dependent_key] -= 1
            if indegree[dependent_key] == 0:
                dependent = by_key[dependent_key]
                heapq.heappush(
                    ready,
                    (getattr(dependent, order_attribute), dependent_key),
                )

    if len(ordered) != len(by_key):
        cycle_keys = tuple(key for key, count in indegree.items() if count > 0)
        raise CircularComponentDependency(cycle_keys)
    return tuple(ordered)


def _definition_for_key(registry, component_key):
    try:
        return registry.get(component_key)
    except (UnknownComponentError, KeyError, LookupError) as exc:
        raise UnknownBackupComponent(component_key) from exc


def _requested_dependency_closure(
    *,
    eligible_by_key,
    registry,
    requested_component_keys,
):
    selected = {}
    visiting = set()

    def include(component_key):
        if component_key in selected:
            return
        if component_key in visiting:
            raise CircularComponentDependency((*visiting, component_key))
        definition = _definition_for_key(registry, component_key)
        if component_key not in eligible_by_key:
            raise BackupScopeNotAllowed(
                "A requested backup component is not eligible for the resolved scope."
            )
        visiting.add(component_key)
        for dependency_key in definition.required_component_keys:
            if dependency_key not in eligible_by_key:
                if getattr(registry, "maybe_get", lambda _key: None)(dependency_key) is None:
                    raise MissingComponentDependency(component_key, dependency_key)
                raise BackupScopeNotAllowed(
                    "A component dependency is not eligible for the resolved scope."
                )
            include(dependency_key)
        visiting.remove(component_key)
        selected[component_key] = definition

    for requested_key in requested_component_keys:
        include(str(requested_key))
    return tuple(selected.values())


def resolve_component_plan(
    *,
    scope,
    enabled_products,
    registry=COMPONENT_REGISTRY,
    requested_component_keys=None,
) -> ResolvedComponentPlan:
    """Resolve the Phase 1 registry and enforce dependency closure explicitly."""

    try:
        eligible = tuple(registry.resolve(scope, enabled_products))
    except ScopeResolutionError as exc:
        raise BackupScopeNotAllowed(str(exc)) from exc
    except UnknownComponentError as exc:
        raise UnknownBackupComponent() from exc
    except ComponentRegistryError as exc:
        # A registry-level dependency failure must remain fail-closed without
        # leaking internal model or filesystem information.
        raise MissingComponentDependency() from exc

    eligible_by_key = {definition.key: definition for definition in eligible}
    if requested_component_keys is None:
        selected = eligible
    else:
        requested = tuple(dict.fromkeys(str(key) for key in requested_component_keys))
        for key in requested:
            _definition_for_key(registry, key)
        selected = _requested_dependency_closure(
            eligible_by_key=eligible_by_key,
            registry=registry,
            requested_component_keys=requested,
        )

    export_definitions = order_component_definitions(
        selected,
        order_attribute="export_order",
    )
    import_definitions = order_component_definitions(
        selected,
        order_attribute="import_order",
    )
    return ResolvedComponentPlan(
        export_components=tuple(
            ComponentPlanItem.from_definition(definition)
            for definition in export_definitions
        ),
        import_components=tuple(
            ComponentPlanItem.from_definition(definition)
            for definition in import_definitions
        ),
    )


def planning_stage_reports() -> tuple[PipelineStageReport, ...]:
    """Report planning validation without claiming operational stage completion."""

    validated = {
        PipelineStage.AUTHORIZE: "Actor authorization was validated.",
        PipelineStage.RESOLVE_SCOPE: "Current product entitlement was resolved.",
        PipelineStage.ACQUIRE_LOCK: "No conflicting tenant operation lock was found.",
        PipelineStage.RESOLVE_COMPONENTS: "Registered component dependencies were resolved.",
    }
    reports = []
    for stage in PIPELINE_STAGE_ORDER:
        if stage in validated:
            reports.append(
                PipelineStageReport(
                    stage,
                    PipelineStageState.VALIDATED,
                    validated[stage],
                )
            )
        elif stage == PipelineStage.PREPARE_SNAPSHOT:
            reports.append(
                PipelineStageReport(
                    stage,
                    PipelineStageState.PLANNED,
                    (
                        "The internal SQLite snapshot provider is available, "
                        "but planning performs no filesystem or database work."
                    ),
                )
            )
        elif stage == PipelineStage.EXPORT_COMPONENTS:
            reports.append(
                PipelineStageReport(
                    stage,
                    PipelineStageState.PLANNED,
                    (
                        "The internal tenant logical export provider is available, "
                        "but planning performs no database or filesystem work."
                    ),
                )
            )
        else:
            reports.append(
                PipelineStageReport(
                    stage,
                    PipelineStageState.NOT_STARTED,
                    "This operational stage was not started.",
                )
            )
    return tuple(reports)
