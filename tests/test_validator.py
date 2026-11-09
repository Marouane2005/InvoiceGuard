import json
import pathlib
import subprocess
import sys

from invoiceguard.validator import validate_invoice

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "invoiceguard" / "validator.py"


def make_invoice(items, subtotal, tax, grand_total, warnings=None):
    return {
        "line_items": items,
        "financial_summary": {"subtotal": subtotal, "tax_amount": tax, "grand_total": grand_total},
        "ocr_warnings": [] if warnings is None else warnings,
    }


def test_happy_path_valid():
    r = validate_invoice(make_invoice([{"quantity": 10, "unit_price": 50.0, "total_amount": 500.0}], 500.0, 50.0, 550.0))
    assert r == {"is_valid": True, "requires_review": False, "issues": []}


def test_misread_unit_price_caught_by_line_item_check():
    r = validate_invoice(make_invoice([{"quantity": 5, "unit_price": 150.0, "total_amount": 150.0}], 150.0, 75.0, 825.0))
    assert r["requires_review"] is True
    assert any("5 x 150" in i for i in r["issues"])
    assert any("expected total" in i for i in r["issues"])


def test_null_grand_total_forces_review():
    r = validate_invoice(make_invoice([{"quantity": 1, "unit_price": 100.0, "total_amount": 100.0}], 100.0, None, None))
    assert r["requires_review"] is True
    assert any("grand_total is missing" in i for i in r["issues"])


def test_null_line_total_forces_review():
    r = validate_invoice(make_invoice([{"quantity": 1, "unit_price": 100.0, "total_amount": None}], None, None, None))
    assert r["requires_review"] is True
    assert any("total_amount is missing" in i for i in r["issues"])


def test_no_line_items_forces_review():
    r = validate_invoice(make_invoice([], 0.0, 0.0, 0.0))
    assert r["requires_review"] is True
    assert any("no line items extracted" in i for i in r["issues"])


def test_float_precision_within_tolerance():
    r = validate_invoice(make_invoice([
        {"quantity": 1, "unit_price": 0.1, "total_amount": 0.1},
        {"quantity": 1, "unit_price": 0.2, "total_amount": 0.2}], 0.3, 0.0, 0.3))
    assert r["is_valid"] is True and r["requires_review"] is False


def test_line_mismatch_even_if_total_matches():
    r = validate_invoice(make_invoice([{"quantity": 2, "unit_price": 100.0, "total_amount": 150.0}], 150.0, 0.0, 150.0))
    assert r["requires_review"] is True
    assert any("2 x 100" in i for i in r["issues"])


# --- regression tests for the two gaps found by execution ---
def test_ambiguous_date_invoice_is_reviewed_via_ocr_warnings():
    inv = make_invoice([{"quantity": 5, "unit_price": 200.0, "total_amount": 1000.0}], 1000.0, 0.0, 1000.0,
                       warnings=["issue_date '12/05/23' ambiguous; set to null"])
    r = validate_invoice(inv)
    assert r["requires_review"] is True
    assert r["issues"] == ["ocr_warning: issue_date '12/05/23' ambiguous; set to null"]


def test_null_subtotal_forces_review():
    r = validate_invoice(make_invoice([{"quantity": 1, "unit_price": 100.0, "total_amount": 100.0}], None, 0.0, 100.0))
    assert r["requires_review"] is True
    assert "subtotal is missing" in r["issues"]


# --- hardening ---
def test_missing_ocr_warnings_key_forces_review():
    inv = make_invoice([{"quantity": 1, "unit_price": 100.0, "total_amount": 100.0}], 100.0, 0.0, 100.0)
    del inv["ocr_warnings"]
    assert "ocr_warnings field missing (schema violation)" in validate_invoice(inv)["issues"]


def test_string_amount_from_model_does_not_crash_and_is_flagged():
    inv = make_invoice([{"quantity": 1, "unit_price": 150.0, "total_amount": "150.00"}], 150.0, 0.0, 150.0)
    r = validate_invoice(inv)
    assert r["requires_review"] is True
    assert any("not a valid number" in i for i in r["issues"])


def test_nan_and_bool_are_rejected():
    inv = make_invoice([{"quantity": True, "unit_price": float("nan"), "total_amount": 1.0}], 1.0, 0.0, 1.0)
    r = validate_invoice(inv)
    assert sum("not a valid number" in i for i in r["issues"]) == 2


def test_wrong_container_types_do_not_crash():
    r = validate_invoice({"line_items": "oops", "financial_summary": [], "ocr_warnings": "x"})
    assert r["requires_review"] is True
    assert "line_items is not a list" in r["issues"]
    assert "financial_summary is missing or not an object" in r["issues"]


def test_cli_exit_codes(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(make_invoice([{"quantity": 1, "unit_price": 10.0, "total_amount": 10.0}], 10.0, 0.0, 10.0)))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(make_invoice([], None, None, None)))
    broken = tmp_path / "broken.json"
    broken.write_text("{nope")
    run = lambda p: subprocess.run([sys.executable, str(SCRIPT), str(p)], capture_output=True, text=True)
    assert run(good).returncode == 0
    assert run(bad).returncode == 1
    assert run(broken).returncode == 2


# --- report root causes, not side effects ---
def test_null_line_total_reports_only_the_cause():
    inv = make_invoice([{"quantity": 5, "unit_price": 150.0, "total_amount": None}], 750.0, 75.0, 825.0)
    assert validate_invoice(inv)["issues"] == ["line_items[0].total_amount is missing"]


def test_string_line_total_reports_only_the_cause():
    inv = make_invoice([{"quantity": 5, "unit_price": 150.0, "total_amount": "750.00"}], 750.0, 75.0, 825.0)
    assert validate_invoice(inv)["issues"] == ["line_items[0].total_amount is not a valid number: '750.00'"]


def test_missing_subtotal_and_incomplete_lines_do_not_invent_an_expected_total():
    inv = make_invoice([{"quantity": 5, "unit_price": 150.0, "total_amount": None}], None, 75.0, 825.0)
    assert validate_invoice(inv)["issues"] == ["subtotal is missing", "line_items[0].total_amount is missing"]


def test_real_mismatch_is_still_reported_when_all_lines_are_complete():
    inv = make_invoice([{"quantity": 1, "unit_price": 10.0, "total_amount": 10.0}], 99.0, 0.0, 99.0)
    assert validate_invoice(inv)["issues"] == ["line items sum to 10.00, but subtotal is 99.00"]



# --- discount, shipping and rounding lines ---
def summary_invoice(subtotal=1000.0, tax=190.0, grand=1160.0, **extra):
    inv = make_invoice([{"quantity": 10, "unit_price": 100.0, "total_amount": 1000.0}], subtotal, tax, grand)
    inv["financial_summary"].update(extra)
    return inv


def test_discount_and_shipping_balance_the_total():
    r = validate_invoice(summary_invoice(discount_amount=50.0, shipping_amount=20.0))   # 1000 - 50 + 20 + 190 = 1160
    assert r == {"is_valid": True, "requires_review": False, "issues": []}


def test_a_discount_reduces_the_total_whichever_sign_the_reader_copied():
    assert validate_invoice(summary_invoice(discount_amount=-50.0, shipping_amount=20.0))["issues"] == []


def test_rounding_keeps_its_sign():
    ok = validate_invoice(summary_invoice(tax=189.98, grand=1190.0, rounding_adjustment=0.02))
    assert ok["issues"] == []
    wrong_sign = validate_invoice(summary_invoice(tax=189.98, grand=1190.0, rounding_adjustment=-0.02))
    assert wrong_sign["issues"] == ["expected total 1189.96, but grand_total is 1190.00"]


def test_a_misread_discount_is_still_caught():
    r = validate_invoice(summary_invoice(discount_amount=5.0, shipping_amount=20.0))   # really 50
    assert r["issues"] == ["expected total 1205.00, but grand_total is 1160.00"]


def test_an_unlisted_discount_still_fails_as_before():
    assert validate_invoice(summary_invoice(grand=1110.0))["issues"] == ["expected total 1190.00, but grand_total is 1110.00"]


def test_new_fields_that_are_not_numbers_are_flagged_not_crashed_on():
    r = validate_invoice(summary_invoice(discount_amount="50", shipping_amount=True))
    assert any("discount_amount is not a valid number" in i for i in r["issues"]) and any("shipping_amount is not a valid number" in i for i in r["issues"])


def test_old_invoices_without_the_new_keys_still_validate():
    assert validate_invoice(summary_invoice(grand=1190.0))["issues"] == []   # 1000 + 190, no discount/shipping keys at all


def test_shipped_eval_sample_passes_the_validator():
    sample = pathlib.Path(__file__).resolve().parents[1] / "eval_cases" / "sample-001.expected.json"
    assert validate_invoice(json.loads(sample.read_text()))["is_valid"] is True
