import copy

from invoiceguard.policy import apply_policy, normalize_invoice
from conftest import GOOD_RAW


def test_normalize_drops_unknown_keys_and_fills_shape():
    raw = {"invoice_metadata": {"vendor_name": "A", "evil": 1}, "line_items": [{"description": "x", "extra": 1}],
           "financial_summary": {"grand_total": 5}, "ocr_warnings": ["w"], "other": 1}
    out = normalize_invoice(raw)
    assert set(out) == {"invoice_metadata", "line_items", "financial_summary", "ocr_warnings"}
    assert out["invoice_metadata"]["vendor_name"] == "A" and "evil" not in out["invoice_metadata"]
    assert out["line_items"] == [{"description": "x", "quantity": None, "unit_price": None, "total_amount": None}]
    assert out["financial_summary"] == {"subtotal": None, "discount_amount": None, "shipping_amount": None, "tax_amount": None,
                                        "rounding_adjustment": None, "grand_total": 5}


def test_normalize_handles_garbage():
    out = normalize_invoice("not a dict")
    assert out["line_items"] is None and out["ocr_warnings"] == []
    out = normalize_invoice({"ocr_warnings": "oops", "invoice_metadata": {"invoice_number": 9942, "vendor_name": ["x"]}})
    assert out["invoice_metadata"]["invoice_number"] == "9942" and out["invoice_metadata"]["vendor_name"] is None
    assert any("ocr_warnings" in w for w in out["ocr_warnings"]) and any("vendor_name" in w for w in out["ocr_warnings"])


def test_clean_invoice_has_no_issues_and_input_is_not_mutated():
    inv = normalize_invoice(GOOD_RAW)
    before = copy.deepcopy(inv)
    out, issues = apply_policy(inv)
    assert issues == [] and inv == before and out["invoice_metadata"]["currency"] == "USD"


def test_ambiguous_or_invalid_dates_become_null_with_warning():
    for bad in ("12/05/23", "2023-13-45", "yesterday", 20231205):
        inv = normalize_invoice(GOOD_RAW)
        inv["invoice_metadata"]["issue_date"] = bad
        out, _ = apply_policy(inv)
        assert out["invoice_metadata"]["issue_date"] is None
        assert any("issue_date" in w and "set to null" in w for w in out["ocr_warnings"])


def test_currency_rules():
    inv = normalize_invoice(GOOD_RAW)
    inv["invoice_metadata"]["currency"] = "usd"
    assert apply_policy(inv)[0]["invoice_metadata"]["currency"] == "USD"

    inv["invoice_metadata"]["currency"] = "$"
    out, issues = apply_policy(inv)
    assert out["invoice_metadata"]["currency"] is None and "currency is missing" in issues
    assert any("currency" in w for w in out["ocr_warnings"])

    out, issues = apply_policy(inv, default_currency="MAD")
    assert out["invoice_metadata"]["currency"] == "MAD" and "currency is missing" not in issues


def test_required_fields_and_date_order():
    inv = normalize_invoice(GOOD_RAW)
    inv["invoice_metadata"].update(vendor_name=None, invoice_number=None, due_date="2023-11-01")
    _, issues = apply_policy(inv)
    assert {"vendor_name is missing", "invoice_number is missing", "due_date is before issue_date"} <= set(issues)


from datetime import date


def _dated(issue):
    inv = normalize_invoice(GOOD_RAW)
    inv["invoice_metadata"]["issue_date"], inv["invoice_metadata"]["due_date"] = issue, None
    return apply_policy(inv, today=date(2026, 10, 25))[1]


def test_misread_years_are_caught_but_back_dated_uploads_are_not():
    assert _dated("2026-11-25") == []                                              # 31 days ahead: normal (billing in advance)
    assert _dated("2026-11-26") == ["issue_date is more than a month in the future"]  # 32 days: probably a misread year
    assert _dated("2028-10-25") == ["issue_date is more than a month in the future"]
    assert _dated("2019-05-01") == []                                              # old invoices are normal in a backlog
    assert _dated("1989-12-31") == ["issue_date is before 1990"]
