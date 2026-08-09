"""Focused Platform Admin Backup & Restore control-center tests for Phase 3D."""

import uuid
from datetime import time, timedelta
from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.backups import platform_services
from apps.backups.engine.availability import get_engine_capability
from apps.backups.enums import (
    ActivitySeverity,
    BackupScope,
    BackupStatus,
    BackupTrigger,
    CompatibilityStatus,
    IntegrityStatus,
)
from apps.backups.models import (
    BackupActivity,
    BackupRecord,
    BackupSchedule,
    RestoreOperation,
)
from apps.backups.platform_services import (
    PlatformActionCapability,
    PlatformPreflightOutcome,
)
from apps.platformadmin.middleware import SESSION_KEY

from .test_backups_phase1 import BackupPhase1TestCase


class BackupPhase3DPlatformUITests(BackupPhase1TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.set_entitlements(cls, cls.business_a, pos=True, wms=False)
        cls.set_entitlements(cls, cls.business_b, pos=True, wms=False)
        cls.platform_admin = User.objects.create_superuser(
            email="phase3d-platform@example.com",
            password="StrongPass123!",
            full_name="Phase 3D Platform Admin",
        )
        cls.platform_reader = User.objects.create_user(
            email="phase3d-reader@example.com",
            password="StrongPass123!",
            full_name="Phase 3D Platform Reader",
            is_platform_admin=True,
        )

    def setUp(self):
        self.client.force_login(self.platform_admin)

    def _backup(self, *, business=None, **overrides):
        scope = overrides.pop("scope", BackupScope.POS)
        values = self.backup_model_kwargs(
            business=business or self.business_a,
            scope=scope,
            **overrides,
        )
        return BackupRecord.objects.create(**values)

    def _eligible_backup(self, *, business=None, **overrides):
        values = {
            "status": BackupStatus.SUCCEEDED,
            "integrity_status": IntegrityStatus.VERIFIED,
            "compatibility_status": CompatibilityStatus.COMPATIBLE,
            "storage_backend_identifier": "private-store",
            "opaque_object_key": f"tenant-object-{uuid.uuid4()}",
            "whole_artifact_hash": "b" * 64,
            "backup_size_bytes": 5 * 1024 * 1024,
            "duration": timedelta(minutes=2, seconds=7),
            "component_count": 4,
            "total_row_count": 120,
            "media_count": 3,
            "completed_at": timezone.now(),
            "verified_at": timezone.now(),
        }
        values.update(overrides)
        return self._backup(business=business, **values)

    def _install_preflight(self, backup, *, ready=True):
        restore = RestoreOperation.objects.create(
            business=backup.business,
            source_backup=backup,
            requested_scope=backup.scope,
            requested_by=self.platform_admin,
            reason="Platform recovery test",
            compatibility_status=CompatibilityStatus.COMPATIBLE,
            idempotency_key=f"phase3d-restore:{uuid.uuid4()}",
        )
        session = self.client.session
        session["backups_platform_preflight"] = {
            "business_public_id": str(backup.business.public_id),
            "backup_public_id": str(backup.public_id),
            "restore_public_id": str(restore.public_id),
            "actor_public_id": str(self.platform_admin.public_id),
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

    def test_platform_admin_can_access_dashboard(self):
        response = self.client.get(reverse("platformadmin:backup_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Backup &amp; Restore Control Center")

    def test_platform_reader_can_access_dashboard(self):
        self.client.force_login(self.platform_reader)
        self.assertEqual(
            self.client.get(reverse("platformadmin:backup_list")).status_code,
            200,
        )

    def test_tenant_owner_cannot_access(self):
        self.client.force_login(self.owner_a)
        self.assertEqual(
            self.client.get(reverse("platformadmin:backup_list")).status_code,
            403,
        )

    def test_normal_staff_cannot_access(self):
        self.client.force_login(self.cashier_a)
        self.assertEqual(
            self.client.get(reverse("platformadmin:backup_list")).status_code,
            403,
        )

    def test_dashboard_kpi_counts_are_correct(self):
        self._eligible_backup()
        self._backup(business=self.business_b, status=BackupStatus.FAILED)
        self._backup(status=BackupStatus.PREPARING)
        response = self.client.get(reverse("platformadmin:backup_list"))
        summary = response.context["summary"]
        self.assertEqual(summary["businesses_with_backups"], 2)
        self.assertEqual(summary["successful_backups"], 1)
        self.assertEqual(summary["failed_backups"], 1)
        self.assertEqual(summary["active_backups"], 1)
        self.assertEqual(summary["total_durable_storage"], 5 * 1024 * 1024)
        self.assertEqual(summary["tenants_without_success"], 1)

    def test_business_name_filter_works(self):
        own = self._backup()
        self._backup(business=self.business_b)
        response = self.client.get(
            reverse("platformadmin:backup_list"), {"business_name": "Alpha"}
        )
        self.assertEqual(list(response.context["page_obj"].object_list), [own])

    def test_business_uuid_filter_works(self):
        self._backup()
        other = self._backup(business=self.business_b)
        response = self.client.get(
            reverse("platformadmin:backup_list"),
            {"business_uuid": self.business_b.public_id},
        )
        self.assertEqual(list(response.context["page_obj"].object_list), [other])

    def test_status_filter_works(self):
        self._backup()
        failed = self._backup(status=BackupStatus.FAILED)
        response = self.client.get(
            reverse("platformadmin:backup_list"), {"status": BackupStatus.FAILED}
        )
        self.assertEqual(list(response.context["page_obj"].object_list), [failed])

    def test_backup_detail_renders(self):
        backup = self._eligible_backup()
        response = self.client.get(
            reverse("platformadmin:backup_detail", args=[backup.public_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(backup.public_id))
        self.assertContains(response, self.business_a.name)

    def test_business_backup_overview_renders(self):
        self._eligible_backup()
        response = self.client.get(
            reverse("platformadmin:backup_business", args=[self.business_a.public_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tenant backup overview")
        self.assertContains(response, str(self.business_a.public_id))

    def test_cross_tenant_backup_binding_is_enforced(self):
        other = self._eligible_backup(business=self.business_b)
        response = self.client.get(
            reverse(
                "platformadmin:backup_preflight",
                args=[self.business_a.public_id, other.public_id],
            )
        )
        self.assertEqual(response.status_code, 404)

    def test_last_successful_backup_is_correct(self):
        successful = self._eligible_backup()
        self._backup(status=BackupStatus.FAILED)
        response = self.client.get(
            reverse("platformadmin:backup_business", args=[self.business_a.public_id])
        )
        self.assertEqual(
            response.context["backup_summary"]["latest_successful"], successful
        )

    def test_failed_backup_summary_is_safe(self):
        backup = self._backup(
            status=BackupStatus.FAILED,
            failure_code="internal.python.trace",
            sanitized_failure_summary="Safe execution summary.",
        )
        response = self.client.get(
            reverse("platformadmin:backup_detail", args=[backup.public_id])
        )
        self.assertContains(response, "Safe execution summary.")
        self.assertNotContains(response, "internal.python.trace")

    def test_active_backup_state_uses_safe_label(self):
        self._backup(status=BackupStatus.SNAPSHOTTING)
        response = self.client.get(
            reverse("platformadmin:backup_business", args=[self.business_a.public_id])
        )
        self.assertContains(response, "Creating backup")

    def test_size_and_duration_are_human_readable(self):
        backup = self._eligible_backup()
        response = self.client.get(
            reverse("platformadmin:backup_detail", args=[backup.public_id])
        )
        self.assertContains(response, "5.0\u00a0MB")
        self.assertContains(response, "2m 7s")

    def test_schedule_status_is_visible(self):
        BackupSchedule.objects.create(
            business=self.business_a,
            enabled=True,
            timezone_name="Asia/Muscat",
            local_execution_time=time(2, 30),
            next_run=timezone.now() + timedelta(days=1),
            scope=BackupScope.ALL_ENABLED,
            created_by=self.platform_admin,
        )
        response = self.client.get(
            reverse("platformadmin:backup_business", args=[self.business_a.public_id])
        )
        self.assertContains(response, "Asia/Muscat")
        self.assertContains(response, "Next scheduled backup")

    def test_retention_policy_summary_is_visible(self):
        self._eligible_backup(
            trigger=BackupTrigger.SCHEDULED,
            scope=BackupScope.ALL_ENABLED,
            included_products=["POS"],
            scheduled_local_date=timezone.localdate(),
            retention_eligible=True,
        )
        response = self.client.get(
            reverse("platformadmin:backup_business", args=[self.business_a.public_id])
        )
        self.assertContains(response, "Latest 5 successful daily full backups")
        self.assertContains(response, "Currently retained")

    def test_durable_integrity_status_is_visible(self):
        backup = self._eligible_backup()
        response = self.client.get(
            reverse("platformadmin:backup_detail", args=[backup.public_id])
        )
        self.assertContains(response, "Durable storage verified")
        self.assertContains(response, "Configured durable storage")

    def test_manual_backup_is_post_only(self):
        response = self.client.get(
            reverse("platformadmin:backup_manual", args=[self.business_a.public_id])
        )
        self.assertEqual(response.status_code, 405)

    def test_manual_backup_is_disabled_while_engine_unavailable(self):
        before = BackupRecord.objects.count()
        response = self.client.post(
            reverse("platformadmin:backup_manual", args=[self.business_a.public_id]),
            {"scope": BackupScope.POS},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(BackupRecord.objects.count(), before)

    def test_manual_backup_never_executes_inline(self):
        capability = PlatformActionCapability(True, "Available")
        with (
            patch.object(
                platform_services, "manual_backup_capability", return_value=capability
            ),
            patch.object(platform_services, "_enqueue_backup") as enqueue,
            patch("apps.backups.engine.runtime.request_backup_execution") as execute,
        ):
            platform_services.platform_request_manual_backup(
                business=self.business_a,
                actor=self.platform_admin,
                scope=BackupScope.POS,
            )
        enqueue.assert_called_once()
        execute.assert_not_called()

    def test_platform_actor_identity_is_used(self):
        capability = PlatformActionCapability(True, "Available")
        with (
            patch.object(
                platform_services, "manual_backup_capability", return_value=capability
            ),
            patch.object(platform_services, "_enqueue_backup"),
        ):
            backup = platform_services.platform_request_manual_backup(
                business=self.business_a,
                actor=self.platform_admin,
                scope=BackupScope.POS,
            )
        self.assertEqual(backup.created_by, self.platform_admin)
        self.assertTrue(backup.creator_actor_snapshot["platform_staff"])
        self.assertTrue(
            BackupActivity.objects.filter(
                backup=backup,
                event_type=platform_services.PLATFORM_MANUAL_BACKUP_REQUESTED,
            ).exists()
        )

    def test_scope_choices_respect_tenant_entitlement(self):
        response = self.client.get(
            reverse("platformadmin:backup_business", args=[self.business_a.public_id])
        )
        choices = dict(response.context["create_form"].fields["scope"].choices)
        self.assertIn(BackupScope.POS, choices)
        self.assertIn(BackupScope.ALL_ENABLED, choices)
        self.assertNotIn(BackupScope.WMS, choices)

    def test_restore_action_only_on_eligible_successful_backup(self):
        eligible = self._eligible_backup()
        failed = self._backup(status=BackupStatus.FAILED)
        response = self.client.get(reverse("platformadmin:backup_list"))
        eligible_url = reverse(
            "platformadmin:backup_preflight",
            args=[self.business_a.public_id, eligible.public_id],
        )
        failed_url = reverse(
            "platformadmin:backup_preflight",
            args=[self.business_a.public_id, failed.public_id],
        )
        self.assertContains(response, eligible_url)
        self.assertNotContains(response, failed_url)

    def test_restore_preflight_get_has_no_execution_side_effect(self):
        backup = self._eligible_backup()
        with patch.object(platform_services, "platform_run_restore_preflight") as run:
            response = self.client.get(
                reverse(
                    "platformadmin:backup_preflight",
                    args=[self.business_a.public_id, backup.public_id],
                )
            )
        self.assertEqual(response.status_code, 200)
        run.assert_not_called()
        self.assertFalse(RestoreOperation.objects.exists())

    def test_preflight_post_causes_no_tenant_data_mutation(self):
        backup = self._eligible_backup()
        product_name = self.product_a.name
        outcome = PlatformPreflightOutcome(
            restore_public_id=str(uuid.uuid4()),
            ready=False,
            compatibility="Not verified",
            component_count=0,
            record_count=0,
            media_count=0,
            messages=("No mutation.",),
        )
        with patch.object(
            platform_services, "platform_run_restore_preflight", return_value=outcome
        ):
            self.client.post(
                reverse(
                    "platformadmin:backup_preflight",
                    args=[self.business_a.public_id, backup.public_id],
                ),
                {"reason": "Validate recovery point"},
            )
        self.product_a.refresh_from_db()
        self.assertEqual(self.product_a.name, product_name)

    def test_ready_preflight_shows_final_confirmation(self):
        backup = self._eligible_backup()
        self._install_preflight(backup, ready=True)
        response = self.client.get(
            reverse(
                "platformadmin:backup_restore",
                args=[self.business_a.public_id, backup.public_id],
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Final restore confirmation")
        self.assertContains(response, "Ready")

    def test_not_ready_preflight_disables_restore(self):
        backup = self._eligible_backup()
        self._install_preflight(backup, ready=False)
        response = self.client.get(
            reverse(
                "platformadmin:backup_restore",
                args=[self.business_a.public_id, backup.public_id],
            )
        )
        self.assertContains(response, "disabled")
        self.assertContains(response, "Not ready")

    def test_wrong_tenant_preflight_is_rejected(self):
        backup = self._eligible_backup()
        response = self.client.post(
            reverse(
                "platformadmin:backup_preflight",
                args=[self.business_b.public_id, backup.public_id],
            ),
            {"reason": "Wrong tenant"},
        )
        self.assertEqual(response.status_code, 404)

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=False)
    def test_restore_mutation_is_blocked_when_setting_false(self):
        backup = self._eligible_backup()
        self._install_preflight(backup, ready=True)
        response = self.client.post(
            reverse(
                "platformadmin:backup_restore",
                args=[self.business_a.public_id, backup.public_id],
            ),
            {"acknowledge_replacement": "on", "confirmation": "RESTORE"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertContains(
            response, "Restore execution is not yet enabled.", status_code=503
        )

    def test_restore_is_never_executed_inline(self):
        backup = self._eligible_backup()
        self._install_preflight(backup, ready=True)
        with patch("apps.backups.engine.restore_mutation.execute_restore") as execute:
            self.client.post(
                reverse(
                    "platformadmin:backup_restore",
                    args=[self.business_a.public_id, backup.public_id],
                ),
                {"acknowledge_replacement": "on", "confirmation": "RESTORE"},
            )
        execute.assert_not_called()

    def test_support_session_owner_page_has_no_platform_controls(self):
        session = self.client.session
        session[SESSION_KEY] = {
            "admin_id": self.platform_admin.pk,
            "owner_id": self.owner_a.pk,
            "business_id": self.business_a.pk,
            "business_name": self.business_a.name,
            "reason": "Phase 3D support check",
            "started": timezone.now().isoformat(),
        }
        session["active_business_id"] = self.business_a.pk
        session.save()
        response = self.client.get(reverse("backups:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Backup &amp; Restore Control Center")
        self.assertNotContains(response, "Queue manual backup")

    def test_platform_page_remains_accessible_during_support_session(self):
        session = self.client.session
        session[SESSION_KEY] = {
            "admin_id": self.platform_admin.pk,
            "owner_id": self.owner_a.pk,
            "business_id": self.business_a.pk,
            "business_name": self.business_a.name,
            "reason": "Phase 3D support check",
            "started": timezone.now().isoformat(),
        }
        session.save()
        response = self.client.get(reverse("platformadmin:backup_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["platform_actor"], self.platform_admin)

    def test_activity_filtering_works(self):
        backup = self._backup()
        match = BackupActivity.objects.create(
            business=self.business_a,
            backup=backup,
            event_type="retention.warning",
            severity=ActivitySeverity.WARNING,
            sanitized_message="Safe retention warning.",
        )
        BackupActivity.objects.create(
            business=self.business_b,
            backup=self._backup(business=self.business_b),
            event_type="backup.success",
        )
        response = self.client.get(
            reverse("platformadmin:backup_activity"),
            {"business": "Alpha", "event": "retention", "severity": "WARNING"},
        )
        self.assertEqual(list(response.context["page_obj"].object_list), [match])

    def test_sensitive_paths_keys_and_key_material_are_not_rendered(self):
        backup = self._eligible_backup(
            storage_backend_identifier="provider-secret",
            opaque_object_key="path/object-key-secret",
            encryption_key_identifier="kek-secret",
            encrypted_data_key_envelope="dek-secret",
        )
        response = self.client.get(
            reverse("platformadmin:backup_detail", args=[backup.public_id])
        )
        for secret in (
            "provider-secret",
            "path/object-key-secret",
            "kek-secret",
            "dek-secret",
        ):
            self.assertNotContains(response, secret)

    def test_no_destructive_or_artifact_actions(self):
        self._eligible_backup()
        response = self.client.get(reverse("platformadmin:backup_list"))
        self.assertNotContains(response, ">Delete<")
        self.assertNotContains(response, ">Download<")

    def test_mobile_render_has_responsive_table_contract(self):
        self._eligible_backup()
        response = self.client.get(
            reverse("platformadmin:backup_list"),
            HTTP_USER_AGENT="Mozilla/5.0 (iPhone; Mobile)",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "platform-backup-table")
        self.assertContains(response, 'data-label="Business"')

    def test_owner_ui_regression_remains_tenant_scoped(self):
        own = self._eligible_backup()
        self._eligible_backup(business=self.business_b)
        self.client.force_login(self.owner_a)
        response = self.client.get(reverse("backups:history"))
        self.assertIn(own, response.context["page_obj"].object_list)
        self.assertEqual(len(response.context["page_obj"].object_list), 1)

    @override_settings(BACKUP_RESTORE_MUTATION_ENABLED=False)
    def test_phase3b_mutation_guard_remains_disabled(self):
        capability = get_engine_capability()
        self.assertFalse(capability.restore_mutation_setting_enabled)
        self.assertFalse(platform_services.restore_mutation_capability().enabled)


def load_tests(loader, standard_tests, pattern):
    del standard_tests, pattern
    return loader.loadTestsFromTestCase(BackupPhase3DPlatformUITests)
