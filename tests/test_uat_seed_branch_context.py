"""Focused UAT seed invariants for multi-branch customer ownership."""

from io import StringIO

from django.core.management import call_command
from django.db.models import F
from django.test import TestCase, override_settings

from apps.branches.models import Branch
from apps.core.management.commands.seed_uat import BUSINESS_NAME
from apps.customers.models import Customer
from apps.sales.models import Sale
from apps.tenants.models import Business


class UATSeedBranchContextTests(TestCase):
    @override_settings(DEBUG=True)
    def test_seed_is_branch_consistent_and_idempotent(self):
        output = StringIO()
        call_command("seed_uat", stdout=output)
        business = Business.objects.get(name=BUSINESS_NAME)
        branches = {
            branch.code: branch
            for branch in Branch.objects.for_business(business)
        }

        self.assertEqual(
            Customer.objects.for_business(business).filter(
                home_branch=branches["AH"],
                is_walk_in=False,
            ).count(),
            75,
        )
        self.assertEqual(
            Customer.objects.for_business(business).filter(
                home_branch=branches["MB"],
                is_walk_in=False,
            ).count(),
            75,
        )
        for branch in branches.values():
            self.assertEqual(
                Customer.objects.for_business(business).filter(
                    home_branch=branch,
                    is_walk_in=True,
                ).count(),
                1,
            )
        self.assertFalse(
            Sale.objects.for_business(business).exclude(
                customer__home_branch_id=F("branch_id")
            ).exists()
        )

        before = {
            "customers": Customer.objects.for_business(business).count(),
            "sales": Sale.objects.for_business(business).count(),
        }
        call_command("seed_uat", stdout=StringIO())
        self.assertEqual(
            Customer.objects.for_business(business).count(), before["customers"]
        )
        self.assertEqual(Sale.objects.for_business(business).count(), before["sales"])
