"""Function-view decorators mirroring apps.core.mixins."""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect


def business_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapper(request, *args, **kwargs):
        if request.business is None or request.membership is None:
            return redirect("tenants:no_business")
        return view_func(request, *args, **kwargs)

    return wrapper


def require_permission(code):
    def decorator(view_func):
        @wraps(view_func)
        @business_required
        def wrapper(request, *args, **kwargs):
            if not request.membership.has_perm(code):
                raise PermissionDenied
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator
