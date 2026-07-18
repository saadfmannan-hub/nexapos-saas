"""Opt-in function-view guards for commercial modules and role permissions."""

from functools import wraps

from django.core.exceptions import ImproperlyConfigured

from apps.core.decorators import business_required

from .access import _normalize_module_keys, require_access


def module_permission_required(module_key, permission_code=None, action=None):
    """Require a module before checking the separate role-permission layer."""

    modules = _normalize_module_keys(module_key)
    if not modules:
        raise ImproperlyConfigured(
            "module_permission_required() requires at least one non-empty module key."
        )

    def decorator(view_func):
        @wraps(view_func)
        @business_required
        def wrapper(request, *args, **kwargs):
            require_access(
                request,
                modules,
                permission_code=permission_code,
                action=action,
            )
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator
