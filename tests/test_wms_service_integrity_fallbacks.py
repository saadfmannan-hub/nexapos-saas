"""Narrow database-race fallbacks for production, orders, and salary."""

from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.wms_orders.models import (
    WmsWorkshopOrder,
    WmsWorkshopOrderStatusHistory,
)
from apps.wms_production.models import WmsProductionEntry
from apps.wms_salary.models import WmsSalary
from tests.test_wms_phase4 import WmsPhase4Base
from tests.test_wms_phase5 import WmsPhase5Base
from tests.test_wms_phase7 import WmsPhase7Base


class WmsProductionIntegrityFallbackTests(WmsPhase4Base):
    def test_database_duplicate_becomes_validation_error(self):
        self.create_entry()

        with (
            mock.patch.object(
                WmsProductionEntry,
                "full_clean",
                autospec=True,
                return_value=None,
            ),
            self.assertRaisesMessage(
                ValidationError,
                "Production already exists for this employee on this date.",
            ),
        ):
            self.create_entry()

        self.assertEqual(
            WmsProductionEntry.objects.for_business(self.business_a).filter(
                employee=self.employee_a,
                production_date=self.production_date,
            ).count(),
            1,
        )

    def test_unrelated_integrity_error_is_reraised(self):
        with (
            mock.patch.object(
                WmsProductionEntry,
                "save",
                autospec=True,
                side_effect=IntegrityError("unrelated production failure"),
            ),
            self.assertRaisesMessage(
                IntegrityError,
                "unrelated production failure",
            ),
        ):
            self.create_entry()

        self.assertFalse(
            WmsProductionEntry.objects.for_business(self.business_a).exists()
        )


class WmsOrderIntegrityFallbackTests(WmsPhase5Base):
    def test_database_duplicate_becomes_validation_error(self):
        reference = "RACE-ORDER-001"
        self.create_batch((reference,))
        scoped_orders = WmsWorkshopOrder.objects.for_business(self.business_a)

        with (
            mock.patch.object(
                WmsWorkshopOrder.objects,
                "for_business",
                side_effect=(scoped_orders.none(), scoped_orders),
            ),
            mock.patch.object(
                WmsWorkshopOrder,
                "full_clean",
                autospec=True,
                return_value=None,
            ),
            self.assertRaisesMessage(
                ValidationError,
                "One or more order references already exist.",
            ),
        ):
            self.create_batch((reference.lower(),))

        self.assertEqual(
            WmsWorkshopOrder.objects.for_business(self.business_a).filter(
                order_reference=reference,
            ).count(),
            1,
        )

    def test_history_and_audit_integrity_errors_are_reraised(self):
        failures = (
            (
                "apps.wms_orders.services.WmsWorkshopOrderStatusHistory.objects.create",
                "UNRELATED-HISTORY",
                "unrelated order history failure",
            ),
            (
                "apps.wms_orders.services.audit.log",
                "UNRELATED-AUDIT",
                "unrelated order audit failure",
            ),
        )
        for patch_target, reference, message in failures:
            with (
                self.subTest(patch_target=patch_target),
                mock.patch(
                    patch_target,
                    side_effect=IntegrityError(message),
                ),
                self.assertRaisesMessage(IntegrityError, message),
            ):
                self.create_batch((reference,))

            self.assertFalse(
                WmsWorkshopOrder.objects.for_business(self.business_a).filter(
                    order_reference=reference,
                ).exists()
            )
            self.assertFalse(
                WmsWorkshopOrderStatusHistory.objects.for_business(
                    self.business_a
                ).exists()
            )


class WmsSalaryIntegrityFallbackTests(WmsPhase7Base):
    def test_database_duplicate_becomes_validation_error(self):
        self.calculate()
        scoped_salaries = WmsSalary.objects.for_business(self.business_a)

        with (
            mock.patch.object(
                WmsSalary.objects,
                "for_business",
                side_effect=(scoped_salaries.none(), scoped_salaries),
            ),
            mock.patch.object(
                WmsSalary,
                "full_clean",
                autospec=True,
                return_value=None,
            ),
            self.assertRaisesMessage(
                ValidationError,
                "Salary already exists for this employee and month.",
            ),
        ):
            self.calculate()

        self.assertEqual(
            WmsSalary.objects.for_business(self.business_a).filter(
                employee=self.fixed_employee,
                salary_year=self.salary_year,
                salary_month=self.salary_month,
            ).count(),
            1,
        )

    def test_recalculation_reraises_unrelated_error_and_restores_snapshots(self):
        salary = self.calculate()
        day_count = salary.days.count()
        location_count = salary.location_snapshots.count()

        with (
            mock.patch.object(
                WmsSalary,
                "save",
                autospec=True,
                side_effect=IntegrityError("unrelated salary failure"),
            ),
            self.assertRaisesMessage(
                IntegrityError,
                "unrelated salary failure",
            ),
        ):
            self.calculate()

        salary.refresh_from_db()
        self.assertEqual(salary.days.count(), day_count)
        self.assertEqual(salary.location_snapshots.count(), location_count)
