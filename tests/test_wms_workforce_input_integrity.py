from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models.query import QuerySet
from django.urls import reverse

from apps.wms_workforce import services
from apps.wms_workforce.models import (
    WmsEmployee,
    WmsEmployeeCategoryAssignment,
    WmsProductionCategory,
)
from tests.test_wms_phase2 import (
    WmsPhase2Base,
    make_category,
    make_employee,
)


class WmsWorkforceInputIntegrityTests(WmsPhase2Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner_a)

    def employee_data(self, code, *, location=None, full_name=None):
        return {
            "location": location or self.location_a,
            "employee_code": code,
            "full_name": full_name or f"Employee {code}",
            "mobile": "",
            "joining_date": date(2026, 2, 1),
            "compensation_type": WmsEmployee.CompensationType.FIXED_SALARY,
            "fixed_monthly_salary": Decimal("150.000"),
            "default_per_piece_rate": None,
            "notes": "",
        }

    def employee_payload(self, code="VIEW-RACE"):
        data = self.employee_data(code)
        data["location"] = data["location"].pk
        data["joining_date"] = data["joining_date"].isoformat()
        data["fixed_monthly_salary"] = "150.000"
        data["default_per_piece_rate"] = ""
        return data

    @staticmethod
    def category_data(name, code):
        return {
            "name": name,
            "code": code,
            "display_order": 0,
            "description": "",
        }

    def test_employee_database_duplicate_is_tenant_scoped_validation(self):
        make_employee(self.business_a, self.location_a, "EMP-RACE")

        with (
            mock.patch.object(
                WmsEmployee,
                "full_clean",
                autospec=True,
                return_value=None,
            ),
            self.assertRaises(ValidationError) as raised,
        ):
            services.save_employee(
                business=self.business_a,
                cleaned_data=self.employee_data("emp-race"),
                user=self.owner_a,
            )

        self.assertEqual(
            raised.exception.message_dict["employee_code"],
            ["This employee code is already in use."],
        )
        self.assertEqual(
            WmsEmployee.objects.for_business(self.business_a).filter(
                employee_code__iexact="EMP-RACE"
            ).count(),
            1,
        )
        other = services.save_employee(
            business=self.business_b,
            cleaned_data=self.employee_data(
                "emp-race",
                location=self.location_b,
                full_name="Other Tenant Employee",
            ),
            user=self.owner_b,
        )
        self.assertEqual(other.employee_code, "EMP-RACE")

    def test_employee_edit_database_collision_preserves_existing_rows(self):
        existing = make_employee(self.business_a, self.location_a, "EMP-ONE")
        edited = make_employee(self.business_a, self.location_a, "EMP-TWO")

        with (
            mock.patch.object(
                WmsEmployee,
                "full_clean",
                autospec=True,
                return_value=None,
            ),
            self.assertRaises(ValidationError) as raised,
        ):
            services.save_employee(
                business=self.business_a,
                cleaned_data=self.employee_data(
                    existing.employee_code,
                    full_name="Colliding Edit",
                ),
                instance=edited,
                user=self.owner_a,
            )

        self.assertIn("employee_code", raised.exception.message_dict)
        existing.refresh_from_db()
        edited.refresh_from_db()
        self.assertEqual(existing.employee_code, "EMP-ONE")
        self.assertEqual(edited.employee_code, "EMP-TWO")
        self.assertEqual(edited.full_name, "Employee EMP-TWO")

    def test_category_database_collisions_are_tenant_scoped_validation(self):
        make_category(self.business_a, "Stitching", "STITCH")

        with (
            mock.patch.object(
                WmsProductionCategory,
                "full_clean",
                autospec=True,
                return_value=None,
            ),
            self.assertRaises(ValidationError) as raised,
        ):
            services.save_category(
                business=self.business_a,
                cleaned_data=self.category_data("stitching", "stitch"),
                user=self.owner_a,
            )

        self.assertEqual(
            raised.exception.message_dict,
            {
                "name": ["This production category already exists."],
                "code": ["This category code is already in use."],
            },
        )
        self.assertEqual(
            WmsProductionCategory.objects.for_business(self.business_a).filter(
                name__iexact="Stitching"
            ).count(),
            1,
        )
        other = services.save_category(
            business=self.business_b,
            cleaned_data=self.category_data("stitching", "stitch"),
            user=self.owner_b,
        )
        self.assertEqual(other.name, "stitching")
        self.assertEqual(other.code, "STITCH")

    def test_category_edit_database_collision_preserves_existing_rows(self):
        existing = make_category(self.business_a, "Cutting", "CUT")
        edited = make_category(self.business_a, "Finishing", "FIN")

        with (
            mock.patch.object(
                WmsProductionCategory,
                "full_clean",
                autospec=True,
                return_value=None,
            ),
            self.assertRaises(ValidationError) as raised,
        ):
            services.save_category(
                business=self.business_a,
                cleaned_data=self.category_data("cutting", "cut"),
                instance=edited,
                user=self.owner_a,
            )

        self.assertEqual(set(raised.exception.message_dict), {"name", "code"})
        existing.refresh_from_db()
        edited.refresh_from_db()
        self.assertEqual(existing.name, "Cutting")
        self.assertEqual(edited.name, "Finishing")
        self.assertEqual(edited.code, "FIN")

    def test_assignment_absent_row_race_becomes_validation_error(self):
        employee = make_employee(self.business_a, self.location_a, "ASSIGN-RACE")
        category = make_category(self.business_a, "Race Category", "RACE")
        existing = services.save_assignment(
            business=self.business_a,
            employee=employee,
            category=category,
            user=self.owner_a,
        )

        with (
            mock.patch.object(
                QuerySet,
                "first",
                autospec=True,
                return_value=None,
            ),
            mock.patch.object(
                WmsEmployeeCategoryAssignment,
                "full_clean",
                autospec=True,
                return_value=None,
            ),
            self.assertRaises(ValidationError) as raised,
        ):
            services.save_assignment(
                business=self.business_a,
                employee=employee,
                category=category,
                user=self.owner_a,
            )

        self.assertEqual(
            raised.exception.message_dict["category"],
            [
                "This production category is already assigned to this "
                "employee."
            ],
        )
        self.assertEqual(
            WmsEmployeeCategoryAssignment.objects.for_business(
                self.business_a
            ).filter(employee=employee, category=category).count(),
            1,
        )
        self.assertTrue(
            WmsEmployeeCategoryAssignment.objects.filter(pk=existing.pk).exists()
        )

    def test_unrelated_integrity_errors_are_reraised(self):
        with (
            mock.patch.object(
                WmsEmployee,
                "save",
                autospec=True,
                side_effect=IntegrityError("unrelated employee failure"),
            ),
            self.assertRaisesMessage(
                IntegrityError,
                "unrelated employee failure",
            ),
        ):
            services.save_employee(
                business=self.business_a,
                cleaned_data=self.employee_data("UNIQUE-EMPLOYEE"),
                user=self.owner_a,
            )

        with (
            mock.patch.object(
                WmsProductionCategory,
                "save",
                autospec=True,
                side_effect=IntegrityError("unrelated category failure"),
            ),
            self.assertRaisesMessage(
                IntegrityError,
                "unrelated category failure",
            ),
        ):
            services.save_category(
                business=self.business_a,
                cleaned_data=self.category_data("Unique Category", "UNIQUE"),
                user=self.owner_a,
            )

        employee = make_employee(
            self.business_a,
            self.location_a,
            "UNIQUE-ASSIGNMENT",
        )
        category = make_category(
            self.business_a,
            "Unique Assignment Category",
            "UNIQUE-ASG",
        )
        with (
            mock.patch.object(
                WmsEmployeeCategoryAssignment,
                "save",
                autospec=True,
                side_effect=IntegrityError("unrelated assignment failure"),
            ),
            self.assertRaisesMessage(
                IntegrityError,
                "unrelated assignment failure",
            ),
        ):
            services.save_assignment(
                business=self.business_a,
                employee=employee,
                category=category,
                user=self.owner_a,
            )

    def test_assignment_instance_refetch_uses_tenant_scoped_manager(self):
        employee_a = make_employee(self.business_a, self.location_a, "SCOPE-A")
        category_a = make_category(self.business_a, "Scope A", "SCOPE-A")
        employee_b = make_employee(self.business_b, self.location_b, "SCOPE-B")
        category_b = make_category(self.business_b, "Scope B", "SCOPE-B")
        assignment_b = services.save_assignment(
            business=self.business_b,
            employee=employee_b,
            category=category_b,
            user=self.owner_b,
        )
        assignment_b.business_id = self.business_a.pk
        manager = WmsEmployeeCategoryAssignment.objects

        with (
            mock.patch.object(
                manager,
                "for_business",
                wraps=manager.for_business,
            ) as scoped,
            self.assertRaisesMessage(
                ValidationError,
                "The WMS assignment belongs to another business.",
            ),
        ):
            services.save_assignment(
                business=self.business_a,
                employee=employee_a,
                category=category_a,
                instance=assignment_b,
                user=self.owner_a,
            )

        scoped.assert_called_with(self.business_a)

    def test_workforce_views_render_service_validation_errors(self):
        with mock.patch(
            "apps.wms_workforce.views.services.save_employee",
            side_effect=ValidationError(
                {"employee_code": "This employee code is already in use."}
            ),
        ):
            employee_response = self.client.post(
                reverse("wms:employee_create"),
                self.employee_payload(),
            )
        self.assertEqual(employee_response.status_code, 200)
        self.assertIn("employee_code", employee_response.context["form"].errors)

        with mock.patch(
            "apps.wms_workforce.views.services.save_category",
            side_effect=ValidationError(
                {"code": "This category code is already in use."}
            ),
        ):
            category_response = self.client.post(
                reverse("wms:category_create"),
                {
                    "name": "View Race Category",
                    "code": "VIEW-RACE",
                    "display_order": "0",
                    "description": "",
                },
            )
        self.assertEqual(category_response.status_code, 200)
        self.assertIn("code", category_response.context["form"].errors)

        employee = make_employee(self.business_a, self.location_a, "VIEW-ASG")
        category = make_category(
            self.business_a,
            "View Assignment Category",
            "VIEW-ASG",
        )
        with mock.patch(
            "apps.wms_workforce.views.services.save_assignment",
            side_effect=ValidationError(
                {
                    "category": (
                        "This production category is already assigned to "
                        "this employee."
                    )
                }
            ),
        ):
            assignment_response = self.client.post(
                reverse("wms:assignment_add", args=[employee.public_id]),
                {"category": category.pk, "per_piece_rate": ""},
            )
        self.assertEqual(assignment_response.status_code, 400)
        self.assertIn(
            "category",
            assignment_response.context["assignment_form"].errors,
        )

    def test_assignment_rate_view_messages_service_validation_error(self):
        employee = make_employee(
            self.business_a,
            self.location_a,
            "RATE-VIEW",
            compensation_type=WmsEmployee.CompensationType.PER_PIECE,
            fixed_salary=None,
            piece_rate=Decimal("0.500"),
        )
        category = make_category(self.business_a, "Rate View", "RATE-VIEW")
        assignment = services.save_assignment(
            business=self.business_a,
            employee=employee,
            category=category,
            user=self.owner_a,
        )

        with mock.patch(
            "apps.wms_workforce.views.services.save_assignment",
            side_effect=ValidationError("Assignment write conflict."),
        ):
            response = self.client.post(
                reverse(
                    "wms:assignment_rate",
                    args=[employee.public_id, assignment.public_id],
                ),
                {"per_piece_rate": "0.750"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            "Assignment write conflict.",
            [str(message) for message in get_messages(response.wsgi_request)],
        )
