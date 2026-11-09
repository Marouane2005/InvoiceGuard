"""Deterministic invoice validator. Single source of truth for invoice math.

Usage:
    python validator.py invoice.json      # exit 0 = clean, 1 = needs review, 2 = bad input
    cat invoice.json | python validator.py
"""
from __future__ import annotations

import argparse
import json
import math
import sys

TOL = 0.01


def _num(value, path: str, issues: list):
    """Return value as float, or None. Non-numeric (non-None) values are reported."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        issues.append(f"{path} is not a valid number: {value!r}")
        return None
    return float(value)


def validate_invoice(invoice: dict) -> dict:
    issues: list = []

    summary = invoice.get("financial_summary")
    if not isinstance(summary, dict):
        issues.append("financial_summary is missing or not an object")
        summary = {}

    raw_items = invoice.get("line_items")
    if raw_items is None or raw_items == []:
        issues.append("no line items extracted")
        items = []
    elif not isinstance(raw_items, list):
        issues.append("line_items is not a list")
        items = []
    else:
        items = raw_items

    grand_total = _num(summary.get("grand_total"), "grand_total", issues)
    subtotal = _num(summary.get("subtotal"), "subtotal", issues)
    tax = _num(summary.get("tax_amount"), "tax_amount", issues)
    discount = _num(summary.get("discount_amount"), "discount_amount", issues)
    shipping = _num(summary.get("shipping_amount"), "shipping_amount", issues)
    rounding = _num(summary.get("rounding_adjustment"), "rounding_adjustment", issues)

    if summary.get("grand_total") is None:
        issues.append("grand_total is missing")
    if summary.get("subtotal") is None:
        issues.append("subtotal is missing")

    items_sum = 0.0
    items_complete = bool(items)  # derived sums are only trustworthy when every line total is a real number
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            issues.append(f"line_items[{i}] is not an object")
            items_complete = False
            continue
        qty = _num(item.get("quantity"), f"line_items[{i}].quantity", issues)
        unit = _num(item.get("unit_price"), f"line_items[{i}].unit_price", issues)
        raw_total = item.get("total_amount")
        total = _num(raw_total, f"line_items[{i}].total_amount", issues)
        if total is None:
            if raw_total is None:
                issues.append(f"line_items[{i}].total_amount is missing")
            items_complete = False
            continue
        items_sum += total
        if qty is not None and unit is not None and not math.isclose(qty * unit, total, abs_tol=TOL):
            issues.append(f"line_items[{i}]: {qty:g} x {unit:g} = {qty * unit:.2f}, but total_amount is {total:.2f}")

    if subtotal is not None and items_complete and not math.isclose(items_sum, subtotal, abs_tol=TOL):
        issues.append(f"line items sum to {items_sum:.2f}, but subtotal is {subtotal:.2f}")

    base = subtotal if subtotal is not None else (items_sum if items_complete else None)
    if grand_total is not None and base is not None:
        # a discount always reduces the total, whichever sign the reader copied; rounding keeps its sign
        expected = base - abs(discount or 0.0) + (shipping or 0.0) + (rounding or 0.0) + (tax or 0.0)
        if not math.isclose(expected, grand_total, abs_tol=TOL):
            issues.append(f"expected total {expected:.2f}, but grand_total is {grand_total:.2f}")

    warnings = invoice.get("ocr_warnings")
    if warnings is None:
        issues.append("ocr_warnings field missing (schema violation)")
    elif not isinstance(warnings, list):
        issues.append("ocr_warnings is not a list (schema violation)")
    else:
        issues.extend(f"ocr_warning: {w}" for w in warnings)

    return {"is_valid": not issues, "requires_review": bool(issues), "issues": issues}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validate extracted invoice JSON.")
    parser.add_argument("path", nargs="?", default="-", help="JSON file path, or - for stdin")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read() if args.path == "-" else open(args.path, encoding="utf-8").read()
        invoice = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2
    if not isinstance(invoice, dict):
        print("input error: top-level JSON value must be an object", file=sys.stderr)
        return 2
    result = validate_invoice(invoice)
    print(json.dumps(result, indent=2))
    return 1 if result["requires_review"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
