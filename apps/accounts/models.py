"""Users, roles and business memberships."""
import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantManager, TimeStampedModel


class UserManager(BaseUserManager):
    use_in_migrations = True

    @classmethod
    def normalize_email(cls, email):
        """Use one canonical representation for the login identifier."""
        return super().normalize_email(email).strip().lower()

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_platform_admin", True)
        return self._create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """Platform-wide user account. Email is the login identifier.

    A user belongs to businesses through Membership records; platform
    staff are flagged with is_platform_admin and use the separate
    platform admin dashboard.
    """

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    email = models.EmailField(_("email address"), unique=True)
    full_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=30, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)  # Django admin access
    is_platform_admin = models.BooleanField(default=False)
    email_verified = models.BooleanField(default=False)

    # Brute-force protection
    failed_login_attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["full_name"]

    def __str__(self):
        return f"{self.full_name} <{self.email}>"

    @property
    def is_locked(self):
        return bool(self.locked_until and self.locked_until > timezone.now())

    @property
    def is_platform_staff(self):
        """May access the platform super-admin area. Django superusers
        always qualify, even without an explicit platform flag and even
        when they belong to no business workspace."""
        return bool(self.is_platform_admin or self.is_superuser)


class Role(TimeStampedModel):
    """A named permission bundle within one business.

    System roles are created automatically at registration; businesses
    may add custom roles. The owner role implicitly has all permissions.
    """

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    business = models.ForeignKey(
        "tenants.Business", on_delete=models.CASCADE, related_name="roles"
    )
    name = models.CharField(max_length=80)
    is_owner = models.BooleanField(default=False)
    is_system = models.BooleanField(default=False)
    permissions = models.JSONField(default=list, blank=True)

    objects = TenantManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["business", "name"], name="uniq_role_name_per_business"
            )
        ]
        ordering = ["name"]

    def __str__(self):
        return self.name


class Membership(TimeStampedModel):
    """Links a user to a business with a role and branch restrictions."""

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    business = models.ForeignKey(
        "tenants.Business", on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="memberships")
    branches = models.ManyToManyField(
        "branches.Branch", blank=True, related_name="memberships",
        help_text="Empty means access to all branches.",
    )
    is_active = models.BooleanField(default=True)

    objects = TenantManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["business", "user"], name="uniq_membership_per_business"
            )
        ]

    def __str__(self):
        return f"{self.user} @ {self.business} ({self.role})"

    @cached_property
    def permission_set(self) -> set:
        if self.role.is_owner:
            from apps.core.permissions import ALL_PERMISSION_CODES

            return set(ALL_PERMISSION_CODES)
        return set(self.role.permissions or [])

    def has_perm(self, code: str) -> bool:
        return code in self.permission_set

    @cached_property
    def allowed_branch_ids(self):
        """None means all branches; otherwise a set of branch ids."""
        ids = set(self.branches.values_list("id", flat=True))
        return ids or None

    @cached_property
    def allowed_warehouse_ids(self):
        """None means all warehouses; otherwise assigned-branch warehouses only."""
        allowed = self.allowed_branch_ids
        if allowed is None:
            return None

        from apps.branches.models import Warehouse

        return set(
            Warehouse.objects.for_business(self.business_id)
            .filter(branch_id__in=allowed)
            .values_list("id", flat=True)
        )

    def can_access_branch(self, branch) -> bool:
        if branch is None:
            return True
        if branch.business_id != self.business_id:
            return False
        allowed = self.allowed_branch_ids
        return allowed is None or branch.id in allowed


class LoginHistory(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="login_history", null=True, blank=True
    )
    email_attempted = models.EmailField(blank=True)
    success = models.BooleanField(default=False)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "login histories"
