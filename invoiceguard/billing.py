"""Draft monthly statements from measured usage. Exact decimal money maths. Drafts only: nothing is sent or charged."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

NOTE = "Draft only. No tax is included. Check each line against the customer's plan before invoicing anyone."


def parse_amount(text, name: str) -> Decimal:
    try:
        value = Decimal(str(text).strip())
    except InvalidOperation:
        raise ValueError(f"{name} must be a number such as 0.25, got {text!r}") from None
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a number of 0 or more")
    return value


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def draft_statements(rows: list, price_per_document: Decimal, included_documents: int = 0,
                     base_fee: Decimal = Decimal(0), currency: str = "USD") -> list:
    if included_documents < 0:
        raise ValueError("included documents must be 0 or more")
    out = []
    for row in rows:
        billable = max(0, row["documents"] - included_documents)
        usage_charge = money(price_per_document * billable)
        out.append({
            "customer_id": row["id"], "customer": row["name"], "month": row["month"], "documents": row["documents"],
            "included": included_documents, "billable_documents": billable, "base_fee": str(money(base_fee)),
            "usage_charge": str(usage_charge), "total": str(money(base_fee) + usage_charge), "currency": currency,
        })
    return out
