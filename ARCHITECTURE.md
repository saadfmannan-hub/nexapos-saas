# Architecture

## Overview

NexaPOS is a row-level multi-tenant Django monolith with a service-layer
core. HTTP views are thin; all financial and inventory state changes go
through transactional service functions.

```
config/                 settings (base/development/production), urls, wsgi
apps/
  core/                 TenantModel base, managers, middleware, permissions
                        registry, money helpers, error pages, seed command
  accounts/             User (email login), Role, Membership, LoginHistory
  tenants/              Business, BusinessSettings, registration, onboarding
  subscriptions/        Plan, Subscription, Coupon, payments, limit service
  branches/             Branch, Warehouse
  catalog/              Category, Brand, Unit, TaxRate, Product, Variant
  inventory/            StockLevel, StockMovement (ledger), transfers,
                        adjustments, counts (services + workflows)
  customers/            Customer, CustomerPayment, balances, statements
  suppliers/            Supplier, SupplierPayment
  purchases/            Purchase, items, returns (services)
  sales/                PaymentMethod, Sale, SaleItem, SalePayment,
                        InvoiceSequence, HeldSale, SaleReturn (services)
  registers/            CashRegister, Shift (open/close/X/Z services)
  expenses/             ExpenseCategory, Expense (approval workflow)
  reports/              query registry, dashboard, CSV/XLSX/PDF exports
  notifications/        in-app Notification + notify services
  audit/                immutable AuditLog + log() service
  platformadmin/        SaaS admin, SupportAccessGrant, Announcement
  api/                  DRF v1 (token auth, tenant + plan gated)
```

## Multi-tenancy & data isolation

- `tenants.Business` is the tenant root. Every tenant-owned model extends
  `core.TenantModel`: mandatory `business` FK + non-guessable `public_id`
  UUID used in all URLs.
- `TenantManager.for_business(business)` is the **single funnel** for
  tenant filtering. Views never query tenant models without it.
- `core.middleware.BusinessMiddleware` resolves `request.business` and
  `request.membership` from the user's active membership (session-pinned
  for multi-business users).
- `core.mixins.get_tenant_object()` resolves objects and raises **404**
  for cross-tenant ids (indistinguishable from nonexistent).
- Forms scope all FK choices to the tenant (`TenantStyledModelForm`).
- Unique constraints are tenant-scoped (SKU, barcode, invoice number,
  branch code, customer/supplier codes…).
- `tests/test_tenancy.py` proves isolation across pages, JSON endpoints,
  reports, exports, and the API.

## Permissions

`core/permissions.py` defines ~45 permission codes and 10 default role
templates created per business at registration. `Membership.has_perm()`
is enforced in views via `require_permission` / mixins; templates only
*hide* menus. The owner role implicitly has every permission. Custom
roles are a plan feature.

## Subscription enforcement

`subscriptions.services` provides `check_limit`, `limit_state`,
`has_feature`, `require_operational`. The `SubscriptionMiddleware` blocks
write requests for non-operational subscriptions (expired / suspended /
cancelled) while keeping data readable. Limits are enforced at creation
points (branches, users, warehouses, products, customers, monthly
invoices). Existing data is never deleted on downgrade or expiry.

## Inventory logic

Stock is **never** edited as a bare number:

- `inventory.services.record_movement()` writes an immutable
  `StockMovement` and updates the cached `StockLevel` inside one DB
  transaction (with `select_for_update` row locking on PostgreSQL).
- Negative stock is blocked/warned/allowed per business policy.
- Moving-average cost is updated on opening stock and purchase receipts.
- Transfers move stock out on dispatch and in on receipt; adjustments and
  approved count variances flow through the same ledger.

## Financial logic

- All money/quantities are `Decimal` stored at 3 decimal places (covers
  OMR/KWD/BHD); each business configures display precision 0–3.
- `sales.services.complete_sale()` is the single transactional entry
  point: validates tenancy, prices, discounts (permission + cap), credit
  limits, computes tax (inclusive/exclusive, per-product override),
  snapshots cost and gross profit per line, allocates a concurrency-safe
  invoice number (`InvoiceSequence` + `select_for_update`), writes
  payments, deducts stock, updates customer balances.
- Completed sales are immutable; corrections use `void_sale()` or
  `process_return()` (quantity-capped, refund to cash/card/store
  credit/customer account, stock restocking optional).
- Gross profit = net selling value (excl. tax) − cost snapshot.
  Estimated net profit = gross profit − operating expenses (labelled
  *estimated* in the UI).

## Reports

`reports/queries.py` holds a registry of ~19 report functions returning
`{columns, rows, totals}`. The same dataset feeds the HTML table and the
CSV/XLSX/PDF exporters, so exports always match the visible filters.

## Performance notes

- Tenant + date composite indexes on hot tables (sales, movements).
- `select_related`/`prefetch_related` on list views; server-side
  pagination everywhere; POS product grid capped at 60 items per query;
  report row caps (2000–5000) with date filters encouraged.
- Dashboard aggregates are single GROUP BY queries; Redis cache slot is
  configured when `REDIS_URL` is set.
