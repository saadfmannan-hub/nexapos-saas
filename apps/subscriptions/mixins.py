"""Opt-in class-based view guard for subscription modules."""

from django.core.exceptions import ImproperlyConfigured

from apps.core.mixins import BusinessRequiredMixin

from .access import _normalize_module_keys, require_access


class ModulePermissionRequiredMixin(BusinessRequiredMixin):
    """Require declared modules, permission, and an optional scope hook.

    Place this mixin before the concrete Django view and other dispatch
    implementations so its subscription guard runs before the view handler.
    """

    required_modules: str | tuple[str, ...] = ()
    access_action: str | None = None

    def get_required_modules(self) -> tuple[str, ...]:
        modules = _normalize_module_keys(self.required_modules)
        if not modules:
            raise ImproperlyConfigured(f"{self.__class__.__name__} must declare required_modules.")
        return modules

    def has_module_scope(self, context) -> bool:
        """Override when a CBV can validate branch/warehouse/object scope."""

        return True

    def dispatch(self, request, *args, **kwargs):
        # BusinessRequiredMixin remains authoritative for authentication and
        # no-business redirects.  The module guard runs only with that context.
        if (
            request.user.is_authenticated
            and getattr(request, "business", None)
            and getattr(request, "membership", None)
        ):
            context = require_access(
                request,
                self.get_required_modules(),
                permission_code=self.permission_required,
                action=self.access_action,
            )
            if not self.has_module_scope(context):
                require_access(
                    request,
                    self.get_required_modules(),
                    permission_code=self.permission_required,
                    action=self.access_action,
                    scope_allowed=False,
                )
        return super().dispatch(request, *args, **kwargs)
