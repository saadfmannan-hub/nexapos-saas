# NexaPOS — Multi-Tenant Point of Sale & Business Management SaaS

A production-ready, commercially sellable, multi-tenant POS and business
management platform built with Django. Every registered business gets its
own secure workspace: branches, warehouses, employees, products, customers,
sales, inventory, invoices, reports and subscription.

The product name, branding, currency, tax rates and all business specifics
are **fully configurable** — nothing is hardcoded for any one company or
country. The default name "NexaPOS" comes from the `PRODUCT_NAME`
environment variable.

## Features

- **Multi-tenant SaaS core** — strict row-level isolation, tenant-scoped
  unique constraints, UUID public identifiers, automated isolation tests
- **POS screen** — barcode scanning, product grid, cart, split payments,
  change calculation, hold/resume, keyboard shortcuts, touch friendly
- **Sales & invoicing** — concurrency-safe per-branch invoice numbering,
  immutable financial snapshots, A4 invoice, 80mm/58mm thermal receipts,
  PDF download, reprint tracking, void with audit
- **Products** — categories, brands, units, tax rates, variants, barcode
  generation & label printing, CSV/Excel import, archive (never delete)
- **Inventory** — append-only stock ledger, average cost, transfers,
  adjustments with approval, physical counts with variance, low-stock alerts
- **Customers & credit** — credit limits, credit sales, collections,
  statements with running balance, store credit, walk-in customer
- **Purchases & suppliers** — purchase orders, partial receiving, supplier
  payments, payables, purchase returns
- **Registers & shifts** — open/close with expected vs actual cash,
  X/Z reports, cash difference approval, audited reopening
- **Expenses** — categories, approval thresholds, attachments
- **Dashboard & reports** — real-data KPIs and Chart.js charts, 19 reports
  with filters and CSV / Excel / PDF export
- **Subscriptions** — plans with limits & feature flags, trials, grace
  periods, read-only suspension (data is never deleted), coupons,
  manual/bank-transfer activation, gateway-ready
- **Platform super admin** — manage businesses, plans, payments,
  announcements, audited time-limited support access
- **Security** — RBAC with 40+ permissions, login rate limiting, login
  history, immutable audit log, protected media, CSRF/secure cookies
- **API v1** — DRF, token auth, tenant-aware, plan-gated
- **PWA** — installable, offline fallback page, no financial data cached

## Technology stack

Python 3.12+ · Django 5.2 · Django REST Framework · PostgreSQL (prod) /
SQLite (dev) · Redis + Celery (optional) · Bootstrap 5 · Alpine.js ·
Chart.js · openpyxl · xhtml2pdf · WhiteNoise · Gunicorn · Docker

## Local setup (Windows / macOS / Linux)

```bash
# 1. Clone and enter the project
cd "POS Project"

# 2. Create a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

# 3. Install dependencies
pip install -r requirements/development.txt

# 4. Configure environment (optional for dev — sane defaults exist)
copy .env.example .env          # then edit as needed

# 5. Migrate and run
python manage.py migrate
python manage.py runserver
```

Open http://127.0.0.1:8000 — register a business, or seed demo data.

### Platform super admin

```bash
python manage.py createsuperuser
```

Superusers are platform admins automatically; sign in and open
`/platform/`.

### Demo data

```bash
python manage.py seed_demo
```

Creates a clearly-marked "Demo Business" with branches, staff, products
(including variants), a received purchase, sales, a return, a transfer,
an expense, and a closed shift.

Demo logins (local development only):

| Role    | Email                     | Password      |
|---------|---------------------------|---------------|
| Owner   | demo-owner@example.com    | DemoPass123!  |
| Manager | demo-manager@example.com  | DemoPass123!  |
| Cashier | demo-cashier@example.com  | DemoPass123!  |

### Temporary client demo login

The deployment seeds a temporary demo tenant when `Demo Tailoring` is missing.

| Role           | Email              | Password   |
|----------------|--------------------|------------|
| Business Owner | demo@tailoring.com | Demo@2026  |

### Running tests

```bash
python manage.py test tests
```

95 tests cover authentication, tenant isolation, POS, payments, credit,
inventory, purchases, returns, shifts, subscriptions, finance and exports.

### Smoke test (all pages)

```bash
python manage.py seed_demo
python manage.py shell -c "exec(open('scripts/smoke_test.py').read())"
```

## Docker setup

```bash
copy .env.example .env    # set SECRET_KEY and POSTGRES_PASSWORD
docker compose up --build
```

Services: `web` (Gunicorn, migrations on boot), `db` (PostgreSQL 16),
`redis`, `worker` (Celery), `beat`. Health check: `/api/v1/health/`.

### Render persistence note

SQLite on Render Free is not persistent. PostgreSQL is required for permanent accounts/data.
If `DATABASE_URL` is not set, the app falls back to SQLite for development convenience only.

## Environment variables

See `.env.example`. Key ones:

| Variable | Purpose | Default |
|---|---|---|
| `PRODUCT_NAME` | White-label product name | NexaPOS |
| `SECRET_KEY` | Django secret (required in prod) | dev fallback |
| `DEBUG` | Never `True` in production | False |
| `ALLOWED_HOSTS` | Comma-separated hosts | localhost |
| `DATABASE_URL` | e.g. `postgres://u:p@host/db` | SQLite |
| `REDIS_URL` / `CELERY_BROKER_URL` | Optional cache/queue | local memory |
| `EMAIL_HOST` etc. | SMTP (console backend in dev) | console |
| `DEFAULT_TRIAL_DAYS` | Trial length for new businesses | 14 |
| `PLATFORM_SUPPORT_EMAIL`, `PLATFORM_PRIMARY_COLOR`, … | Branding | — |

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — multi-tenancy, models, services
- [SECURITY.md](SECURITY.md) — isolation, auth, production checklist
- [DEPLOYMENT.md](DEPLOYMENT.md) — VPS, Docker, nginx, HTTPS, backups
- [API.md](API.md) — REST API usage and rules
- [DEVELOPMENT_PROGRESS.md](DEVELOPMENT_PROGRESS.md) — status & known limits
- [CHANGELOG.md](CHANGELOG.md)

## Common errors

| Problem | Fix |
|---|---|
| `SECRET_KEY must be set in production` | Set `SECRET_KEY` env var |
| Static files missing in production | Run `python manage.py collectstatic` |
| PDF export fails | Ensure `xhtml2pdf` installed (`pip install -r requirements/base.txt`) |
| "An open shift is required before selling" | Open a shift under Registers, or enable *Allow sales without an open shift* in Business Settings |
| Locked account after failed logins | Wait 15 minutes or clear `locked_until` for the user |
