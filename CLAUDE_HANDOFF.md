# NexaPOS — Claude Handoff

A complete orientation so a **brand-new Claude session** can continue
development without losing context. Read this first, then PROJECT_STATUS.md
and CHANGELOG.md.

---

## 0. Working environment (read carefully)

- **OS:** Windows. Project root: `C:\Users\Admin\POS Project`.
- **Always use the venv Python**, not bare `python`:
  `C:\Users\Admin\POS Project\.venv\Scripts\python.exe`
- **Shell:** PowerShell. Avoid `cd`-prefixed compound commands; the tool
  already runs in the project dir.
- **Default settings module:** `config.settings.development` (SQLite).
  `manage.py` sets this automatically.
- **Run server:** `manage.py runserver` → http://127.0.0.1:8000
  (a preview launch config "nexapos" uses port 8799).
- **`git commit` prints exit 255** from CRLF stderr warnings but the commit
  **does succeed** — confirm with `git log --oneline`. Set git identity is
  already configured locally.
- **Commit/push only when asked.** End commit messages with the
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` trailer.
- There is an unrelated `C:\Users\Admin\nexa-website` (marketing site) —
  **not part of this project**; ignore it.

---

## 1. Folder structure

```
POS Project/
  config/
    settings/{base,development,production}.py   # env-driven, split settings
    urls.py  wsgi.py  asgi.py  celery.py
  apps/                       # 18 Django apps (all under apps/)
    core/         # TenantModel, managers, middleware, permissions registry,
                  # money helpers, currencies, imports helper, templatetags,
                  # error pages, seed_demo command
    accounts/     # custom User (email login), Role, Membership, LoginHistory
    tenants/      # Business, BusinessSettings, registration, onboarding, settings
    subscriptions/# Plan, Subscription, Coupon, SubscriptionPayment, limits,
                  # SubscriptionMiddleware, helpers (upgrade page)
    branches/     # Branch, Warehouse
    catalog/      # Category, Brand, Unit, TaxRate, Product, ProductVariant,
                  # import/export, archive/delete
    inventory/    # StockLevel, StockMovement(ledger), transfers, adjustments,
                  # counts, import/export
    customers/    # Customer, CustomerGroup, CustomerPayment, statements
    suppliers/    # Supplier, SupplierPayment
    purchases/    # Purchase, PurchaseItem, returns, PO docs (print/pdf/email/share)
    sales/        # PaymentMethod, Sale, SaleItem, SalePayment, InvoiceSequence,
                  # HeldSale, SaleReturn; POS endpoints; complete_sale/void/return
    registers/    # CashRegister, Shift; open/close/X/Z
    expenses/     # ExpenseCategory, Expense (approval workflow)
    reports/      # query registry, dashboard view, exports (csv/xlsx/pdf), pdf.py
    notifications/# Notification + notify services
    audit/        # immutable AuditLog + log() service
    platformadmin/# SaaS super-admin: dashboard, business mgmt, plans, coupons,
                  # announcements, support access, login-as-owner, settings,
                  # PlatformConfig, SupportAccessGrant, Announcement,
                  # SupportSessionMiddleware
    api/          # DRF v1 (serializers, viewsets, token, health)
  templates/      # layouts/, components/, auth/, dashboard/, pos/, invoices/,
                  # reports/, customers/, catalog/, inventory/, purchases/,
                  # suppliers/, registers/, expenses/, platformadmin/, errors/, emails/
  static/         # vendored css/js (bootstrap, alpine, chart.js, icons), app.css, sw.js
  tests/          # 15 test modules (see §9); base.py = shared two-tenant fixture
  scripts/        # smoke_test.py (authenticated all-pages check)
  requirements/   # base.txt, development.txt, production.txt
  docker/  Dockerfile  docker-compose.yml
  README.md ARCHITECTURE.md SECURITY.md DEPLOYMENT.md API.md
  PROJECT_PLAN.md DEVELOPMENT_PROGRESS.md PROJECT_STATUS.md
  CHANGELOG.md CLAUDE_HANDOFF.md
```

---

## 2. Key files (where to look first)

| Concern | File |
|---|---|
| Tenant base model / managers | `apps/core/models.py` (`TenantModel`, `for_business`) |
| Tenant request resolution | `apps/core/middleware.py` (`BusinessMiddleware`) |
| Object lookup (404 isolation) | `apps/core/mixins.py` (`get_tenant_object`, mixins) |
| Permission codes + default roles | `apps/core/permissions.py` |
| View guards | `apps/core/decorators.py` (`require_permission`, `business_required`) |
| Money formatting | `apps/core/money.py`, `apps/core/templatetags/money_tags.py` |
| Currencies (symbols/precision) | `apps/core/currencies.py` |
| Shared import parser | `apps/core/imports.py` |
| **Sale completion / void / return / payments** | `apps/sales/services.py` |
| **Invoice numbering** | `apps/sales/services.py` → `next_invoice_number`, `LIFETIME_SEQUENCE` |
| POS endpoints | `apps/sales/views.py` |
| **Stock ledger** | `apps/inventory/services.py` (`record_movement`), `workflows.py` |
| Subscription limits / status | `apps/subscriptions/services.py`, `models.py` (`display_status`) |
| Subscription enforcement | `apps/subscriptions/middleware.py` |
| Platform admin | `apps/platformadmin/views.py`, `middleware.py`, `models.py` |
| Audit | `apps/audit/services.py` (`log()`), `models.py` |
| Reports registry | `apps/reports/queries.py` (`REPORTS`, `REPORT_GROUPS`) |
| Dashboard | `apps/reports/views.py` → `dashboard` |
| Demo data | `apps/core/management/commands/seed_demo.py` |
| URL map | `config/urls.py` (+ each app's `urls.py`) |

---

## 3. Business logic invariants (do not break)

1. **Money is always `Decimal`** stored at 3 dp; never float. Use
   `apps.core.money` helpers; display with `{% money %}`.
2. **Stock changes only via `inventory.services.record_movement`** — it
   writes the ledger + cached level atomically and enforces the negative-
   stock policy. Never set `StockLevel.quantity` directly.
3. **Completed sales are immutable.** Corrections = void / return / add
   payment. Item prices, costs, tax are snapshots.
4. **All financial/stock operations are wrapped in `transaction.atomic`.**
5. **Every business query goes through `for_business(...)`** and object
   fetches through `get_tenant_object(...)` (returns 404 cross-tenant).
6. **Invoice number = configured Business Settings prefix + lifetime
   3-digit counter** (`PREFIX-NNN`), never year-based. Optional per-branch.
7. **Audit every critical action** via `apps.audit.services.log(...)`
   (captures user, business, IP, user-agent, before/after).
8. **Permissions enforced in the backend** (decorators/mixins), never only
   by hiding menus.

---

## 4. Multi-tenant architecture (detail)

- `TenantModel`: abstract base — `public_id` (UUID, used in all URLs),
  `business` FK, `created_at/updated_at`, `TenantManager` with
  `.for_business(business)`.
- `BusinessMiddleware` (after auth, before subscription) sets
  `request.business` / `request.membership` from the session-pinned active
  membership; filters out inactive/suspended businesses (so suspended
  tenants can't access anything).
- `Membership.has_perm(code)` + `allowed_branch_ids` enforce role and
  branch scoping. Owner role implies all permissions.
- Tenant-scoped unique constraints (conditional `UniqueConstraint`s)
  prevent cross-tenant collisions while allowing reuse across tenants.
- **Tests:** `tests/test_tenancy.py` proves Business A can't read/edit/
  export Business B via pages, JSON endpoints, reports, exports or API.

---

## 5. Platform admin system

- Access gate: `apps.platformadmin.views.platform_admin_required` →
  allows `user.is_platform_staff` (= `is_platform_admin` **or**
  `is_superuser`). Django superusers always qualify, even with no business.
- Lives under `/platform/`. Separate base template
  `templates/platformadmin/_base.html` (has its own `extra_js` block —
  needed for Chart.js on the dashboard).
- **Dashboard** (`dashboard` view): MRR, revenue this/last/total,
  business counts by `display_status`, user metrics, plan-distribution +
  status charts, expiring-soon list.
- **Business management:** list (status badges), detail (usage, owner,
  suspension/reactivation audit info), actions = suspend / activate
  (reactivate) / extend / extend_trial — all audited; suspend/reactivate
  store `suspended_by`/`reactivated_by`/dates.
- **Login-As-Owner (support mode):** `support_login_as` sets a
  `support_session` in the session; `SupportSessionMiddleware` swaps
  `request.user` to the owner (keeps `request.support_admin`). A sticky
  banner (`templates/layouts/base.html`) + `support_exit` view end it.
  Reason required; owner notified; audited on enter & exit (with duration).
- **Platform settings** (`/platform/settings/`): `PlatformConfig.expiry_mode`
  (`read_only` | `suspend`).
- **Status badges:** `apps/platformadmin/templatetags/platform_tags.py`
  (`sub_status_badge`) + `.sub-*` CSS in `static/css/app.css`
  (trial=yellow, active=green, expiring=orange, expired=red,
  suspended=dark red).

---

## 6. Subscription system

- `Plan` — prices, trial days, per-resource limits (0 = unlimited),
  feature flags (`feature_*`), support level. Managed by platform admin;
  seed creates an editable Starter plan if none exist.
- `Subscription` — status (trial/active/grace/past_due/suspended/
  cancelled/expired); computed `effective_status`, `display_status`
  (adds `expiring_soon` within 7 days and `suspended`), `is_operational`,
  `is_expiring_soon`, `days_until_expiry`.
- **Enforcement:**
  - `services.check_limit(business, resource)` / `limit_state(...)` at
    every create path; over-limit → upgrade page (`subscriptions/helpers.py`).
  - `services.has_feature(business, feature)` gates feature-flagged modules.
  - `SubscriptionMiddleware` makes non-operational subs read-only (Option A)
    or fully blocked (Option B) per `PlatformConfig.expiry_mode`. Allowlist
    keeps accounts/subscription/platform/admin reachable.
- **Never delete tenant data on expiry/suspension** — only block creation.
- `SubscriptionPayment` records platform revenue (manual/bank/gateway).

---

## 7. Audit system

- `apps.audit.models.AuditLog` — **append-only** (`save()` refuses
  updates; no business-facing delete). Fields: business, user, action,
  module, object_type/id, description, old_values/new_values, ip_address,
  user_agent, created_at.
- Write via `apps.audit.services.log(action, *, business=, user=,
  request=, module=, obj=, description=, old_values=, new_values=)` —
  pulls user/business/IP from `request` when given.
- Business admins view their tenant's log at `/audit/`; platform actions
  at `/platform/audit/`.
- Audited actions include: auth login/logout/lockout, sale
  completed/voided/returned/deleted/payment_added/delivery_status,
  product saved/archived/restored/deleted/exported/imported, stock
  movements/adjustments/transfers/counts, customer saved/payment/
  statement_exported/imported, expense lifecycle, shift open/close/
  approve/reopen, and platform business_registered/suspended/reactivated/
  subscription_extended/trial_extended/login_as_owner/
  support_session_ended/support_access_granted+revoked/settings_changed/
  plan_saved.

---

## 8. Demo accounts

Run `python manage.py seed_demo` (idempotent; refuses if "Demo Business"
exists). Creates a clearly-marked demo tenant with 2 branches, warehouse,
catalog (incl. variants), a received purchase, sales, a return, a transfer,
an expense, and a closed shift.

| Role | Email | Password |
|---|---|---|
| Owner | `demo-owner@example.com` | `DemoPass123!` |
| Manager | `demo-manager@example.com` | `DemoPass123!` |
| Cashier | `demo-cashier@example.com` | `DemoPass123!` |

**Platform super-admin:** not seeded — create with
`python manage.py createsuperuser` (superusers are platform staff →
`/platform/`). The dev DB also has a real `admin@nexapos.com` superuser.

> Demo credentials are for local development only; never ship them.

---

## 9. Tests & verification

- `python manage.py test` → **217 tests, all passing.** Shared fixture:
  `tests/base.py` (`TenantTestCase`) builds Business A + Business B with
  users, products and stock — use it for new tests.
- Modules: `test_auth, test_tenancy, test_pos, test_inventory, test_returns,
  test_shifts, test_subscriptions, test_finance, test_exports,
  test_bugfixes, test_phase_update, test_phase21, test_invoice_prefix,
  test_platform_access, test_platform_enhancements`.
- **Always run the suite after changes** and add regression tests for any
  fix. After migrations: `manage.py migrate` then `manage.py test`.
- `python manage.py shell -c "exec(open('scripts/smoke_test.py').read())"`
  renders every major business page (needs demo data).
- Migration safety check: `manage.py makemigrations --check --dry-run`
  ("No changes detected" = clean).

---

## 10. Current status

- Version **1.4.0**, head `2430740`. Core is production-ready; 217 tests
  green; `manage.py check` clean.
- Last delivered: platform reactivation, subscription status system,
  SaaS metrics + charts, Login-As-Owner support mode, configurable expiry
  mode (read-only/suspend), extended audit. See CHANGELOG.md.
- All bug-fix sprints to date are closed (payment precision, customer
  detail crash, PO documents, currency formatting + registry, subscription
  limit enforcement, invoice prefix → simple format, platform superuser
  access).

---

## 11. Next-phase recommendations (with where to start)

1. **Payment gateway (Stripe):** extend `subscriptions.SubscriptionPayment`
   (`method="gateway"`) + a webhook/checkout view; activate subs on success.
   For POS card capture, add a gateway PaymentMethod kind + service hook in
   `complete_sale`.
2. **Email + Celery sweeps:** set SMTP in settings; add Celery beat tasks
   for trial-expiry, expiry, credit-overdue notifications (services already
   exist in subscriptions/customers).
3. **Quotations module** + **Balance Sheet** report (the only requested
   document types not yet built) — model a `Quotation` mirroring `Sale`,
   convertible to a sale.
4. **i18n / Arabic + RTL:** wrap remaining strings, add `locale/`
   catalogs, RTL CSS variant; invoice templates are translation-ready.
5. **Async exports / 10k+ import jobs:** move large exports/imports to
   Celery with a results page.
6. **Dark mode** theme pass over `static/css/app.css`.

When implementing: follow the invariants in §3, keep multi-tenant
isolation (§4), add tests using `tests/base.py`, run the full suite, update
CHANGELOG.md + PROJECT_STATUS.md, and commit only when asked.
