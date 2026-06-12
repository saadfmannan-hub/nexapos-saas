# Development Progress

**Current phase:** 10/10 complete — commercially ready core delivered.
**Test status:** 95/95 automated tests passing; full-page smoke test
passing (all major pages return 200 with demo data).

## Completed features

### Phase 1 — Foundation
- Django 5.2 project, split settings, env-based config, git history
- Row-level multi-tenancy: TenantModel/Manager, BusinessMiddleware,
  permission registry (45 codes), 10 default roles
- Business registration + provisioning (branch, warehouse, roles, units,
  payment methods, register, expense categories, walk-in customer, trial)
- Auth: login w/ lockout + history, logout, password reset, change
  password, profile; onboarding checklist; business switcher
- Branches & warehouses CRUD with plan limits

### Phase 2 — Catalog & inventory foundation
- Categories, brands, units, tax rates (per business, no hardcoded VAT)
- Products with variants, tenant-scoped SKU/barcode uniqueness, images,
  archive instead of delete; barcode SVG + label printing; CSV/XLSX
  import with row-level errors; opening stock; stock ledger + levels

### Phase 3 — Core POS
- Touch-friendly POS (Alpine.js): barcode scan, search, category pills,
  cart, line/invoice discounts, price override permission, customer
  search + quick add, hold/resume, keyboard shortcuts (F2/F4/F7/F8/F9)
- Split payments, change calculation, credit & store-credit payment kinds
- Concurrency-safe invoice numbers `PREFIX-YEAR-SEQ`; A4 invoice, 80/58mm
  receipts, PDF, reprint marking, WhatsApp link copy

### Phase 4 — Customers & credit
- Customer CRUD, groups, duplicate-mobile warning, credit limits,
  collections with receipts, statements with running balance, store credit

### Phase 5 — Purchases & suppliers
- Suppliers with payables; purchase orders → partial receiving → payment
  → returns; average-cost updates; supplier statements on profile

### Phase 6 — Advanced inventory
- Transfers (draft → dispatch → receive/cancel), adjustments with reasons
  and approval flow, physical counts with frozen expected quantities and
  variance application, movement history, stock valuation

### Phase 7 — Returns & registers
- Invoice-linked returns (quantity-capped, partial/full, restock toggle,
  5 refund methods), voids with stock/balance reversal
- Registers, shifts with expected vs actual cash, X/Z reports, cash
  difference notifications, audited reopening

### Phase 8 — Expenses & reporting
- Expenses with categories, attachments, approval threshold workflow
- Real-data dashboard (KPIs + 4 charts, clickable into reports)
- 19-report center with date/branch/warehouse filters and CSV/XLSX/PDF
  exports that match on-screen data

### Phase 9 — SaaS administration
- Platform dashboard (businesses, subscription statuses, revenue, failed
  logins), business suspend/activate (no data deletion), manual
  subscription/trial extension with payment records, plan CRUD, coupons,
  announcements, audited time-limited support access with owner
  notification + revocation, platform audit log

### Phase 10 — Commercial readiness
- Immutable audit logging across modules; in-app notifications
- DRF API v1 (token, tenant + plan gated) + health endpoint
- PWA: manifest, service worker (static-only cache), offline page
- Docker (web/db/redis/worker/beat), Gunicorn, WhiteNoise
- Docs: README, ARCHITECTURE, SECURITY, DEPLOYMENT, API, CHANGELOG
- Demo seed command; smoke-test script; ruff/black/pre-commit configs
- Error pages (400/403/404/500) with support codes

## Known issues / limitations

- Email sending uses the console backend until SMTP is configured.
- 2FA, Celery-based scheduled jobs (e.g. nightly credit-overdue scans)
  and per-tenant JSON export are architecture-ready but not implemented.
- Online payment gateway integration is intentionally not hardcoded;
  manual/bank-transfer activation is supported.
- Arabic translations are not yet authored (i18n scaffolding, locale dir
  and translation-ready templates are in place).
- POS offline mode is limited to a safe offline fallback page — no
  offline sales (by design, until reliable sync exists).
- Custom-domain white-label automation is not implemented (settings and
  branding hooks exist).

## Remaining optional features (post-v1 roadmap)

Industry packs (tailoring, salon, restaurant, service, wholesale price
lists), gift cards, loyalty redemption rules, quotations, landed-cost
allocation, FIFO costing option, customer display / cash-drawer bridge,
Swagger docs, background report exports for very large datasets.
