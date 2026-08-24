"""Focused regressions for WMS database uniqueness race fallbacks."""

from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.urls import reverse

from apps.accounts.models import Membership
from apps.branches.models import Branch
from apps.wms_attendance.models import WmsAttendance
from apps.wms_core import services as core_services
from apps.wms_core.models import WmsLocation, WmsRole, WmsUserAccess
from tests.test_wms_phase1 import make_owner
from tests.test_wms_phase3 import WmsPhase3Base


class WmsIntegrityFallbackTests(WmsPhase3Base):
    password = "StrongPass123!"

    def _new_branch(self, code):
        return Branch.objects.create(
            business=self.business_a,
            name=f"Integrity Branch {code}",
            code=code,
            usage_type=Branch.UsageType.WORKSHOP_STOCK,
        )

    def _new_membership(self, email_prefix):
        account = make_owner(f"{email_prefix}@example.com")
        return Membership.objects.create(
            business=self.business_a,
            user=account,
            role=self.membership_a.role,
            is_active=True,
        )

    def _wms_user_payload(self, email, **overrides):
        payload = {
            "full_name": "Integrity WMS User",
            "email": email,
            "password": self.password,
            "password_confirm": self.password,
            "role": self.access_a.role.pk,
            "allowed_locations": [],
            "is_active": "on",
        }
        payload.update(overrides)
        return payload

    def test_attendance_database_duplicate_becomes_validation_error(self):
        self.create_attendance()

        with (
            mock.patch.object(
                WmsAttendance,
                "full_clean",
                autospec=True,
                return_value=None,
            ),
            self.assertRaisesMessage(
                ValidationError,
                "Attendance already exists for this employee on this date.",
            ),
        ):
            self.create_attendance()

        self.assertEqual(
            WmsAttendance.objects.for_business(self.business_a).filter(
                employee=self.employee_a,
                attendance_date=self.attendance_date,
            ).count(),
            1,
        )

    def test_location_database_duplicate_becomes_field_validation_error(self):
        with (
            mock.patch.object(
                WmsLocation,
                "full_clean",
                autospec=True,
                return_value=None,
            ),
            self.assertRaises(ValidationError) as raised,
        ):
            core_services.save_location(
                business=self.business_a,
                branch=self.location_a.branch,
                location_type=WmsLocation.LocationType.WORKSHOP,
                user=self.owner_a,
            )

        self.assertEqual(
            raised.exception.message_dict["branch"],
            ["A WMS location already exists for this branch."],
        )
        self.assertEqual(
            WmsLocation.objects.for_business(self.business_a).filter(
                branch=self.location_a.branch
            ).count(),
            1,
        )

    def test_role_database_duplicate_becomes_field_validation_errors(self):
        existing = self.access_a.role

        with (
            mock.patch.object(
                WmsRole,
                "full_clean",
                autospec=True,
                return_value=None,
            ),
            self.assertRaises(ValidationError) as raised,
        ):
            core_services.save_role(
                business=self.business_a,
                name=existing.name,
                code=existing.code,
                permissions=existing.permissions,
                user=self.owner_a,
            )

        self.assertEqual(
            raised.exception.message_dict,
            {
                "name": ["A WMS role with this name already exists."],
                "code": ["A WMS role with this code already exists."],
            },
        )

    def test_user_access_database_duplicate_becomes_field_validation_error(self):
        with (
            mock.patch.object(
                WmsUserAccess,
                "full_clean",
                autospec=True,
                return_value=None,
            ),
            self.assertRaises(ValidationError) as raised,
        ):
            core_services.save_user_access(
                business=self.business_a,
                membership=self.membership_a,
                role=self.access_a.role,
                user=self.owner_a,
            )

        self.assertEqual(
            raised.exception.message_dict["membership"],
            ["WMS access already exists for this member."],
        )
        self.assertEqual(
            WmsUserAccess.objects.for_business(self.business_a).filter(
                membership=self.membership_a
            ).count(),
            1,
        )

    def test_wms_user_create_rejects_an_existing_global_account(self):
        original_password = self.owner_b.password

        response = self.client.post(
            reverse("wms:user_create"),
            self._wms_user_payload(f"  {self.owner_b.email.upper()}  "),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("email", response.context["form"].errors)
        self.assertFalse(
            Membership.objects.filter(
                business=self.business_a,
                user=self.owner_b,
            ).exists()
        )
        self.owner_b.refresh_from_db()
        self.assertEqual(self.owner_b.password, original_password)

    def test_wms_user_email_race_renders_a_controlled_form_error(self):
        conflicting_user = make_owner("wms-integrity-race@example.com")

        with (
            mock.patch(
                "apps.wms_core.forms.WmsUserForm.clean_email",
                return_value=conflicting_user.email,
            ),
            mock.patch(
                "apps.wms_core.services._user_email_exists",
                side_effect=(False, True),
            ),
        ):
            response = self.client.post(
                reverse("wms:user_create"),
                self._wms_user_payload(conflicting_user.email),
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("email", response.context["form"].errors)
        self.assertFalse(
            Membership.objects.filter(
                business=self.business_a,
                user=conflicting_user,
            ).exists()
        )

    def test_wms_user_create_reraises_unrelated_integrity_error(self):
        with (
            mock.patch(
                "apps.wms_core.services._user_email_exists",
                return_value=False,
            ),
            mock.patch(
                "apps.wms_core.services.User.objects.create_user",
                side_effect=IntegrityError("unrelated WMS user failure"),
            ),
            self.assertRaisesMessage(
                IntegrityError,
                "unrelated WMS user failure",
            ),
        ):
            core_services.create_wms_user(
                business=self.business_a,
                full_name="Unrelated WMS Failure",
                email="unrelated-wms-user@example.com",
                password=self.password,
                role=self.access_a.role,
                user=self.owner_a,
            )

    def test_non_self_cannot_change_owner_identity_in_wms_service(self):
        original_password = self.owner_a.password
        other_actor = make_owner("wms-integrity-other-actor@example.com")

        with self.assertRaises(ValidationError) as raised:
            core_services.update_wms_user(
                business=self.business_a,
                access=self.access_a,
                full_name="Compromised WMS Owner",
                password="AttackerChosen123!",
                role=self.access_a.role,
                user=other_actor,
            )

        self.assertEqual(
            set(raised.exception.message_dict),
            {"full_name", "password"},
        )
        self.owner_a.refresh_from_db()
        self.assertNotEqual(self.owner_a.full_name, "Compromised WMS Owner")
        self.assertEqual(self.owner_a.password, original_password)

    def test_shared_identity_is_protected_but_wms_access_can_change(self):
        shared_user = make_owner("legacy-shared-wms-user@example.com")
        membership_a = Membership.objects.create(
            business=self.business_a,
            user=shared_user,
            role=self.membership_a.role,
        )
        Membership.objects.create(
            business=self.business_b,
            user=shared_user,
            role=self.business_b.memberships.get(user=self.owner_b).role,
        )
        access = core_services.save_user_access(
            business=self.business_a,
            membership=membership_a,
            role=self.access_a.role,
            user=self.owner_a,
        )
        new_role = (
            WmsRole.objects.for_business(self.business_a)
            .filter(is_active=True)
            .exclude(pk=self.access_a.role_id)
            .first()
        )
        original_password = shared_user.password
        edit_url = reverse("wms:user_edit", args=[access.public_id])

        rejected = self.client.post(
            edit_url,
            self._wms_user_payload(
                shared_user.email,
                full_name="Hijacked Shared WMS User",
                password="AttackerChosen123!",
                password_confirm="AttackerChosen123!",
                role=new_role.pk,
                allowed_locations=[self.location_a.pk],
                is_active="",
            ),
        )

        self.assertEqual(rejected.status_code, 200)
        self.assertIn("full_name", rejected.context["form"].errors)
        self.assertIn("password", rejected.context["form"].errors)
        shared_user.refresh_from_db()
        access.refresh_from_db()
        self.assertEqual(shared_user.full_name, "Legacy Shared Wms User")
        self.assertEqual(shared_user.password, original_password)
        self.assertEqual(access.role, self.access_a.role)
        self.assertTrue(access.is_active)
        self.assertFalse(access.allowed_locations.exists())

        allowed = self.client.post(
            edit_url,
            self._wms_user_payload(
                shared_user.email,
                full_name=shared_user.full_name,
                password="",
                password_confirm="",
                role=new_role.pk,
                allowed_locations=[self.location_a.pk],
                is_active="",
            ),
        )

        self.assertRedirects(allowed, reverse("wms:user_list"))
        shared_user.refresh_from_db()
        access.refresh_from_db()
        self.assertEqual(shared_user.password, original_password)
        self.assertEqual(access.role, new_role)
        self.assertFalse(access.is_active)
        self.assertEqual(
            set(access.allowed_locations.values_list("pk", flat=True)),
            {self.location_a.pk},
        )

    def test_unrelated_integrity_errors_are_reraised(self):
        next_day = self.attendance_date + timedelta(days=1)
        with (
            mock.patch.object(
                WmsAttendance,
                "save",
                autospec=True,
                side_effect=IntegrityError("unrelated attendance failure"),
            ),
            self.assertRaisesMessage(IntegrityError, "unrelated attendance failure"),
        ):
            self.create_attendance(attendance_date=next_day)

        branch = self._new_branch("INTEGRITY-LOCATION")
        with (
            mock.patch.object(
                WmsLocation,
                "save",
                autospec=True,
                side_effect=IntegrityError("unrelated location failure"),
            ),
            self.assertRaisesMessage(IntegrityError, "unrelated location failure"),
        ):
            core_services.save_location(
                business=self.business_a,
                branch=branch,
                location_type=WmsLocation.LocationType.WORKSHOP,
                user=self.owner_a,
            )

        with (
            mock.patch.object(
                WmsRole,
                "save",
                autospec=True,
                side_effect=IntegrityError("unrelated role failure"),
            ),
            self.assertRaisesMessage(IntegrityError, "unrelated role failure"),
        ):
            core_services.save_role(
                business=self.business_a,
                name="Unique Integrity Role",
                code="unique-integrity-role",
                permissions=["wms.dashboard.view"],
                user=self.owner_a,
            )

        membership = self._new_membership("integrity-unrelated-member")
        with (
            mock.patch.object(
                WmsUserAccess,
                "save",
                autospec=True,
                side_effect=IntegrityError("unrelated access failure"),
            ),
            self.assertRaisesMessage(IntegrityError, "unrelated access failure"),
        ):
            core_services.save_user_access(
                business=self.business_a,
                membership=membership,
                role=self.access_a.role,
                user=self.owner_a,
            )

    def test_core_views_render_service_validation_errors(self):
        branch = self._new_branch("VIEW-LOCATION")
        with mock.patch(
            "apps.wms_core.views.services.save_location",
            side_effect=ValidationError(
                {"branch": "A WMS location already exists for this branch."}
            ),
        ):
            response = self.client.post(
                reverse("wms:location_create"),
                {
                    "branch": branch.pk,
                    "location_type": WmsLocation.LocationType.WORKSHOP,
                    "is_active": "on",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("branch", response.context["form"].errors)

        with mock.patch(
            "apps.wms_core.views.services.save_role",
            side_effect=ValidationError(
                {"code": "A WMS role with this code already exists."}
            ),
        ):
            response = self.client.post(
                reverse("wms:role_create"),
                {
                    "name": "View Race Role",
                    "code": "view-race-role",
                    "permissions": ["wms.dashboard.view"],
                    "is_active": "on",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("code", response.context["form"].errors)

        membership = self._new_membership("integrity-view-member")
        with mock.patch(
            "apps.wms_core.views.services.save_user_access",
            side_effect=ValidationError(
                {"membership": "WMS access already exists for this member."}
            ),
        ):
            response = self.client.post(
                reverse("wms:user_access_create"),
                {
                    "membership": membership.pk,
                    "role": self.access_a.role.pk,
                    "is_active": "on",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("membership", response.context["form"].errors)

    def test_attendance_view_renders_service_validation_error(self):
        next_day = self.attendance_date + timedelta(days=1)
        with mock.patch(
            "apps.wms_attendance.views.services.create_attendance",
            side_effect=ValidationError(
                "Attendance already exists for this employee on this date."
            ),
        ):
            response = self.client.post(
                reverse("wms:attendance_create"),
                self.entry_payload(attendance_date=next_day),
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Attendance already exists for this employee on this date.",
            response.context["form"].non_field_errors(),
        )
