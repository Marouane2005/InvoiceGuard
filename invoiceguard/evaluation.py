"""Measures how well the reader plus the checks perform on invoices YOU have labelled.

The number that matters most is `clean_wrong`: invoices that passed every check but contain a wrong value.
Those are the ones nobody looks at. A case is a file (pdf/png/jpg/gif/webp) plus `<name>.expected.json`.
Only fields present in the expected file are scored.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import policy
from .media import MEDIA_EXT, detect_media_type
from .validator import validate_invoice

META_FIELDS = policy.META_KEYS
SUMMARY_FIELDS = policy.SUMMARY_KEYS
_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"}


@dataclass(frozen=True)
class Case:
    name: str
    data: bytes
    expected: dict


def load_cases(directory) -> tuple:
    """Returns (cases, problems). Problems are files skipped, with the reason."""
    cases, problems = [], []
    for path in sorted(Path(directory).iterdir()):
        if path.suffix.lower() not in _EXTENSIONS:
            continue
        expected = path.with_name(path.stem + ".expected.json")
        if not expected.exists():
            problems.append(f"{path.name}: skipped, no {expected.name}")
            continue
        try:
            data = path.read_bytes()
            exp = json.loads(expected.read_text())
        except (OSError, ValueError) as exc:
            problems.append(f"{path.name}: skipped ({exc.__class__.__name__})")
            continue
        if detect_media_type(data) is None or not isinstance(exp, dict):
            problems.append(f"{path.name}: skipped, not a supported file or the expected file is not an object")
            continue
        cases.append(Case(path.name, data, exp))
    return cases, problems


def _norm(value):
    return " ".join(value.casefold().split()) if isinstance(value, str) else value


def same(got, want) -> bool:
    if isinstance(got, bool) or isinstance(want, bool):  # True must never count as 1.0
        return isinstance(got, bool) and isinstance(want, bool) and got == want
    if isinstance(got, (int, float)) and not isinstance(got, bool) and isinstance(want, (int, float)) and not isinstance(want, bool):
        return abs(got - want) <= 0.01
    return _norm(got) == _norm(want)


def _compare(invoice: dict, expected: dict) -> tuple:
    """Returns (scored, wrong) where wrong maps field -> (got, expected)."""
    scored, wrong = [], {}
    for field in META_FIELDS:
        if field in (expected.get("invoice_metadata") or {}):
            scored.append(field)
            got, want = invoice["invoice_metadata"].get(field), expected["invoice_metadata"][field]
            if not same(got, want):
                wrong[field] = (got, want)
    for field in SUMMARY_FIELDS:
        if field in (expected.get("financial_summary") or {}):
            scored.append(field)
            got, want = invoice["financial_summary"].get(field), expected["financial_summary"][field]
            if not same(got, want):
                wrong[field] = (got, want)
    if "line_items" in expected:
        scored.append("line_items")
        got_items = invoice["line_items"] if isinstance(invoice["line_items"], list) else []
        want_items = expected["line_items"]
        ok = len(got_items) == len(want_items)
        for g, w in zip(got_items, want_items):
            ok = ok and isinstance(g, dict) and all(same(g.get(k), v) for k, v in w.items())
        if not ok:
            wrong["line_items"] = (got_items, want_items)
    return scored, wrong


def evaluate(cases: list, extractor, default_currency: Optional[str] = None) -> dict:
    details = []
    field_total: dict[str, int] = {}
    field_right: dict[str, int] = {}
    for case in cases:
        media = detect_media_type(case.data)
        try:
            result = extractor.extract(case.data, media)
        except Exception as exc:
            details.append({"case": case.name, "outcome": "read_failed", "error": str(exc)[:200]})
            continue
        invoice, policy_issues = policy.apply_policy(policy.normalize_invoice(result.invoice), default_currency)
        issues = validate_invoice(invoice)["issues"] + policy_issues
        scored, wrong = _compare(invoice, case.expected)
        for field in scored:
            field_total[field] = field_total.get(field, 0) + 1
            field_right[field] = field_right.get(field, 0) + (0 if field in wrong else 1)
        flagged, correct = bool(issues), not wrong
        outcome = ("flagged_correct" if correct else "flagged_wrong") if flagged else ("clean_correct" if correct else "clean_wrong")
        details.append({"case": case.name, "outcome": outcome, "issues": issues,
                        "wrong_fields": {k: {"got": v[0], "expected": v[1]} for k, v in wrong.items()},
                        "tokens": [result.input_tokens, result.output_tokens]})
    counts = {k: sum(1 for d in details if d["outcome"] == k)
              for k in ("clean_correct", "clean_wrong", "flagged_wrong", "flagged_correct", "read_failed")}
    read = len(details) - counts["read_failed"]
    clean = counts["clean_correct"] + counts["clean_wrong"]
    return {
        "documents": len(details), "read_failed": counts["read_failed"], "counts": counts,
        "auto_pass_rate": round(clean / read, 4) if read else None,
        "wrong_among_auto_passed": round(counts["clean_wrong"] / clean, 4) if clean else None,
        "review_rate": round((read - clean) / read, 4) if read else None,
        "unneeded_reviews": counts["flagged_correct"],
        "field_accuracy": {f: round(field_right[f] / field_total[f], 4) for f in sorted(field_total)},
        "cases": details,
    }


def render_markdown(result: dict) -> str:
    c = result["counts"]
    lines = ["# Accuracy evaluation", "",
             f"Documents: **{result['documents']}**. Could not be read: **{result['read_failed']}**.", "",
             "| Outcome | Count | Meaning |", "|---|---|---|",
             f"| Passed, all correct | {c['clean_correct']} | Saved a human's time |",
             f"| **Passed, something wrong** | **{c['clean_wrong']}** | **Nobody looks at these. This is the risk number** |",
             f"| Sent to review, was wrong | {c['flagged_wrong']} | The system did its job |",
             f"| Sent to review, was correct | {c['flagged_correct']} | Unneeded review (costs time, not money) |", "",
             f"- Automatically passed: {result['auto_pass_rate']}",
             f"- Wrong among the automatically passed: **{result['wrong_among_auto_passed']}**",
             f"- Sent to a person: {result['review_rate']}", "", "## Field accuracy", "", "| Field | Correct |", "|---|---|"]
    lines += [f"| {f} | {v:.1%} |" for f, v in result["field_accuracy"].items()]
    bad = [d for d in result["cases"] if d["outcome"] == "clean_wrong"]
    if bad:
        lines += ["", "## Passed but wrong (fix these first)", ""]
        for d in bad:
            lines.append(f"- `{d['case']}`: " + "; ".join(f"{k}: got {v['got']!r}, expected {v['expected']!r}" for k, v in d["wrong_fields"].items()))
    return "\n".join(lines) + "\n"
