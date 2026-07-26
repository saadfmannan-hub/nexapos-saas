"""Central version and migration-compatibility metadata."""

import hashlib
import json
import re
from dataclasses import dataclass

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.loader import MigrationLoader

from .enums import CompatibilityStatus

BACKUP_FORMAT_VERSION = "1.0"
APPLICATION_VERSION = "1.0.0"
DEFAULT_COMPONENT_VERSION = "1.0"
MINIMUM_RESTORE_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class CompatibilityAssessment:
    status: str
    reason: str


def get_application_version():
    """Return the deploy-provided release version or the Phase 1 baseline."""

    return str(getattr(settings, "NEXA_APPLICATION_VERSION", APPLICATION_VERSION))


def get_minimum_restore_version():
    return str(
        getattr(settings, "NEXA_MINIMUM_RESTORE_VERSION", MINIMUM_RESTORE_VERSION)
    )


def schema_migration_fingerprint(using=DEFAULT_DB_ALIAS):
    """Hash the applied Django migration graph for one database alias.

    Both applied node names and their applied parent edges are included.  The
    result changes when the deployed schema graph changes and remains
    database-engine neutral.
    """

    connection = connections[using]
    loader = MigrationLoader(connection, ignore_no_migrations=True)
    applied = frozenset(loader.applied_migrations)
    graph_rows = []
    for key in sorted(applied):
        node = loader.graph.node_map.get(key)
        parents = ()
        if node is not None:
            parents = tuple(
                sorted(
                    f"{parent.key[0]}.{parent.key[1]}"
                    for parent in node.parents
                    if parent.key in applied
                )
            )
        graph_rows.append(
            {
                "migration": f"{key[0]}.{key[1]}",
                "applied_parents": parents,
            }
        )
    canonical = json.dumps(
        graph_rows,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def current_version_metadata(using=DEFAULT_DB_ALIAS):
    return {
        "format_version": BACKUP_FORMAT_VERSION,
        "application_version": get_application_version(),
        "schema_fingerprint": schema_migration_fingerprint(using=using),
        "minimum_restore_version": get_minimum_restore_version(),
    }


def _version_tuple(value):
    numbers = re.findall(r"\d+", str(value))
    return tuple(int(number) for number in numbers[:3]) or (0,)


def assess_restore_compatibility(
    *,
    format_version,
    minimum_restore_version,
    schema_fingerprint,
    using=DEFAULT_DB_ALIAS,
):
    """Provide a conservative Phase 1 compatibility verdict.

    A matching format and migration fingerprint is compatible.  A known
    format with a different schema requires a future adapter/dry-run.  Unknown
    formats or a minimum application version newer than this deployment fail
    closed.
    """

    current_app = get_application_version()
    if str(format_version) != BACKUP_FORMAT_VERSION:
        return CompatibilityAssessment(
            CompatibilityStatus.INCOMPATIBLE,
            "The backup format is not supported by this application release.",
        )
    if _version_tuple(minimum_restore_version) > _version_tuple(current_app):
        return CompatibilityAssessment(
            CompatibilityStatus.INCOMPATIBLE,
            "The backup requires a newer application release.",
        )
    current_schema = schema_migration_fingerprint(using=using)
    if schema_fingerprint != current_schema:
        return CompatibilityAssessment(
            CompatibilityStatus.REQUIRES_UPGRADE,
            "The migration graph differs and requires a future compatibility adapter.",
        )
    return CompatibilityAssessment(
        CompatibilityStatus.COMPATIBLE,
        "The backup metadata matches the current format and migration graph.",
    )
