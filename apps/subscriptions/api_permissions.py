"""Opt-in DRF permission base for subscription-aware API resources."""

from django.core.exceptions import ImproperlyConfigured
from rest_framework.exceptions import APIException, NotFound, PermissionDenied
from rest_framework.permissions import BasePermission

from .access import _normalize_module_keys, evaluate_access
from .exceptions import AccessDenial, DenialCode


class APIAuthenticationRequired(APIException):
    status_code = 401
    default_code = DenialCode.AUTHENTICATION_REQUIRED.value
    default_detail = {
        "code": DenialCode.AUTHENTICATION_REQUIRED.value,
        "detail": "Authentication is required.",
    }


def _api_denied(denial: AccessDenial):
    raise PermissionDenied(
        detail={"code": denial.code.value, "detail": denial.message},
        code=denial.code.value,
    )


class HasSubscriptionModuleAccess(BasePermission):
    """Require API Access plus every module declared by an API view.

    The permission never looks up a user's first membership.  Browser-session
    context, explicit ``request.api_*`` attributes, or view hooks must identify
    both the business and membership.  Adopting views must still tenant-scope
    their querysets; object-scope hooks are a final defense and deny with 404.
    """

    def get_business(self, request, view):
        hook = getattr(view, "get_api_business", None)
        if hook:
            return hook(request)
        return getattr(request, "api_business", None) or getattr(request, "business", None)

    def get_membership(self, request, view):
        hook = getattr(view, "get_api_membership", None)
        if hook:
            return hook(request)
        return getattr(request, "api_membership", None) or getattr(request, "membership", None)

    def get_required_modules(self, view) -> tuple[str, ...]:
        declared_modules = getattr(view, "required_modules", ())
        single_module = getattr(view, "required_module", None)
        if single_module:
            if isinstance(declared_modules, str):
                declared_modules = (declared_modules, single_module)
            else:
                try:
                    declared_modules = (*tuple(declared_modules), single_module)
                except TypeError:
                    declared_modules = ()
        modules = _normalize_module_keys(declared_modules)
        if not modules:
            raise ImproperlyConfigured(
                f"{view.__class__.__name__} must declare at least one required API module."
            )
        return tuple(dict.fromkeys(("api_access", *modules)))

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            raise APIAuthenticationRequired

        business = self.get_business(request, view)
        membership = self.get_membership(request, view)
        decision = evaluate_access(
            request,
            self.get_required_modules(view),
            permission_code=getattr(
                view,
                "required_permission",
                getattr(view, "permission_required", None),
            ),
            action=getattr(view, "access_action", None),
            business=business,
            membership=membership,
        )
        if not decision.allowed:
            _api_denied(decision.denial)

        scope_hook = getattr(view, "has_api_scope", None)
        if scope_hook and not scope_hook(request, decision.context):
            decision = evaluate_access(
                request,
                self.get_required_modules(view),
                permission_code=getattr(
                    view,
                    "required_permission",
                    getattr(view, "permission_required", None),
                ),
                action=getattr(view, "access_action", None),
                business=business,
                membership=membership,
                scope_allowed=False,
            )
            _api_denied(decision.denial)

        request.api_access_context = decision.context
        request.api_business = business
        request.api_membership = membership
        return True

    def has_object_permission(self, request, view, obj):
        hook = getattr(view, "has_api_object_scope", None)
        context = getattr(request, "api_access_context", None)
        business = getattr(context, "business", None)
        object_business_id = getattr(obj, "business_id", None)

        if business is None:
            raise NotFound()
        if object_business_id is None and hook is None:
            raise NotFound()
        if object_business_id is not None and object_business_id != business.pk:
            raise NotFound()
        if hook is not None and not hook(request, context, obj):
            raise NotFound()
        return True
