"""Focused safety and planning tests for Backup & Restore Phase 2A."""

import json
import tempfile
import uuid
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from apps.backups.engine.availability import (
    PHASE_2A_DISABLED_REASON,
    assert_real_execution_available,
    get_engine_capability,
)
from apps.backups.engine.checks import check_backup_staging_root
from apps.backups.engine.events import (
    COMPONENT_PLAN_RESOLVED,
    ENGINE_DISABLED,
    EXECUTION_PLAN_CREATED,
    EXECUTION_PLAN_REQUESTED,
)
from apps.backups.engine.exceptions import (
    BackupEngineDisabled,
    BackupLockUnavailable,
    BackupScopeNotAllowed,
    BackupTenantMismatch,
    CircularComponentDependency,
    MissingComponentDependency,
    UnknownBackupComponent,
    UnsafeWorkspacePath,
)
from apps.backups.engine.orchestration import prepare_backup_execution
from apps.backups.engine.pipeline import (
    PIPELINE_STAGE_ORDER,
    PipelineStage,
    PipelineStageState,
    order_component_definitions,
    resolve_component_plan,
)
from apps.backups.engine.workspace import (
    BackupWorkspaceManager,
    WorkspaceArea,
)
from apps.backups.enums import (
    BackupScope,
    BackupStatus,
    IntegrityStatus,
    OperationKind,
    ProductOwner,
    RestoreBehavior,
)
from apps.backups.models import BackupActivity, BackupRecord
from apps.backups.registry import ComponentDefinition
from apps.backups.services import acquire_tenant_operation_lock
from apps.backups.tasks import (
    check_backup_async_execution_configuration,
    execute_backup,
)

from .test_backups_phase1 import BackupPhase1TestCase


class BackupPhase2APlanningTests(BackupPhase1TestCase):
    def prepare(self, backup):
        return prepare_backup_execution(
            business=backup.business,
            backup_record=backup,
            actor=backup.created_by,
        )

    def test_pos_only_plan_is_deterministic_and_contains_no_wms(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        backup = self.make_backup(scope=BackupScope.POS)

        first = self.prepare(backup)
        second = self.prepare(backup)

        self.assertEqual(first, second)
        self.assertEqual(first.scope, BackupScope.POS)
        self.assertEqual(first.resolved_products, (ProductOwner.POS,))
        self.assertTrue(first.pos_component_keys)
        self.assertFalse(first.wms_component_keys)
        self.assertFalse(
            any(key.startswith("wms.") for key in first.ordered_component_keys)
        )

    def test_wms_only_plan_contains_no_pos_components(self):
        self.set_entitlements(self.business_a, pos=False, wms=True)
        backup = self.make_backup(scope=BackupScope.WMS)

        plan = self.prepare(backup)

        self.assertEqual(plan.resolved_products, (ProductOwner.WMS,))
        self.assertTrue(plan.wms_component_keys)
        self.assertFalse(plan.pos_component_keys)
        self.assertFalse(
            any(key.startswith("pos.") for key in plan.ordered_component_keys)
        )

    def test_all_enabled_combined_plan_contains_both_products(self):
        self.set_entitlements(self.business_a, pos=True, wms=True)
        backup = self.make_backup(scope=BackupScope.ALL_ENABLED)

        plan = self.prepare(backup)

        self.assertEqual(
            plan.resolved_products,
            (ProductOwner.POS, ProductOwner.WMS),
        )
        self.assertTrue(plan.pos_component_keys)
        self.assertTrue(plan.wms_component_keys)
        self.assertTrue(plan.shared_component_keys)

    def test_all_enabled_pos_only_resolves_pos_only(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        plan = self.prepare(self.make_backup(scope=BackupScope.ALL_ENABLED))

        self.assertEqual(plan.resolved_products, (ProductOwner.POS,))
        self.assertTrue(plan.pos_component_keys)
        self.assertFalse(plan.wms_component_keys)

    def test_all_enabled_wms_only_resolves_wms_only(self):
        self.set_entitlements(self.business_a, pos=False, wms=True)
        plan = self.prepare(self.make_backup(scope=BackupScope.ALL_ENABLED))

        self.assertEqual(plan.resolved_products, (ProductOwner.WMS,))
        self.assertFalse(plan.pos_component_keys)
        self.assertTrue(plan.wms_component_keys)

    def test_all_enabled_uses_current_entitlement_at_planning_time(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        backup = self.make_backup(scope=BackupScope.ALL_ENABLED)
        self.assertEqual(backup.included_products, [ProductOwner.POS])
        self.set_entitlements(self.business_a, pos=True, wms=True)

        plan = self.prepare(backup)

        self.assertEqual(
            plan.resolved_products,
            (ProductOwner.POS, ProductOwner.WMS),
        )
        self.assertTrue(plan.wms_component_keys)

    def test_disabled_product_scope_is_rejected(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        backup = BackupRecord.objects.create(
            **self.backup_model_kwargs(
                business=self.business_a,
                scope=BackupScope.WMS,
            )
        )
        with self.assertRaises(BackupScopeNotAllowed):
            self.prepare(backup)

    def test_dependencies_precede_dependents_in_export_and_import_orders(self):
        self.set_entitlements(self.business_a, pos=True, wms=True)
        plan = self.prepare(self.make_backup(scope=BackupScope.ALL_ENABLED))
        export_positions = {
            key: position for position, key in enumerate(plan.ordered_component_keys)
        }
        import_positions = {
            key: position
            for position, key in enumerate(plan.import_ordered_component_keys)
        }

        for component in plan.component_plan:
            for dependency in component.required_component_keys:
                self.assertLess(
                    export_positions[dependency],
                    export_positions[component.key],
                )
                self.assertLess(
                    import_positions[dependency],
                    import_positions[component.key],
                )

    def test_reference_and_dependency_only_classifications_are_retained(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        plan = self.prepare(self.make_backup())
        by_key = {component.key: component for component in plan.component_plan}

        self.assertEqual(
            by_key["shared.tenant_identity"].restore_behavior,
            RestoreBehavior.REFERENCE_ONLY,
        )
        self.assertEqual(
            by_key["shared.tenant_settings"].restore_behavior,
            RestoreBehavior.DEPENDENCY_ONLY,
        )

    def test_stage_reports_stop_before_snapshot_and_never_complete(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        backup = self.make_backup()

        plan = self.prepare(backup)
        states = {report.stage: report.state for report in plan.stage_reports}
        backup.refresh_from_db()

        self.assertEqual(plan.future_required_stages, PIPELINE_STAGE_ORDER)
        self.assertEqual(
            states[PipelineStage.PREPARE_SNAPSHOT],
            PipelineStageState.PLANNED,
        )
        self.assertEqual(
            states[PipelineStage.COMPLETE],
            PipelineStageState.NOT_STARTED,
        )
        self.assertEqual(backup.status, BackupStatus.QUEUED)
        self.assertEqual(backup.integrity_status, IntegrityStatus.NOT_CHECKED)

    def test_conflicting_tenant_lock_blocks_planning(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        backup = self.make_backup()
        acquire_tenant_operation_lock(
            business=self.business_a,
            operation_kind=OperationKind.RESTORE,
            operation_public_id=uuid.uuid4(),
        )

        with self.assertRaises(BackupLockUnavailable):
            self.prepare(backup)

    def test_planning_activity_is_append_only_and_tenant_scoped(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        backup = self.make_backup()

        self.prepare(backup)
        events = set(
            BackupActivity.objects.for_business(self.business_a)
            .filter(backup=backup)
            .values_list("event_type", flat=True)
        )

        self.assertTrue(
            {
                EXECUTION_PLAN_REQUESTED,
                COMPONENT_PLAN_RESOLVED,
                ENGINE_DISABLED,
                EXECUTION_PLAN_CREATED,
            }.issubset(events)
        )
        self.assertFalse(
            BackupActivity.objects.for_business(self.business_b)
            .filter(backup=backup)
            .exists()
        )


class BackupPhase2ARegistryTests(SimpleTestCase):
    def definition(self, key, *, dependencies=(), order=100):
        return ComponentDefinition(
            key=key,
            product_owner=ProductOwner.SHARED,
            included_model_labels=(f"tests.{key.replace('.', '_')}",),
            required_component_keys=dependencies,
            export_order=order,
            import_order=order,
        )

    def test_missing_dependency_is_rejected(self):
        definition = self.definition("shared.child", dependencies=("shared.missing",))
        with self.assertRaises(MissingComponentDependency):
            order_component_definitions((definition,))

    def test_circular_dependency_is_rejected(self):
        first = self.definition("shared.first", dependencies=("shared.second",))
        second = self.definition("shared.second", dependencies=("shared.first",))
        with self.assertRaises(CircularComponentDependency):
            order_component_definitions((first, second))

    def test_unknown_requested_component_fails_closed(self):
        with self.assertRaises(UnknownBackupComponent):
            resolve_component_plan(
                scope=BackupScope.POS,
                enabled_products=(ProductOwner.POS,),
                requested_component_keys=("unknown.component",),
            )

    def test_topological_order_wins_over_declared_numeric_order(self):
        parent = self.definition("shared.parent", order=200)
        child = self.definition(
            "shared.child",
            dependencies=("shared.parent",),
            order=10,
        )
        ordered = order_component_definitions((child, parent))
        self.assertEqual(
            tuple(definition.key for definition in ordered),
            ("shared.parent", "shared.child"),
        )


class BackupPhase2AContextAndManifestTests(BackupPhase1TestCase):
    def setUp(self):
        super().setUp()
        self.set_entitlements(self.business_a, pos=True, wms=True)
        self.backup = self.make_backup(scope=BackupScope.ALL_ENABLED)
        self.plan = prepare_backup_execution(
            business=self.business_a,
            backup_record=self.backup,
            actor=self.owner_a,
        )

    def test_context_uses_correct_tenant_and_is_immutable(self):
        context = self.plan.context
        self.assertEqual(context.business_id, self.business_a.pk)
        self.assertEqual(context.business_public_id, self.business_a.public_id)
        self.assertEqual(context.backup_public_id, self.backup.public_id)
        with self.assertRaises(FrozenInstanceError):
            context.business_id = self.business_b.pk

    def test_context_contains_no_sensitive_or_path_fields(self):
        flattened = json.dumps(asdict(self.plan.context), default=str).lower()
        field_names = set(asdict(self.plan.context))
        forbidden = {
            "password",
            "secret",
            "credential",
            "encryption_key",
            "storage_secret",
            "session_token",
            "filesystem_path",
        }
        self.assertTrue(field_names.isdisjoint(forbidden))
        self.assertNotIn("password", flattened)
        self.assertNotIn("secret_key", flattened)
        self.assertIsNone(self.plan.context.workspace_reference)

    def test_manifest_contains_authoritative_versions_and_identity(self):
        manifest = self.plan.manifest
        self.assertEqual(manifest.backup_format_version, self.backup.format_version)
        self.assertEqual(manifest.application_version, self.backup.application_version)
        self.assertEqual(
            manifest.schema_migration_fingerprint,
            self.backup.schema_fingerprint,
        )
        self.assertEqual(manifest.tenant_public_id, str(self.business_a.public_id))
        self.assertEqual(
            manifest.included_products,
            (ProductOwner.POS, ProductOwner.WMS),
        )
        self.assertEqual(
            manifest.included_component_keys,
            self.plan.ordered_component_keys,
        )

    def test_manifest_has_placeholders_and_never_claims_verification(self):
        payload = self.plan.manifest.to_ordered_dict()
        self.assertIsNone(payload["total_record_count"])
        self.assertIsNone(payload["total_media_count"])
        self.assertIsNone(payload["artifact_hash"])
        self.assertEqual(payload["verification_state"], "NOT_VERIFIED")
        self.assertTrue(
            all(component["record_count"] is None for component in payload["components"])
        )
        self.assertTrue(
            all(component["content_hash"] is None for component in payload["components"])
        )

    def test_manifest_structure_is_deterministic_and_contains_no_paths_or_secrets(self):
        second = prepare_backup_execution(
            business=self.business_a,
            backup_record=self.backup,
            actor=self.owner_a,
        )
        first_payload = self.plan.manifest.to_ordered_dict()
        second_payload = second.manifest.to_ordered_dict()
        serialized = json.dumps(first_payload).lower()

        self.assertEqual(first_payload, second_payload)
        self.assertEqual(
            tuple(first_payload),
            (
                "backup_format_version",
                "application_version",
                "schema_migration_fingerprint",
                "backup_public_id",
                "tenant_public_id",
                "scope",
                "included_products",
                "included_component_keys",
                "components",
                "dependency_metadata",
                "trigger_type",
                "created_timestamp",
                "compatibility",
                "total_record_count",
                "total_media_count",
                "artifact_hash",
                "verification_state",
            ),
        )
        for forbidden in (
            "password",
            "credential",
            "storage_key",
            "private_key",
            "filesystem_path",
            "media_root",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_another_tenants_record_is_rejected_before_activity_write(self):
        self.set_entitlements(self.business_b, pos=True, wms=False)
        other_backup = self.make_backup(
            business=self.business_b,
            actor=self.owner_b,
        )
        before = BackupActivity.objects.for_business(self.business_a).count()

        with self.assertRaises(BackupTenantMismatch):
            prepare_backup_execution(
                business=self.business_a,
                backup_record=other_backup,
                actor=self.owner_a,
            )

        self.assertEqual(
            BackupActivity.objects.for_business(self.business_a).count(),
            before,
        )


class BackupPhase2AWorkspaceTests(SimpleTestCase):
    def test_create_containment_generated_name_and_idempotent_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            base = Path(temporary_root)
            staging = base / "private-staging"
            with override_settings(
                BACKUP_STAGING_ROOT=staging,
                MEDIA_ROOT=base / "media",
                STATIC_ROOT=base / "static",
            ):
                manager = BackupWorkspaceManager()
                workspace = manager.create()

                self.assertRegex(workspace.path.name, r"^ws-[0-9a-f]{32}$")
                self.assertEqual(workspace.path.parent, staging.resolve())
                self.assertEqual(
                    workspace.system_area_path(WorkspaceArea.COMPONENTS).parent,
                    workspace.path,
                )
                self.assertTrue(manager.cleanup(workspace))
                self.assertFalse(manager.cleanup(workspace.reference))

    def test_path_traversal_and_absolute_child_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root) / "staging"
            with override_settings(
                BACKUP_STAGING_ROOT=root,
                MEDIA_ROOT=Path(temporary_root) / "media",
                STATIC_ROOT=Path(temporary_root) / "static",
            ):
                workspace = BackupWorkspaceManager().create()
                try:
                    for unsafe in ("..", "../escape", str(Path(temporary_root).resolve())):
                        with self.subTest(unsafe=unsafe):
                            with self.assertRaises(UnsafeWorkspacePath):
                                workspace.child_path(unsafe)
                finally:
                    BackupWorkspaceManager().cleanup(workspace)

    def test_public_staging_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            media = Path(temporary_root) / "media"
            with override_settings(
                BACKUP_STAGING_ROOT=media / "backups",
                MEDIA_ROOT=media,
                STATIC_ROOT=Path(temporary_root) / "static",
            ):
                with self.assertRaises(UnsafeWorkspacePath):
                    BackupWorkspaceManager()
                errors = check_backup_staging_root(None)
                self.assertEqual([error.id for error in errors], ["backups.E020"])


class BackupPhase2AEngineGuardTests(BackupPhase1TestCase):
    @override_settings(
        BACKUP_EXECUTION_ENGINE_ENABLED=False,
        BACKUP_ENGINE_ENABLED=False,
    )
    def test_engine_is_disabled_by_default_and_real_execution_is_rejected(self):
        capability = get_engine_capability()
        self.assertFalse(capability.real_execution_available)
        with self.assertRaises(BackupEngineDisabled):
            assert_real_execution_available()

    @override_settings(
        BACKUP_EXECUTION_ENGINE_ENABLED=True,
        BACKUP_ENGINE_ENABLED=False,
        CELERY_BROKER_URL="redis://worker-only.example/0",
        CELERY_TASK_ALWAYS_EAGER=False,
    )
    def test_setting_cannot_enable_incomplete_provider_stack(self):
        capability = get_engine_capability()
        self.assertTrue(capability.setting_enabled)
        self.assertFalse(capability.provider_stack_ready)
        self.assertFalse(capability.real_execution_available)
        self.assertEqual(capability.disabled_reason, PHASE_2A_DISABLED_REASON)
        self.assertEqual(
            [error.id for error in check_backup_async_execution_configuration(None)],
            ["backups.E012"],
        )

    def test_disabled_task_marks_record_failed_without_artifact_metadata(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        backup = self.make_backup()

        with self.assertRaises(BackupEngineDisabled):
            execute_backup(backup.public_id, self.business_a.public_id)
        backup.refresh_from_db()

        self.assertEqual(backup.status, BackupStatus.FAILED)
        self.assertEqual(backup.integrity_status, IntegrityStatus.NOT_CHECKED)
        self.assertEqual(backup.failure_code, "backup_engine_disabled")
        self.assertEqual(backup.storage_backend_identifier, "")
        self.assertEqual(backup.opaque_object_key, "")
        self.assertEqual(backup.encryption_key_identifier, "")
        self.assertEqual(backup.whole_artifact_hash, "")
        self.assertEqual(backup.backup_size_bytes, 0)

    def test_disabled_entrypoint_is_not_a_registered_celery_task(self):
        self.assertFalse(hasattr(execute_backup, "delay"))
        self.assertFalse(hasattr(execute_backup, "apply_async"))
