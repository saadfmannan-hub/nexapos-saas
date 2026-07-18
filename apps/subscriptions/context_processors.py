"""Request-cached commercial capabilities exposed to Django templates."""

from dataclasses import dataclass
from types import MappingProxyType

from .access import AccessMode, get_access_context
from .feature_registry import FEATURE_REGISTRY


@dataclass(frozen=True, slots=True)
class TemplateModuleCapability:
    key: str
    label: str
    category: str
    enabled: bool
    can_write: bool
    denial_code: str


def subscription_capabilities(request):
    context = get_access_context(request)
    capabilities = {}
    for key, definition in FEATURE_REGISTRY.items():
        module_denial = context.module_denials.get(key)
        denial = context.denial or module_denial
        enabled = key in context.effective_modules
        capabilities[key] = TemplateModuleCapability(
            key=key,
            label=definition.label,
            category=definition.category,
            enabled=enabled,
            can_write=enabled and context.mode == AccessMode.FULL,
            denial_code=denial.code.value if denial else "",
        )
    immutable_capabilities = MappingProxyType(capabilities)
    return {
        "effective_modules": context.effective_modules,
        "subscription_access_mode": context.mode.value,
        "module_capabilities": immutable_capabilities,
        "business_capabilities": immutable_capabilities,
    }
