"""Approved Nexa WMS permission registry and Phase 1 system roles."""

from types import MappingProxyType

WMS_PERMISSIONS = MappingProxyType(
    {
        "wms.dashboard.view": "View WMS dashboard",
        "wms.orders.view": "View workshop orders",
        "wms.orders.manage": "Manage workshop orders",
        "wms.orders.finish": "Finish workshop orders",
        "wms.alterations.view": "View alterations",
        "wms.alterations.manage": "Manage alterations",
        "wms.alterations.complete": "Complete alterations",
        "wms.employees.view": "View WMS employees",
        "wms.employees.manage": "Manage WMS employees",
        "wms.attendance.view": "View attendance",
        "wms.attendance.manage": "Manage attendance",
        "wms.attendance.correct": "Correct attendance",
        "wms.production.view": "View production entries",
        "wms.production.manage": "Manage production entries",
        "wms.production.approve": "Approve production",
        "wms.production.correct": "Correct production entries",
        "wms.categories.view": "View production categories",
        "wms.categories.manage": "Manage production categories",
        "wms.salary.view": "View salary information",
        "wms.salary.calculate": "Calculate salary",
        "wms.salary.finalize": "Finalize salary",
        "wms.reports.view": "View WMS reports",
        "wms.reports.export": "Export WMS reports",
        "wms.users.manage": "Manage WMS user access",
        "wms.settings.manage": "Manage WMS settings",
    }
)
WMS_PERMISSION_CODES = tuple(WMS_PERMISSIONS)

WMS_SYSTEM_ROLE_TEMPLATES = MappingProxyType(
    {
        "owner_admin": {
            "name": "Owner / WMS Admin",
            "is_admin": True,
            "permissions": WMS_PERMISSION_CODES,
        },
        "workshop_manager": {
            "name": "Workshop Manager",
            "is_admin": False,
            "permissions": (
                "wms.dashboard.view",
                "wms.orders.view",
                "wms.orders.manage",
                "wms.orders.finish",
                "wms.alterations.view",
                "wms.alterations.manage",
                "wms.alterations.complete",
                "wms.employees.view",
                "wms.employees.manage",
                "wms.attendance.view",
                "wms.production.view",
                "wms.production.manage",
                "wms.production.approve",
                "wms.production.correct",
                "wms.categories.view",
                "wms.categories.manage",
                "wms.reports.view",
                "wms.reports.export",
            ),
        },
        "attendance_manager": {
            "name": "Attendance Manager",
            "is_admin": False,
            "permissions": (
                "wms.dashboard.view",
                "wms.employees.view",
                "wms.attendance.view",
                "wms.attendance.manage",
                "wms.attendance.correct",
                "wms.reports.view",
            ),
        },
        "production_entry": {
            "name": "Production Entry User",
            "is_admin": False,
            "permissions": (
                "wms.dashboard.view",
                "wms.employees.view",
                "wms.production.view",
                "wms.production.manage",
                "wms.categories.view",
            ),
        },
        "report_viewer": {
            "name": "Report Viewer",
            "is_admin": False,
            "permissions": (
                "wms.dashboard.view",
                "wms.orders.view",
                "wms.alterations.view",
                "wms.employees.view",
                "wms.attendance.view",
                "wms.production.view",
                "wms.categories.view",
                "wms.reports.view",
                "wms.reports.export",
            ),
        },
    }
)


def validate_wms_permissions(values):
    """Return a de-duplicated permission list or raise for unknown values."""

    normalized = list(dict.fromkeys(values or []))
    unknown = sorted(set(normalized).difference(WMS_PERMISSIONS))
    if unknown:
        from django.core.exceptions import ValidationError

        raise ValidationError(
            {"permissions": f"Unknown WMS permissions: {', '.join(unknown)}."}
        )
    return normalized
