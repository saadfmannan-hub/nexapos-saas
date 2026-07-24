from .access import evaluate_wms_access, first_permitted_wms_route


def wms_access(request):
    decision = evaluate_wms_access(request)
    if not decision.allowed:
        return {
            "wms_navigation_available": False,
            "wms_home_route": "",
            "wms_permissions": frozenset(),
        }
    permissions = frozenset(decision.user_access.permission_set)
    home_route = first_permitted_wms_route(decision.user_access)
    return {
        "wms_navigation_available": bool(home_route),
        "wms_home_route": home_route or "",
        "wms_permissions": permissions,
    }
