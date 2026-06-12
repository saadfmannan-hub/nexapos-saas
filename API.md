# API (v1)

A read-first REST foundation for future mobile apps, e-commerce and
accounting integrations.

## Rules

- **Versioned paths**: everything lives under `/api/v1/`.
- **Authentication**: session (browser) or DRF token.
- **Tenancy**: every request is resolved to the caller's active business
  membership; all querysets are tenant-filtered. Object URLs use UUID
  `public_id`s.
- **Plan gating**: the business's subscription plan must have
  `feature_api_access` enabled, otherwise requests return 403.
- **Permissions**: each endpoint additionally requires the corresponding
  business permission (e.g. `sales.view`).
- **Rate limiting**: DRF throttling (1000/h per user, 100/h anonymous).
- **Pagination**: page-number pagination, 25 per page.

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/health/` | Unauthenticated health check |
| POST | `/api/v1/auth/token/` | Obtain token (`username`=email, `password`) |
| GET | `/api/v1/me/` | Current user, business, role, permissions |
| GET | `/api/v1/products/` `/{public_id}/` | Products with variants |
| GET | `/api/v1/categories/` | Categories |
| GET | `/api/v1/customers/` | Customers |
| GET | `/api/v1/sales/` `/{public_id}/` | Sales with line items |

## Example

```bash
curl -X POST https://pos.example.com/api/v1/auth/token/ \
     -d "username=owner@example.com&password=secret"
# {"token": "abc123..."}

curl -H "Authorization: Token abc123..." \
     https://pos.example.com/api/v1/products/
```

## Future integrations

The service layer (`sales.services.complete_sale`,
`purchases.services.*`, `inventory.services.record_movement`) is
transport-agnostic — write endpoints can be added by serializing into
those functions without duplicating business rules. Payment gateways
plug in at `subscriptions.SubscriptionPayment` (method `gateway`).
OpenAPI/Swagger can be generated with `drf-spectacular` as a follow-up.
