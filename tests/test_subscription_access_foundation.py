"""Focused tests for Subscription & Module Architecture v2.0 foundations."""

from datetime import timedelta
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured
from django.http import Http404, HttpResponse
from django.test import RequestFactory
from django.utils import timezone
from django.views import View
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView

from apps.accounts.models import Membership, Role, User
from apps.branches.models import Branch, Warehouse
from apps.core.mixins import get_tenant_object
from apps.subscriptions import services as subscription_services
from apps.subscriptions.access import (
    AccessMode,
    calculate_effective_modules,
    evaluate_access,
    evaluate_actor_access,
    get_access_context,
)
from apps.subscriptions.api_permissions import HasSubscriptionModuleAccess
from apps.subscriptions.context_processors import subscription_capabilities
from apps.subscriptions.decorators import module_permission_required
from apps.subscriptions.exceptions import DenialCode, ModuleAccessDenied
from apps.subscriptions.feature_registry import (
    ACTIVE_MODULE_KEYS,
    FEATURE_REGISTRY,
    FUTURE_PLAN_FIELDS,
    ModuleDefinition,
    get_module_definition,
)
from apps.subscriptions.middleware import SubscriptionMiddleware
from apps.subscriptions.mixins import ModulePermissionRequiredMixin
from apps.subscriptions.models import Subscription

from .base import TenantTestCase


class AccessFoundationTestCase(TenantTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.api_factory = APIRequestFactory()
        self.membership = Membership.objects.select_related("role").get(
            business=self.business_a,
            user=self.owner_a,
        )
        self.subscription = Subscription.objects.select_related("plan").get(
            business=self.business_a
        )
        self.plan = self.subscription.plan

    def set_features(self, **features):
        for name, value in features.items():
            setattr(self.plan, name, value)
        self.plan.save()

    def set_status(self, status, **fields):
        self.subscription.status = status
        for name, value in fields.items():
            setattr(self.subscription, name, value)
        self.subscription.save()

    def request(self, method="get", *, user=None, business=True, membership=True):
        request = getattr(self.factory, method)("/", data={})
        request.user = user or self.owner_a
        request.business = self.business_a if business is True else business
        request.membership = self.membership if membership is True else membership
        return request

    def api_request(self, method="get", *, explicit_context=True, user=None):
        request = getattr(self.api_factory, method)("/api/foundation/", data={})
        force_authenticate(request, user=user or self.owner_a)
        if explicit_context:
            request.api_business = self.business_a
            request.api_membership = self.membership
        return request


class RegistryResolutionTests(AccessFoundationTestCase):
    def resolve_custom_registry(self, definitions, **features):
        registry = MappingProxyType({definition.key: definition for definition in definitions})
        with patch("apps.subscriptions.access.FEATURE_REGISTRY", registry):
            return calculate_effective_modules(SimpleNamespace(**features))

    def test_known_module_resolves(self):
        self.assertEqual(get_module_definition("pos_core"), FEATURE_REGISTRY["pos_core"])
        self.assertEqual(set(ACTIVE_MODULE_KEYS), set(FEATURE_REGISTRY))

    def test_unknown_module_fails_closed(self):
        self.set_features(feature_sales=True)
        decision = evaluate_access(self.request(), "not_registered")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.denial.code, DenialCode.UNKNOWN_MODULE)

    def test_derived_customers_follow_pos_core(self):
        self.plan.feature_sales = False
        self.assertNotIn("customers", calculate_effective_modules(self.plan).effective_modules)
        self.plan.feature_sales = True
        self.assertIn("customers", calculate_effective_modules(self.plan).effective_modules)

    def test_derived_users_staff_follow_pos_core(self):
        self.plan.feature_sales = False
        self.assertNotIn("users_staff", calculate_effective_modules(self.plan).effective_modules)
        self.plan.feature_sales = True
        self.assertIn("users_staff", calculate_effective_modules(self.plan).effective_modules)

    def test_purchases_fail_without_inventory(self):
        self.plan.feature_sales = True
        self.plan.feature_suppliers = True
        self.plan.feature_inventory = False
        self.plan.feature_purchases = True
        result = calculate_effective_modules(self.plan)
        self.assertNotIn("purchases", result.effective_modules)
        self.assertEqual(
            result.denials["purchases"].code,
            DenialCode.MODULE_DEPENDENCY_MISSING,
        )

    def test_purchases_fail_without_suppliers(self):
        self.plan.feature_sales = True
        self.plan.feature_inventory = True
        self.plan.feature_suppliers = False
        self.plan.feature_purchases = True
        self.assertNotIn("purchases", calculate_effective_modules(self.plan).effective_modules)

    def test_tailoring_fails_without_pos_core(self):
        self.plan.feature_sales = False
        self.plan.feature_inventory = True
        self.plan.feature_tailoring_module = True
        self.assertNotIn("tailoring", calculate_effective_modules(self.plan).effective_modules)

    def test_tailoring_fails_without_inventory(self):
        self.plan.feature_sales = True
        self.plan.feature_inventory = False
        self.plan.feature_tailoring_module = True
        self.assertNotIn("tailoring", calculate_effective_modules(self.plan).effective_modules)

    def test_api_access_does_not_enable_underlying_modules(self):
        self.plan.feature_api_access = True
        self.plan.feature_sales = False
        result = calculate_effective_modules(self.plan)
        self.assertIn("api_access", result.effective_modules)
        self.assertNotIn("pos_core", result.effective_modules)
        self.assertNotIn("customers", result.effective_modules)

    def test_future_fields_never_become_effective_modules(self):
        for field in FUTURE_PLAN_FIELDS:
            setattr(self.plan, field, True)
        result = calculate_effective_modules(self.plan)
        self.assertNotIn("executive_dashboard", result.effective_modules)
        self.assertNotIn("ai_assistant", result.effective_modules)
        self.assertTrue(result.effective_modules.issubset(FEATURE_REGISTRY))

    def test_two_module_dependency_cycle_fails_closed(self):
        result = self.resolve_custom_registry(
            (
                ModuleDefinition(
                    key="a",
                    label="A",
                    category="Test",
                    plan_field="feature_a",
                    dependencies=("b",),
                ),
                ModuleDefinition(
                    key="b",
                    label="B",
                    category="Test",
                    plan_field="feature_b",
                    dependencies=("a",),
                ),
            ),
            feature_a=True,
            feature_b=True,
        )
        self.assertEqual(result.effective_modules, frozenset())
        self.assertEqual(result.denials["a"].code, DenialCode.MODULE_DEPENDENCY_MISSING)
        self.assertEqual(result.denials["b"].code, DenialCode.MODULE_DEPENDENCY_MISSING)

    def test_self_dependency_fails_closed(self):
        result = self.resolve_custom_registry(
            (
                ModuleDefinition(
                    key="a",
                    label="A",
                    category="Test",
                    plan_field="feature_a",
                    dependencies=("a",),
                ),
            ),
            feature_a=True,
        )
        self.assertEqual(result.effective_modules, frozenset())

    def test_acyclic_chain_is_ordering_independent(self):
        definitions = (
            ModuleDefinition(key="a", label="A", category="Test", plan_field="feature_a"),
            ModuleDefinition(
                key="b",
                label="B",
                category="Test",
                plan_field="feature_b",
                dependencies=("a",),
            ),
            ModuleDefinition(
                key="c",
                label="C",
                category="Test",
                plan_field="feature_c",
                dependencies=("b",),
            ),
        )
        expected = frozenset({"a", "b", "c"})
        features = {"feature_a": True, "feature_b": True, "feature_c": True}
        self.assertEqual(
            self.resolve_custom_registry(definitions, **features).effective_modules,
            expected,
        )
        self.assertEqual(
            self.resolve_custom_registry(reversed(definitions), **features).effective_modules,
            expected,
        )

    def test_unknown_dependency_fails_closed(self):
        result = self.resolve_custom_registry(
            (
                ModuleDefinition(
                    key="a",
                    label="A",
                    category="Test",
                    plan_field="feature_a",
                    dependencies=("not_registered",),
                ),
            ),
            feature_a=True,
        )
        self.assertEqual(result.effective_modules, frozenset())
        self.assertEqual(result.denials["a"].missing_dependencies, ("not_registered",))


class SubscriptionAccessModeTests(AccessFoundationTestCase):
    def test_active_is_full(self):
        self.set_status(Subscription.Status.ACTIVE)
        self.assertEqual(get_access_context(self.request()).mode, AccessMode.FULL)

    def test_valid_trial_is_full(self):
        self.plan.allow_trial = True
        self.plan.save()
        self.set_status(
            Subscription.Status.TRIAL,
            trial_ends_at=timezone.now() + timedelta(days=1),
        )
        self.assertEqual(get_access_context(self.request()).mode, AccessMode.FULL)

    def test_grace_is_full(self):
        self.set_status(Subscription.Status.GRACE)
        self.assertEqual(get_access_context(self.request()).mode, AccessMode.FULL)

    def test_past_due_is_read_only(self):
        self.set_status(Subscription.Status.PAST_DUE)
        self.assertEqual(get_access_context(self.request()).mode, AccessMode.READ_ONLY)

    def test_expired_is_read_only(self):
        self.set_status(Subscription.Status.EXPIRED)
        self.assertEqual(get_access_context(self.request()).mode, AccessMode.READ_ONLY)

    def test_cancelled_is_read_only(self):
        self.set_status(Subscription.Status.CANCELLED)
        self.assertEqual(get_access_context(self.request()).mode, AccessMode.READ_ONLY)

    def test_suspended_is_denied(self):
        self.set_status(Subscription.Status.SUSPENDED)
        context = get_access_context(self.request())
        self.assertEqual(context.mode, AccessMode.DENIED)
        self.assertEqual(context.denial.code, DenialCode.SUBSCRIPTION_SUSPENDED)

    def test_missing_subscription_is_denied(self):
        self.subscription.delete()
        context = get_access_context(self.request())
        self.assertEqual(context.mode, AccessMode.DENIED)
        self.assertEqual(context.denial.code, DenialCode.SUBSCRIPTION_MISSING)

    def test_inactive_plan_is_denied(self):
        self.plan.is_active = False
        self.plan.save()
        context = get_access_context(self.request())
        self.assertEqual(context.mode, AccessMode.DENIED)
        self.assertEqual(context.denial.code, DenialCode.PLAN_INACTIVE)

    def test_inactive_business_is_denied(self):
        self.business_a.is_active = False
        self.business_a.save()
        context = get_access_context(self.request())
        self.assertEqual(context.mode, AccessMode.DENIED)
        self.assertEqual(context.denial.code, DenialCode.BUSINESS_INACTIVE)

    def test_expired_trial_is_denied_not_read_only(self):
        self.set_status(
            Subscription.Status.TRIAL,
            trial_ends_at=timezone.now() - timedelta(seconds=1),
        )
        context = get_access_context(self.request())
        self.assertEqual(context.mode, AccessMode.DENIED)
        self.assertEqual(context.denial.code, DenialCode.TRIAL_INVALID)

    def test_trial_expiry_exact_boundary_is_denied(self):
        self.plan.allow_trial = True
        self.plan.save()
        self.set_status(
            Subscription.Status.TRIAL,
            trial_ends_at=timezone.now(),
        )
        context = get_access_context(self.request())
        self.assertEqual(context.mode, AccessMode.DENIED)
        self.assertEqual(context.denial.code, DenialCode.TRIAL_INVALID)

    def test_grace_state_post_remains_allowed(self):
        self.set_features(feature_sales=True)
        self.set_status(Subscription.Status.GRACE)
        decision = evaluate_access(
            self.request("post"),
            "pos_core",
            permission_code="sales.view",
        )
        self.assertTrue(decision.allowed)


class DecoratorAndMixinTests(AccessFoundationTestCase):
    @staticmethod
    @module_permission_required("pos_core", "sales.view")
    def guarded_view(request):
        return HttpResponse("allowed")

    @staticmethod
    @module_permission_required("not_registered", "sales.view")
    def unknown_view(request):
        return HttpResponse("not reached")

    def test_enabled_module_and_permission_allow(self):
        self.set_features(feature_sales=True)
        response = self.guarded_view(self.request())
        self.assertEqual(response.status_code, 200)

    def test_disabled_module_denies_owner(self):
        self.set_features(feature_sales=False)
        with self.assertRaises(ModuleAccessDenied) as caught:
            self.guarded_view(self.request())
        self.assertEqual(caught.exception.denial.code, DenialCode.MODULE_DISABLED)

    def test_enabled_module_missing_permission_denies(self):
        self.set_features(feature_sales=True)
        role = Role.objects.create(business=self.business_a, name="No Sales", permissions=[])
        self.membership.role = role
        self.membership.save(update_fields=["role"])
        self.membership = Membership.objects.select_related("role").get(pk=self.membership.pk)
        with self.assertRaises(ModuleAccessDenied) as caught:
            self.guarded_view(self.request(user=self.owner_a))
        self.assertEqual(caught.exception.denial.code, DenialCode.PERMISSION_DENIED)

    def test_read_only_allows_safe_get(self):
        self.set_features(feature_sales=True)
        self.set_status(Subscription.Status.PAST_DUE)
        self.assertEqual(self.guarded_view(self.request("get")).status_code, 200)

    def test_read_only_denies_post(self):
        self.set_features(feature_sales=True)
        self.set_status(Subscription.Status.PAST_DUE)
        with self.assertRaises(ModuleAccessDenied) as caught:
            self.guarded_view(self.request("post"))
        self.assertEqual(caught.exception.denial.code, DenialCode.SUBSCRIPTION_READ_ONLY)

    def test_central_action_matrix_never_weakens_http_method(self):
        self.set_features(feature_sales=True)
        self.set_status(Subscription.Status.PAST_DUE)
        for method in ("get", "head", "options"):
            with self.subTest(method=method, action="read"):
                decision = evaluate_access(
                    self.request(method),
                    "pos_core",
                    permission_code="sales.view",
                    action="read",
                )
                self.assertTrue(decision.allowed)
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method, action="read"):
                decision = evaluate_access(
                    self.request(method),
                    "pos_core",
                    permission_code="sales.view",
                    action="read",
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.denial.code, DenialCode.SUBSCRIPTION_READ_ONLY)

        strengthened_get = evaluate_access(
            self.request("get"),
            "pos_core",
            permission_code="sales.view",
            action="write",
        )
        self.assertFalse(strengthened_get.allowed)
        self.assertEqual(
            strengthened_get.denial.code,
            DenialCode.SUBSCRIPTION_READ_ONLY,
        )

    def test_decorator_explicit_read_cannot_weaken_post(self):
        self.set_features(feature_sales=True)
        self.set_status(Subscription.Status.PAST_DUE)

        @module_permission_required("pos_core", "sales.view", action="read")
        def guarded_post(request):
            return HttpResponse("not reached")

        with self.assertRaises(ModuleAccessDenied) as caught:
            guarded_post(self.request("post"))
        self.assertEqual(caught.exception.denial.code, DenialCode.SUBSCRIPTION_READ_ONLY)

    def test_empty_direct_module_requirements_fail_closed(self):
        self.set_features(feature_sales=True)
        for modules in ((), []):
            with self.subTest(modules=modules):
                decision = evaluate_access(self.request(), modules)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.denial.code, DenialCode.UNKNOWN_MODULE)

    def test_empty_decorator_module_requirements_raise_configuration_error(self):
        for modules in ((), [], ""):
            with self.subTest(modules=modules):
                with self.assertRaises(ImproperlyConfigured):
                    module_permission_required(modules)

    def test_unknown_module_denies(self):
        self.set_features(feature_sales=True)
        with self.assertRaises(ModuleAccessDenied) as caught:
            self.unknown_view(self.request())
        self.assertEqual(caught.exception.denial.code, DenialCode.UNKNOWN_MODULE)

    def test_unknown_stored_permission_is_denied(self):
        self.set_features(feature_sales=True)
        role = Role.objects.create(
            business=self.business_a,
            name="Unknown Permission",
            permissions=["made.up"],
        )
        self.membership.role = role
        self.membership.save(update_fields=["role"])
        self.membership = Membership.objects.select_related("role").get(pk=self.membership.pk)

        @module_permission_required("pos_core", "made.up")
        def unknown_permission_view(request):
            return HttpResponse("not reached")

        with self.assertRaises(ModuleAccessDenied) as caught:
            unknown_permission_view(self.request())
        self.assertEqual(caught.exception.denial.code, DenialCode.PERMISSION_DENIED)

    def test_owner_is_denied_unknown_permission(self):
        self.assertTrue(self.membership.role.is_owner)
        self.set_features(feature_sales=True)

        @module_permission_required("pos_core", "made.up")
        def unknown_permission_view(request):
            return HttpResponse("not reached")

        with self.assertRaises(ModuleAccessDenied) as caught:
            unknown_permission_view(self.request())
        self.assertEqual(caught.exception.denial.code, DenialCode.PERMISSION_DENIED)

    def test_decorator_checks_every_required_module(self):
        self.set_features(feature_sales=True, feature_inventory=False)

        @module_permission_required(("pos_core", "inventory"), "products.view")
        def multiple_module_view(request):
            return HttpResponse("not reached")

        with self.assertRaises(ModuleAccessDenied) as caught:
            multiple_module_view(self.request())
        self.assertEqual(caught.exception.denial.module_key, "inventory")

    def test_request_level_context_is_reused(self):
        self.set_features(feature_sales=True)
        request = self.request()
        with self.assertNumQueries(1):
            first = get_access_context(request)
        with self.assertNumQueries(0):
            second = get_access_context(request)
        self.assertIs(first, second)

    def test_context_resolution_primes_legacy_subscription_relation_cache(self):
        self.set_features(feature_sales=True)
        fresh_business = self.business_a.__class__.objects.get(pk=self.business_a.pk)
        request = self.request(business=fresh_business)

        with self.assertNumQueries(1):
            context = get_access_context(request)
        with self.assertNumQueries(0):
            self.assertIs(fresh_business.subscription, context.subscription)
            subscription_services.require_operational(fresh_business)

    def test_actor_access_reloads_an_explicit_membership_identity(self):
        self.set_features(feature_sales=True)
        forged_membership = self.business_b.memberships.get(user=self.owner_b)
        forged_membership.business = self.business_a
        forged_membership.user = self.owner_a
        forged_membership.role = self.membership.role

        decision = evaluate_actor_access(
            self.owner_a,
            self.business_a,
            "pos_core",
            action="read",
            membership=forged_membership,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.denial.code, DenialCode.MEMBERSHIP_REQUIRED)

    def test_tenant_mismatch_remains_404(self):
        self.set_features(feature_sales=True)

        @module_permission_required("pos_core", "products.view")
        def tenant_object_view(request):
            get_tenant_object(
                self.product_b.__class__,
                request.business,
                public_id=self.product_b.public_id,
            )
            return HttpResponse("not reached")

        with self.assertRaises(Http404):
            tenant_object_view(self.request())

    def test_mixin_derives_get_as_read_and_checks_permission(self):
        self.set_features(feature_sales=True)

        class GuardedView(ModulePermissionRequiredMixin, View):
            required_modules = ("pos_core",)
            permission_required = "sales.view"

            def get(self, request):
                return HttpResponse("allowed")

        response = GuardedView.as_view()(self.request("get"))
        self.assertEqual(response.status_code, 200)

    def test_mixin_derives_post_as_write(self):
        self.set_features(feature_sales=True)
        self.set_status(Subscription.Status.EXPIRED)

        class GuardedView(ModulePermissionRequiredMixin, View):
            required_modules = "pos_core"
            permission_required = "sales.view"

            def post(self, request):
                return HttpResponse("not reached")

        with self.assertRaises(ModuleAccessDenied) as caught:
            GuardedView.as_view()(self.request("post"))
        self.assertEqual(caught.exception.denial.code, DenialCode.SUBSCRIPTION_READ_ONLY)

    def test_mixin_explicit_read_cannot_weaken_post(self):
        self.set_features(feature_sales=True)
        self.set_status(Subscription.Status.EXPIRED)

        class GuardedView(ModulePermissionRequiredMixin, View):
            required_modules = "pos_core"
            permission_required = "sales.view"
            access_action = "read"

            def post(self, request):
                return HttpResponse("not reached")

        with self.assertRaises(ModuleAccessDenied) as caught:
            GuardedView.as_view()(self.request("post"))
        self.assertEqual(caught.exception.denial.code, DenialCode.SUBSCRIPTION_READ_ONLY)

    def test_empty_mixin_module_requirements_raise_configuration_error(self):
        class EmptyModulesView(ModulePermissionRequiredMixin, View):
            required_modules = []

            def get(self, request):
                return HttpResponse("not reached")

        with self.assertRaises(ImproperlyConfigured):
            EmptyModulesView.as_view()(self.request())

    def test_mixin_checks_every_required_module(self):
        self.set_features(feature_sales=True, feature_inventory=False)

        class MultipleModulesView(ModulePermissionRequiredMixin, View):
            required_modules = ("pos_core", "inventory")
            permission_required = "products.view"

            def get(self, request):
                return HttpResponse("not reached")

        with self.assertRaises(ModuleAccessDenied) as caught:
            MultipleModulesView.as_view()(self.request())
        self.assertEqual(caught.exception.denial.module_key, "inventory")

    def test_mixin_scope_hook_denies_after_entitlements_pass(self):
        self.set_features(feature_sales=True)

        class ScopeDeniedView(ModulePermissionRequiredMixin, View):
            required_modules = "pos_core"
            permission_required = "sales.view"

            def has_module_scope(self, context):
                return False

            def get(self, request):
                return HttpResponse("not reached")

        with self.assertRaises(ModuleAccessDenied) as caught:
            ScopeDeniedView.as_view()(self.request("get"))
        self.assertEqual(caught.exception.denial.code, DenialCode.SCOPE_DENIED)

    def test_context_cache_is_isolated_by_business_and_membership(self):
        role_b = self.business_b.roles.get(is_owner=True)
        membership_b = Membership.objects.create(
            business=self.business_b,
            user=self.owner_a,
            role=role_b,
        )
        request = self.request()

        with self.assertNumQueries(1):
            context_a = get_access_context(request)
        with self.assertNumQueries(1):
            context_b = get_access_context(
                request,
                business=self.business_b,
                membership=membership_b,
            )
        with self.assertNumQueries(0):
            self.assertIs(get_access_context(request), context_a)
            self.assertIs(
                get_access_context(
                    request,
                    business=self.business_b,
                    membership=membership_b,
                ),
                context_b,
            )

        self.assertEqual(context_a.business, self.business_a)
        self.assertEqual(context_b.business, self.business_b)
        self.assertNotEqual(context_a.subscription.business_id, context_b.subscription.business_id)


class TemplateCapabilityTests(AccessFoundationTestCase):
    def test_context_uses_loaded_subscription_without_database_queries(self):
        self.set_features(feature_sales=True)
        subscription = Subscription.objects.select_related("plan").get(business=self.business_a)
        request = self.request()
        request.subscription = subscription
        request._subscription_resolved = True
        with self.assertNumQueries(0):
            context = subscription_capabilities(request)
        self.assertIn("pos_core", context["effective_modules"])

    def test_disabled_modules_are_absent_and_reported_safely(self):
        self.set_features(feature_sales=True, feature_inventory=False)
        context = subscription_capabilities(self.request())
        self.assertNotIn("inventory", context["effective_modules"])
        self.assertFalse(context["module_capabilities"]["inventory"].enabled)
        self.assertEqual(
            context["module_capabilities"]["inventory"].denial_code,
            DenialCode.MODULE_DISABLED.value,
        )

    def test_subscription_access_mode_is_exposed(self):
        self.set_status(Subscription.Status.CANCELLED)
        context = subscription_capabilities(self.request())
        self.assertEqual(context["subscription_access_mode"], AccessMode.READ_ONLY.value)

    def test_anonymous_context_is_safe_and_query_free(self):
        request = self.factory.get("/accounts/login/")
        request.user = AnonymousUser()
        request.business = None
        request.membership = None
        with self.assertNumQueries(0):
            capabilities = subscription_capabilities(request)
        self.assertEqual(capabilities["effective_modules"], frozenset())
        self.assertEqual(capabilities["subscription_access_mode"], AccessMode.DENIED.value)

    def test_authenticated_public_context_is_safe_and_query_free(self):
        request = self.request(business=None, membership=None)
        with self.assertNumQueries(0):
            capabilities = subscription_capabilities(request)
        self.assertEqual(capabilities["effective_modules"], frozenset())
        self.assertEqual(capabilities["subscription_access_mode"], AccessMode.DENIED.value)

    def test_platform_admin_without_membership_is_safe_and_query_free(self):
        platform_admin = User.objects.create_user(
            email="platform@example.com",
            password="StrongPass123!",
            full_name="Platform Admin",
            is_platform_admin=True,
        )
        request = self.request(
            user=platform_admin,
            business=None,
            membership=None,
        )
        with self.assertNumQueries(0):
            context = get_access_context(request)
        self.assertEqual(context.mode, AccessMode.DENIED)
        self.assertEqual(context.denial.code, DenialCode.MEMBERSHIP_REQUIRED)

    def test_superuser_without_membership_has_no_tenant_module_access(self):
        superuser = User.objects.create_superuser(
            email="superuser@example.com",
            password="StrongPass123!",
            full_name="Superuser",
        )
        request = self.request(
            user=superuser,
            business=None,
            membership=None,
        )
        with self.assertNumQueries(0):
            context = get_access_context(request)
        self.assertEqual(context.mode, AccessMode.DENIED)
        self.assertEqual(context.denial.code, DenialCode.MEMBERSHIP_REQUIRED)


class MiddlewareCacheTests(AccessFoundationTestCase):
    def setUp(self):
        super().setUp()
        self.middleware = SubscriptionMiddleware(lambda request: HttpResponse())

    def test_middleware_primes_subscription_plan_and_legacy_relation_cache(self):
        self.set_features(feature_sales=True)
        fresh_business = self.business_a.__class__.objects.get(pk=self.business_a.pk)
        request = self.request(business=fresh_business)

        with self.assertNumQueries(1):
            self.middleware.process_request(request)

        self.assertEqual(request.subscription.business_id, fresh_business.pk)
        self.assertIn("plan", request.subscription._state.fields_cache)
        with self.assertNumQueries(0):
            self.assertIs(fresh_business.subscription, request.subscription)
            self.assertTrue(subscription_services.has_feature(fresh_business, "sales"))
            capabilities = subscription_capabilities(request)
        self.assertIn("pos_core", capabilities["effective_modules"])

    def test_middleware_negative_caches_missing_subscription(self):
        self.subscription.delete()
        fresh_business = self.business_a.__class__.objects.get(pk=self.business_a.pk)
        request = self.request(business=fresh_business)

        with self.assertNumQueries(1):
            self.middleware.process_request(request)
        with self.assertNumQueries(0):
            self.assertIsNone(subscription_services.get_subscription(fresh_business))

    def test_anonymous_login_uses_no_subscription_query(self):
        request = self.factory.get("/accounts/login/")
        request.user = AnonymousUser()
        request.business = None
        request.membership = None
        with self.assertNumQueries(0):
            self.middleware.process_request(request)
            subscription_capabilities(request)

    def test_public_no_business_request_uses_no_subscription_query(self):
        request = self.request(business=None, membership=None)
        with self.assertNumQueries(0):
            self.middleware.process_request(request)
            subscription_capabilities(request)


class FoundationAPIView(APIView):
    permission_classes = [HasSubscriptionModuleAccess]
    required_modules = ("pos_core",)
    required_permission = "sales.view"

    def get(self, request):
        return Response({"allowed": True})

    def post(self, request):
        return Response({"allowed": True})


class FoundationObjectAPIView(FoundationAPIView):
    target = None

    def get(self, request):
        self.check_object_permissions(request, self.target)
        return Response({"allowed": True})


class BranchScopedFoundationAPIView(FoundationObjectAPIView):
    def has_api_object_scope(self, request, context, obj):
        branch = obj if isinstance(obj, Branch) else obj.branch
        return context.membership.can_access_branch(branch)


class APIPermissionFoundationTests(AccessFoundationTestCase):
    view = staticmethod(FoundationAPIView.as_view())

    def test_api_module_disabled_denies(self):
        self.set_features(feature_sales=True, feature_api_access=False)
        response = self.view(self.api_request())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(str(response.data["code"]), DenialCode.MODULE_DISABLED.value)

    def test_underlying_module_disabled_denies(self):
        self.set_features(feature_sales=False, feature_api_access=True)
        response = self.view(self.api_request())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(str(response.data["code"]), DenialCode.MODULE_DISABLED.value)

    def test_api_and_underlying_module_allow(self):
        self.set_features(feature_sales=True, feature_api_access=True)
        response = self.view(self.api_request())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["allowed"])

    def test_missing_or_ambiguous_business_context_fails_safely(self):
        role_b = self.business_b.roles.get(is_owner=True)
        Membership.objects.create(
            business=self.business_b,
            user=self.owner_a,
            role=role_b,
        )
        self.set_features(feature_sales=True, feature_api_access=True)
        response = self.view(self.api_request(explicit_context=False))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(str(response.data["code"]), DenialCode.MEMBERSHIP_REQUIRED.value)

    def test_owner_does_not_bypass_underlying_module(self):
        self.assertTrue(self.membership.role.is_owner)
        self.set_features(feature_sales=False, feature_api_access=True)
        response = self.view(self.api_request())
        self.assertEqual(response.status_code, 403)

    def test_read_only_api_get_allowed_and_post_denied(self):
        self.set_features(feature_sales=True, feature_api_access=True)
        self.set_status(Subscription.Status.PAST_DUE)
        self.assertEqual(self.view(self.api_request("get")).status_code, 200)
        response = self.view(self.api_request("post"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(str(response.data["code"]), DenialCode.SUBSCRIPTION_READ_ONLY.value)

    def test_api_explicit_read_cannot_weaken_post(self):
        self.set_features(feature_sales=True, feature_api_access=True)
        self.set_status(Subscription.Status.PAST_DUE)

        class ExplicitReadAPIView(FoundationAPIView):
            access_action = "read"

        response = ExplicitReadAPIView.as_view()(self.api_request("post"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(str(response.data["code"]), DenialCode.SUBSCRIPTION_READ_ONLY.value)

    def test_empty_api_module_requirements_raise_configuration_error(self):
        class EmptyModulesAPIView(APIView):
            permission_classes = [HasSubscriptionModuleAccess]
            required_modules = []

            def get(self, request):
                return Response({"allowed": True})

        with self.assertRaises(ImproperlyConfigured):
            EmptyModulesAPIView.as_view()(self.api_request())

    def test_api_checks_every_required_module(self):
        self.set_features(
            feature_sales=True,
            feature_inventory=False,
            feature_api_access=True,
        )

        class MultipleModulesAPIView(FoundationAPIView):
            required_modules = ("pos_core", "inventory")

        response = MultipleModulesAPIView.as_view()(self.api_request())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(str(response.data["code"]), DenialCode.MODULE_DISABLED.value)

    def test_unauthenticated_api_request_returns_401(self):
        request = self.api_factory.get("/api/foundation/")
        response = self.view(request)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(str(response.data["code"]), DenialCode.AUTHENTICATION_REQUIRED.value)

    def test_api_scope_hook_denies_after_entitlements_pass(self):
        self.set_features(feature_sales=True, feature_api_access=True)

        class ScopeDeniedAPIView(FoundationAPIView):
            def has_api_scope(self, request, context):
                return False

        response = ScopeDeniedAPIView.as_view()(self.api_request())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(str(response.data["code"]), DenialCode.SCOPE_DENIED.value)

    def test_same_tenant_api_object_is_allowed(self):
        self.set_features(feature_sales=True, feature_api_access=True)
        response = FoundationObjectAPIView.as_view(target=self.product_a)(self.api_request())
        self.assertEqual(response.status_code, 200)

    def test_cross_tenant_api_object_uses_404_secrecy(self):
        self.set_features(feature_sales=True, feature_api_access=True)
        response = FoundationObjectAPIView.as_view(target=self.product_b)(self.api_request())
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("code", response.data)

    def test_out_of_scope_branch_and_warehouse_use_404_secrecy(self):
        self.set_features(feature_sales=True, feature_api_access=True)
        restricted_branch = Branch.objects.create(
            business=self.business_a,
            name="Restricted Branch",
            code="REST",
        )
        restricted_warehouse = Warehouse.objects.create(
            business=self.business_a,
            branch=restricted_branch,
            name="Restricted Warehouse",
            code="REST-WH",
        )
        self.membership.branches.add(self.branch_a)

        for target in (restricted_branch, restricted_warehouse):
            with self.subTest(target=target):
                response = BranchScopedFoundationAPIView.as_view(target=target)(self.api_request())
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("code", response.data)

    def test_object_entitlement_failure_remains_structured_403(self):
        self.set_features(feature_sales=True, feature_api_access=False)
        response = FoundationObjectAPIView.as_view(target=self.product_a)(self.api_request())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(str(response.data["code"]), DenialCode.MODULE_DISABLED.value)
