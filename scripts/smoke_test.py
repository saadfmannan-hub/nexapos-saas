"""Authenticated smoke test of all major pages against the live database.

Run:  python manage.py shell -c "exec(open('scripts/smoke_test.py').read())"
Requires the demo business (python manage.py seed_demo).
"""
from django.test import Client

from apps.accounts.models import User

client = Client()
owner = User.objects.get(email="demo-owner@example.com")
client.force_login(owner)

PAGES = [
    "/dashboard/",
    "/sales/pos/",
    "/sales/",
    "/sales/returns/",
    "/customers/",
    "/products/",
    "/products/categories/",
    "/products/brands/",
    "/products/units/",
    "/products/taxes/",
    "/products/import/",
    "/inventory/stock/",
    "/inventory/movements/",
    "/inventory/transfers/",
    "/inventory/adjustments/",
    "/inventory/counts/",
    "/purchases/",
    "/purchases/new/",
    "/suppliers/",
    "/registers/",
    "/expenses/",
    "/expenses/categories/",
    "/reports/",
    "/reports/sales_summary/",
    "/reports/product_sales/",
    "/reports/profit/",
    "/reports/current_stock/",
    "/reports/receivables/",
    "/reports/shifts/",
    "/notifications/",
    "/audit/",
    "/branches/",
    "/accounts/users/",
    "/accounts/roles/",
    "/settings/",
    "/settings/profile/",
    "/subscription/",
    "/onboarding/",
    "/accounts/profile/",
]

failures = []
for page in PAGES:
    response = client.get(page)
    status = response.status_code
    marker = "OK " if status == 200 else "FAIL"
    if status != 200:
        failures.append((page, status))
    print(f"{marker} {status} {page}")

# Detail pages from demo data
from apps.sales.models import Sale

sale = Sale.objects.filter(business__name="Demo Business").first()
if sale:
    for suffix in ("", "invoice/", "receipt/", "invoice.pdf"):
        page = f"/sales/{sale.public_id}/{suffix}"
        response = client.get(page)
        if response.status_code != 200:
            failures.append((page, response.status_code))
        print(f"{'OK ' if response.status_code == 200 else 'FAIL'} "
              f"{response.status_code} {page}")

# Exports
for export in ("csv", "xlsx", "pdf"):
    page = f"/reports/sales_summary/?export={export}"
    response = client.get(page)
    if response.status_code != 200:
        failures.append((page, response.status_code))
    print(f"{'OK ' if response.status_code == 200 else 'FAIL'} "
          f"{response.status_code} {page}")

print()
if failures:
    print(f"SMOKE TEST FAILED: {len(failures)} page(s):")
    for page, status in failures:
        print(f"  {status} {page}")
else:
    print("SMOKE TEST PASSED: all pages returned 200.")
