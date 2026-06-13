# Changelog

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
