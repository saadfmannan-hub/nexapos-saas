# NexaPOS — Project Status

**Current version:** 1.5.0
**Status:** Production-ready core; actively maintained.
**Last verified:** `manage.py check` clean · `makemigrations --check` clean ·
platform admin suite green (`tests/test_platform_enhancements`, 27/27).
**Known pre-existing test gap:** 28 money/tax/returns/shift tests currently
fail due to an earlier VAT rework (business-level `vat_enabled` vs. the
per-product tax assumed by `tests/base.py`) — unrelated to current work;
see §7 and CHANGELOG 1.5.0.
**Repo head:** see CHANGELOG.md for version history.

---

## 1. Project overview

NexaPOS is a **multi-tenant Point-of-Sale and Business-Management SaaS**.
It is sold to many independent businesses; each registered business gets
its own isolated workspace (branches, warehouses, employees, products,
customers, sales, inventory, invoices, reports, subscription).

Nothing is hardcoded to one company: product name, branding, currency,
tax, country and business type are all configurable. The platform name
comes from `PRODUCT_NAME` (default `NexaPOS`).

Two distinct user planes:
- **Business plane** — business owners and their staff operate their own
  tenant workspace.
- **Platform plane** — the SaaS operator (platform super-admin) manages
  all businesses, plans, revenue, suspensions and support access.

---

## 2. Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.12+ (3.13 in local dev) |
| Framework | Django 5.2.x |
| API | Django REST Framework (v1, token auth) |
| DB | PostgreSQL (production) · SQLite (local/dev & tests) |
| Cache/queue | Redis + Celery (optional; eager when no broker) |
| Frontend | Django templates + Bootstrap 5 + Alpine.js (vendored, no build step) |
| Charts | Chart.js (vendored) |
| Excel/CSV | openpyxl + csv |
| PDF | xhtml2pdf (pinned `==0.2.15`; pure-Python, Windows-friendly) |
| Static | WhiteNoise (`WHITENOISE_MANIFEST_STRICT = False`) |
| Server | Gunicorn |
| Containers | Docker + docker-compose (web/db/redis/worker/beat) |

All third-party JS/CSS is vendored under `static/` — there is **no npm/build
pipeline**. Edit templates/CSS directly.

---

## 3. SaaS architecture (multi-tenancy)

- **Row-level multi-tenancy.** `tenants.Business` is the tenant root.
  Every business-owned model extends `apps.core.models.TenantModel`
  (mandatory `business` FK + non-guessable UUID `public_id` used in URLs).
- **Single filtering funnel:** `Model.objects.for_business(business)`.
  Views never query tenant models without it.
- **Request resolution:** `apps.core.middleware.BusinessMiddleware` sets
  `request.business` and `request.membership` from the active membership
  (session-pinned for multi-business users).
- **Object lookups:** `apps.core.mixins.get_tenant_object()` returns 404
  for cross-tenant ids (indistinguishable from nonexistent).
- **Tenant-scoped unique constraints:** SKU, barcode, invoice number,
  branch/customer/supplier codes, etc.
- **Subscription gating:** `apps.subscriptions` enforces plan limits and
  read-only/suspended states (`SubscriptionMiddleware`).
- **Support impersonation:** `apps.platformadmin.middleware.SupportSessionMiddleware`
  lets a platform admin act as a business owner for support, audited.
- Cross-tenant isolation is covered by `tests/test_tenancy.py`.

## 4. POS architecture

- **Service-layer core.** Views are thin; all money/stock mutations run
  through transactional services. The POS screen is an Alpine.js app
  (`templates/pos/pos.html`) talking to JSON endpoints in
  `apps/sales/views.py`.
- **`apps.sales.services.complete_sale()`** is the single transactional
  entry point: validates tenancy/prices/discounts/credit, computes tax
  (inclusive/exclusive, per-product), snapshots cost & gross profit per
  line, allocates a concurrency-safe invoice number, writes payments,
  deducts stock, updates customer balances.
- **Immutable financial records.** Completed sales are snapshots; prices,
  costs and tax are frozen. Corrections happen via **void**, **return**,
  or later **payments** — never edits.
- **Money = Decimal**, stored at 3 dp; display precision configurable per
  business (`apps/core/money.py`, `{% money %}` tag in
  `apps/core/templatetags/money_tags.py`).
- **Stock = append-only ledger.** `apps.inventory.services.record_movement()`
  writes a `StockMovement` and updates the cached `StockLevel` atomically;
  never edit stock directly.
- **Invoice numbers:** `PREFIX-NNN` (configured Business Settings prefix +
  lifetime 3-digit counter, no year). Optional per-branch scheme
  (`PREFIX-BRANCH-NNN`). See `next_invoice_number`.

---

## 5. Completed features

**Foundation & tenancy** — registration + onboarding, email-login auth
(lockout, login history, password reset), RBAC (≈50 permission codes, 10
default roles + custom roles), branches & warehouses, subscription plans.

**Catalog & inventory** — categories/brands/units/taxes, products with
variants, barcodes + label printing, CSV/XLSX import **and export**,
archive/restore/safe-delete, ledger-based stock, transfers, adjustments,
physical counts, valuation, low-stock alerts, **inventory import/export**.

**POS & sales** — touch POS (barcode, search, cart, split payments,
change, hold/resume, quick discount/qty, recent customers, keyboard
shortcuts), A4 invoice + 80/58mm thermal receipts + PDF, void, returns
(quantity-capped, multiple refund methods), delivery dates + statuses,
**multi-payment ledger** (dated payments, balance, paid/partial/overpaid).

**Customers & credit** — profiles, credit limits, collections, store
credit, **redesigned landscape statement PDF** + CSV, customer
import/export, receivables.

**Purchases & suppliers** — POs, partial receiving, payments, payables,
purchase returns, PO print/PDF/email/signed-share-link, supplier
statements.

**Registers & shifts** — open/close with expected-vs-actual cash, X/Z
reports, cash-difference alerts, audited reopening.

**Expenses & reporting** — expenses with approval thresholds; real-data
owner dashboard (KPIs, trends, sparklines, charts); ~23 reports incl.
P&L, cash flow, expense analysis, sales-by-customer; CSV/XLSX/PDF export.

**SaaS administration** — platform dashboard with MRR/revenue/business/
user metrics + charts; plans/coupons/announcements; business suspend +
**reactivate** (with who/when/why); subscription/trial extension &
payments; colour-coded subscription **status system**; **Login-As-Owner**
support mode with banner + audit; **configurable expiry mode**
(read-only / suspend); time-limited audited support-access grants;
**Create Business** (provision a new tenant + owner account + subscription —
trial or active — with auto-generated credentials, audited) from the panel.

**Cross-cutting** — immutable audit log (business + platform actions),
in-app notifications, DRF API v1 (tenant + plan gated, health endpoint),
PWA shell (manifest, offline page, safe service worker), Docker, full
docs, demo seed command.

---

## 6. Pending / not-yet-implemented features

- **Online payment gateways** — architecture is gateway-agnostic
  (`SubscriptionPayment.method = "gateway"`); no Stripe/PayPal wired.
- **Email delivery** — uses console backend until SMTP env vars set.
- **Two-factor auth** — user model & login flow isolated for it; not built.
- **Celery scheduled jobs** — e.g. nightly trial-expiry / credit-overdue
  sweeps; services exist, no beat schedule defined.
- **Per-tenant full JSON export / tenant data portability** — reports
  cover most needs; no one-click tenant dump.
- **Arabic / RTL translations** — i18n scaffolding & translation-ready
  templates exist; strings not yet translated.
- **Full dark mode** — markup is theme-friendly but no dark theme shipped.
- **Custom-domain white-label automation** — branding hooks/models exist;
  no domain provisioning.
- **Industry packs** (tailoring/salon/restaurant/wholesale) — deferred by
  design; v1 is a generic retail core.
- **Background/async large exports** — current exports are synchronous
  (10k-row import cap).

---

## 7. Known issues / caveats

- **VAT test baseline mismatch (pre-existing, open).** Tax is now computed
  from the business-level `BusinessSettings.effective_vat_rate`
  (`vat_enabled` defaults off) in `sales.services.complete_sale`, but
  `tests/base.py` and ~28 money/tax/returns/shift tests still assume the
  older **per-product** `tax_rate`, so they fail with `tax_amount == 0`.
  This is unrelated to the Create Business feature; it surfaced after the
  demo-seed migrations (which previously masked it) were guarded out of the
  test DB. Reconcile by enabling VAT in the fixture (or restoring
  per-product tax) — left untouched pending a decision.
- **Demo-seed migrations** (`accounts/0004_seed_render_admin`,
  `accounts/0005_seed_demo_tailoring`) are demo-only and now **no-op under
  the test runner** so they no longer pollute the test database.
- **Reserved stock** is always 0 in inventory export (no reservation
  subsystem); available == current. Stated honestly, not faked.
- **Product export Warehouse/Branch columns** show "All" with total stock
  unless a warehouse/branch filter is applied.
- **Invoice prefix is used verbatim** — if a business saves `AK B-000011`
  the numbers read `AK B-000011-001`. No auto-slugify (deliberate).
- **`git commit` exits 255 on this Windows setup** due to CRLF warnings on
  stderr, but commits succeed — verify with `git log`.
- **Cached template loader stays active under `runserver --noreload`**, so
  restart the server to pick up template edits when reload is off.
- Statement/PO/invoice PDFs are `Content-Disposition: attachment`, so they
  download rather than render inline in some preview tools.

---

## 8. Deployment

### Local (Windows / dev)
```powershell
cd "C:\Users\Admin\POS Project"
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py seed_demo        # optional demo data
.\.venv\Scripts\python.exe manage.py runserver        # http://127.0.0.1:8000
.\.venv\Scripts\python.exe manage.py createsuperuser  # platform admin → /platform/
```

### Docker (production-style)
```bash
cp .env.example .env   # set SECRET_KEY, ALLOWED_HOSTS, POSTGRES_PASSWORD, CSRF_TRUSTED_ORIGINS
docker compose up --build -d
docker compose exec web python manage.py createsuperuser
```
Services: `web` (Gunicorn, migrates on boot), `db` (PostgreSQL 16),
`redis`, `worker`, `beat`. Health check: `GET /api/v1/health/`.

### Production checklist (see SECURITY.md / DEPLOYMENT.md)
- `DJANGO_SETTINGS_MODULE=config.settings.production`, real `SECRET_KEY`,
  `DEBUG=False`, `ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS` set.
- Production settings already enable SSL redirect, secure cookies, HSTS.
- `collectstatic` (done in Docker image build); WhiteNoise serves static.
- Reverse proxy (nginx) for TLS; replicate `protected_media` rules for
  sensitive uploads.
- PostgreSQL backups (see DEPLOYMENT.md).

### Tests
```bash
python manage.py test               # full suite (217 tests)
python manage.py test tests.test_tenancy   # isolation only
```

---

## 9. Next development priorities

1. **Payment gateway integration** (Stripe-first) on the existing
   `SubscriptionPayment` + checkout-ready architecture — biggest
   commercial unlock.
2. **Email backend + transactional emails** (invoices, statements, trial/
   expiry notices) and the **Celery beat** sweeps for expiry/overdue.
3. **Quotations module** (and a Balance Sheet report) — the only document
   types the prompts asked for that aren't built yet.
4. **i18n/Arabic + RTL** rollout using the existing scaffolding.
5. **Async exports / background jobs** for very large datasets.
6. **Dark mode** theme pass.

See CLAUDE_HANDOFF.md for the detailed map a new session needs.
