"""Business rules around the validator: clean up model output, enforce required fields.

validator.py checks the arithmetic. This module checks everything else, deterministically.
"""
from __future__ import annotations

import copy
import re
from datetime import date, timedelta
from typing import Optional

META_KEYS = ("invoice_number", "vendor_name", "issue_date", "due_date", "currency")
ITEM_KEYS = ("description", "quantity", "unit_price", "total_amount")
SUMMARY_KEYS = ("subtotal", "discount_amount", "shipping_amount", "tax_amount", "rounding_adjustment", "grand_total")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


def _valid_date(value) -> Optional[date]:
    if not isinstance(value, str) or not _ISO_DATE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def normalize_invoice(raw) -> dict:
    """Force the expected shape. Unknown keys are dropped; wrong types are left for the validator to flag."""
    raw = raw if isinstance(raw, dict) else {}
    warnings: list = []
    meta_in = raw.get("invoice_metadata") if isinstance(raw.get("invoice_metadata"), dict) else {}
    sum_in = raw.get("financial_summary") if isinstance(raw.get("financial_summary"), dict) else {}
    meta = {k: meta_in.get(k) for k in META_KEYS}
    for key in ("invoice_number", "vendor_name"):
        value = meta[key]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            meta[key] = str(value)
        elif value is not None and not isinstance(value, str):
            warnings.append(f"invoice_metadata.{key}: unexpected format; set to null")
            meta[key] = None
    items_raw = raw.get("line_items")
    if isinstance(items_raw, list):
        items = [{k: it.get(k) for k in ITEM_KEYS} if isinstance(it, dict) else it for it in items_raw]
    else:
        items = items_raw
    w = raw.get("ocr_warnings")
    if isinstance(w, list):
        warnings = [str(x) for x in w] + warnings
    elif w is not None:
        warnings.append("ocr_warnings came back in an unexpected format")
    return {
        "invoice_metadata": meta,
        "line_items": items,
        "financial_summary": {k: sum_in.get(k) for k in SUMMARY_KEYS},
        "ocr_warnings": warnings,
    }


def apply_policy(invoice: dict, default_currency: Optional[str] = None, today: Optional[date] = None) -> tuple:
    """Returns (cleaned invoice, extra issues). Never mutates the input."""
    inv = copy.deepcopy(invoice)
    meta, warnings, issues = inv["invoice_metadata"], inv["ocr_warnings"], []

    for field in ("issue_date", "due_date"):
        value = meta.get(field)
        if value is not None and _valid_date(value) is None:
            warnings.append(f"invoice_metadata.{field}: {value!r} is not a valid YYYY-MM-DD date; set to null")
            meta[field] = None

    cur = meta.get("currency")
    if cur is not None:
        norm = cur.strip().upper() if isinstance(cur, str) else None
        if norm and _CURRENCY.match(norm):
            meta["currency"] = norm
        else:
            warnings.append(f"invoice_metadata.currency: {cur!r} is not a 3-letter currency code; set to null")
            meta["currency"] = None
    if meta.get("currency") is None:
        if default_currency:
            meta["currency"] = default_currency
        else:
            issues.append("currency is missing")

    issued, due = _valid_date(meta.get("issue_date")), _valid_date(meta.get("due_date"))
    if issued and due and due < issued:
        issues.append("due_date is before issue_date")
    if issued:  # catches misread years (2026 read as 2028) without blocking normal back-dated uploads
        if issued > (today or date.today()) + timedelta(days=31):
            issues.append("issue_date is more than a month in the future")
        if issued.year < 1990:
            issues.append("issue_date is before 1990")
    for key in ("invoice_number", "vendor_name"):
        if meta.get(key) is None:
            issues.append(f"{key} is missing")
    return inv, issues
