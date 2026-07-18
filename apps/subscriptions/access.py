"""Central subscription state and commercial-module authorization service."""

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType, SimpleNamespace
from typing import Mapping

from django.utils import timezone

from apps.core.permissions import PERMISSIONS

from .exceptions import AccessDenial, DenialCode, ModuleAccessDenied
from .feature_registry import FEATURE_REGISTRY, get_module_definition
from .models import Subscription

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_CACHE_ATTRIBUTE = "_nexapos_access_contexts"


class AccessMode(str, Enum):
    FULL = "full"
    READ_ONLY = "read_only"
    DENIED = "denied"


class AccessAction(str, Enum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class ModuleResolution:
    effective_modules: frozenset[str]
    raw_enabled_modules: frozenset[str]
    denials: Mapping[str, AccessDenial]


@dataclass(frozen=True, slots=True)
class AccessContext:
    """Immutable request entitlement state.

    Role permissions and data scope are intentionally not folded into
    ``effective_modules``.  They remain separate authorization layers.
    """

    mode: AccessMode
    effective_modules: frozenset[str]
    business: object | None
    membership: object | None
    subscription: Subscription | None
    plan: object | None
    denial: AccessDenial | None
    module_denials: Mapping[str, AccessDenial]

    @property
    def can_write(self) -> bool:
        return self.mode == AccessMode.FULL

    def has_module(self, module_key: str) -> bool:
        return module_key in self.effective_modules


@dataclass(frozen=True, slots=True)
class AccessDecision:
    allowed: bool
    context: AccessContext
    denial: AccessDenial | None = None


def _denial(
    code: DenialCode,
    message: str,
    *,
    module_key: str | None = None,
    missing_dependencies: tuple[str, ...] = (),
) -> AccessDenial:
    return AccessDenial(
        code=code,
        message=message,
        module_key=module_key,
        missing_dependencies=missing_dependencies,
    )


def _dependency_keys(definition) -> tuple[str, ...]:
    dependencies = definition.dependencies
    if definition.derived_from and definition.derived_from not in dependencies:
        return (*dependencies, definition.derived_from)
    return dependencies


def _cyclic_module_keys() -> frozenset[str]:
    """Return every registry module participating in a dependency cycle."""

    states: dict[str, int] = {}
    stack: list[str] = []
    stack_indexes: dict[str, int] = {}
    cyclic: set[str] = set()

    def visit(module_key: str) -> None:
        states[module_key] = 1
        stack_indexes[module_key] = len(stack)
        stack.append(module_key)

        for dependency in _dependency_keys(FEATURE_REGISTRY[module_key]):
            if dependency not in FEATURE_REGISTRY:
                continue
            state = states.get(dependency, 0)
            if state == 0:
                visit(dependency)
            elif state == 1:
                cyclic.update(stack[stack_indexes[dependency] :])

        stack.pop()
        stack_indexes.pop(module_key)
        states[module_key] = 2

    for module_key in FEATURE_REGISTRY:
        if states.get(module_key, 0) == 0:
            visit(module_key)
    return frozenset(cyclic)


def calculate_effective_modules(plan) -> ModuleResolution:
    """Resolve raw and derived plan modules, pruning unmet dependencies."""

    raw_enabled = {
        key
        for key, definition in FEATURE_REGISTRY.items()
        if definition.plan_field and bool(getattr(plan, definition.plan_field, False))
    }
    cyclic_modules = _cyclic_module_keys()
    effective = raw_enabled.difference(cyclic_modules)

    # Derived modules and dependency pruning can affect each other, so resolve
    # to a fixed point.  The registry is small and immutable.
    while True:
        previous = frozenset(effective)
        for key, definition in FEATURE_REGISTRY.items():
            if (
                key not in cyclic_modules
                and definition.derived_from
                and definition.derived_from in effective
            ):
                effective.add(key)
        for key in tuple(effective):
            definition = FEATURE_REGISTRY[key]
            if not set(_dependency_keys(definition)).issubset(effective):
                effective.remove(key)
        if frozenset(effective) == previous:
            break

    denials = {}
    for key, definition in FEATURE_REGISTRY.items():
        if key in effective:
            continue
        dependencies = _dependency_keys(definition)
        missing = tuple(dep for dep in dependencies if dep not in effective)
        if key in cyclic_modules:
            denials[key] = _denial(
                DenialCode.MODULE_DEPENDENCY_MISSING,
                f"{definition.label} has cyclic module dependencies.",
                module_key=key,
                missing_dependencies=missing,
            )
        elif definition.derived_from:
            source = definition.derived_from
            if source not in effective and source not in missing:
                missing = (source, *missing)
            denials[key] = _denial(
                DenialCode.MODULE_DEPENDENCY_MISSING,
                f"{definition.label} requires enabled parent modules.",
                module_key=key,
                missing_dependencies=missing,
            )
        elif key not in raw_enabled:
            denials[key] = _denial(
                DenialCode.MODULE_DISABLED,
                f"{definition.label} is not included in this plan.",
                module_key=key,
            )
        else:
            denials[key] = _denial(
                DenialCode.MODULE_DEPENDENCY_MISSING,
                f"{definition.label} has unmet module dependencies.",
                module_key=key,
                missing_dependencies=missing,
            )

    return ModuleResolution(
        effective_modules=frozenset(effective),
        raw_enabled_modules=frozenset(raw_enabled),
        denials=MappingProxyType(denials),
    )


def _resolve_action(request, action: AccessAction | str | None) -> AccessAction:
    method = str(getattr(request, "method", "GET")).upper()
    method_action = AccessAction.READ if method in SAFE_METHODS else AccessAction.WRITE
    if method_action == AccessAction.WRITE:
        return AccessAction.WRITE

    if isinstance(action, AccessAction):
        explicit_action = action
    elif action is not None:
        normalized = str(action).strip().lower()
        if normalized in {"read", "safe", "view"}:
            explicit_action = AccessAction.READ
        else:
            # Write aliases and malformed explicit actions both fail safely.
            explicit_action = AccessAction.WRITE
    else:
        explicit_action = method_action
    return explicit_action


def _normalize_module_keys(module_keys) -> tuple[str, ...]:
    if isinstance(module_keys, str):
        modules = (module_keys,)
    else:
        try:
            modules = tuple(module_keys)
        except TypeError:
            return ()
    if not modules or any(not isinstance(key, str) or not key.strip() for key in modules):
        return ()
    return modules


def _subscription_mode(subscription, plan) -> tuple[AccessMode, AccessDenial | None]:
    if subscription.status == Subscription.Status.TRIAL:
        trial_end = subscription.trial_ends_at
        if not plan.allow_trial or trial_end is None or trial_end <= timezone.now():
            return AccessMode.DENIED, _denial(
                DenialCode.TRIAL_INVALID,
                "The trial is not valid or has expired.",
            )
        return AccessMode.FULL, None

    effective_status = subscription.effective_status
    if effective_status in {
        Subscription.Status.ACTIVE,
        Subscription.Status.GRACE,
    }:
        return AccessMode.FULL, None
    if effective_status in {
        Subscription.Status.PAST_DUE,
        Subscription.Status.EXPIRED,
        Subscription.Status.CANCELLED,
    }:
        from apps.platformadmin.models import PlatformConfig

        platform_config = PlatformConfig.get_solo()
        if platform_config.expiry_mode == PlatformConfig.ExpiryMode.SUSPEND:
            return AccessMode.DENIED, _denial(
                DenialCode.SUBSCRIPTION_INACTIVE,
                "The subscription is not active.",
            )
        return AccessMode.READ_ONLY, None
    if effective_status == Subscription.Status.SUSPENDED:
        return AccessMode.DENIED, _denial(
            DenialCode.SUBSCRIPTION_SUSPENDED,
            "The subscription is suspended.",
        )
    return AccessMode.DENIED, _denial(
        DenialCode.SUBSCRIPTION_INACTIVE,
        "The subscription does not permit application access.",
    )


def _empty_context(
    denial: AccessDenial,
    *,
    business=None,
    membership=None,
    subscription=None,
    plan=None,
) -> AccessContext:
    return AccessContext(
        mode=AccessMode.DENIED,
        effective_modules=frozenset(),
        business=business,
        membership=membership,
        subscription=subscription,
        plan=plan,
        denial=denial,
        module_denials=MappingProxyType({}),
    )


def _cache_subscription_on_business(business, subscription):
    """Keep legacy subscription helpers on the central resolver's query result."""

    business_relation = Subscription._meta.get_field("business")
    business_relation.remote_field.set_cached_value(business, subscription)
    if subscription is not None:
        business_relation.set_cached_value(subscription, business)
    return subscription


def _load_subscription(request, business):
    request_business = getattr(request, "business", None)
    is_request_business = (
        request_business is not None and getattr(request_business, "pk", None) == business.pk
    )
    resolved_business_id = getattr(
        request,
        "_subscription_business_id",
        getattr(request_business, "pk", None),
    )
    candidate = getattr(request, "subscription", None)
    if candidate is not None and candidate.business_id == business.pk:
        if "plan" in candidate._state.fields_cache:
            return _cache_subscription_on_business(business, candidate)
        candidate = Subscription.objects.select_related("plan").filter(pk=candidate.pk).first()
    elif getattr(request, "_subscription_resolved", False) and resolved_business_id == business.pk:
        return _cache_subscription_on_business(business, None)
    else:
        candidate = (
            Subscription.objects.select_related("plan").filter(business_id=business.pk).first()
        )

    if is_request_business:
        request.subscription = candidate
        request._subscription_resolved = True
        request._subscription_business_id = business.pk
    return _cache_subscription_on_business(business, candidate)


def _cache_key(request, business, membership):
    user = getattr(request, "user", None)
    return (
        getattr(user, "pk", None),
        getattr(business, "pk", None),
        getattr(membership, "pk", None),
    )


def get_access_context(request, *, business=None, membership=None) -> AccessContext:
    """Return the immutable entitlement context, cached for this request.

    Explicit business/membership arguments are used by APIs and other entry
    points that cannot rely on browser-session middleware.  This function
    never chooses the first membership for a user.
    """

    business = business if business is not None else getattr(request, "business", None)
    membership = membership if membership is not None else getattr(request, "membership", None)
    key = _cache_key(request, business, membership)
    cache = getattr(request, _CACHE_ATTRIBUTE, None)
    if cache is not None and key in cache:
        return cache[key]
    if cache is None:
        cache = {}
        setattr(request, _CACHE_ATTRIBUTE, cache)

    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        context = _empty_context(
            _denial(DenialCode.AUTHENTICATION_REQUIRED, "Authentication is required.")
        )
    elif business is None or membership is None:
        context = _empty_context(
            _denial(
                DenialCode.MEMBERSHIP_REQUIRED,
                "An active business membership is required.",
            ),
            business=business,
            membership=membership,
        )
    elif (
        not membership.is_active
        or membership.user_id != user.pk
        or membership.business_id != business.pk
    ):
        context = _empty_context(
            _denial(
                DenialCode.MEMBERSHIP_REQUIRED,
                "The business membership is not active or does not match this user.",
            ),
            business=business,
            membership=membership,
        )
    elif not business.is_active:
        context = _empty_context(
            _denial(DenialCode.BUSINESS_INACTIVE, "The business is inactive."),
            business=business,
            membership=membership,
        )
    else:
        subscription = _load_subscription(request, business)
        if subscription is None:
            context = _empty_context(
                _denial(
                    DenialCode.SUBSCRIPTION_MISSING,
                    "The business does not have a subscription.",
                ),
                business=business,
                membership=membership,
            )
        else:
            plan = subscription.plan
            if not plan.is_active:
                context = _empty_context(
                    _denial(DenialCode.PLAN_INACTIVE, "The assigned plan is inactive."),
                    business=business,
                    membership=membership,
                    subscription=subscription,
                    plan=plan,
                )
            else:
                mode, mode_denial = _subscription_mode(subscription, plan)
                if mode == AccessMode.DENIED:
                    context = _empty_context(
                        mode_denial,
                        business=business,
                        membership=membership,
                        subscription=subscription,
                        plan=plan,
                    )
                else:
                    resolution = calculate_effective_modules(plan)
                    context = AccessContext(
                        mode=mode,
                        effective_modules=resolution.effective_modules,
                        business=business,
                        membership=membership,
                        subscription=subscription,
                        plan=plan,
                        denial=None,
                        module_denials=resolution.denials,
                    )

    cache[key] = context
    return context


def evaluate_access(
    request,
    module_keys: str | tuple[str, ...],
    *,
    permission_code: str | None = None,
    action: AccessAction | str | None = None,
    business=None,
    membership=None,
    scope_allowed: bool = True,
) -> AccessDecision:
    """Evaluate module, permission, and optional scope layers in order."""

    modules = _normalize_module_keys(module_keys)
    context = get_access_context(request, business=business, membership=membership)
    if context.denial is not None:
        return AccessDecision(False, context, context.denial)

    if not modules:
        denial = _denial(
            DenialCode.UNKNOWN_MODULE,
            "At least one registered commercial module is required.",
        )
        return AccessDecision(False, context, denial)

    resolved_action = _resolve_action(request, action)
    if context.mode == AccessMode.READ_ONLY and resolved_action == AccessAction.WRITE:
        denial = _denial(
            DenialCode.SUBSCRIPTION_READ_ONLY,
            "The subscription currently permits read-only access.",
        )
        return AccessDecision(False, context, denial)

    for module_key in modules:
        definition = get_module_definition(module_key)
        if definition is None:
            denial = _denial(
                DenialCode.UNKNOWN_MODULE,
                "The requested commercial module is not registered.",
                module_key=module_key,
            )
            return AccessDecision(False, context, denial)
        if module_key not in context.effective_modules:
            denial = context.module_denials.get(module_key) or _denial(
                DenialCode.MODULE_DISABLED,
                f"{definition.label} is not available.",
                module_key=module_key,
            )
            return AccessDecision(False, context, denial)

    if permission_code is not None:
        if not isinstance(permission_code, str) or permission_code not in PERMISSIONS:
            denial = _denial(
                DenialCode.PERMISSION_DENIED,
                "The required role permission is not registered.",
            )
            return AccessDecision(False, context, denial)
        if not context.membership.has_perm(permission_code):
            denial = _denial(
                DenialCode.PERMISSION_DENIED,
                f"Permission '{permission_code}' is required.",
            )
            return AccessDecision(False, context, denial)
    if not scope_allowed:
        denial = _denial(
            DenialCode.SCOPE_DENIED,
            "The requested object is outside the allowed business scope.",
        )
        return AccessDecision(False, context, denial)
    return AccessDecision(True, context)


def evaluate_public_access(
    business,
    module_keys: str | tuple[str, ...],
    *,
    action: AccessAction | str = AccessAction.READ,
) -> AccessDecision:
    """Evaluate current business/module entitlement for an anonymous output.

    Public signed-document routes intentionally have no actor membership or
    role permission.  They must still re-evaluate the same business,
    subscription, plan, state, and effective-module rules on every request.
    Callers should translate every denial to one generic not-found response so
    public URLs never disclose tenant or subscription state.
    """

    if business is None or not getattr(business, "is_active", False):
        denial = _denial(DenialCode.BUSINESS_INACTIVE, "The business is inactive.")
        return AccessDecision(False, _empty_context(denial, business=business), denial)

    subscription = (
        Subscription.objects.select_related("plan")
        .filter(business_id=getattr(business, "pk", None))
        .first()
    )
    if subscription is None:
        denial = _denial(
            DenialCode.SUBSCRIPTION_MISSING,
            "The business does not have a subscription.",
        )
        return AccessDecision(False, _empty_context(denial, business=business), denial)

    plan = subscription.plan
    if not plan.is_active:
        denial = _denial(DenialCode.PLAN_INACTIVE, "The assigned plan is inactive.")
        context = _empty_context(
            denial,
            business=business,
            subscription=subscription,
            plan=plan,
        )
        return AccessDecision(False, context, denial)

    mode, mode_denial = _subscription_mode(subscription, plan)
    if mode == AccessMode.DENIED:
        context = _empty_context(
            mode_denial,
            business=business,
            subscription=subscription,
            plan=plan,
        )
        return AccessDecision(False, context, mode_denial)

    resolution = calculate_effective_modules(plan)
    context = AccessContext(
        mode=mode,
        effective_modules=resolution.effective_modules,
        business=business,
        membership=None,
        subscription=subscription,
        plan=plan,
        denial=None,
        module_denials=resolution.denials,
    )
    modules = _normalize_module_keys(module_keys)
    if not modules:
        denial = _denial(
            DenialCode.UNKNOWN_MODULE,
            "At least one registered commercial module is required.",
        )
        return AccessDecision(False, context, denial)

    resolved_action = _resolve_action(SimpleNamespace(method="GET"), action)
    if mode == AccessMode.READ_ONLY and resolved_action == AccessAction.WRITE:
        denial = _denial(
            DenialCode.SUBSCRIPTION_READ_ONLY,
            "The subscription currently permits read-only access.",
        )
        return AccessDecision(False, context, denial)

    for module_key in modules:
        definition = get_module_definition(module_key)
        if definition is None:
            denial = _denial(
                DenialCode.UNKNOWN_MODULE,
                "The requested commercial module is not registered.",
                module_key=module_key,
            )
            return AccessDecision(False, context, denial)
        if module_key not in context.effective_modules:
            denial = context.module_denials.get(module_key) or _denial(
                DenialCode.MODULE_DISABLED,
                f"{definition.label} is not available.",
                module_key=module_key,
            )
            return AccessDecision(False, context, denial)

    return AccessDecision(True, context)


def require_access(*args, **kwargs) -> AccessContext:
    """Evaluate access and raise a structured Django 403 on failure."""

    decision = evaluate_access(*args, **kwargs)
    if not decision.allowed:
        raise ModuleAccessDenied(decision.denial)
    return decision.context


def _resolve_actor_membership(*, user, business, membership=None, request=None):
    """Resolve only the exact actor/business membership for service access.

    Service entry points cannot rely on browser-session middleware, but they
    also must never choose a user's first membership.  Reuse the matching
    request membership when possible; otherwise reload an explicitly supplied
    membership by its exact business/user identity, or perform the same exact
    lookup protected by the business/user uniqueness rule.
    """

    request_membership = getattr(request, "membership", None)
    if (
        request_membership is not None
        and getattr(request_membership, "business_id", None) == getattr(business, "pk", None)
        and getattr(request_membership, "user_id", None) == getattr(user, "pk", None)
        and (
            membership is None
            or getattr(request_membership, "pk", None) == getattr(membership, "pk", None)
        )
    ):
        return request_membership

    if business is None or user is None or getattr(user, "pk", None) is None:
        return None

    from apps.accounts.models import Membership

    memberships = Membership.objects.select_related("role").filter(
        business=business,
        user=user,
        is_active=True,
    )
    if membership is not None:
        return memberships.filter(pk=getattr(membership, "pk", None)).first()
    return memberships.first()


def evaluate_actor_access(
    user,
    business,
    module_keys: str | tuple[str, ...],
    *,
    permission_code: str | None = None,
    action: AccessAction | str = AccessAction.WRITE,
    membership=None,
    request=None,
    scope_allowed: bool = True,
) -> AccessDecision:
    """Evaluate central access for a non-HTTP service actor.

    ``request`` remains optional and is used only to reuse an existing request
    cache when the service was reached through a browser view.  Authorization
    still derives from the explicit actor, business, and exact membership, so
    omitting a request never becomes a bypass.
    """

    authorization_request = request
    request_user = getattr(request, "user", None)
    if request is not None and getattr(request_user, "pk", None) != getattr(user, "pk", None):
        # Never authorize an explicit service actor through a different
        # request actor's cached membership or subscription context.
        authorization_request = None

    membership = _resolve_actor_membership(
        user=user,
        business=business,
        membership=membership,
        request=authorization_request,
    )
    if authorization_request is None:
        normalized_action = (
            action.value if isinstance(action, AccessAction) else str(action).strip().lower()
        )
        authorization_request = SimpleNamespace(
            user=user,
            business=business,
            membership=membership,
            method="GET" if normalized_action in {"read", "safe", "view"} else "POST",
        )

    return evaluate_access(
        authorization_request,
        module_keys,
        permission_code=permission_code,
        action=action,
        business=business,
        membership=membership,
        scope_allowed=scope_allowed,
    )


def require_actor_access(*args, **kwargs) -> AccessContext:
    """Raise the same structured 403 used by browser guards for a service actor."""

    decision = evaluate_actor_access(*args, **kwargs)
    if not decision.allowed:
        raise ModuleAccessDenied(decision.denial)
    return decision.context
