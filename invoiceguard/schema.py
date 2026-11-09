"""The invoice structure the reader must fill in."""
from __future__ import annotations

_STR = {"type": ["string", "null"]}
_NUM = {"type": ["number", "null"]}


def _obj(properties: dict) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


INVOICE_SCHEMA: dict = _obj({
    "invoice_metadata": _obj({
        "invoice_number": {**_STR, "description": "Invoice number exactly as printed, or null."},
        "vendor_name": {**_STR, "description": "Name of the company issuing the invoice, or null."},
        "issue_date": {**_STR, "description": "YYYY-MM-DD. null if missing or if day/month order is unclear."},
        "due_date": {**_STR, "description": "YYYY-MM-DD. null if missing or if day/month order is unclear."},
        "currency": {**_STR, "description": "3-letter ISO 4217 code only if stated or unambiguous (a bare $ is ambiguous), else null."},
    }),
    "line_items": {
        "type": "array",
        "items": _obj({
            "description": _STR,
            "quantity": _NUM,
            "unit_price": _NUM,
            "total_amount": _NUM,
        }),
    },
    "financial_summary": _obj({
        "subtotal": {**_NUM, "description": "Sum of the line items before any invoice-level discount, shipping, rounding or tax, as printed."},
        "discount_amount": {**_NUM, "description": "Invoice-level discount taken off the subtotal, as a positive number. null if none."},
        "shipping_amount": {**_NUM, "description": "Shipping or delivery charged outside the line items, as a positive number. null if none."},
        "tax_amount": {**_NUM, "description": "Total tax (VAT, sales tax) as printed. null if none is printed."},
        "rounding_adjustment": {**_NUM, "description": "Rounding line, keeping its sign (negative reduces the total). null if none."},
        "grand_total": {**_NUM, "description": "Final amount due as printed."},
    }),
    "ocr_warnings": {
        "type": "array",
        "items": {"type": "string"},
        "description": "One entry per ambiguity, naming the field, e.g. \"line_items[0].total_amount: '1S0.00' unreadable (150.00 or 1S0.00); set to null\".",
    },
})

EXTRACTION_TOOL: dict = {
    "name": "record_invoice",
    "description": "Record the data read from the invoice. Call exactly once.",
    "input_schema": INVOICE_SCHEMA,
}
