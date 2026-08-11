"""Report the secret-free production activation gate."""

import json

from django.core.management.base import BaseCommand

from apps.backups.activation_readiness import assess_production_activation_readiness


class Command(BaseCommand):
    help = (
        "Report backup production activation readiness without creating, uploading, "
        "deleting, decrypting, or restoring backup data."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--attest-providers",
            action="store_true",
            help="Explicitly run non-mutating KMS DescribeKey and S3 HeadBucket checks.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit the sanitized result as JSON.",
        )

    def handle(self, *args, **options):
        del args
        result = assess_production_activation_readiness(
            attest_providers=options["attest_providers"]
        )
        if options["json"]:
            self.stdout.write(json.dumps(result.as_dict(), indent=2, sort_keys=True))
            return

        self.stdout.write(f"Checked at: {result.checked_at.isoformat()}")
        self.stdout.write(
            "Markers: "
            + (", ".join(marker.value for marker in result.markers) or "NONE")
        )
        for check in result.checks:
            state = "READY" if check.ready else "NOT_READY"
            self.stdout.write(f"{check.identifier}: {state} - {check.summary}")
        self.stdout.write(
            "Backup execution enabled: "
            + ("yes" if result.backup_execution_enabled else "no")
        )
        self.stdout.write(
            "Restore mutation enabled: "
            + ("yes" if result.restore_mutation_enabled else "no")
        )
