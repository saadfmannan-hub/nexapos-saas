import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST

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


def _require_custom_role_assignment(request, *, current_role=None):
    """Gate only selecting a new custom role; preserve existing assignments."""

    if request.method != "POST":
        return
    selected = Role.objects.for_business(request.business).filter(
        pk=request.POST.get("role")
    ).first()
    if (
        selected is not None
        and not selected.is_system
        and not selected.is_owner
        and (current_role is None or selected.pk != current_role.pk)
    ):
        require_access(
            request,
            "custom_roles",
            permission_code="users.manage",
            action=AccessAction.WRITE,
        )


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
    form = StyledPasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        audit.log("auth.password_changed", user=user, request=request,
                  module="accounts", description="Password changed.")
        messages.success(request, "Password changed successfully.")
        return redirect("accounts:profile")
    return render(request, "auth/change_password.html", {"form": form})


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
    memberships = (
        Membership.objects.for_business(request.business)
        .select_related("user", "role")
        .prefetch_related("branches")
        .order_by("user__full_name")
    )
    current, limit, _allowed = subscriptions.limit_state(request.business, "users")
    return render(request, "accounts/user_list.html", {
        "memberships": memberships, "active_nav": "users",
        "user_count": current, "user_limit": limit,
    })


@module_permission_required("pos_core", "users.manage")
def user_create(request):
    from apps.subscriptions.helpers import guard_limit

    blocked = guard_limit(request, "users")
    if blocked:
        return blocked

    _require_custom_role_assignment(request)
    custom_roles_enabled = _custom_roles_enabled(request)
    form = EmployeeForm(
        request.business,
        request.POST or None,
        custom_roles_enabled=custom_roles_enabled,
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            email = form.cleaned_data["email"]
            user = User.objects.filter(email__iexact=email).first()
            if user is None:
                user = User.objects.create_user(
                    email=email,
                    password=form.cleaned_data["password"],
                    full_name=form.cleaned_data["full_name"],
                    phone=form.cleaned_data["phone"],
                )
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
        messages.success(request, "Employee added.")
        return redirect("accounts:user_list")
    return render(request, "accounts/user_form.html",
                  {"form": form, "active_nav": "users", "creating": True})


@module_permission_required("pos_core", "users.manage")
def user_edit(request, public_id):
    membership = get_tenant_object(Membership, request.business, public_id=public_id)
    _require_custom_role_assignment(request, current_role=membership.role)
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
        with transaction.atomic():
            user = membership.user
            old_role = membership.role.name
            old_email = user.email
            user.full_name = form.cleaned_data["full_name"]
            user.email = form.cleaned_data["email"]
            user.phone = form.cleaned_data["phone"]
            if form.cleaned_data["password"]:
                user.set_password(form.cleaned_data["password"])
            user.save()
            if not membership.role.is_owner:
                membership.role = form.cleaned_data["role"]
                membership.is_active = form.cleaned_data["is_active"]
            membership.save()
            membership.branches.set(form.cleaned_data["branches"])
            audit.log("user.updated", request=request, module="accounts", obj=user,
                      old_values={"email": old_email, "role": old_role},
                      new_values={"email": user.email, "role": membership.role.name},
                      description=f"Employee {user.email} updated.")
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
        role.save()
        audit.log("role.saved", request=request, module="accounts", obj=role,
                  description=f"Role '{role.name}' saved.")
        messages.success(request, "Role saved.")
        return redirect("accounts:role_list")
    return render(request, "accounts/role_form.html",
                  {"form": form, "active_nav": "users", "role": instance})
