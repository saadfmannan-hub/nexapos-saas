# Security

## Tenant isolation

- Every tenant-owned row carries a mandatory `business` foreign key.
- All queries flow through `Model.objects.for_business(request.business)`.
- Object lookups use UUID `public_id`s and return **404** (never 403) on
  cross-tenant access so identifiers cannot be probed.
- Forms restrict all foreign-key choices to the active tenant.
- JSON/AJAX endpoints (POS search, checkout, item search) re-validate
  every id server-side against the tenant.
- File uploads under `expenses/` and `purchases/` are served through a
  protected view that checks tenant ownership; direct media access to
  another tenant's files is denied.
- Automated cross-tenant tests: `tests/test_tenancy.py`.

## Authentication & sessions

- Email login, Django PBKDF2 password hashing, password validators
  (length ≥ 8, common-password, numeric checks).
- Failed-login rate limiting: account locks for 15 minutes after 5
  failures (configurable via `LOGIN_MAX_FAILED_ATTEMPTS` /
  `LOGIN_LOCKOUT_MINUTES`). All attempts are recorded with IP and user
  agent in `LoginHistory` and shown to the user.
- "Remember me" controls session expiry; otherwise sessions end with the
  browser. Session cookies are HTTPOnly; CSRF protection everywhere.
- Password reset uses Django's signed-token flow (console email backend
  in development; SMTP in production).
- Two-factor authentication: architecture-ready (user model and login
  flow are isolated in `apps/accounts`); not enabled in v1.

## Authorization

- Role-based permissions enforced **in views/services**, never only in
  templates. Branch restrictions enforced server-side
  (`Membership.can_access_branch`).
- Platform staff (`is_platform_admin`) use a separate dashboard and
  cannot see tenant business data without a reasoned, time-limited,
  audited `SupportAccessGrant` (owner notified per business setting,
  revocable).

## Audit & immutability

- `AuditLog` is append-only (`save()` refuses updates) and has no
  tenant-facing delete path. Logins, sales, voids, returns, stock
  changes, settings, subscription and platform actions are recorded with
  user, IP, user agent and before/after values where relevant.
- Completed sales, payments and stock movements are immutable snapshots.

## Production checklist

- [ ] `DEBUG=False`, strong unique `SECRET_KEY` (production settings
      refuse to boot with the dev key)
- [ ] `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` set
- [ ] HTTPS terminated at nginx; `SECURE_SSL_REDIRECT=True`,
      secure cookies, HSTS enabled (defaults in production settings)
- [ ] PostgreSQL with least-privilege DB user; automated backups
- [ ] Media directory not directly served for sensitive folders —
      replicate `apps.core.views.protected_media` rules via
      `X-Accel-Redirect` if serving media from nginx
- [ ] SMTP credentials via environment, never committed
- [ ] Regular dependency updates (`pip list --outdated`)
- [ ] Review platform admin accounts; enable support-access notifications

## Secret management

All secrets come from environment variables (see `.env.example`). `.env`
is git-ignored. Never commit real credentials; rotate `SECRET_KEY` only
with a session-invalidation plan.

## Backup security

Database dumps contain all tenants' data: encrypt at rest, restrict
access, and never expose restore functionality inside the business
dashboard. Platform restore actions must be manual, restricted and
audited (see DEPLOYMENT.md).
