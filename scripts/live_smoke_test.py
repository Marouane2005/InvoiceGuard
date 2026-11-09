"""Run this ONCE with your own key to prove the real AI reading works end to end.

    ANTHROPIC_API_KEY=... python scripts/live_smoke_test.py

It builds a small PDF invoice, reads it with the real provider, runs every check, and compares the
result with the known answer. It costs one document of API usage. Exit code 0 = everything matched.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from make_sample_pdf import SAMPLE_INVOICE_LINES, make_pdf  # noqa: E402

from invoiceguard import policy  # noqa: E402
from invoiceguard.config import Settings  # noqa: E402
from invoiceguard.extractor import ClaudeExtractor, ExtractionError  # noqa: E402
from invoiceguard.validator import validate_invoice  # noqa: E402

EXPECTED = {"vendor_name": "ACME Supplies Ltd", "invoice_number": "SMK-1001", "issue_date": "2025-03-01",
            "due_date": "2025-03-31", "currency": "USD", "subtotal": 120.0, "tax_amount": 12.0, "grand_total": 132.0}


def main() -> int:
    settings = Settings.from_env()
    if not settings.anthropic_api_key:
        print("Set ANTHROPIC_API_KEY first.", file=sys.stderr)
        return 2
    extractor = ClaudeExtractor(settings.anthropic_api_key, settings.model, settings.anthropic_base_url)
    try:
        result = extractor.extract(make_pdf(SAMPLE_INVOICE_LINES), "application/pdf")
    except ExtractionError as exc:
        print(f"Reading failed: {exc}", file=sys.stderr)
        return 1
    invoice, policy_issues = policy.apply_policy(policy.normalize_invoice(result.invoice))
    verdict = validate_invoice(invoice)
    meta, summary = invoice["invoice_metadata"], invoice["financial_summary"]
    got = {**{k: meta[k] for k in ("vendor_name", "invoice_number", "issue_date", "due_date", "currency")}, **summary}
    wrong = {k: (got.get(k), v) for k, v in EXPECTED.items() if got.get(k) != v}
    print(json.dumps({"model": result.model, "tokens": [result.input_tokens, result.output_tokens],
                      "issues": verdict["issues"] + policy_issues, "mismatches (got, expected)": wrong}, indent=2))
    ok = not wrong and not verdict["issues"] and not policy_issues
    print("PASS: reading matched the known answer." if ok else "CHECK: see mismatches above.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
