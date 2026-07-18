from datetime import datetime
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.expenses.services import (
    RecurringExpenseGenerationError,
    ensure_recurring_expenses_for_month,
)
from apps.subscriptions.access import AccessAction, evaluate_public_access
from apps.tenants.models import Business


class Command(BaseCommand):
    help = "Generate applicable monthly recurring expenses without duplicates."

    def add_arguments(self, parser):
        parser.add_argument(
            "--business",
            dest="business_public_id",
            help="Generate only for the business with this public UUID.",
        )
        parser.add_argument(
            "--month",
            help="Target calendar month in YYYY-MM format (defaults to current month).",
        )

    def handle(self, *args, **options):
        target_date = self._target_date(options.get("month"))
        businesses = self._businesses(options.get("business_public_id"))
        specific_business = bool(options.get("business_public_id"))
        total_created = 0
        total_existing = 0
        skipped = 0

        for business in businesses:
            reason = self._ineligible_reason(business)
            if reason:
                if specific_business:
                    raise CommandError(reason)
                skipped += 1
                self.stdout.write(
                    self.style.WARNING(f"Skipped {business.name}: {reason}")
                )
                continue
            try:
                result = ensure_recurring_expenses_for_month(
                    business, target_date
                )
            except RecurringExpenseGenerationError as exc:
                if specific_business:
                    raise CommandError(str(exc)) from exc
                skipped += 1
                self.stdout.write(
                    self.style.WARNING(f"Skipped {business.name}: {exc}")
                )
                continue
            total_created += result.created
            total_existing += result.existing
            self.stdout.write(
                f"{business.name}: created={result.created}, "
                f"existing={result.existing}"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Recurring expenses for {target_date:%Y-%m}: "
                f"created={total_created}, existing={total_existing}, "
                f"skipped_businesses={skipped}"
            )
        )

    @staticmethod
    def _target_date(raw_month):
        if not raw_month:
            return timezone.localdate().replace(day=1)
        try:
            parsed = datetime.strptime(raw_month, "%Y-%m")
            if parsed.strftime("%Y-%m") != raw_month:
                raise ValueError
            return parsed.date()
        except (TypeError, ValueError) as exc:
            raise CommandError("--month must use YYYY-MM format.") from exc

    @staticmethod
    def _businesses(public_id):
        qs = Business.objects.select_related("subscription__plan").order_by("id")
        if not public_id:
            return qs.filter(is_active=True)
        try:
            business_id = UUID(public_id)
            return [qs.get(public_id=business_id)]
        except (TypeError, ValueError, Business.DoesNotExist) as exc:
            raise CommandError(
                "--business must be a valid business public UUID."
            ) from exc

    @staticmethod
    def _ineligible_reason(business):
        decision = evaluate_public_access(
            business, "expenses", action=AccessAction.WRITE
        )
        return "" if decision.allowed else decision.denial.message
