"""Shared, transaction-bound data for customer-facing print headers."""
from dataclasses import dataclass


@dataclass(frozen=True)
class PrintHeader:
    business_name: str
    branch_name: str
    branch_address: str
    phone: str
    commercial_registration: str
    tax_registration_number: str


def build_print_header(sale, *, include_legal=True):
    """Resolve header values from the historical transaction branch."""
    business = sale.business
    branch = sale.branch
    return PrintHeader(
        business_name=(business.name or "").strip(),
        branch_name=(branch.name or "").strip(),
        branch_address=(branch.address or "").strip(),
        phone=(branch.phone or business.phone or "").strip(),
        commercial_registration=(
            (business.commercial_registration or "").strip()
            if include_legal else ""
        ),
        tax_registration_number=(
            (business.tax_registration_number or "").strip()
            if include_legal else ""
        ),
    )
