# Project Plan

## Goal

A production-ready, multi-tenant POS & business management SaaS sellable
to many independent retail/service businesses, with nothing hardcoded
for any specific company, country, currency or tax regime.

## Architectural decisions

| Decision | Choice | Rationale |
|---|---|---|
| Tenancy | Row-level (shared schema) | Simplest safe model for v1; centralized `for_business()` funnel + tenant-scoped constraints + UUID URLs |
| Stack | Django 5.2 + Bootstrap 5 + Alpine.js | Fast, maintainable, one consistent UI framework; API-ready via DRF |
| Money | Decimal stored at 3 dp, display precision per business | OMR/KWD/BHD compatible without hardcoding any currency |
| Stock | Append-only ledger + cached levels | Auditability; "never edit stock without history" |
| Financial records | Immutable snapshots | Old invoices keep original prices/taxes/costs forever |
| PDF | xhtml2pdf | Pure-Python, works on Windows dev machines and Linux servers |
| Background jobs | Celery optional (eager without broker) | Local dev needs no Redis; production compose ships worker+beat |

## Phases (all delivered)

1. Foundation — tenancy, auth, roles, registration, subscriptions, base UI
2. Catalog & inventory foundation — products, variants, taxes, ledger, import
3. Core POS — cart, payments, invoices, receipts, stock deduction
4. Customers & credit — receivables, statements, collections
5. Purchases & suppliers — POs, receiving, payables, returns
6. Advanced inventory — transfers, adjustments, counts, valuation
7. Returns & registers — refunds, store credit, shifts, X/Z
8. Expenses & reporting — approvals, dashboard, exports
9. SaaS administration — platform admin, plans, suspension, support access
10. Commercial readiness — audit, notifications, API, PWA, Docker, docs, QA

Validation after each phase: migrations, `manage.py check`, automated
tests (95 passing), authenticated smoke test of every page, demo seed
exercising all services end-to-end.
