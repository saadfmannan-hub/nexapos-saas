"""Structured subscription and module authorization failures."""

from dataclasses import dataclass
from enum import Enum

from django.core.exceptions import PermissionDenied


class DenialCode(str, Enum):
    AUTHENTICATION_REQUIRED = "authentication_required"
    MEMBERSHIP_REQUIRED = "membership_required"
    BUSINESS_INACTIVE = "business_inactive"
    SUBSCRIPTION_MISSING = "subscription_missing"
    SUBSCRIPTION_READ_ONLY = "subscription_read_only"
    SUBSCRIPTION_SUSPENDED = "subscription_suspended"
    SUBSCRIPTION_INACTIVE = "subscription_inactive"
    TRIAL_INVALID = "trial_invalid"
    PLAN_INACTIVE = "plan_inactive"
    MODULE_DISABLED = "module_disabled"
    MODULE_DEPENDENCY_MISSING = "module_dependency_missing"
    PERMISSION_DENIED = "permission_denied"
    SCOPE_DENIED = "scope_denied"
    UNKNOWN_MODULE = "unknown_module"


@dataclass(frozen=True, slots=True)
class AccessDenial:
    code: DenialCode
    message: str
    module_key: str | None = None
    missing_dependencies: tuple[str, ...] = ()


class ModuleAccessDenied(PermissionDenied):
    """Django 403 carrying a stable entitlement denial payload."""

    def __init__(self, denial: AccessDenial):
        self.denial = denial
        self.code = denial.code.value
        super().__init__(denial.message)
