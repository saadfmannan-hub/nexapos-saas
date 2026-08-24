"""Focused regression coverage for WMS Phase 2 workforce configuration."""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase
from django.urls import reverse

from apps.accounts.models import Membership, Role
from apps.audit.models import AuditLog
from apps.branches.models import Branch
from apps.tenants.services import provision_business
from apps.wms_core import services as core_services
from apps.wms_core.models import WmsLocation, WmsRole, WmsUserAccess
from apps.wms_core.permissions import WMS_SYSTEM_ROLE_TEMPLATES
from apps.wms_workforce import services
from apps.wms_workforce.forms import WmsEmployeeForm
from apps.wms_workforce.models import (
    WmsEmployee,
    WmsEmployeeCategoryAssignment,
    WmsProductionCategory,
)
from tests.test_wms_phase1 import make_owner, make_plan


def make_location(business, code, name):
    branch = Branch.objects.create(
        business=business,
        name=name,
        code=code,
        usage_type=Branch.UsageType.WORKSHOP_STOCK,
    )
    return core_services.save_location(
        business=business,
        branch=branch,
        location_type=WmsLocation.LocationType.WORKSHOP,
        user=business.owner,
    )


def make_employee(
    business,
    location,
    code,
    *,
    compensation_type=WmsEmployee.CompensationType.FIXED_SALARY,
    fixed_salary=Decimal("100.000"),
    piece_rate=None,
    active=True,
):
    return WmsEmployee.objects.create(
        business=business,
        location=location,
        employee_code=code,
        full_name=f"Employee {code}",
        mobile="+968 9000 0000",
        joining_date=date(2026, 1, 1),
        compensation_type=compensation_type,
        fixed_monthly_salary=fixed_salary,
        default_per_piece_rate=piece_rate,
        is_active=active,
    )


def make_category(business, name, code=""):
    return WmsProductionCategory.objects.create(
        business=business,
        name=name,
        code=code,
    )


class WmsPhase2Base(TestCase):
    def setUp(self):
        self.plan = make_plan("Phase 2 WMS", wms=True)
        self.owner_a = make_owner("phase2-owner-a@example.com")
        self.owner_b = make_owner("phase2-owner-b@example.com")
        self.business_a = provision_business(
            owner=self.owner_a,
            name="Phase 2 Business A",
            plan=self.plan,
        )
        self.business_b = provision_business(
            owner=self.owner_b,
            name="Phase 2 Business B",
            plan=self.plan,
        )
        self.location_a = make_location(
            self.business_a,
            "A-WS",
            "Workshop A",
        )
        self.location_b = make_location(
            self.business_b,
            "B-WS",
            "Workshop B",
        )
        self.membership_a = self.business_a.memberships.get(user=self.owner_a)
        self.access_a = WmsUserAccess.objects.for_business(
            self.business_a
        ).get(membership=self.membership_a)


class WmsPhase2ModelTests(WmsPhase2Base):
    def test_employee_code_is_normalized_and_unique_per_tenant_case_insensitively(self):
        employee = make_employee(
            self.business_a,
            self.location_a,
            "  emp   01  ",
        )

        self.assertEqual(employee.employee_code, "EMP 01")
        with self.assertRaises(ValidationError):
            make_employee(
                self.business_a,
                self.location_a,
                "emp 01",
            )
        other = make_employee(
            self.business_b,
            self.location_b,
            "emp 01",
        )
        self.assertEqual(other.employee_code, "EMP 01")

    def test_employee_compensation_configuration_rejects_contradictions_and_negatives(self):
        with self.assertRaises(ValidationError):
            make_employee(
                self.business_a,
                self.location_a,
                "BAD-FIXED",
                fixed_salary=Decimal("100"),
                piece_rate=Decimal("1"),
            )
        with self.assertRaises(ValidationError):
            make_employee(
                self.business_a,
                self.location_a,
                "BAD-PIECE",
                compensation_type=WmsEmployee.CompensationType.PER_PIECE,
                fixed_salary=None,
                piece_rate=None,
            )
        with self.assertRaises(ValidationError):
            make_employee(
                self.business_a,
                self.location_a,
                "NEG-FIXED",
                fixed_salary=Decimal("-1"),
            )
        employee = make_employee(
            self.business_a,
            self.location_a,
            "PIECE-OK",
            compensation_type=WmsEmployee.CompensationType.PER_PIECE,
            fixed_salary=None,
            piece_rate=Decimal("0.500"),
        )
        self.assertEqual(employee.default_per_piece_rate, Decimal("0.500"))

    def test_employee_form_enforces_all_compensation_value_matrices(self):
        def form_data(compensation_type, fixed_salary, piece_rate):
            return {
                "location": self.location_a.pk,
                "employee_code": "FORM-MATRIX",
                "full_name": "Form Matrix Employee",
                "mobile": "",
                "joining_date": "2026-02-01",
                "compensation_type": compensation_type,
                "fixed_monthly_salary": fixed_salary,
                "default_per_piece_rate": piece_rate,
                "notes": "",
            }

        valid_cases = (
            (
                "fixed salary",
                WmsEmployee.CompensationType.FIXED_SALARY,
                "150.000",
                "",
                Decimal("150.000"),
                None,
            ),
            (
                "per piece",
                WmsEmployee.CompensationType.PER_PIECE,
                "",
                "0.500",
                None,
                Decimal("0.500"),
            ),
            (
                "hybrid",
                WmsEmployee.CompensationType.HYBRID,
                "200.000",
                "0.500",
                Decimal("200.000"),
                Decimal("0.500"),
            ),
        )
        for (
            label,
            compensation_type,
            fixed_salary,
            piece_rate,
            expected_fixed,
            expected_piece,
        ) in valid_cases:
            with self.subTest(label=label, validity="valid"):
                form = WmsEmployeeForm(
                    self.business_a,
                    self.access_a,
                    form_data(compensation_type, fixed_salary, piece_rate),
                )
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(
                    form.cleaned_data["fixed_monthly_salary"],
                    expected_fixed,
                )
                self.assertEqual(
                    form.cleaned_data["default_per_piece_rate"],
                    expected_piece,
                )

        invalid_cases = (
            (
                "fixed salary requires fixed value",
                WmsEmployee.CompensationType.FIXED_SALARY,
                "",
                "",
                "fixed_monthly_salary",
            ),
            (
                "fixed salary rejects piece value",
                WmsEmployee.CompensationType.FIXED_SALARY,
                "150.000",
                "0.500",
                "default_per_piece_rate",
            ),
            (
                "per piece requires default rate",
                WmsEmployee.CompensationType.PER_PIECE,
                "",
                "",
                "default_per_piece_rate",
            ),
            (
                "per piece rejects fixed value",
                WmsEmployee.CompensationType.PER_PIECE,
                "150.000",
                "0.500",
                "fixed_monthly_salary",
            ),
            (
                "hybrid requires fixed value",
                WmsEmployee.CompensationType.HYBRID,
                "",
                "0.500",
                "fixed_monthly_salary",
            ),
            (
                "hybrid requires default rate",
                WmsEmployee.CompensationType.HYBRID,
                "200.000",
                "",
                "default_per_piece_rate",
            ),
        )
        for (
            label,
            compensation_type,
            fixed_salary,
            piece_rate,
            error_field,
        ) in invalid_cases:
            with self.subTest(label=label, validity="invalid"):
                form = WmsEmployeeForm(
                    self.business_a,
                    self.access_a,
                    form_data(compensation_type, fixed_salary, piece_rate),
                )
                self.assertFalse(form.is_valid())
                self.assertIn(error_field, form.errors)

    def test_employee_form_mode_transitions_clear_obsolete_values(self):
        cases = (
            (
                "fixed-to-hybrid",
                WmsEmployee.CompensationType.FIXED_SALARY,
                Decimal("150.000"),
                None,
                WmsEmployee.CompensationType.HYBRID,
                "200.000",
                "0.500",
                Decimal("200.000"),
                Decimal("0.500"),
            ),
            (
                "per-piece-to-hybrid",
                WmsEmployee.CompensationType.PER_PIECE,
                None,
                Decimal("0.400"),
                WmsEmployee.CompensationType.HYBRID,
                "220.000",
                "0.600",
                Decimal("220.000"),
                Decimal("0.600"),
            ),
            (
                "hybrid-to-fixed",
                WmsEmployee.CompensationType.HYBRID,
                Decimal("200.000"),
                Decimal("0.500"),
                WmsEmployee.CompensationType.FIXED_SALARY,
                "250.000",
                "",
                Decimal("250.000"),
                None,
            ),
            (
                "hybrid-to-per-piece",
                WmsEmployee.CompensationType.HYBRID,
                Decimal("200.000"),
                Decimal("0.500"),
                WmsEmployee.CompensationType.PER_PIECE,
                "",
                "0.800",
                None,
                Decimal("0.800"),
            ),
        )
        for (
            code,
            initial_type,
            initial_fixed,
            initial_piece,
            target_type,
            target_fixed,
            target_piece,
            expected_fixed,
            expected_piece,
        ) in cases:
            with self.subTest(transition=code):
                employee = make_employee(
                    self.business_a,
                    self.location_a,
                    f"MODE-{code.upper()}",
                    compensation_type=initial_type,
                    fixed_salary=initial_fixed,
                    piece_rate=initial_piece,
                )
                form = WmsEmployeeForm(
                    self.business_a,
                    self.access_a,
                    {
                        "location": self.location_a.pk,
                        "employee_code": employee.employee_code,
                        "full_name": employee.full_name,
                        "mobile": employee.mobile,
                        "joining_date": employee.joining_date.isoformat(),
                        "compensation_type": target_type,
                        "fixed_monthly_salary": target_fixed,
                        "default_per_piece_rate": target_piece,
                        "notes": employee.notes,
                    },
                    instance=employee,
                )
                self.assertTrue(form.is_valid(), form.errors)

                saved = services.save_employee(
                    business=self.business_a,
                    cleaned_data=form.cleaned_data,
                    instance=employee,
                    user=self.owner_a,
                )

                self.assertEqual(saved.compensation_type, target_type)
                self.assertEqual(saved.fixed_monthly_salary, expected_fixed)
                self.assertEqual(saved.default_per_piece_rate, expected_piece)

    def test_employee_location_must_be_tenant_valid_and_active_for_new_records(self):
        with self.assertRaises(ValidationError):
            make_employee(
                self.business_a,
                self.location_b,
                "CROSS-LOCATION",
            )

        self.location_a.is_active = False
        self.location_a.save()
        with self.assertRaises(ValidationError):
            make_employee(
                self.business_a,
                self.location_a,
                "INACTIVE-LOCATION",
            )

    def test_inactive_location_does_not_hide_or_block_unrelated_historical_edit(self):
        employee = make_employee(
            self.business_a,
            self.location_a,
            "HISTORY-1",
        )
        self.location_a.is_active = False
        self.location_a.save()

        employee.full_name = "Historical Employee Updated"
        employee.save()

        self.assertEqual(
            WmsEmployee.objects.get(pk=employee.pk).full_name,
            "Historical Employee Updated",
        )
        self.client.force_login(self.owner_a)
        response = self.client.get(
            reverse("wms:employee_detail", args=[employee.public_id])
        )
        self.assertEqual(response.status_code, 200)

    def test_category_name_and_code_are_tenant_unique_and_normalized(self):
        category = make_category(
            self.business_a,
            "  Computer   Design ",
            " cd ",
        )
        self.assertEqual(category.name, "Computer Design")
        self.assertEqual(category.code, "CD")
        with self.assertRaises(ValidationError):
            make_category(self.business_a, "computer design", "OTHER")
        with self.assertRaises(ValidationError):
            make_category(self.business_a, "Other", "cd")
        other = make_category(self.business_b, "computer design", "cd")
        self.assertEqual(other.code, "CD")

    def test_assignment_validation_uniqueness_and_effective_rate(self):
        employee = make_employee(
            self.business_a,
            self.location_a,
            "PIECE-1",
            compensation_type=WmsEmployee.CompensationType.PER_PIECE,
            fixed_salary=None,
            piece_rate=Decimal("0.400"),
        )
        category = make_category(self.business_a, "Daraz", "DRZ")
        assignment = WmsEmployeeCategoryAssignment.objects.create(
            business=self.business_a,
            employee=employee,
            category=category,
        )
        self.assertEqual(
            assignment.effective_per_piece_rate,
            Decimal("0.400"),
        )
        assignment.per_piece_rate = Decimal("0.650")
        assignment.save()
        self.assertEqual(
            assignment.effective_per_piece_rate,
            Decimal("0.650"),
        )
        with self.assertRaises(ValidationError):
            WmsEmployeeCategoryAssignment.objects.create(
                business=self.business_a,
                employee=employee,
                category=category,
            )

    def test_historical_hybrid_override_survives_fixed_assignment_lifecycle(self):
        employee = make_employee(
            self.business_a,
            self.location_a,
            "HYBRID-HISTORY",
            compensation_type=WmsEmployee.CompensationType.HYBRID,
            fixed_salary=Decimal("200.000"),
            piece_rate=Decimal("0.500"),
        )
        category = make_category(self.business_a, "Historical Hybrid", "HH")
        assignment = services.save_assignment(
            business=self.business_a,
            employee=employee,
            category=category,
            per_piece_rate=Decimal("0.700"),
            user=self.owner_a,
        )

        employee.compensation_type = WmsEmployee.CompensationType.FIXED_SALARY
        employee.default_per_piece_rate = None
        employee.save()
        assignment.refresh_from_db()
        self.assertEqual(assignment.per_piece_rate, Decimal("0.700"))

        services.set_assignment_active(
            business=self.business_a,
            assignment=assignment,
            is_active=False,
            user=self.owner_a,
        )
        assignment = services.save_assignment(
            business=self.business_a,
            employee=employee,
            category=category,
            per_piece_rate=None,
            user=self.owner_a,
        )
        assignment.refresh_from_db()
        self.assertTrue(assignment.is_active)
        self.assertEqual(assignment.per_piece_rate, Decimal("0.700"))

        other_employee = make_employee(
            self.business_a,
            self.location_a,
            "FIXED-HISTORY-TARGET",
            fixed_salary=Decimal("250.000"),
        )
        assignment.employee = other_employee
        with self.assertRaises(ValidationError):
            assignment.save()
        assignment.refresh_from_db()
        self.assertEqual(assignment.employee, employee)

        other_category = make_category(
            self.business_a,
            "Historical Hybrid Target",
            "HHT",
        )
        assignment.category = other_category
        with self.assertRaises(ValidationError):
            assignment.save()
        assignment.refresh_from_db()
        self.assertEqual(assignment.category, category)

    def test_assignment_rejects_cross_tenant_and_inactive_records(self):
        employee = make_employee(
            self.business_a,
            self.location_a,
            "ASSIGN-1",
        )
        category_a = make_category(self.business_a, "Iron")
        category_b = make_category(self.business_b, "Iron")

        with self.assertRaises(ValidationError):
            WmsEmployeeCategoryAssignment.objects.create(
                business=self.business_a,
                employee=employee,
                category=category_b,
            )
        employee.is_active = False
        employee.save()
        with self.assertRaises(ValidationError):
            WmsEmployeeCategoryAssignment.objects.create(
                business=self.business_a,
                employee=employee,
                category=category_a,
            )

        employee.is_active = True
        employee.save()
        category_a.is_active = False
        category_a.save()
        with self.assertRaises(ValidationError):
            WmsEmployeeCategoryAssignment.objects.create(
                business=self.business_a,
                employee=employee,
                category=category_a,
            )

    def test_assignment_deactivation_preserves_record_and_reactivation_revalidates(self):
        employee = make_employee(
            self.business_a,
            self.location_a,
            "ASSIGN-2",
        )
        category = make_category(self.business_a, "Side")
        assignment = services.save_assignment(
            business=self.business_a,
            employee=employee,
            category=category,
            user=self.owner_a,
        )

        services.set_assignment_active(
            business=self.business_a,
            assignment=assignment,
            is_active=False,
            user=self.owner_a,
        )
        assignment.refresh_from_db()
        self.assertFalse(assignment.is_active)
        category.is_active = False
        category.save()
        with self.assertRaises(ValidationError):
            services.set_assignment_active(
                business=self.business_a,
                assignment=assignment,
                is_active=True,
                user=self.owner_a,
            )
        self.assertTrue(
            WmsEmployeeCategoryAssignment.objects.filter(pk=assignment.pk).exists()
        )

    def test_employee_and_category_deletion_are_protected_by_assignments(self):
        employee = make_employee(
            self.business_a,
            self.location_a,
            "PROTECT-1",
        )
        category = make_category(self.business_a, "Button")
        assignment = services.save_assignment(
            business=self.business_a,
            employee=employee,
            category=category,
            user=self.owner_a,
        )
        services.set_assignment_active(
            business=self.business_a,
            assignment=assignment,
            is_active=False,
            user=self.owner_a,
        )

        with self.assertRaises(ProtectedError):
            employee.delete()
        with self.assertRaises(ProtectedError):
            category.delete()

    def test_employee_form_excludes_cross_tenant_and_inactive_locations(self):
        self.location_a.is_active = False
        self.location_a.save()
        form = WmsEmployeeForm(
            self.business_a,
            self.access_a,
        )

        self.assertNotIn(
            self.location_a,
            form.fields["location"].queryset,
        )
        self.assertNotIn(
            self.location_b,
            form.fields["location"].queryset,
        )

    def test_default_role_permission_matrix_includes_phase2_categories(self):
        roles = {
            role.code: set(role.permissions)
            for role in WmsRole.objects.for_business(self.business_a)
        }
        self.assertIn("wms.categories.manage", roles["owner_admin"])
        self.assertIn("wms.categories.manage", roles["workshop_manager"])
        self.assertIn("wms.categories.view", roles["production_entry"])
        self.assertIn("wms.categories.view", roles["report_viewer"])
        self.assertNotIn("wms.categories.view", roles["attendance_manager"])
        self.assertEqual(
            set(WMS_SYSTEM_ROLE_TEMPLATES["owner_admin"]["permissions"]),
            roles["owner_admin"],
        )


class WmsPhase2ViewTests(WmsPhase2Base):
    def setUp(self):
        super().setUp()
        self.employee_a = make_employee(
            self.business_a,
            self.location_a,
            "VIEW-A",
        )
        self.employee_b = make_employee(
            self.business_b,
            self.location_b,
            "VIEW-B",
        )
        self.category_a = make_category(self.business_a, "Body", "BODY")
        self.category_b = make_category(self.business_b, "Body", "BODY")
        self.assignment_a = services.save_assignment(
            business=self.business_a,
            employee=self.employee_a,
            category=self.category_a,
            user=self.owner_a,
        )
        self.client.force_login(self.owner_a)

    def employee_payload(self, **overrides):
        data = {
            "location": self.location_a.pk,
            "employee_code": "NEW-01",
            "full_name": "New Workshop Employee",
            "mobile": "+968 9999 0000",
            "joining_date": "2026-02-01",
            "compensation_type": WmsEmployee.CompensationType.FIXED_SALARY,
            "fixed_monthly_salary": "150.000",
            "default_per_piece_rate": "",
            "notes": "",
        }
        data.update(overrides)
        return data

    def test_authorized_employee_list_detail_create_edit_and_sections(self):
        self.employee_a.is_active = False
        self.employee_a.save()
        response = self.client.get(reverse("wms:employee_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Active Employees")
        self.assertContains(response, "Inactive Employees")
        self.assertEqual(response.context["inactive_count"], 1)

        response = self.client.post(
            reverse("wms:employee_create"),
            self.employee_payload(),
        )
        employee = WmsEmployee.objects.for_business(self.business_a).get(
            employee_code="NEW-01"
        )
        self.assertRedirects(
            response,
            reverse("wms:employee_detail", args=[employee.public_id]),
        )
        response = self.client.post(
            reverse("wms:employee_edit", args=[employee.public_id]),
            self.employee_payload(full_name="Updated Employee"),
        )
        self.assertRedirects(
            response,
            reverse("wms:employee_detail", args=[employee.public_id]),
        )
        employee.refresh_from_db()
        self.assertEqual(employee.full_name, "Updated Employee")
        self.assertEqual(employee.created_by, self.owner_a)
        self.assertEqual(employee.updated_by, self.owner_a)

    def test_employee_form_exposes_hybrid_choice_and_ux_markers(self):
        employee = make_employee(
            self.business_a,
            self.location_a,
            "HYBRID-FORM",
            compensation_type=WmsEmployee.CompensationType.HYBRID,
            fixed_salary=Decimal("200.000"),
            piece_rate=Decimal("0.500"),
        )
        response = self.client.get(reverse("wms:employee_edit", args=[employee.public_id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["form"]["compensation_type"].value(),
            WmsEmployee.CompensationType.HYBRID,
        )
        self.assertContains(response, "Hybrid (Fixed + Production)")
        self.assertContains(response, 'data-compensation-field="fixed"')
        self.assertContains(response, 'data-compensation-field="production"')
        content = response.content.decode()
        self.assertIn(
            'type === "fixed_salary" || type === "hybrid"',
            content,
        )
        self.assertIn(
            'type === "per_piece" || type === "hybrid"',
            content,
        )
        self.assertIn("input.disabled = !enabled", content)
        self.assertIn("input.required = enabled", content)

    def test_employee_filters_search_name_code_mobile_location_and_compensation(self):
        response = self.client.get(
            reverse("wms:employee_list"),
            {
                "q": "VIEW-A",
                "location": self.location_a.public_id,
                "compensation_type": WmsEmployee.CompensationType.FIXED_SALARY,
            },
        )
        self.assertContains(response, "Employee VIEW-A")
        self.assertNotContains(response, "Employee VIEW-B")

    def test_cross_tenant_employee_category_assignment_and_status_ids_are_rejected(self):
        self.assertEqual(
            self.client.get(
                reverse(
                    "wms:employee_detail",
                    args=[self.employee_b.public_id],
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse(
                    "wms:employee_status",
                    args=[self.employee_b.public_id, "deactivate"],
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse(
                    "wms:category_edit",
                    args=[self.category_b.public_id],
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse(
                    "wms:category_status",
                    args=[self.category_b.public_id, "deactivate"],
                )
            ).status_code,
            404,
        )
        response = self.client.post(
            reverse("wms:employee_create"),
            self.employee_payload(
                location=self.location_b.pk,
                employee_code="FORGED-LOCATION",
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            WmsEmployee.objects.filter(
                business=self.business_a,
                employee_code="FORGED-LOCATION",
            ).exists()
        )
        response = self.client.post(
            reverse(
                "wms:assignment_add",
                args=[self.employee_a.public_id],
            ),
            {
                "category": self.category_b.pk,
                "per_piece_rate": "",
            },
        )
        self.assertEqual(response.status_code, 400)
        other_assignment = services.save_assignment(
            business=self.business_b,
            employee=self.employee_b,
            category=self.category_b,
            user=self.owner_b,
        )
        response = self.client.post(
            reverse(
                "wms:assignment_status",
                args=[
                    self.employee_a.public_id,
                    other_assignment.public_id,
                    "deactivate",
                ],
            )
        )
        self.assertEqual(response.status_code, 404)

    def test_explicit_location_scope_hides_other_location_employees(self):
        second_location = make_location(
            self.business_a,
            "A-WS2",
            "Workshop A Two",
        )
        other_employee = make_employee(
            self.business_a,
            second_location,
            "OTHER-LOCATION",
        )
        self.access_a.allowed_locations.set([self.location_a])

        response = self.client.get(reverse("wms:employee_list"))
        self.assertNotContains(response, other_employee.full_name)
        self.assertEqual(
            self.client.get(
                reverse(
                    "wms:employee_detail",
                    args=[other_employee.public_id],
                )
            ).status_code,
            404,
        )

    def test_employee_and_category_lifecycle_are_post_only_and_preserve_assignments(self):
        employee_url = reverse(
            "wms:employee_status",
            args=[self.employee_a.public_id, "deactivate"],
        )
        category_url = reverse(
            "wms:category_status",
            args=[self.category_a.public_id, "deactivate"],
        )
        self.assertEqual(self.client.get(employee_url).status_code, 405)
        self.assertEqual(self.client.get(category_url).status_code, 405)

        self.assertEqual(self.client.post(employee_url).status_code, 302)
        self.employee_a.refresh_from_db()
        self.assignment_a.refresh_from_db()
        self.assertFalse(self.employee_a.is_active)
        self.assertFalse(self.assignment_a.is_active)
        self.assertTrue(
            WmsEmployeeCategoryAssignment.objects.filter(
                pk=self.assignment_a.pk
            ).exists()
        )
        self.location_a.is_active = False
        self.location_a.save()
        activate_url = reverse(
            "wms:employee_status",
            args=[self.employee_a.public_id, "activate"],
        )
        self.assertEqual(self.client.post(activate_url).status_code, 302)
        self.employee_a.refresh_from_db()
        self.assertFalse(self.employee_a.is_active)

    def test_category_list_create_edit_lifecycle_and_assignment_count(self):
        response = self.client.get(reverse("wms:category_list"))
        self.assertContains(response, "Active Categories")
        self.assertContains(response, "Body")
        self.assertEqual(
            list(response.context["active_page"])[0].active_assignment_count,
            1,
        )

        response = self.client.post(
            reverse("wms:category_create"),
            {
                "name": "Computer Design",
                "code": "CD",
                "display_order": "10",
                "description": "",
            },
        )
        self.assertRedirects(response, reverse("wms:category_list"))
        category = WmsProductionCategory.objects.for_business(
            self.business_a
        ).get(code="CD")
        response = self.client.post(
            reverse("wms:category_edit", args=[category.public_id]),
            {
                "name": "Computer Design Updated",
                "code": "CD",
                "display_order": "11",
                "description": "",
            },
        )
        self.assertRedirects(response, reverse("wms:category_list"))
        category.refresh_from_db()
        self.assertEqual(category.name, "Computer Design Updated")

    def test_assignment_ui_supports_category_specific_piece_rate_and_lifecycle(self):
        employee = make_employee(
            self.business_a,
            self.location_a,
            "PIECE-UI",
            compensation_type=WmsEmployee.CompensationType.PER_PIECE,
            fixed_salary=None,
            piece_rate=Decimal("0.400"),
        )
        category = make_category(self.business_a, "Computer Design", "CD")
        response = self.client.post(
            reverse("wms:assignment_add", args=[employee.public_id]),
            {
                "category": category.pk,
                "per_piece_rate": "0.650",
            },
        )
        self.assertRedirects(
            response,
            reverse("wms:employee_detail", args=[employee.public_id]),
        )
        assignment = WmsEmployeeCategoryAssignment.objects.get(
            business=self.business_a,
            employee=employee,
            category=category,
        )
        self.assertEqual(
            assignment.effective_per_piece_rate,
            Decimal("0.650"),
        )

        response = self.client.post(
            reverse(
                "wms:assignment_rate",
                args=[employee.public_id, assignment.public_id],
            ),
            {"per_piece_rate": ""},
        )
        self.assertEqual(response.status_code, 302)
        assignment.refresh_from_db()
        self.assertEqual(
            assignment.effective_per_piece_rate,
            Decimal("0.400"),
        )
        self.client.post(
            reverse(
                "wms:assignment_status",
                args=[employee.public_id, assignment.public_id, "deactivate"],
            )
        )
        assignment.refresh_from_db()
        self.assertFalse(assignment.is_active)
        self.client.post(
            reverse(
                "wms:assignment_status",
                args=[employee.public_id, assignment.public_id, "activate"],
            )
        )
        assignment.refresh_from_db()
        self.assertTrue(assignment.is_active)

    def test_hybrid_assignment_override_can_be_added_and_edited(self):
        employee = make_employee(
            self.business_a,
            self.location_a,
            "HYBRID-RATE-UI",
            compensation_type=WmsEmployee.CompensationType.HYBRID,
            fixed_salary=Decimal("200.000"),
            piece_rate=Decimal("0.500"),
        )
        category = make_category(self.business_a, "Hybrid Daraz", "HYB-DRZ")

        response = self.client.post(
            reverse("wms:assignment_add", args=[employee.public_id]),
            {
                "category": category.pk,
                "per_piece_rate": "0.700",
            },
        )
        self.assertRedirects(
            response,
            reverse("wms:employee_detail", args=[employee.public_id]),
        )
        assignment = WmsEmployeeCategoryAssignment.objects.get(
            business=self.business_a,
            employee=employee,
            category=category,
        )
        self.assertEqual(assignment.per_piece_rate, Decimal("0.700"))
        self.assertEqual(assignment.effective_per_piece_rate, Decimal("0.700"))

        response = self.client.get(reverse("wms:employee_detail", args=[employee.public_id]))
        self.assertContains(
            response,
            reverse(
                "wms:assignment_rate",
                args=[employee.public_id, assignment.public_id],
            ),
        )
        response = self.client.post(
            reverse(
                "wms:assignment_rate",
                args=[employee.public_id, assignment.public_id],
            ),
            {"per_piece_rate": "0.850"},
        )
        self.assertRedirects(
            response,
            reverse("wms:employee_detail", args=[employee.public_id]),
        )
        assignment.refresh_from_db()
        self.assertEqual(assignment.per_piece_rate, Decimal("0.850"))
        self.assertEqual(assignment.effective_per_piece_rate, Decimal("0.850"))

    def test_fixed_employee_cannot_acquire_category_rate(self):
        category = make_category(self.business_a, "Fixed Protection", "FIX-PROT")
        response = self.client.post(
            reverse("wms:assignment_add", args=[self.employee_a.public_id]),
            {
                "category": category.pk,
                "per_piece_rate": "0.700",
            },
        )
        self.assertRedirects(
            response,
            reverse("wms:employee_detail", args=[self.employee_a.public_id]),
        )
        assignment = WmsEmployeeCategoryAssignment.objects.get(
            business=self.business_a,
            employee=self.employee_a,
            category=category,
        )
        self.assertIsNone(assignment.per_piece_rate)

        response = self.client.post(
            reverse(
                "wms:assignment_rate",
                args=[self.employee_a.public_id, assignment.public_id],
            ),
            {"per_piece_rate": "0.900"},
        )
        self.assertRedirects(
            response,
            reverse("wms:employee_detail", args=[self.employee_a.public_id]),
        )
        assignment.refresh_from_db()
        self.assertIsNone(assignment.per_piece_rate)

        assignment.per_piece_rate = Decimal("0.900")
        with self.assertRaises(ValidationError):
            assignment.save()

    def test_view_only_role_sees_navigation_but_not_manage_actions(self):
        core_role = Role.objects.create(
            business=self.business_a,
            name="WMS Viewer Core",
            permissions=[],
        )
        viewer = make_owner("phase2-viewer@example.com")
        membership = Membership.objects.create(
            business=self.business_a,
            user=viewer,
            role=core_role,
        )
        wms_role = WmsRole.objects.create(
            business=self.business_a,
            name="Phase 2 Viewer",
            code="phase2_viewer",
            permissions=[
                "wms.employees.view",
                "wms.categories.view",
            ],
        )
        core_services.save_user_access(
            business=self.business_a,
            membership=membership,
            role=wms_role,
            user=self.owner_a,
        )
        self.client.force_login(viewer)

        response = self.client.get(reverse("wms:employee_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Employees")
        self.assertContains(response, "Production Categories")
        self.assertNotContains(response, "Add employee")
        self.assertEqual(
            self.client.get(reverse("wms:employee_create")).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("wms:category_create")).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse(
                    "wms:employee_status",
                    args=[self.employee_a.public_id, "deactivate"],
                )
            ).status_code,
            403,
        )

    def test_missing_access_disabled_entitlement_and_pos_only_are_denied(self):
        core_role = Role.objects.create(
            business=self.business_a,
            name="No WMS",
            permissions=[],
        )
        user = make_owner("phase2-no-access@example.com")
        Membership.objects.create(
            business=self.business_a,
            user=user,
            role=core_role,
        )
        self.client.force_login(user)
        self.assertEqual(
            self.client.get(reverse("wms:employee_list")).status_code,
            403,
        )

        self.plan.feature_wms = False
        self.plan.save(update_fields=["feature_wms"])
        self.client.force_login(self.owner_a)
        self.assertEqual(
            self.client.get(reverse("wms:category_list")).status_code,
            403,
        )

        pos_plan = make_plan("Phase 2 POS Only", pos=True)
        pos_owner = make_owner("phase2-pos@example.com")
        provision_business(
            owner=pos_owner,
            name="Phase 2 POS",
            plan=pos_plan,
        )
        self.client.force_login(pos_owner)
        self.assertEqual(
            self.client.get(reverse("wms:employee_list")).status_code,
            403,
        )

    def test_compensation_change_assignment_and_lifecycle_emit_audit_events(self):
        response = self.client.post(
            reverse(
                "wms:employee_edit",
                args=[self.employee_a.public_id],
            ),
            self.employee_payload(
                employee_code=self.employee_a.employee_code,
                full_name=self.employee_a.full_name,
                compensation_type=WmsEmployee.CompensationType.PER_PIECE,
                fixed_monthly_salary="",
                default_per_piece_rate="0.500",
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.client.post(
            reverse(
                "wms:assignment_status",
                args=[
                    self.employee_a.public_id,
                    self.assignment_a.public_id,
                    "deactivate",
                ],
            )
        )
        self.client.post(
            reverse(
                "wms:category_status",
                args=[self.category_a.public_id, "deactivate"],
            )
        )
        actions = set(
            AuditLog.objects.filter(business=self.business_a).values_list(
                "action",
                flat=True,
            )
        )
        expected_actions = {
            "wms.employee_updated",
            "wms.employee_compensation_changed",
            "wms.employee_category_unassigned",
            "wms.category_deactivated",
        }
        self.assertFalse(
            expected_actions.difference(actions),
            expected_actions.difference(actions),
        )

    def test_csrf_is_required_for_state_change(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner_a)
        response = csrf_client.post(
            reverse(
                "wms:employee_status",
                args=[self.employee_a.public_id, "deactivate"],
            )
        )
        self.assertEqual(response.status_code, 403)
