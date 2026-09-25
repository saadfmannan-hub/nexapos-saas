import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST
from rest_framework.authtoken.models import Token

from apps.audit import services as audit
from apps.audit.services import client_ip
from apps.core.mixins import get_tenant_object
from apps.subscriptions import services as subscriptions
from apps.subscriptions.access import AccessAction, get_access_context, require_access
from apps.subscriptions.decorators import module_permission_required

from .forms import (
    EmployeeForm,
    LoginForm,
    ProfileForm,
    RoleForm,
    StyledPasswordChangeForm,
    StyledSetPasswordForm,
)
from .models import LoginHistory, Membership, Role, User
from .services import post_login_redirect, resolve_user_home_route

security_log = logging.getLogger("nexapos.security")


def _custom_roles_enabled(request):
    return get_access_context(request).has_module("custom_roles")


def _require_custom_role_assignment(request, *, selected_role, current_role=None):
    """Gate only selecting a new custom role; preserve existing assignments."""

    if (
        not selected_role.is_system
        and not selected_role.is_owner
        and (current_role is None or selected_role.pk != current_role.pk)
    ):
        require_access(
            request,
            "custom_roles",
            permission_code="users.manage",
            action=AccessAction.WRITE,
        )


def _protected_identity_changes(form, user):
    """Return identity fields a non-self admin tried to change."""

    changes = []
    if form.cleaned_data["full_name"] != user.full_name.strip():
        changes.append("full_name")
    if form.cleaned_data["email"] != User.objects.normalize_email(user.email):
        changes.append("email")
    if form.cleaned_data["phone"] != user.phone.strip():
        changes.append("phone")
    if form.cleaned_data["password"]:
        changes.append("password")
    return changes


def _add_protected_identity_errors(form, fields):
    message = (
        "Only the account holder can change identity or password for an owner "
        "or shared account."
    )
    for field in fields:
        form.add_error(field, message)


def login_view(request):
    if request.user.is_authenticated:
        return post_login_redirect(request, next_url=request.GET.get("next"))
    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].lower()
        password = form.cleaned_data["password"]
        user = User.objects.filter(email__iexact=email).first()

        def record(success, u=None):
            LoginHistory.objects.create(
                user=u, email_attempted=email, success=success,
                ip_address=client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
            )

        if user and user.is_locked:
            record(False, user)
            messages.error(request, "Account temporarily locked due to repeated "
                                    "failed logins. Try again later.")
            return render(request, "auth/login.html", {"form": form})

        if user and user.is_active and user.check_password(password):
            user.failed_login_attempts = 0
            user.locked_until = None
            user.save(update_fields=["failed_login_attempts", "locked_until"])
            auth_login(request, user)
            if not form.cleaned_data["remember_me"]:
                request.session.set_expiry(0)  # browser session only
            record(True, user)
            audit.log("auth.login", user=user, request=request, module="accounts",
                      description=f"{user.email} signed in.")
            return post_login_redirect(request, next_url=request.GET.get("next"))

        # Failure path
        if user:
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= settings.LOGIN_MAX_FAILED_ATTEMPTS:
                user.locked_until = timezone.now() + timezone.timedelta(
                    minutes=settings.LOGIN_LOCKOUT_MINUTES
                )
                user.failed_login_attempts = 0
                security_log.warning("Account locked after failed logins: %s", email)
            user.save(update_fields=["failed_login_attempts", "locked_until"])
        record(False, user)
        security_log.info("Failed login attempt for %s from %s", email, client_ip(request))
        messages.error(request, "Invalid email or password.")
    return render(request, "auth/login.html", {"form": form})


def no_access_view(request):
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    route_name = resolve_user_home_route(
        request.user, membership=getattr(request, "membership", None), request=request
    )
    if route_name != "accounts:no_access":
        return redirect(route_name)
    return render(request, "accounts/no_access.html")


@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        audit.log("auth.logout", user=request.user, request=request,
                  module="accounts", description=f"{request.user.email} signed out.")
    auth_logout(request)
    return redirect("accounts:login")


def profile_view(request):
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    form = ProfileForm(request.POST or None, instance=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Profile updated.")
        return redirect("accounts:profile")
    history = request.user.login_history.all()[:15]
    return render(request, "auth/profile.html", {"form": form, "history": history})


def change_password_view(request):
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    if getattr(request, "support_admin", None) is not None:
        raise PermissionDenied
    form = StyledPasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = form.save(commit=False)
            user.must_change_password = False
            user.save(update_fields=["password", "must_change_password"])
            Token.objects.filter(user=user).delete()
        update_session_auth_hash(request, user)
        audit.log("auth.password_changed", user=user, request=request,
                  module="accounts", description="Password changed.")
        messages.success(request, "Password changed successfully.")
        return redirect("accounts:profile")
    return render(request, "auth/change_password.html", {
        "form": form,
        "password_change_required": request.user.must_change_password,
    })


class PasswordResetView(auth_views.PasswordResetView):
    template_name = "auth/password_reset.html"
    email_template_name = "emails/password_reset_email.html"
    subject_template_name = "emails/password_reset_subject.txt"
    success_url = reverse_lazy("accounts:password_reset_done")


class PasswordResetDoneView(auth_views.PasswordResetDoneView):
    template_name = "auth/password_reset_done.html"


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "auth/password_reset_confirm.html"
    success_url = reverse_lazy("accounts:password_reset_complete")
    form_class = StyledSetPasswordForm


class PasswordResetCompleteView(auth_views.PasswordResetCompleteView):
    template_name = "auth/password_reset_complete.html"


# ---------------------------------------------------------------------------
# Employee / role management (business admin)
# ---------------------------------------------------------------------------
@module_permission_required("pos_core", "users.manage")
def user_list(request):
    memberships = list(
        Membership.objects.for_business(request.business)
        .select_related("user", "role")
        .prefetch_related("branches")
        .order_by("user__full_name", "user__email")
    )
    active_memberships = [membership for membership in memberships if membership.is_active]
    inactive_memberships = [
        membership for membership in memberships if not membership.is_active
    ]
    current, limit, _allowed = subscriptions.limit_state(request.business, "users")
    return render(request, "accounts/user_list.html", {
        "active_memberships": active_memberships,
        "inactive_memberships": inactive_memberships,
        "active_user_count": len(active_memberships),
        "inactive_user_count": len(inactive_memberships),
        "active_nav": "users",
        "user_count": current, "user_limit": limit,
    })


def _lock_and_check_user_seat(business):
    """Serialize active-seat allocation for creates and reactivations."""
    from apps.tenants.models import Business

    Business.objects.select_for_update().only("pk").get(pk=business.pk)
    subscriptions.check_limit(business, "users")


@module_permission_required("pos_core", "users.manage")
def user_create(request):
    custom_roles_enabled = _custom_roles_enabled(request)
    form = EmployeeForm(
        request.business,
        request.POST or None,
        custom_roles_enabled=custom_roles_enabled,
    )
    if request.method == "POST" and form.is_valid():
        _require_custom_role_assignment(
            request,
            selected_role=form.cleaned_data["role"],
        )
        integrity_stage = None
        try:
            with transaction.atomic():
                if form.cleaned_data["is_active"]:
                    _lock_and_check_user_seat(request.business)
                email = form.cleaned_data["email"]
                integrity_stage = "user"
                user = User.objects.create_user(
                    email=email,
                    password=form.cleaned_data["password"],
                    full_name=form.cleaned_data["full_name"],
                    phone=form.cleaned_data["phone"],
                )
                integrity_stage = "membership"
                membership = Membership.objects.create(
                    business=request.business,
                    user=user,
                    role=form.cleaned_data["role"],
                    is_active=form.cleaned_data["is_active"],
                )
                membership.branches.set(form.cleaned_data["branches"])
                audit.log("user.created", request=request, module="accounts", obj=user,
                          description=f"Employee {user.email} added with role "
                                      f"{form.cleaned_data['role'].name}.")
        except (
            subscriptions.LimitExceeded,
            subscriptions.SubscriptionInactive,
        ) as exc:
            from apps.subscriptions.helpers import limit_blocked_response

            return limit_blocked_response(request, exc, resource="users")
        except IntegrityError:
            if (
                integrity_stage != "user"
                or not User.objects.filter(email=email).exists()
            ):
                raise
            form.add_error("email", "An account with this email already exists.")
        else:
            messages.success(request, "Employee added.")
            return redirect("accounts:user_list")
    return render(request, "accounts/user_form.html",
                  {"form": form, "active_nav": "users", "creating": True})


@module_permission_required("pos_core", "users.manage")
def user_edit(request, public_id):
    membership = get_tenant_object(Membership, request.business, public_id=public_id)
    custom_roles_enabled = _custom_roles_enabled(request)
    initial = {
        "full_name": membership.user.full_name,
        "email": membership.user.email,
        "phone": membership.user.phone,
        "role": membership.role,
        "branches": list(membership.branches.all()),
        "is_active": membership.is_active,
    }
    form = EmployeeForm(request.business, request.POST or None,
                        editing=membership, initial=initial,
                        custom_roles_enabled=custom_roles_enabled)
    if request.method == "POST" and form.is_valid():
        _require_custom_role_assignment(
            request,
            selected_role=form.cleaned_data["role"],
            current_role=membership.role,
        )
        reactivating = (
            not membership.is_active
            and form.cleaned_data["is_active"]
            and not membership.role.is_owner
        )
        protected_identity_fields = []
        identity_email_conflict = False
        try:
            with transaction.atomic():
                if reactivating:
                    _lock_and_check_user_seat(request.business)
                membership = (
                    Membership.objects.select_for_update()
                    .select_related("user", "role")
                    .get(pk=membership.pk, business=request.business)
                )
                user = User.objects.select_for_update().get(pk=membership.user_id)
                identity_protected = (
                    request.user.pk != user.pk
                    and (
                        membership.role.is_owner
                        or Membership.objects.filter(user=user)
                        .exclude(pk=membership.pk)
                        .exists()
                    )
                )
                if identity_protected:
                    protected_identity_fields = _protected_identity_changes(form, user)
                if not protected_identity_fields:
                    old_role = membership.role.name
                    old_email = user.email
                    old_active = membership.is_active
                    if not identity_protected:
                        user.full_name = form.cleaned_data["full_name"]
                        user.email = form.cleaned_data["email"]
                        user.phone = form.cleaned_data["phone"]
                        if form.cleaned_data["password"]:
                            user.set_password(form.cleaned_data["password"])
                        try:
                            with transaction.atomic():
                                user.save()
                        except IntegrityError:
                            conflicts = User.objects.filter(
                                email=form.cleaned_data["email"]
                            ).exclude(pk=user.pk)
                            if not conflicts.exists():
                                raise
                            identity_email_conflict = True
                    if not identity_email_conflict:
                        if not membership.role.is_owner:
                            membership.role = form.cleaned_data["role"]
                            membership.is_active = form.cleaned_data["is_active"]
                        membership.save()
                        membership.branches.set(form.cleaned_data["branches"])
                        audit.log(
                            "user.updated",
                            request=request,
                            module="accounts",
                            obj=user,
                            old_values={
                                "email": old_email,
                                "role": old_role,
                                "is_active": old_active,
                            },
                            new_values={
                                "email": user.email,
                                "role": membership.role.name,
                                "is_active": membership.is_active,
                            },
                            description=f"Employee {user.email} updated.",
                        )
        except (
            subscriptions.LimitExceeded,
            subscriptions.SubscriptionInactive,
        ) as exc:
            from apps.subscriptions.helpers import limit_blocked_response

            return limit_blocked_response(request, exc, resource="users")
        if protected_identity_fields:
            _add_protected_identity_errors(form, protected_identity_fields)
        elif identity_email_conflict:
            form.add_error("email", "An account with this email already exists.")
        else:
            messages.success(request, "Employee updated.")
            return redirect("accounts:user_list")
    return render(request, "accounts/user_form.html",
                  {"form": form, "active_nav": "users", "membership": membership})


@module_permission_required("pos_core", "users.manage")
def role_list(request):
    roles = Role.objects.for_business(request.business).order_by("-is_owner", "name")
    context = get_access_context(request)
    can_custom = context.has_module("custom_roles") and context.can_write
    return render(request, "accounts/role_list.html",
                  {"roles": roles, "active_nav": "users", "can_custom": can_custom})


@module_permission_required("pos_core", "users.manage", action=AccessAction.WRITE)
def role_form(request, public_id=None):
    instance = None
    if public_id:
        instance = get_tenant_object(Role, request.business, public_id=public_id)
        if instance.is_owner:
            messages.error(request, "The owner role cannot be edited.")
            return redirect("accounts:role_list")
        if not instance.is_system:
            require_access(
                request,
                "custom_roles",
                permission_code="users.manage",
                action=AccessAction.WRITE,
            )
    else:
        require_access(
            request,
            "custom_roles",
            permission_code="users.manage",
            action=AccessAction.WRITE,
        )

    form = RoleForm(request.business, request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        role = form.save(commit=False)
        role.business = request.business
        if instance is None:
            role.is_system = False
        try:
            with transaction.atomic():
                role.save()
        except IntegrityError:
            conflicts = Role.objects.for_business(request.business).filter(
                name=role.name
            )
            if role.pk:
                conflicts = conflicts.exclude(pk=role.pk)
            if not conflicts.exists():
                raise
            form.add_error("name", "A role with this name already exists.")
        else:
            audit.log("role.saved", request=request, module="accounts", obj=role,
                      description=f"Role '{role.name}' saved.")
            messages.success(request, "Role saved.")
            return redirect("accounts:role_list")
    return render(request, "accounts/role_form.html",
                  {"form": form, "active_nav": "users", "role": instance})
