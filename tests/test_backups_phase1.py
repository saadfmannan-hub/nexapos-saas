"""Focused contract tests for Backup & Restore Phase 1 foundations.

These tests deliberately exercise metadata, authorization, isolation, and
state-machine contracts only. They must never create an artifact, run a backup
worker, or mutate tenant operational data.
"""

import uuid
from datetime import time, timedelta

from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import PROTECT
from django.http import Http404
from django.test import SimpleTestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from apps.accounts.models import Membership, User
from apps.backups.enums import (
    BackupScope,
    BackupStatus,
    BackupTrigger,
    IntegrityStatus,
    OperationKind,
    ProductOwner,
    RestoreStatus,
)
from apps.backups.models import (
    BackupActivity,
    BackupComponent,
    BackupRecord,
    BackupSchedule,
    DownloadGrant,
    RestoreOperation,
    TenantOperationLock,
)
from apps.backups.registry import (
    COMPONENT_REGISTRY,
    UnclassifiedTenantModelsError,
    UnknownComponentError,
    assert_models_classified,
    get_component_definition,
    resolve_components,
)
from apps.backups.selectors import get_backup_for_business
from apps.backups.services import (
    IdempotencyConflict,
    ScopeNotAllowed,
    TenantOperationLocked,
    acquire_tenant_operation_lock,
    available_backup_scopes,
    create_backup_activity,
    create_backup_request,
    create_restore_request,
    heartbeat_tenant_operation_lock,
    release_tenant_operation_lock,
    resolve_product_entitlements,
    resolve_requested_scope,
    set_backup_integrity,
    set_restore_safety_backup,
    transition_backup,
    transition_restore,
    upsert_backup_schedule,
)
from apps.backups.state_machines import (
    InvalidStateTransition,
    validate_backup_transition,
    validate_integrity_transition,
    validate_restore_transition,
)
from apps.backups.tasks import (
    assert_safe_async_execution_configuration,
    check_backup_async_execution_configuration,
)
from apps.core.permissions import ALL_PERMISSION_CODES, DEFAULT_ROLES
from apps.subscriptions.models import Plan, Subscription

from .base import TenantTestCase

BACKUP_PERMISSION_CODES = frozenset(
    {
        "backups.view",
        "backups.create",
        "backups.download",
        "backups.schedule",
        "backups.pin",
        "backups.restore",
    }
)


class BackupStateMachineTests(SimpleTestCase):
    def test_required_backup_states_and_scopes_are_stable(self):
        self.assertEqual(
            {value.value for value in BackupScope},
            {"POS", "WMS", "ALL_ENABLED"},
        )
        self.assertEqual(
            {value.value for value in BackupStatus},
            {
                "QUEUED",
                "PREPARING",
                "SNAPSHOTTING",
                "PACKAGING",
                "UPLOADING",
                "VERIFYING",
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                "DELETION_PENDING",
                "DELETED",
            },
        )

    def test_valid_backup_transition_is_accepted(self):
        self.assertEqual(
            validate_backup_transition(BackupStatus.QUEUED, BackupStatus.PREPARING),
            BackupStatus.PREPARING,
        )

    def test_invalid_backup_transition_is_rejected(self):
        with self.assertRaises(InvalidStateTransition):
            validate_backup_transition(BackupStatus.QUEUED, BackupStatus.SUCCEEDED)

    def test_terminal_backup_state_cannot_restart(self):
        with self.assertRaises(InvalidStateTransition):
            validate_backup_transition(BackupStatus.DELETED, BackupStatus.PREPARING)

    def test_integrity_transition_graph_is_explicit(self):
        self.assertEqual(
            validate_integrity_transition(
                IntegrityStatus.NOT_CHECKED,
                IntegrityStatus.VERIFYING,
            ),
            IntegrityStatus.VERIFYING,
        )
        with self.assertRaises(InvalidStateTransition):
            validate_integrity_transition(
                IntegrityStatus.NOT_CHECKED,
                IntegrityStatus.VERIFIED,
            )

    def test_restore_cannot_skip_authorization_and_locking(self):
        self.assertEqual(
            validate_restore_transition(RestoreStatus.QUEUED, RestoreStatus.AUTHORIZING),
            RestoreStatus.AUTHORIZING,
        )
        with self.assertRaises(InvalidStateTransition):
            validate_restore_transition(RestoreStatus.QUEUED, RestoreStatus.RESTORING)


class BackupAsyncSafetyTests(SimpleTestCase):
    @override_settings(CELERY_BROKER_URL="", CELERY_TASK_ALWAYS_EAGER=True)
    def test_execution_guard_rejects_local_eager_configuration(self):
        with self.assertRaises(ImproperlyConfigured):
            assert_safe_async_execution_configuration()

    @override_settings(
        CELERY_BROKER_URL="redis://worker-only.example/0",
        CELERY_TASK_ALWAYS_EAGER=False,
    )
    def test_execution_guard_accepts_dedicated_async_configuration(self):
        self.assertTrue(assert_safe_async_execution_configuration())

    @override_settings(
        BACKUP_ENGINE_ENABLED=False,
        CELERY_BROKER_URL="",
        CELERY_TASK_ALWAYS_EAGER=True,
    )
    def test_phase1_system_check_does_not_activate_an_engine(self):
        self.assertEqual(check_backup_async_execution_configuration(None), [])

    @override_settings(
        BACKUP_ENGINE_ENABLED=True,
        CELERY_BROKER_URL="",
        CELERY_TASK_ALWAYS_EAGER=True,
    )
    def test_future_engine_system_check_fails_closed(self):
        errors = check_backup_async_execution_configuration(None)
        self.assertEqual(
            {error.id for error in errors},
            {"backups.E010", "backups.E011", "backups.E012"},
        )


class BackupComponentRegistryTests(SimpleTestCase):
    def test_registry_is_explicit_and_immutable(self):
        self.assertTrue(COMPONENT_REGISTRY.definitions)
        with self.assertRaises(TypeError):
            COMPONENT_REGISTRY.definitions["unknown"] = object()

    def test_unknown_component_fails_closed(self):
        with self.assertRaises(UnknownComponentError):
            get_component_definition("not.registered")

    def test_unclassified_tenant_model_fails_closed(self):
        with self.assertRaises(UnclassifiedTenantModelsError) as caught:
            assert_models_classified(("future_app.UnclassifiedTenantModel",))
        self.assertEqual(
            caught.exception.model_labels,
            ("future_app.UnclassifiedTenantModel",),
        )

    def test_current_tenant_model_graph_is_explicitly_classified(self):
        self.assertTrue(assert_models_classified())

    def test_pos_scope_never_resolves_wms_components(self):
        definitions = resolve_components(BackupScope.POS, {ProductOwner.POS})
        owners = {definition.product_owner for definition in definitions}
        self.assertIn(ProductOwner.POS, owners)
        self.assertNotIn(ProductOwner.WMS, owners)

    def test_wms_scope_never_resolves_pos_components(self):
        definitions = resolve_components(BackupScope.WMS, {ProductOwner.WMS})
        owners = {definition.product_owner for definition in definitions}
        self.assertIn(ProductOwner.WMS, owners)
        self.assertNotIn(ProductOwner.POS, owners)

    def test_all_enabled_resolves_only_enabled_products(self):
        definitions = resolve_components(
            BackupScope.ALL_ENABLED,
            {ProductOwner.POS},
        )
        owners = {definition.product_owner for definition in definitions}
        self.assertIn(ProductOwner.POS, owners)
        self.assertNotIn(ProductOwner.WMS, owners)


class BackupPhase1TestCase(TenantTestCase):
    """Tenant fixtures plus isolated product-plan helpers."""

    def set_entitlements(self, business, *, pos, wms):
        plan = Plan.objects.create(
            name=f"Backup scope {business.public_id} {uuid.uuid4()}",
            allow_trial=False,
            feature_sales=pos,
            feature_wms=wms,
        )
        subscription = Subscription.objects.get(business=business)
        subscription.plan = plan
        subscription.status = Subscription.Status.ACTIVE
        subscription.trial_ends_at = None
        subscription.current_period_end = timezone.now() + timedelta(days=30)
        subscription.save(
            update_fields=[
                "plan",
                "status",
                "trial_ends_at",
                "current_period_end",
                "updated_at",
            ]
        )
        business._state.fields_cache.pop("subscription", None)
        return plan

    def make_backup(
        self,
        *,
        business=None,
        actor=None,
        scope=BackupScope.POS,
        trigger=BackupTrigger.MANUAL,
        idempotency_key=None,
    ):
        business = business or self.business_a
        actor = actor or self.owner_a
        return create_backup_request(
            business=business,
            scope=scope,
            trigger=trigger,
            actor=actor,
            idempotency_key=idempotency_key or f"test:{uuid.uuid4()}",
        )

    def backup_model_kwargs(
        self,
        *,
        business=None,
        scope=BackupScope.POS,
        trigger=BackupTrigger.MANUAL,
        idempotency_key=None,
        **overrides,
    ):
        business = business or self.business_a
        products = (
            [ProductOwner.WMS]
            if scope == BackupScope.WMS
            else [ProductOwner.POS]
        )
        values = {
            "business": business,
            "tenant_public_id_snapshot": business.public_id,
            "scope": scope,
            "included_products": products,
            "trigger": trigger,
            "format_version": "1.0",
            "application_version": "1.0.0",
            "schema_fingerprint": "a" * 64,
            "minimum_restore_version": "1.0.0",
            "idempotency_key": idempotency_key or f"model:{uuid.uuid4()}",
        }
        values.update(overrides)
        return values


class BackupModelContractTests(BackupPhase1TestCase):
    def test_public_identifiers_are_uuid_and_unique(self):
        backup_a = BackupRecord.objects.create(**self.backup_model_kwargs())
        backup_b = BackupRecord.objects.create(**self.backup_model_kwargs())
        restore = RestoreOperation.objects.create(
            business=self.business_a,
            source_backup=backup_a,
            requested_scope=BackupScope.POS,
            requested_by=self.owner_a,
            reason="Phase 1 metadata test",
            idempotency_key=f"restore:{uuid.uuid4()}",
        )
        activity = BackupActivity.objects.create(
            business=self.business_a,
            backup=backup_a,
            event_type="backup.test",
        )
        grant = DownloadGrant.objects.create(
            business=self.business_a,
            backup=backup_a,
            issued_to=self.owner_a,
            token_hash=uuid.uuid4().hex,
            expires_at=timezone.now() + timedelta(minutes=5),
        )

        public_ids = {
            backup_a.public_id,
            backup_b.public_id,
            restore.public_id,
            activity.public_id,
            grant.public_id,
        }
        self.assertEqual(len(public_ids), 5)
        self.assertTrue(all(isinstance(value, uuid.UUID) for value in public_ids))

    def test_tenant_snapshot_and_identity_fields_are_immutable(self):
        backup = BackupRecord.objects.create(**self.backup_model_kwargs())
        self.assertEqual(backup.tenant_public_id_snapshot, self.business_a.public_id)

        backup.business = self.business_b
        with self.assertRaises(ValidationError):
            backup.save()
        with self.assertRaises(ValueError):
            BackupRecord.objects.filter(pk=backup.pk).update(
                tenant_public_id_snapshot=self.business_b.public_id
            )

    def test_evidence_foreign_keys_use_safe_deletion_behavior(self):
        self.assertIs(
            BackupRecord._meta.get_field("business").remote_field.on_delete,
            PROTECT,
        )
        self.assertIs(
            BackupComponent._meta.get_field("backup").remote_field.on_delete,
            PROTECT,
        )
        self.assertIs(
            RestoreOperation._meta.get_field("source_backup").remote_field.on_delete,
            PROTECT,
        )

    def test_scheduled_backup_requires_local_date(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BackupRecord.objects.create(
                    **self.backup_model_kwargs(trigger=BackupTrigger.SCHEDULED)
                )

    def test_succeeded_backup_requires_verified_integrity(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BackupRecord.objects.create(
                    **self.backup_model_kwargs(status=BackupStatus.SUCCEEDED)
                )

    def test_component_key_is_unique_per_backup(self):
        backup = BackupRecord.objects.create(**self.backup_model_kwargs())
        component_values = {
            "backup": backup,
            "component_key": "pos.catalog",
            "product_category": ProductOwner.POS,
            "component_version": "1.0",
        }
        BackupComponent.objects.create(**component_values)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BackupComponent.objects.create(**component_values)

    def test_restore_rejects_cross_tenant_source_backup(self):
        other_backup = BackupRecord.objects.create(
            **self.backup_model_kwargs(business=self.business_b)
        )
        restore = RestoreOperation(
            business=self.business_a,
            source_backup=other_backup,
            requested_scope=BackupScope.POS,
            requested_by=self.owner_a,
            reason="Must fail tenant validation",
            idempotency_key=f"restore:{uuid.uuid4()}",
        )
        with self.assertRaises(ValidationError):
            restore.full_clean()

    def test_schedule_rejects_cross_tenant_backup_references(self):
        other_backup = BackupRecord.objects.create(
            **self.backup_model_kwargs(business=self.business_b)
        )
        schedule = BackupSchedule(
            business=self.business_a,
            enabled=True,
            timezone_name=self.business_a.timezone,
            local_execution_time=time(2, 0),
            last_successful_backup=other_backup,
            created_by=self.owner_a,
        )
        with self.assertRaises(ValidationError):
            schedule.full_clean()

    def test_only_one_daily_schedule_exists_per_business(self):
        BackupSchedule.objects.create(
            business=self.business_a,
            enabled=True,
            timezone_name=self.business_a.timezone,
            local_execution_time=time(2, 0),
            created_by=self.owner_a,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BackupSchedule.objects.create(
                    business=self.business_a,
                    enabled=False,
                    timezone_name=self.business_a.timezone,
                    local_execution_time=time(3, 0),
                    created_by=self.owner_a,
                )

    def test_backup_deletion_is_a_tombstone_not_a_hard_delete(self):
        backup = BackupRecord.objects.create(
            **self.backup_model_kwargs(
                status=BackupStatus.DELETED,
                deleted_at=timezone.now(),
            )
        )
        with self.assertRaises(ValueError):
            backup.delete()
        with self.assertRaises(ValueError):
            BackupRecord.objects.filter(pk=backup.pk).delete()
        self.assertTrue(
            BackupRecord.objects.filter(
                pk=backup.pk,
                status=BackupStatus.DELETED,
                deleted_at__isnull=False,
            ).exists()
        )

    def test_activity_is_append_only_through_instance_and_queryset(self):
        backup = BackupRecord.objects.create(**self.backup_model_kwargs())
        activity = BackupActivity.objects.create(
            business=self.business_a,
            backup=backup,
            event_type="backup.requested",
            sanitized_message="Request recorded.",
        )

        activity.sanitized_message = "Tampered"
        with self.assertRaises(ValueError):
            activity.save()
        with self.assertRaises(ValueError):
            activity.delete()
        with self.assertRaises(ValueError):
            BackupActivity.objects.filter(pk=activity.pk).update(
                sanitized_message="Tampered"
            )
        with self.assertRaises(ValueError):
            BackupActivity.objects.filter(pk=activity.pk).delete()
        activity.refresh_from_db()
        self.assertEqual(activity.sanitized_message, "Request recorded.")


class BackupScopeServiceTests(BackupPhase1TestCase):
    def test_pos_only_scope_resolution(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        self.assertEqual(
            resolve_product_entitlements(self.business_a),
            (ProductOwner.POS,),
        )
        self.assertEqual(
            available_backup_scopes(self.business_a),
            (BackupScope.POS, BackupScope.ALL_ENABLED),
        )
        resolution = resolve_requested_scope(
            self.business_a,
            BackupScope.ALL_ENABLED,
        )
        self.assertEqual(resolution.included_products, (ProductOwner.POS,))

    def test_wms_only_scope_resolution(self):
        self.set_entitlements(self.business_a, pos=False, wms=True)
        self.assertEqual(
            resolve_product_entitlements(self.business_a),
            (ProductOwner.WMS,),
        )
        self.assertEqual(
            available_backup_scopes(self.business_a),
            (BackupScope.WMS, BackupScope.ALL_ENABLED),
        )
        resolution = resolve_requested_scope(
            self.business_a,
            BackupScope.ALL_ENABLED,
        )
        self.assertEqual(resolution.included_products, (ProductOwner.WMS,))

    def test_combined_scope_resolution(self):
        self.set_entitlements(self.business_a, pos=True, wms=True)
        self.assertEqual(
            available_backup_scopes(self.business_a),
            (
                BackupScope.POS,
                BackupScope.WMS,
                BackupScope.ALL_ENABLED,
            ),
        )
        resolution = resolve_requested_scope(
            self.business_a,
            BackupScope.ALL_ENABLED,
        )
        self.assertEqual(
            resolution.included_products,
            (ProductOwner.POS, ProductOwner.WMS),
        )

    def test_disabled_product_scope_is_rejected_by_service(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        with self.assertRaises(ScopeNotAllowed):
            resolve_requested_scope(self.business_a, BackupScope.WMS)
        with self.assertRaises(ScopeNotAllowed):
            self.make_backup(scope=BackupScope.WMS)
        self.assertFalse(
            BackupRecord.objects.for_business(self.business_a).exists()
        )

    def test_tenant_with_no_enabled_product_has_no_scope(self):
        self.set_entitlements(self.business_a, pos=False, wms=False)
        self.assertEqual(available_backup_scopes(self.business_a), ())
        with self.assertRaises(ScopeNotAllowed):
            resolve_requested_scope(self.business_a, BackupScope.ALL_ENABLED)

    def test_owner_ui_lists_only_currently_entitled_scopes(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        self.client.force_login(self.owner_a)
        response = self.client.get(reverse("backups:dashboard"))
        self.assertEqual(response.status_code, 200)
        choices = dict(response.context["create_form"].fields["scope"].choices)
        self.assertIn(BackupScope.POS, choices)
        self.assertIn(BackupScope.ALL_ENABLED, choices)
        self.assertNotIn(BackupScope.WMS, choices)


class BackupServiceIsolationAndIdempotencyTests(BackupPhase1TestCase):
    def test_queued_request_records_scope_versions_actor_and_activity(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        backup = self.make_backup(idempotency_key="backup:metadata-contract")

        self.assertEqual(backup.status, BackupStatus.QUEUED)
        self.assertEqual(backup.integrity_status, IntegrityStatus.NOT_CHECKED)
        self.assertEqual(backup.scope, BackupScope.POS)
        self.assertEqual(backup.included_products, [ProductOwner.POS])
        self.assertEqual(backup.tenant_public_id_snapshot, self.business_a.public_id)
        self.assertEqual(backup.created_by, self.owner_a)
        self.assertTrue(backup.format_version)
        self.assertEqual(len(backup.schema_fingerprint), 64)
        self.assertTrue(
            BackupActivity.objects.filter(
                business=self.business_a,
                backup=backup,
                event_type="backup.requested",
            ).exists()
        )

    def test_same_tenant_idempotency_key_returns_same_request(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        first = self.make_backup(idempotency_key="backup:same-request")
        second = self.make_backup(idempotency_key="backup:same-request")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            BackupRecord.objects.for_business(self.business_a)
            .filter(idempotency_key="backup:same-request")
            .count(),
            1,
        )

    def test_idempotency_key_reuse_for_different_request_is_rejected(self):
        self.set_entitlements(self.business_a, pos=True, wms=True)
        self.make_backup(
            scope=BackupScope.POS,
            idempotency_key="backup:conflict",
        )
        with self.assertRaises(IdempotencyConflict):
            self.make_backup(
                scope=BackupScope.WMS,
                idempotency_key="backup:conflict",
            )

    def test_idempotency_key_is_tenant_scoped(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        self.set_entitlements(self.business_b, pos=True, wms=False)
        first = self.make_backup(idempotency_key="backup:tenant-local")
        second = self.make_backup(
            business=self.business_b,
            actor=self.owner_b,
            idempotency_key="backup:tenant-local",
        )
        self.assertNotEqual(first.pk, second.pk)

    def test_cross_tenant_selector_and_owner_detail_fail_with_not_found(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        self.set_entitlements(self.business_b, pos=True, wms=False)
        other = self.make_backup(
            business=self.business_b,
            actor=self.owner_b,
        )
        with self.assertRaises(Http404):
            get_backup_for_business(self.business_a, other.public_id)

        self.client.force_login(self.owner_a)
        response = self.client.get(
            reverse("backups:detail", kwargs={"public_id": other.public_id})
        )
        self.assertEqual(response.status_code, 404)

    def test_disabled_product_history_is_hidden_from_owner(self):
        self.set_entitlements(self.business_a, pos=False, wms=True)
        wms_backup = self.make_backup(scope=BackupScope.WMS)
        self.set_entitlements(self.business_a, pos=True, wms=False)

        with self.assertRaises(Http404):
            get_backup_for_business(self.business_a, wms_backup.public_id)
        self.client.force_login(self.owner_a)
        response = self.client.get(reverse("backups:history"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(wms_backup, list(response.context["page_obj"].object_list))

    def test_non_owner_cannot_create_metadata_without_permission(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        with self.assertRaises(PermissionDenied):
            self.make_backup(actor=self.cashier_a)

    def test_activity_service_rejects_cross_tenant_reference(self):
        other_backup = BackupRecord.objects.create(
            **self.backup_model_kwargs(business=self.business_b)
        )
        with self.assertRaises(ValidationError):
            create_backup_activity(
                business=self.business_a,
                backup=other_backup,
                event_type="backup.rejected",
                actor=self.owner_a,
            )

    def test_activity_service_redacts_sensitive_metadata(self):
        backup = BackupRecord.objects.create(**self.backup_model_kwargs())
        activity = create_backup_activity(
            business=self.business_a,
            backup=backup,
            event_type="backup.security_test",
            actor=self.owner_a,
            structured_metadata={
                "safe": "visible",
                "token": "must-not-persist",
                "nested": {"encryption_key": "must-not-persist"},
            },
        )
        self.assertEqual(activity.structured_metadata["safe"], "visible")
        self.assertEqual(activity.structured_metadata["token"], "[REDACTED]")
        self.assertEqual(
            activity.structured_metadata["nested"]["encryption_key"],
            "[REDACTED]",
        )


class BackupLifecycleAndLockTests(BackupPhase1TestCase):
    def complete_verified_backup(self, backup):
        for status in (
            BackupStatus.PREPARING,
            BackupStatus.SNAPSHOTTING,
            BackupStatus.PACKAGING,
            BackupStatus.UPLOADING,
            BackupStatus.VERIFYING,
        ):
            backup = transition_backup(backup, status)
        backup = set_backup_integrity(backup, IntegrityStatus.VERIFYING)
        backup = set_backup_integrity(backup, IntegrityStatus.VERIFIED)
        return transition_backup(backup, BackupStatus.SUCCEEDED)

    def test_transition_service_rejects_skipping_lifecycle(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        backup = self.make_backup()
        backup = transition_backup(backup, BackupStatus.PREPARING)
        self.assertIsNotNone(backup.started_at)
        with self.assertRaises(InvalidStateTransition):
            transition_backup(backup, BackupStatus.SUCCEEDED)

    def test_transition_service_tombstones_failed_metadata(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        backup = self.make_backup()
        backup = transition_backup(
            backup,
            BackupStatus.FAILED,
            failure_code="phase1_test",
            failure_summary="No artifact was created.",
        )
        backup = transition_backup(backup, BackupStatus.DELETION_PENDING)
        backup = transition_backup(backup, BackupStatus.DELETED)

        self.assertEqual(backup.status, BackupStatus.DELETED)
        self.assertIsNotNone(backup.deleted_at)
        self.assertTrue(BackupRecord.objects.filter(pk=backup.pk).exists())

    def test_only_one_active_lock_can_be_acquired_atomically(self):
        now = timezone.now()
        first = acquire_tenant_operation_lock(
            business=self.business_a,
            operation_kind=OperationKind.BACKUP,
            operation_public_id=uuid.uuid4(),
            lease_seconds=300,
            now=now,
        )
        with self.assertRaises(TenantOperationLocked):
            acquire_tenant_operation_lock(
                business=self.business_a,
                operation_kind=OperationKind.RESTORE,
                operation_public_id=uuid.uuid4(),
                lease_seconds=300,
                now=now,
            )
        self.assertEqual(
            TenantOperationLock.objects.for_business(self.business_a)
            .filter(active=True)
            .count(),
            1,
        )
        self.assertTrue(
            release_tenant_operation_lock(first, lock_token=first.lock_token)
        )

    def test_lock_token_is_required_for_heartbeat_and_release(self):
        lock = acquire_tenant_operation_lock(
            business=self.business_a,
            operation_kind=OperationKind.BACKUP,
            operation_public_id=uuid.uuid4(),
        )
        self.assertFalse(
            heartbeat_tenant_operation_lock(
                lock,
                lock_token=uuid.uuid4(),
            )
        )
        self.assertFalse(
            release_tenant_operation_lock(
                lock,
                lock_token=uuid.uuid4(),
            )
        )
        self.assertTrue(
            heartbeat_tenant_operation_lock(
                lock,
                lock_token=lock.lock_token,
            )
        )
        self.assertTrue(
            release_tenant_operation_lock(
                lock,
                lock_token=lock.lock_token,
            )
        )

    def test_expired_lock_is_reclaimed_without_two_active_rows(self):
        acquired_at = timezone.now()
        stale = acquire_tenant_operation_lock(
            business=self.business_a,
            operation_kind=OperationKind.BACKUP,
            operation_public_id=uuid.uuid4(),
            lease_seconds=5,
            now=acquired_at,
        )
        replacement = acquire_tenant_operation_lock(
            business=self.business_a,
            operation_kind=OperationKind.RESTORE,
            operation_public_id=uuid.uuid4(),
            lease_seconds=300,
            now=acquired_at + timedelta(seconds=6),
        )

        stale.refresh_from_db()
        self.assertFalse(stale.active)
        self.assertIsNotNone(stale.released_at)
        self.assertTrue(replacement.active)
        self.assertNotEqual(stale.lock_token, replacement.lock_token)
        self.assertEqual(
            TenantOperationLock.objects.for_business(self.business_a)
            .filter(active=True)
            .count(),
            1,
        )

    def test_schedule_upsert_updates_the_single_tenant_row(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        first = upsert_backup_schedule(
            business=self.business_a,
            local_execution_time=time(2, 0),
            enabled=True,
            actor=self.owner_a,
        )
        second = upsert_backup_schedule(
            business=self.business_a,
            local_execution_time=time(3, 30),
            enabled=False,
            actor=self.owner_a,
        )
        self.assertEqual(first.pk, second.pk)
        self.assertFalse(second.enabled)
        self.assertEqual(second.local_execution_time, time(3, 30))
        self.assertEqual(second.scope, BackupScope.ALL_ENABLED)
        self.assertEqual(
            BackupSchedule.objects.for_business(self.business_a).count(),
            1,
        )

    def test_restore_request_is_metadata_only_and_idempotent(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        backup = self.complete_verified_backup(self.make_backup())
        product_count = self.business_a.catalog_product_set.count()
        first = create_restore_request(
            business=self.business_a,
            source_backup=backup,
            requested_scope=BackupScope.POS,
            actor=self.owner_a,
            reason="Validate Phase 1 restore metadata.",
            idempotency_key="restore:metadata-only",
        )
        second = create_restore_request(
            business=self.business_a,
            source_backup=backup,
            requested_scope=BackupScope.POS,
            actor=self.owner_a,
            reason="Repeated safely.",
            idempotency_key="restore:metadata-only",
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.status, RestoreStatus.QUEUED)
        self.assertIsNone(first.safety_backup)
        self.assertEqual(
            self.business_a.catalog_product_set.count(),
            product_count,
        )

    def test_restore_cannot_validate_without_its_verified_safety_backup(self):
        self.set_entitlements(self.business_a, pos=True, wms=False)
        source = self.complete_verified_backup(self.make_backup())
        restore = create_restore_request(
            business=self.business_a,
            source_backup=source,
            requested_scope=BackupScope.POS,
            actor=self.owner_a,
            reason="Exercise the mandatory safety gate.",
            idempotency_key="restore:safety-gate",
        )
        for status in (
            RestoreStatus.AUTHORIZING,
            RestoreStatus.LOCKING,
            RestoreStatus.SAFETY_BACKUP,
        ):
            restore = transition_restore(restore, status)

        with self.assertRaises(ValidationError):
            transition_restore(restore, RestoreStatus.VALIDATING)

        safety = create_backup_request(
            business=self.business_a,
            scope=BackupScope.ALL_ENABLED,
            actor=self.owner_a,
            trigger=BackupTrigger.PRE_RESTORE_SAFETY,
            parent_restore_operation=restore,
            idempotency_key="backup:restore-safety",
        )
        self.assertTrue(safety.protected)
        safety = self.complete_verified_backup(safety)
        restore = set_restore_safety_backup(restore, safety)
        restore = transition_restore(restore, RestoreStatus.VALIDATING)
        self.assertEqual(restore.status, RestoreStatus.VALIDATING)
        self.assertEqual(restore.safety_backup_id, safety.pk)


class BackupPlatformMetadataViewTests(BackupPhase1TestCase):
    def setUp(self):
        self.platform_admin = User.objects.create_user(
            email="backup-platform@example.com",
            password="StrongPass123!",
            full_name="Backup Platform Admin",
            is_platform_admin=True,
        )
        self.backup_a = BackupRecord.objects.create(
            **self.backup_model_kwargs(
                business=self.business_a,
                idempotency_key="platform:a",
            )
        )
        self.backup_b = BackupRecord.objects.create(
            **self.backup_model_kwargs(
                business=self.business_b,
                idempotency_key="platform:b",
            )
        )
        self.activity_a = BackupActivity.objects.create(
            business=self.business_a,
            backup=self.backup_a,
            event_type="backup.platform_test",
            sanitized_message="Safe platform metadata.",
        )

    def test_platform_admin_can_read_backup_metadata_pages(self):
        self.client.force_login(self.platform_admin)
        urls = (
            reverse("platformadmin:backup_list"),
            reverse(
                "platformadmin:backup_detail",
                kwargs={"public_id": self.backup_a.public_id},
            ),
            reverse("platformadmin:backup_operations"),
            reverse("platformadmin:backup_activity"),
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_platform_activity_get_uses_canonical_restore_relation(self):
        self.client.force_login(self.platform_admin)
        response = self.client.get(reverse("platformadmin:backup_activity"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            self.activity_a,
            list(response.context["page_obj"].object_list),
        )

    def test_platform_business_filter_does_not_mix_tenants(self):
        self.client.force_login(self.platform_admin)
        response = self.client.get(
            reverse("platformadmin:backup_list"),
            {"business": str(self.business_b.public_id)},
        )
        self.assertEqual(response.status_code, 200)
        visible = list(response.context["page_obj"].object_list)
        self.assertEqual(visible, [self.backup_b])
        self.assertNotIn(self.backup_a, visible)

    def test_platform_views_hide_backup_after_product_is_disabled(self):
        self.set_entitlements(self.business_a, pos=False, wms=True)
        self.client.force_login(self.platform_admin)

        response = self.client.get(reverse("platformadmin:backup_list"))
        self.assertEqual(response.status_code, 200)
        visible = list(response.context["page_obj"].object_list)
        self.assertNotIn(self.backup_a, visible)
        self.assertIn(self.backup_b, visible)
        detail = self.client.get(
            reverse(
                "platformadmin:backup_detail",
                kwargs={"public_id": self.backup_a.public_id},
            )
        )
        self.assertEqual(detail.status_code, 404)

    def test_invalid_platform_business_filter_fails_closed(self):
        self.client.force_login(self.platform_admin)
        response = self.client.get(
            reverse("platformadmin:backup_list"),
            {"business": "not-a-business-uuid"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["page_obj"].object_list), [])

    def test_business_owner_cannot_enter_platform_backup_metadata(self):
        self.client.force_login(self.owner_a)
        self.assertEqual(
            self.client.get(reverse("platformadmin:backup_list")).status_code,
            403,
        )

class BackupPermissionTests(BackupPhase1TestCase):
    def test_permission_registry_has_no_owner_delete_capability(self):
        self.assertTrue(BACKUP_PERMISSION_CODES.issubset(ALL_PERMISSION_CODES))
        self.assertNotIn("backups.delete", ALL_PERMISSION_CODES)

    def test_business_owner_receives_all_approved_backup_permissions(self):
        membership = Membership.objects.get(
            business=self.business_a,
            user=self.owner_a,
        )
        self.assertTrue(membership.role.is_owner)
        for permission_code in BACKUP_PERMISSION_CODES:
            with self.subTest(permission_code=permission_code):
                self.assertTrue(membership.has_perm(permission_code))

    def test_non_owner_role_templates_receive_no_backup_permissions(self):
        for role_name, definition in DEFAULT_ROLES.items():
            if definition.get("is_owner"):
                continue
            with self.subTest(role_name=role_name):
                self.assertTrue(
                    BACKUP_PERMISSION_CODES.isdisjoint(definition["permissions"])
                )

    def test_existing_non_owner_is_denied_by_default(self):
        membership = Membership.objects.get(
            business=self.business_a,
            user=self.cashier_a,
        )
        for permission_code in BACKUP_PERMISSION_CODES:
            with self.subTest(permission_code=permission_code):
                self.assertFalse(membership.has_perm(permission_code))

    def test_owner_dashboard_access_and_non_owner_denial(self):
        self.client.force_login(self.owner_a)
        self.assertEqual(
            self.client.get(reverse("backups:dashboard")).status_code,
            200,
        )

        self.client.force_login(self.cashier_a)
        self.assertEqual(
            self.client.get(reverse("backups:dashboard")).status_code,
            403,
        )

    def test_owner_has_no_delete_route(self):
        self.client.force_login(self.owner_a)
        missing_path = f"/backups/{uuid.uuid4()}/delete/"
        self.assertEqual(self.client.post(missing_path).status_code, 404)
        with self.assertRaises(NoReverseMatch):
            reverse("backups:delete", kwargs={"public_id": uuid.uuid4()})
