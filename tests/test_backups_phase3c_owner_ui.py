"""Focused tenant-owner Backup & Restore UI tests for Phase 3C."""

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.backups import owner_services, selectors
from apps.backups.enums import (
    BackupScope,
    BackupStatus,
    BackupTrigger,
    CompatibilityStatus,
    IntegrityStatus,
)
from apps.backups.models import BackupActivity, BackupRecord, RestoreOperation
from apps.backups.owner_services import OwnerActionCapability, OwnerPreflightOutcome
from apps.backups.tasks import RESTORE_QUEUE_NAME
from apps.catalog.models import Product

from .test_backups_phase1 import BackupPhase1TestCase


class BackupPhase3COwnerUITests(BackupPhase1TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.set_entitlements(cls, cls.business_a, pos=True, wms=False)
        cls.set_entitlements(cls, cls.business_b, pos=True, wms=False)

    def setUp(self):
        self.client.force_login(self.owner_a)

    def _backup(self, *, business=None, actor=None, **overrides):
        business = business or self.business_a
        values = self.backup_model_kwargs(
            business=business,
            scope=BackupScope.POS,
            **overrides,
        )
        return BackupRecord.objects.create(**values)

    def _eligible_backup(self, *, business=None, **overrides):
        values = {
            "status": BackupStatus.SUCCEEDED,
            "integrity_status": IntegrityStatus.VERIFIED,
            "compatibility_status": CompatibilityStatus.COMPATIBLE,
            "storage_backend_identifier": "private-store",
            "opaque_object_key": str(uuid.uuid4()),
            "whole_artifact_hash": "b" * 64,
            "backup_size_bytes": 5 * 1024 * 1024,
            "duration": timedelta(minutes=2, seconds=7),
            "completed_at": timezone.now(),
            "verified_at": timezone.now(),
        }
        values.update(overrides)
        return self._backup(business=business, **values)

    def _install_preflight_session(self, backup, *, ready):
        restore = RestoreOperation.objects.create(
            business=self.business_a,
            source_backup=backup,
            requested_scope=backup.scope,
            requested_by=self.owner_a,
            reason="Owner recovery test",
            compatibility_status=CompatibilityStatus.COMPATIBLE,
            idempotency_key=f"ui-restore:{uuid.uuid4()}",
        )
        session = self.client.session
        session["backups_owner_preflight"] = {
            "business_public_id": str(self.business_a.public_id),
            "backup_public_id": str(backup.public_id),
            "restore_public_id": str(restore.public_id),
            "ready": ready,
            "compatibility": "Compatible" if ready else "Not verified",
            "component_count": 4 if ready else 0,
            "record_count": 120 if ready else 0,
            "media_count": 3 if ready else 0,
            "messages": [
                "Restore readiness checks passed."
                if ready
                else "This backup did not pass every restore readiness check."
            ],
        }
        session.save()
        return restore

    def test_owner_can_open_landing_page(self):
        response = self.client.get(reverse("backups:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Backup &amp; Restore")

    def test_unauthorized_staff_cannot_access_landing_page(self):
        self.client.force_login(self.cashier_a)
        self.assertEqual(self.client.get(reverse("backups:dashboard")).status_code, 403)

    def test_tenant_history_is_isolated(self):
        own = self._backup()
        other = self._backup(business=self.business_b)
        response = self.client.get(reverse("backups:history"))
        self.assertIn(own, response.context["page_obj"].object_list)
        self.assertNotIn(other, response.context["page_obj"].object_list)

    def test_cross_tenant_detail_is_not_found(self):
        other = self._backup(business=self.business_b)
        response = self.client.get(
            reverse("backups:detail", kwargs={"public_id": other.public_id})
        )
        self.assertEqual(response.status_code, 404)

    def test_empty_state_is_owner_friendly(self):
        response = self.client.get(reverse("backups:dashboard"))
        self.assertContains(response, "Your first secure backup will appear here.")

    def test_last_backup_card_uses_latest_attempt(self):
        self._eligible_backup()
        failed = self._backup(
            status=BackupStatus.FAILED,
            sanitized_failure_summary="Backup could not be completed.",
        )
        response = self.client.get(reverse("backups:dashboard"))
        self.assertEqual(response.context["latest_backup"], failed)
        self.assertContains(response, "Failed")

    def test_last_successful_card_ignores_later_failure(self):
        successful = self._eligible_backup()
        self._backup(status=BackupStatus.FAILED)
        response = self.client.get(reverse("backups:dashboard"))
        self.assertEqual(response.context["latest_successful"], successful)

    def test_size_and_duration_are_human_readable(self):
        self._eligible_backup()
        response = self.client.get(reverse("backups:dashboard"))
        self.assertContains(response, "5.0\u00a0MB")
        self.assertContains(response, "2m 7s")

    def test_history_is_ordered_newest_first(self):
        first = self._backup()
        second = self._backup()
        rows = list(
            self.client.get(reverse("backups:history")).context["page_obj"].object_list
        )
        self.assertEqual(rows[:2], [second, first])

    def test_safety_backup_type_is_distinct(self):
        source = self._eligible_backup()
        restore = RestoreOperation.objects.create(
            business=self.business_a,
            source_backup=source,
            requested_scope=BackupScope.POS,
            requested_by=self.owner_a,
            reason="Safety backup label test",
            idempotency_key=f"restore:{uuid.uuid4()}",
        )
        self._eligible_backup(
            trigger=BackupTrigger.PRE_RESTORE_SAFETY,
            parent_restore_operation=restore,
            protected=True,
        )
        response = self.client.get(reverse("backups:history"))
        self.assertContains(response, "Safety Backup")

    def test_failed_detail_shows_only_safe_summary(self):
        backup = self._backup(
            status=BackupStatus.FAILED,
            failure_code="provider_secret_failure",
            sanitized_failure_summary="Backup could not be completed safely.",
        )
        response = self.client.get(
            reverse("backups:detail", kwargs={"public_id": backup.public_id})
        )
        self.assertContains(response, "Backup could not be completed safely.")
        self.assertNotContains(response, "provider_secret_failure")

    def test_active_backup_status_is_displayed(self):
        self._backup(status=BackupStatus.SNAPSHOTTING)
        response = self.client.get(reverse("backups:dashboard"))
        self.assertContains(response, "Backup in progress")
        self.assertContains(response, "Creating backup")

    def test_manual_backup_is_post_only(self):
        self.assertEqual(self.client.get(reverse("backups:manual")).status_code, 405)

    def test_manual_backup_does_not_execute_inline(self):
        capability = OwnerActionCapability(True, "Available")
        with (
            patch.object(owner_services, "manual_backup_capability", return_value=capability),
            patch.object(owner_services, "_enqueue_backup") as enqueue,
            patch("apps.backups.engine.runtime.request_backup_execution") as execute,
        ):
            owner_services.request_manual_backup(
                business=self.business_a,
                actor=self.owner_a,
                scope=BackupScope.POS,
            )
        enqueue.assert_called_once()
        execute.assert_not_called()

    def test_manual_backup_enqueues_public_identifiers(self):
        capability = OwnerActionCapability(True, "Available")
        with (
            patch.object(owner_services, "manual_backup_capability", return_value=capability),
            patch.object(owner_services, "_enqueue_backup") as enqueue,
        ):
            backup = owner_services.request_manual_backup(
                business=self.business_a,
                actor=self.owner_a,
                scope=BackupScope.ALL_ENABLED,
            )
        enqueue.assert_called_once_with(
            backup_public_id=backup.public_id,
            business_public_id=self.business_a.public_id,
        )

    def test_duplicate_active_manual_backup_is_blocked(self):
        self._backup(status=BackupStatus.PREPARING)
        with (
            patch.object(
                owner_services,
                "manual_backup_capability",
                return_value=OwnerActionCapability(True, "Available"),
            ),
            patch.object(owner_services, "_enqueue_backup") as enqueue,
            self.assertRaises(owner_services.OwnerBackupActionUnavailable),
        ):
            owner_services.request_manual_backup(
                business=self.business_a,
                actor=self.owner_a,
                scope=BackupScope.POS,
            )
        enqueue.assert_not_called()

    def test_engine_disabled_manual_button_is_disabled(self):
        response = self.client.get(reverse("backups:dashboard"))
        self.assertContains(response, "Manual backup unavailable")
        self.assertContains(response, 'aria-disabled="true"')

    def test_unsafe_async_configuration_prevents_enqueue(self):
        with patch.object(owner_services, "_enqueue_backup") as enqueue:
            with self.assertRaises(owner_services.OwnerBackupActionUnavailable):
                owner_services.request_manual_backup(
                    business=self.business_a,
                    actor=self.owner_a,
                    scope=BackupScope.POS,
                )
        enqueue.assert_not_called()

    def test_manual_scope_choices_respect_entitlement(self):
        response = self.client.get(reverse("backups:dashboard"))
        choices = dict(response.context["create_form"].fields["scope"].choices)
        self.assertIn(BackupScope.POS, choices)
        self.assertIn(BackupScope.ALL_ENABLED, choices)
        self.assertNotIn(BackupScope.WMS, choices)

    def test_restore_action_only_for_eligible_backup(self):
        backup = self._eligible_backup()
        response = self.client.get(reverse("backups:history"))
        self.assertContains(
            response,
            reverse("backups:restore_preflight", kwargs={"public_id": backup.public_id}),
        )

    def test_failed_backup_has_no_restore_action(self):
        backup = self._backup(status=BackupStatus.FAILED)
        response = self.client.get(reverse("backups:history"))
        self.assertNotContains(
            response,
            reverse("backups:restore_preflight", kwargs={"public_id": backup.public_id}),
        )

    def test_deleted_backup_has_no_restore_action(self):
        backup = self._backup(
            status=BackupStatus.DELETED,
            integrity_status=IntegrityStatus.VERIFIED,
            deleted_at=timezone.now(),
            storage_backend_identifier="private-store",
            opaque_object_key=str(uuid.uuid4()),
            whole_artifact_hash="c" * 64,
        )
        self.assertFalse(selectors.is_backup_restore_eligible(self.business_a, backup))

    def test_preflight_get_does_not_execute_check(self):
        backup = self._eligible_backup()
        with patch.object(owner_services, "run_restore_preflight") as run:
            response = self.client.get(
                reverse("backups:restore_preflight", kwargs={"public_id": backup.public_id})
            )
        self.assertEqual(response.status_code, 200)
        run.assert_not_called()

    def test_preflight_post_does_not_mutate_tenant_data(self):
        backup = self._eligible_backup()
        product_count = Product.objects.for_business(self.business_a).count()
        outcome = OwnerPreflightOutcome(
            restore_public_id=str(uuid.uuid4()),
            ready=False,
            compatibility="Not verified",
            component_count=0,
            record_count=0,
            media_count=0,
            messages=("No data was changed.",),
        )
        with patch.object(owner_services, "run_restore_preflight", return_value=outcome):
            self.client.post(
                reverse("backups:restore_preflight", kwargs={"public_id": backup.public_id}),
                {"reason": "Validate recovery readiness"},
            )
        self.assertEqual(Product.objects.for_business(self.business_a).count(), product_count)

    def test_ready_preflight_shows_confirmation(self):
        backup = self._eligible_backup()
        self._install_preflight_session(backup, ready=True)
        response = self.client.get(
            reverse("backups:restore", kwargs={"public_id": backup.public_id})
        )
        self.assertContains(response, "Restore Ready")
        self.assertContains(response, 'Type &quot;RESTORE&quot; to confirm')

    def test_not_ready_preflight_disables_mutation(self):
        backup = self._eligible_backup()
        self._install_preflight_session(backup, ready=False)
        response = self.client.get(
            reverse("backups:restore", kwargs={"public_id": backup.public_id})
        )
        self.assertContains(response, "Not Ready")
        self.assertContains(response, "Restore unavailable")

    def test_wrong_tenant_preflight_is_not_found(self):
        other = self._eligible_backup(business=self.business_b)
        response = self.client.post(
            reverse("backups:restore_preflight", kwargs={"public_id": other.public_id}),
            {"reason": "Cross tenant attempt"},
        )
        self.assertEqual(response.status_code, 404)

    def test_final_restore_requires_explicit_confirmation(self):
        backup = self._eligible_backup()
        self._install_preflight_session(backup, ready=True)
        response = self.client.post(
            reverse("backups:restore", kwargs={"public_id": backup.public_id}),
            {},
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context["form"].errors)

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=False)
    def test_restore_mutation_refuses_when_flag_disabled(self):
        backup = self._eligible_backup()
        self._install_preflight_session(backup, ready=True)
        response = self.client.post(
            reverse("backups:restore", kwargs={"public_id": backup.public_id}),
            {"acknowledge_replacement": "on", "confirmation": "RESTORE"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertContains(
            response,
            "Restore is currently disabled by the system administrator.",
            status_code=503,
        )

    def test_no_restore_executes_inline_in_request(self):
        backup = self._eligible_backup()
        self._install_preflight_session(backup, ready=True)
        with patch("apps.backups.engine.restore_mutation.execute_restore") as execute:
            self.client.post(
                reverse("backups:restore", kwargs={"public_id": backup.public_id}),
                {"acknowledge_replacement": "on", "confirmation": "RESTORE"},
            )
        execute.assert_not_called()

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=True)
    def test_flag_alone_does_not_bypass_missing_restore_task(self):
        backup = self._eligible_backup()
        self._install_preflight_session(backup, ready=True)
        response = self.client.post(
            reverse("backups:restore", kwargs={"public_id": backup.public_id}),
            {"acknowledge_replacement": "on", "confirmation": "RESTORE"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertContains(response, "dedicated secure restore worker", status_code=503)

    def test_restore_queue_name_is_dedicated(self):
        self.assertEqual(RESTORE_QUEUE_NAME, "nexa.restores")

    def test_csrf_protects_manual_post(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner_a)
        response = client.post(reverse("backups:manual"), {"scope": BackupScope.POS})
        self.assertEqual(response.status_code, 403)

    def test_sensitive_storage_metadata_is_never_rendered(self):
        backup = self._eligible_backup(
            storage_backend_identifier="provider-secret",
            opaque_object_key="object-key-secret",
            encryption_key_identifier="kek-secret",
        )
        response = self.client.get(
            reverse("backups:detail", kwargs={"public_id": backup.public_id})
        )
        for secret in ("provider-secret", "object-key-secret", "kek-secret"):
            self.assertNotContains(response, secret)

    def test_platform_admin_controls_are_absent(self):
        response = self.client.get(reverse("backups:dashboard"))
        self.assertNotContains(response, "Platform Admin")
        self.assertNotContains(response, "Retention policy")

    def test_no_delete_or_download_actions(self):
        self._eligible_backup()
        response = self.client.get(reverse("backups:history"))
        self.assertNotContains(response, ">Delete<")
        self.assertNotContains(response, ">Download<")

    def test_navigation_is_visible_to_owner(self):
        response = self.client.get(reverse("backups:dashboard"))
        self.assertContains(response, "Backup &amp; Restore")

    def test_navigation_is_hidden_without_permission(self):
        self.client.force_login(self.cashier_a)
        response = self.client.get(reverse("sales:list"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Backup &amp; Restore")

    def test_mobile_user_agent_preserves_core_rendering(self):
        response = self.client.get(
            reverse("backups:history"),
            HTTP_USER_AGENT="Mozilla/5.0 (iPhone; Mobile)",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "backup-table")

    def test_manual_request_records_owner_activity(self):
        with (
            patch.object(
                owner_services,
                "manual_backup_capability",
                return_value=OwnerActionCapability(True, "Available"),
            ),
            patch.object(owner_services, "_enqueue_backup"),
        ):
            backup = owner_services.request_manual_backup(
                business=self.business_a,
                actor=self.owner_a,
                scope=BackupScope.POS,
            )
        self.assertTrue(
            BackupActivity.objects.filter(
                backup=backup,
                event_type=owner_services.MANUAL_BACKUP_REQUESTED,
            ).exists()
        )

    def test_enqueue_failure_is_safely_recorded(self):
        with (
            patch.object(
                owner_services,
                "manual_backup_capability",
                return_value=OwnerActionCapability(True, "Available"),
            ),
            patch.object(owner_services, "_enqueue_backup", side_effect=RuntimeError),
            self.assertRaises(owner_services.OwnerBackupActionUnavailable),
        ):
            owner_services.request_manual_backup(
                business=self.business_a,
                actor=self.owner_a,
                scope=BackupScope.POS,
            )
        backup = BackupRecord.objects.for_business(self.business_a).latest("created_at")
        self.assertEqual(backup.status, BackupStatus.QUEUED)
        self.assertEqual(backup.failure_code, "")
        self.assertTrue(
            BackupActivity.objects.filter(
                backup=backup,
                event_type="backup.dispatch_failed",
            ).exists()
        )


def load_tests(loader, standard_tests, pattern):
    del standard_tests, pattern
    return loader.loadTestsFromTestCase(BackupPhase3COwnerUITests)
