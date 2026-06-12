# Changelog

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
