# Changelog

All notable changes to NexaPOS. Current version: **1.6.0**.
Format: newest first. Each entry notes SaaS-platform and POS changes.

## Major milestones completed

| Version | Theme | Highlights |
|---|---|---|
| 1.0.0 | Initial commercial release | Full multi-tenant POS SaaS: tenancy, auth/RBAC, catalog+variants, ledger inventory, POS + invoices/receipts, customers/credit, suppliers/purchases, registers/shifts, expenses, dashboard + 19 reports, subscriptions, platform admin, audit, notifications, API v1, PWA, Docker, docs. 95 tests. |
| 1.1.0 | Production bug-fix sprint | Payment precision fix, customer-detail crash fix, PO print/PDF/email/share, money formatting + currency registry, subscription limit enforcement + upgrade page, premium dashboard (trends/sparklines/widgets), POS UX, P&L/cash-flow/expense reports. |
| 1.1.x | Dashboard regression + targeted fixes | Sparkline sizing, icon-font paths, service-worker cache, register branch dropdown, revenue trend zero-fill. |
| 1.2.0 | Business controls | Delivery dates + statuses, multi-payment ledger, customer statements, product/sale lifecycle (archive/restore/delete, void/delete), extended audit. |
| 1.3.0 | Data tools (Phase 2.1) | Customer statement PDF redesign, customer import/export, product export, inventory import/export. |
| 1.3.x | Targeted fixes | Invoice prefix from Business Settings → simplified `PREFIX-NNN` format; platform super-admin access without a workspace. |
| 1.4.0 | Platform/SaaS expansion | Business reactivation, subscription status system, SaaS metrics + charts, Login-As-Owner support mode, configurable expiry mode, extended platform audit. |
| 1.5.0 | Platform: Create Business | Platform admin can provision a new business + owner account + subscription (trial or active) from the admin panel, with auto-generated credentials and platform audit. |
| 1.6.0 | Product Variants Builder + Auto SKU | Dynamic variant builder on product create/edit (types → values → generate combinations with SKU/barcode/prices/opening stock); auto-SKU generation from business-name initials. |

Implementation also tracked phase-by-phase in DEVELOPMENT_PROGRESS.md /
PROJECT_PLAN.md. Detailed per-version notes follow.

---

## 1.6.0 — 2026-06-18 — Product Variants Builder + Auto SKU Generation

### Added
- **Dynamic variant builder on the product create/edit page.** Selecting
  product type "Product with variants" now reveals a Variants section
  (Alpine.js, no build step) — previously nothing appeared and variants
  could only be added one-by-one on a separate page after saving. Users:
  - add **variant types** (Size / Color / Material / Custom) with multiple
    values each (e.g. Size: S, M, L, XL · Color: Black, White);
  - click **Generate Variants** to produce every combination
    (M / Black, M / White, L / Black, L / White, …);
  - edit each generated row's **SKU, Barcode (optional), Purchase Price,
    Sale Price and Opening Stock** before saving.
  Product, variants and per-variant opening stock are persisted in one
  `transaction.atomic` block; opening stock flows through the existing
  `inventory.set_opening_stock` ledger (no new ledger logic). Variant
  SKU/barcode uniqueness is validated across products + variants and
  within the batch — a bad row re-renders the form with no partial save.
- **Auto SKU generation.** A new **"Auto Generate SKU"** checkbox on the
  product form. When enabled, the product and every generated variant
  receive a unique `PREFIX-000001` style SKU; the prefix comes from the
  **business-name initials** (e.g. "Nexa Retail" → `NEX-000001`, "Demo
  Tailoring" → `DEM-000001`), falling back to `SKU` when the name has no
  letters. `catalog.services.generate_sku` scans existing product and
  variant SKUs (and codes reserved earlier in the same request) so
  generated values never collide within the tenant. When the checkbox is
  off, manual SKU entry is unchanged.
- **Append-only edit mode.** On an existing variant product, the builder
  adds new combinations while existing variants are listed read-only with
  links to the current per-variant edit page — the existing per-variant
  editing flow is unchanged.

### Unchanged (verified)
- **Simple/standard products are unaffected** — all variant logic is gated
  on `product_type == "variant"`; standard, service and non-stock paths
  behave exactly as before, and stray variant payloads are ignored.
- Multi-tenant isolation, the POS sale flow, the inventory ledger logic
  (beyond the required per-variant opening-stock call) and the audit core
  are untouched.

### Database migrations
- **None.** Reuses existing `Product` / `ProductVariant` / stock models;
  `generate_sku` scans rather than adding a sequence table.
  `makemigrations --check` is clean.

### Test status
- New `tests/test_variants.py` — **13 tests, all passing**: simple product
  still saves (regression) and ignores stray variant payloads, manual
  duplicate SKU rejected, variant product creates rows + per-variant
  opening stock, generated combinations save with attributes/prices,
  duplicate/colliding variant SKUs rejected with no partial save, auto-SKU
  on product and variants (all unique), and `generate_sku` /
  `sku_prefix_for` unit coverage. Verified live in the browser preview.
- **Known pre-existing issue (unchanged):** the ~28 money/tax/returns/
  shift failures from the VAT rework remain open and were not touched
  (see 1.5.0 and §7 of PROJECT_STATUS).

## 1.5.0 — 2026-06-17 — Platform: Create Business

### Added
- **Platform Admin → Create Business** — a platform admin can provision a
  complete new tenant from `/platform/businesses/new/` (linked from a
  "Create business" button on the Businesses list). One transactional
  flow:
  1. Creates the **owner `User`** account (email login).
  2. Provisions the **business** via the existing
     `tenants.services.provision_business()` — default roles + owner
     membership, head-office branch, main warehouse, default catalog,
     payment methods, register and expense categories.
  3. Assigns a **subscription** on a chosen `Plan`: either **Trial**
     (trial-days, defaulting to the plan's `trial_days`) or **Active**
     (paid period in days, with an optional recorded `SubscriptionPayment`
     — reusing the same logic as the existing "extend" action).
  4. **Generates login credentials** — the admin may type a password or
     leave it blank to auto-generate a strong one, which is shown **once**
     on the success message for the admin to share.
  5. Writes a **`platform.business_created`** audit entry (module
     `platformadmin`, the acting admin as user) on top of the
     `business.registered` tenant audit entry from provisioning.
  6. The new business immediately appears in the Platform Admin
     **Businesses** list.

### New endpoints / templates
- Endpoint: `/platform/businesses/new/` (`platformadmin:business_create`).
- View + form: `business_create` and `BusinessCreateForm` in
  `apps/platformadmin/views.py`.
- Template: `templates/platformadmin/business_create.html`; "Create
  business" button added to `templates/platformadmin/business_list.html`.

### Database migrations
- **None.** The feature reuses existing models and the
  `provision_business` service; no schema change.
  `makemigrations --check` is clean.

### Maintenance — demo-seed migrations no longer pollute the test DB
- `accounts/0004_seed_render_admin` and `accounts/0005_seed_demo_tailoring`
  are demo-only data seeds. They were running against the **test**
  database (Django applies all migrations when building it), which added a
  third business and a `trial_days=0` demo plan — masking real failures.
  Both now **no-op under the test runner** (`"test" in sys.argv`); live and
  demo databases are unaffected. This restores a clean, predictable test
  dataset.

### Test status
- New `CreateBusinessTests` (8 tests) in
  `tests/test_platform_enhancements.py`, all passing; the full
  `test_platform_enhancements` module is green (27/27). `manage.py check`
  clean; `makemigrations --check` clean.
- **Known pre-existing issue (not introduced here):** 28 money/tax/
  returns/shift tests currently fail because of an earlier **VAT rework** —
  `sales.services.complete_sale` now derives tax from the business-level
  `BusinessSettings.effective_vat_rate` (`vat_enabled` defaults to off)
  instead of each product's `tax_rate`, while `tests/base.py` and those
  tests still assume per-product tax. These failures are unrelated to the
  Create Business feature and were previously hidden by the demo-seed
  migrations above (which made the same tests error earlier with
  `SubscriptionInactive`). Reconciling the VAT test baseline is tracked
  separately and intentionally left untouched here.

## 1.4.0 — 2026-06-14 — Platform: reactivation, status system, SaaS metrics, support mode, expiry control

### Added
- **Business reactivation** — suspend/reactivate now record `suspended_by`,
  `suspended_at`, `suspension_reason`, `reactivated_by`, `reactivated_at`,
  shown on the business detail page. Both actions audited; reactivation
  restores access immediately. (Suspension already blocks login/access.)
- **Subscription status system** — `Subscription.display_status` +
  `is_expiring_soon` (lapses within 7 days) drive colour-coded badges:
  Trial (yellow), Active (green), Expiring soon (orange), Expired (red),
  Suspended (dark red). Applied on the overview, business list and detail
  via the `sub_status_badge` template tag.
- **Advanced SaaS metrics** on Platform Overview — MRR (monthly-equivalent
  of paid active subs), revenue this/last month, total revenue, business
  counts by status (total/active/trial/expiring/expired/suspended), user
  metrics (total + active 30d), plan-distribution doughnut and
  status bar chart (Chart.js).
- **Login As Owner (support mode)** — a platform admin can open a tenant as
  its owner via session impersonation (`SupportSessionMiddleware`). Reason
  required; owner notified; audited on enter and exit (with duration). A
  sticky "Support session active" banner with an **Exit** button shows
  throughout; exit returns to the platform panel.
- **Configurable expiry behaviour** — `PlatformConfig.expiry_mode`
  (`read_only` default | `suspend`), editable on the new Platform Settings
  page. Read-only keeps data viewable but blocks new transactions;
  suspend blocks all access with "Subscription expired. Please contact
  support." Enforced in `SubscriptionMiddleware`.
- **Audit** — new actions: `platform.business_reactivated`,
  `platform.login_as_owner`, `platform.support_session_ended`,
  `platform.settings_changed` (joining existing suspend/extend/support
  events; all carry user, tenant, timestamp, IP).

### New models / fields / endpoints
- Models: `platformadmin.PlatformConfig` (singleton; `expiry_mode`).
- Fields: `tenants.Business.suspended_by`, `reactivated_at`,
  `reactivated_by`.
- Endpoints: `/platform/businesses/<id>/login-as/`,
  `/platform/support/exit/`, `/platform/settings/`.

### Database migrations
- `tenants/0003` — add `suspended_by`, `reactivated_at`, `reactivated_by`
  (nullable FKs / datetime; additive, no data change).
- `platformadmin/0002` — create `PlatformConfig`.

### Unchanged (verified)
- Multi-tenant isolation, existing business-owner functionality and
  permissions are intact; suspended/expired handling builds on the
  existing subscription enforcement.

### Test status
- 217/217 tests passing (19 new in `tests/test_platform_enhancements.py`).
  Verified live: platform dashboard with charts, status badges, full
  Login-As-Owner enter/banner/exit cycle, and both expiry modes.

## 1.3.3 — 2026-06-13 — Fix: platform super-admin access without a workspace

### Fixed
- **Superusers were redirected to `/no-business/` and locked out of
  `/platform/`.** Two causes: (1) login always redirected to `dashboard`,
  which requires business membership, so any user without a workspace was
  bounced to `/no-business/`; (2) the platform guard and home/redirect
  logic checked only `is_platform_admin`, never `is_superuser`.
- New `User.is_platform_staff` property (superuser **or** platform admin)
  is now the single check used by the platform guard, the home router,
  the post-login redirect, the `/no-business/` page and the nav link.
  Django superusers always qualify — even with no business and no explicit
  platform flag.
- Login now routes platform staff without a workspace to `/platform/`;
  `/no-business/` redirects them there too instead of dead-ending.
  `/django-admin/` already worked for superusers (is_staff) and is covered
  by a regression test.

### Unchanged (verified)
- Business users still require active workspace membership (a user with no
  membership and no platform flag still lands on `/no-business/`); business
  owners still go to their dashboard. Multi-tenant isolation is intact — a
  superuser with no membership has no active business and cannot reach any
  tenant's business data.

### Notes
- No database migration (property only; no model fields changed).

### Test status
- 198/198 tests passing (10 new in `tests/test_platform_access.py`).
  Verified against the real account: `admin@nexapos.com`
  (`is_superuser=True`) → `is_platform_staff=True`, post-login →
  platform dashboard.

## 1.3.2 — 2026-06-13 — Fix: simplified invoice number format

### Changed
- **Invoice numbers are now `PREFIX-NNN`** (configured prefix + a simple
  zero-padded running number, min 3 digits). The year and the second
  6-digit sequence were removed:
  `INV B-2026-000010` → `INV B-001`, `INV B-002`, … `INV B-999`,
  `INV B-1000`, `INV B-1001`. `ABC` → `ABC-001`. The per-branch opt-in
  becomes `PREFIX-BRANCH-NNN`.
- The counter is now **lifetime** (year-independent): `InvoiceSequence`
  uses a sentinel `year=0` (`services.LIFETIME_SEQUENCE`) so a single
  ongoing sequence per scope never resets — this is what guarantees the
  short, year-less numbers stay unique across years.
- Receipt, A4/PDF invoice, sale detail, sales list, customer statement,
  returns and reports all read the stored `Sale.invoice_number`, so they
  show the identical new format automatically.

### Notes
- **No schema migration** — `year=0` is a valid value of the existing
  field; legacy `InvoiceSequence` rows (real years) are left intact.
- **Historical invoice numbers are unchanged** (immutable per-sale
  snapshots); only new sales use the simplified format.

### Test status
- 188/188 tests passing (5 new format tests incl. the `INV B-001`/`ABC-001`
  examples and the >999 → 1000 rollover; one per-branch test updated to the
  3-digit format). Verified live: a new demo sale minted `AK B-000011-001`
  (no year), shown identically on the receipt, PDF, detail and list, while
  historical `ML-2026-000004` was untouched.

## 1.3.1 — 2026-06-13 — Fix: invoice prefix from Business Settings

### Fixed
- **Invoice numbering ignored the configured prefix.** Root cause: in
  `next_invoice_number`, the per-branch `Branch.invoice_prefix` (e.g. the
  demo "City Mall" branch = "ML") shadowed `BusinessSettings.invoice_prefix`
  via `branch.invoice_prefix or settings.invoice_prefix`, so the value set
  in Business Settings was never reached. New invoices/receipts now always
  use the configured Business Settings prefix
  (e.g. `INV` → `INV-2026-000001`).
- All downstream views read the stored `Sale.invoice_number`, so receipts,
  A4/PDF invoices, credit sales, returns references, customer statements
  and reports now show the configured prefix consistently.

### Added
- **`BusinessSettings.invoice_include_branch_code`** (default off): choose
  between global numbering (`INV-2026-000001`) and per-branch numbering
  (`INV-HK-2026-000001`, each branch counted independently). Exposed in
  Business Settings → Invoices & receipts.
- `InvoiceSequence.branch` is now nullable to support a single global
  per-business counter; switching to global continues above any existing
  per-branch counter for the year so numbers can never collide. Invoice
  numbers stay unique per business.

### Database migrations
- `sales/0003` — `InvoiceSequence.branch` nullable; replace the single
  unique constraint with two conditional ones (branch-scoped + global).
- `tenants/0002` — add `invoice_include_branch_code`; widen
  `invoice_prefix` to 15 chars. Additive; **historical invoice numbers are
  not modified** (they are immutable snapshots on each Sale).

### Test status
- 183/183 automated tests passing (8 new in
  `tests/test_invoice_prefix.py`; one POS test updated to the corrected
  expectation). Verified live: a new sale on the demo DB minted
  `AK B-000011-2026-000012` (the configured prefix) while historical
  `ML-`/`HK-` numbers were untouched.

## 1.3.0 — 2026-06-13 — Phase 2.1: Customer/Product/Inventory data tools

### Added
- **Customer statement PDF — full redesign**: dedicated A4 *landscape*
  accounting layout (`invoices/customer_statement_pdf.html`) replacing the
  generic report template. Logo + company/branch/customer header, a
  four-card summary (opening / debits / credits / closing), a fixed
  column-width ledger (Date · Type · Reference · Debit · Credit · Running
  Balance · Notes) with right-aligned currency, wrapping references, a
  repeating table header on every page (`thead{table-header-group}`), and
  a footer with "Generated by NexaPOS · timestamp · Page x of y".
- **Customer import / export**: list-page Export dropdown (Excel/CSV,
  honors current search/filter); Import page with downloadable template,
  .xlsx/.csv upload, **Skip** vs **Update** existing modes, validation
  (missing name, invalid email, in-file + existing duplicate code/mobile),
  imported/updated/skipped/failed summary, and a downloadable error
  report. Audited (`customer.exported`, `customer.imported`).
- **Product export**: Products-page Export dropdown (Excel/CSV) with all
  fields incl. current stock; honors category / brand / status filters
  plus low-stock and out-of-stock. Single aggregated stock query →
  scales to large catalogs. Audited (`product.exported`).
- **Inventory import / export**: stock-page Export dropdown (Excel/CSV,
  per-warehouse/branch) with current/available/value/last-movement
  columns; Import page with template, four modes (**Add** / **Replace** /
  **Set opening** / **Update minimum only**), validation (product +
  warehouse resolution, strict numeric quantities, duplicate rows),
  summary + error report. Every stock change flows through the existing
  `record_movement` ledger; each import writes an audit record with file
  name, mode and row counts (`inventory.exported`, `inventory.imported`).
- **Permissions**: new `customers.export/import`, `products.export`,
  `inventory.export/import` codes — granted to Owner (implicit),
  Business Administrator and Branch Manager. Cashier/viewer roles are
  blocked (403). A shared `apps/core/imports.py` handles file parsing,
  the 10k-row cap, and error-report generation for all importers.

### Database migrations
- `accounts/0003_phase21_role_permissions` — **data only**, additive and
  idempotent: appends the new permission codes to existing
  "Business Administrator" / "Branch Manager" system roles. No schema
  change, no table/data removal. (No model/schema migrations this phase.)

### Test status
- 175/175 automated tests passing (30 new in `tests/test_phase21.py`).

## 1.2.0 — 2026-06-13 — Business Controls (delivery, ledger, statements, lifecycle, audit)

### Added
- **Delivery dates**: `Sale.delivery_date` + `delivery_status` (Pending /
  In Production / Ready / Delivered / Cancelled). Optional date input in
  the POS payment panel; shown on the sale detail (with a status-update
  control), A4 invoice, 80mm receipt, and sales list. New sales-list
  filters: today's / upcoming / overdue / all scheduled deliveries.
  `Sale.is_delivery_overdue` drives red highlighting.
- **Multi-payment ledger**: `SalePayment` gained `payment_date`,
  `notes` and `received_by` (plus existing created/updated timestamps).
  A new "Record payment" action on credit/partially-paid sales appends
  dated payments, recomputes paid/balance, flips status
  (Credit → Partially Paid → Completed) and reduces the customer
  receivable — all Decimal-safe. `Sale.payment_state` exposes
  Unpaid / Partially Paid / Paid / Overpaid.
- **Dated payment history**: sale detail and the A4 invoice / 80mm
  receipt now show a Date · Method · Reference · Amount · Received-by ·
  Notes history with Total paid and Balance due.
- **Customer account statement**: upgraded to a true running-balance
  ledger (Date · Type · Reference · Debit · Credit · Balance · Notes)
  with opening balance, credit sales as debits, payments/returns as
  credits, date-range + branch filters, balance-brought-forward, and
  PDF / CSV export (export is audited).
- **Product archive / restore / delete**: hard delete only when a product
  has zero history across sales, purchases, stock movements, transfers,
  adjustments and counts (`products.delete` permission); otherwise
  Archive (`products.archive`). Restore action added. Product list filter
  is now Active / Inactive / Archived / All; archived products are hidden
  from POS search, barcode lookup and the default list but stay on
  historical invoices.
- **Sale void / delete**: `sales.delete` allows hard-deleting only
  drafts with no payments, returns or stock movements; everything else
  must be voided (existing void already reverses stock + customer
  balance, keeps the invoice number, records reason/voided_by/voided_at).
- **Audit trail extended**: new audited actions — `sale.payment_added`,
  `sale.deleted`, `sale.delivery_status`, `product.restored`,
  `product.deleted`, `customer.statement_exported` (joining the existing
  sale.completed/voided/returned, product.saved/archived,
  stock, shift, auth and platform events). All viewable on the Audit Log
  page; the log remains append-only.

### Database migrations
- `sales/0002_*` — additive only: `Sale.delivery_date`,
  `Sale.delivery_status`, `SalePayment.payment_date`, `SalePayment.notes`,
  `SalePayment.received_by`, and a `SalePayment` ordering change. No data
  backfill required; existing sales have null delivery and keep working.

### Test status
- 145/145 automated tests passing (34 new in `tests/test_phase_update.py`).

## 1.1.0 — 2026-06-12 — Production-Ready Upgrade & Bug Fix Sprint

### Fixed
- **Bug #1 — Payment validation**: sale grand totals are now rounded to the
  business's currency precision inside `complete_sale` (delta stored in the
  existing `rounding` field), and the POS computes totals/remaining/change
  at the same precision. An exact on-screen payment ("Remaining = 0.00")
  always validates; "Exact amount" always works. Pure-Decimal server math;
  regression tests in `tests/test_bugfixes.py`.
- **Bug #2 — Customer detail crash**: `FieldError: Cannot compute
  Avg('total')` caused by an aggregate alias shadowing the `total` field in
  `customer_detail`. Renamed aliases; page now renders for brand-new
  customers (zero orders) and with history. Regression tests added.
- **Bug #4 — Currency formatting**: new `{% money %}` /
  `|money_p` template tags quantize every displayed amount to the business
  precision (no raw storage decimals, no `-0.000`, thousands separators).
  Applied across sales, customers, suppliers, purchases, shifts, inventory
  and the dashboard. The inventory value column no longer uses integer
  `widthratio` rounding — values are computed in the view with Decimal.
- **Bug #5 — Subscription enforcement**: creation of branches, warehouses,
  users, products and customers over the plan limit now renders a dedicated
  upgrade page naming the exceeded limit with current/allowed counts and a
  full usage table — instead of a passive red highlight. POST attempts are
  blocked server-side (proven by new tests).

### Added
- **Bug #3 — Purchase order documents**: branded PO print view and PDF
  download (logo, supplier block, deliver-to, items, totals, terms &
  conditions, dual signature area), supplier share link (signed,
  time-limited, no login required, 404 on tamper), and email-with-PDF
  attachment including a view-online link. All audited; six new tests.
- **Bug #6 — Currency system**: central currency registry (OMR, USD, EUR,
  GBP, AED, SAR, QAR, KWD, BHD + more) with automatic symbols and standard
  precisions. `Business.currency_display` resolves symbols automatically;
  the business profile now offers a currency dropdown and auto-adopts the
  registry precision on switch. OMR keeps full 3-decimal support.
- **Premium dashboard**: glassmorphism KPI cards with gradient accents,
  icons, trend % vs the previous equal-length period, sparklines (sales /
  profit / expenses), hover lift; new interactive charts (revenue+profit
  trend with gradient fill, payment doughnut, top products, top customers,
  sales by branch, hourly sales pattern, 14-day inventory in/out); activity
  widgets (recent sales, pending receivables & payables, low stock, POs
  awaiting receipt, recent expenses); skeleton-loader CSS utilities.
- **POS UX**: recent-customers quick list (localStorage), 5%/10%/clear
  quick invoice discount buttons, quick cash-tender buttons (next round
  amounts), precision-aligned payment math.
- **Reports**: Profit & Loss (with expense category breakdown), Cash Flow
  (in/out by source), Expense Analysis (share per category), Sales by
  Customer — all with CSV/Excel/PDF export.

### Database migrations
- None — the sprint is migration-safe (uses the existing `Sale.rounding`
  field; no schema changes).

### Test status
- 112/112 automated tests passing; full-page smoke test passing.

## 1.0.0 — 2026-06-12

First commercial release.

### Added
- Multi-tenant SaaS core with row-level isolation and tenant-scoped
  constraints; business registration, onboarding and provisioning
- Authentication with lockout, login history, password reset; RBAC with
  45 permissions and 10 default roles; custom roles (plan-gated)
- Product catalog (categories, brands, units, taxes, variants, barcodes,
  labels, import) and ledger-based inventory (transfers, adjustments,
  physical counts, valuation, low-stock alerts)
- POS with split payments, change, hold/resume, discounts, credit and
  store credit; immutable sales with A4/thermal/PDF invoices and
  concurrency-safe numbering; voids and quantity-capped returns
- Customers with credit/receivables/statements; suppliers and purchases
  with partial receiving, payments and returns
- Cash registers and shifts with X/Z reports and cash differences
- Expenses with approval workflow; real-data dashboard; 19 reports with
  CSV/XLSX/PDF export
- Subscription plans, limits, trials, grace, read-only suspension,
  coupons, manual payments; platform super admin with audited support
  access; immutable audit log; notifications
- DRF API v1, PWA shell, Docker deployment, full documentation

### Database migrations
- Initial migrations for all 18 apps (`*/migrations/0001_*`, `0002_*`).

### Breaking changes
- None (initial release).
