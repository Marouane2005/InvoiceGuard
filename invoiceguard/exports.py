"""Custom CSV layouts, so an export can match whatever import file a customer's accounting tool wants.

A template is plain data (no code):
  {"columns": [{"header": "Supplier", "field": "vendor_name"},
               {"header": "Date", "field": "issue_date", "date_format": "%d/%m/%Y"},
               {"header": "Account", "value": "4000"},
               {"header": "Amount", "field": "grand_total"}],
   "delimiter": ";", "decimal": ",", "decimals": 2, "include_header": true, "utf8_bom": false}
"""
from __future__ import annotations

import csv
import io
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional

from .store import _safe_cell

FIELDS = ("document_id", "filename", "status", "invoice_number", "vendor_name", "issue_date", "due_date", "currency",
          "subtotal", "discount_amount", "shipping_amount", "tax_amount", "rounding_adjustment", "grand_total", "line_items_count", "created_at")
DATE_FIELDS = ("issue_date", "due_date")
NUMBER_FIELDS = ("subtotal", "discount_amount", "shipping_amount", "tax_amount", "rounding_adjustment", "grand_total")
DELIMITERS = (",", ";", "\t")
MAX_COLUMNS = 30
_TEMPLATE_KEYS = {"columns", "delimiter", "include_header", "utf8_bom", "decimal", "decimals"}
_COLUMN_KEYS = {"header", "field", "value", "date_format"}


class TemplateError(ValueError):
    """The template is not valid. The message says which part and how to fix it."""


def validate_template(raw) -> dict:
    if not isinstance(raw, dict):
        raise TemplateError("the template must be an object with a 'columns' list")
    unknown = set(raw) - _TEMPLATE_KEYS
    if unknown:
        raise TemplateError(f"unknown option(s): {', '.join(sorted(map(str, unknown)))}. Allowed: {', '.join(sorted(_TEMPLATE_KEYS))}")
    columns = raw.get("columns")
    if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_COLUMNS:
        raise TemplateError(f"'columns' must be a list of 1 to {MAX_COLUMNS} columns")
    cleaned = []
    for i, col in enumerate(columns, 1):
        if not isinstance(col, dict) or set(col) - _COLUMN_KEYS:
            raise TemplateError(f"column {i}: use only these keys: {', '.join(sorted(_COLUMN_KEYS))}")
        header = col.get("header")
        if not isinstance(header, str) or not 1 <= len(header) <= 100:
            raise TemplateError(f"column {i}: 'header' must be text of 1 to 100 characters")
        if ("field" in col) == ("value" in col):
            raise TemplateError(f"column {i}: give exactly one of 'field' or 'value'")
        item = {"header": header}
        if "field" in col:
            if col["field"] not in FIELDS:
                raise TemplateError(f"column {i}: unknown field {col['field']!r}. Choose from: {', '.join(FIELDS)}")
            item["field"] = col["field"]
        else:
            if not isinstance(col["value"], str) or len(col["value"]) > 100:
                raise TemplateError(f"column {i}: 'value' must be text of at most 100 characters")
            item["value"] = col["value"]
        if col.get("date_format") is not None:
            fmt = col["date_format"]
            if item.get("field") not in DATE_FIELDS:
                raise TemplateError(f"column {i}: 'date_format' only applies to {' and '.join(DATE_FIELDS)}")
            if not isinstance(fmt, str) or "%" not in fmt or len(fmt) > 30:
                raise TemplateError(f"column {i}: 'date_format' must look like %d/%m/%Y")
            try:
                date(2026, 1, 31).strftime(fmt)
            except ValueError:
                raise TemplateError(f"column {i}: 'date_format' {fmt!r} is not a valid date format") from None
            item["date_format"] = fmt
        cleaned.append(item)
    delimiter = raw.get("delimiter", ",")
    if delimiter not in DELIMITERS:
        raise TemplateError("'delimiter' must be a comma, a semicolon, or a tab")
    decimal = raw.get("decimal", ".")
    if decimal not in (".", ","):
        raise TemplateError("'decimal' must be '.' or ','")
    if delimiter == decimal:
        raise TemplateError("the delimiter and the decimal separator cannot be the same character")
    decimals = raw.get("decimals", 2)
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 4:
        raise TemplateError("'decimals' must be a whole number from 0 to 4")
    for key in ("include_header", "utf8_bom"):
        if key in raw and not isinstance(raw[key], bool):
            raise TemplateError(f"'{key}' must be true or false")
    return {"columns": cleaned, "delimiter": delimiter, "decimal": decimal, "decimals": decimals,
            "include_header": raw.get("include_header", True), "utf8_bom": raw.get("utf8_bom", False)}


def _raw_value(record: dict, field: str):
    invoice = record["invoice"]
    meta, summary = invoice.get("invoice_metadata") or {}, invoice.get("financial_summary") or {}
    if field in ("document_id", "filename", "status", "created_at"):
        return record[field]
    if field == "line_items_count":
        items = invoice.get("line_items")
        return len(items) if isinstance(items, list) else 0
    return summary.get(field) if field in NUMBER_FIELDS else meta.get(field)


def _money(value, decimals: int) -> str:
    """Exact half-up rounding (1234.5 with 0 decimals is 1235), never the float formatter's half-to-even."""
    q = Decimal(repr(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)  # never print -0.00
    return f"{q:f}"


def _format(value, col: dict, template: dict) -> str:
    if "value" in col:
        return _safe_cell(col["value"])
    if value is None:
        return ""
    field = col["field"]
    if field in NUMBER_FIELDS and isinstance(value, (int, float)) and not isinstance(value, bool):
        return _money(value, template["decimals"]).replace(".", template["decimal"])
    if field in DATE_FIELDS and "date_format" in col and isinstance(value, str):
        try:
            return date.fromisoformat(value).strftime(col["date_format"])
        except ValueError:
            return value
    return _safe_cell(value) if isinstance(value, str) else str(value)


def render(records: Iterable[dict], template: dict) -> str:
    """Records are oldest first. Spreadsheet formulas in text cells are neutralized."""
    template = validate_template(template)
    out = io.StringIO()
    if template["utf8_bom"]:
        out.write("\ufeff")
    writer = csv.writer(out, delimiter=template["delimiter"])
    if template["include_header"]:
        writer.writerow([c["header"] for c in template["columns"]])
    for record in records:
        writer.writerow([_format(None if "value" in c else _raw_value(record, c["field"]), c, template) for c in template["columns"]])
    return out.getvalue()


def _sample(doc, vendor, number, issue, due, total):
    return {"document_id": doc, "filename": f"{number}.pdf", "status": "clean", "created_at": "2026-10-01T09:00:00+00:00",
            "invoice": {"invoice_metadata": {"invoice_number": number, "vendor_name": vendor, "issue_date": issue, "due_date": due, "currency": "USD"},
                        "line_items": [{"description": "Item", "quantity": 1, "unit_price": total, "total_amount": total}],
                        "financial_summary": {"subtotal": total, "tax_amount": 0.0, "grand_total": total}}}


# Invented data, used only to preview a template without touching any customer's invoices.
SAMPLE_RECORDS = [_sample("0" * 64, "Example Supplies Ltd", "EX-001", "2026-10-01", "2026-10-31", 1234.5),
                  _sample("1" * 64, "Sample Office Co", "EX-002", "2026-10-15", "2026-11-14", 99.99)]
