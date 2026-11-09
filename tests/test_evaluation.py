import json
import pathlib

import pytest

from invoiceguard import evaluation
from invoiceguard.evaluation import Case, evaluate, load_cases, render_markdown, same
from conftest import BAD_RAW, GOOD_RAW, FakeExtractor, pdf

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXPECTED = {"invoice_metadata": {"vendor_name": "Globex Corp", "invoice_number": "9942", "issue_date": "2023-12-05", "currency": "USD"},
            "line_items": [{"quantity": 5, "unit_price": 150.0, "total_amount": 750.0}],
            "financial_summary": {"subtotal": 750.0, "tax_amount": 75.0, "grand_total": 825.0}}


def run(reader_output, expected=EXPECTED, name="c.pdf"):
    return evaluate([Case(name, pdf(1), expected)], FakeExtractor(reader_output))


def test_same_ignores_case_spacing_and_cents_noise():
    assert same("Globex  Corp", "globex corp") and same(750.004, 750.0) and same(None, None)
    assert not same(750.02, 750.0) and not same("a", None) and not same(True, 1.0)


def test_correct_and_clean():
    r = run(GOOD_RAW)
    assert r["counts"]["clean_correct"] == 1 and r["wrong_among_auto_passed"] == 0.0 and r["review_rate"] == 0.0
    assert set(r["field_accuracy"].values()) == {1.0}


def test_misread_is_caught_and_sent_to_review():
    r = run(BAD_RAW)
    assert r["counts"]["flagged_wrong"] == 1 and r["counts"]["clean_wrong"] == 0 and r["review_rate"] == 1.0
    assert "line_items" in r["cases"][0]["wrong_fields"]


def test_confidently_wrong_but_consistent_invoice_is_counted_as_the_dangerous_case():
    wrong_vendor = {**GOOD_RAW, "invoice_metadata": {**GOOD_RAW["invoice_metadata"], "vendor_name": "Globex Corporation"}}
    r = run(wrong_vendor)
    assert r["counts"]["clean_wrong"] == 1 and r["wrong_among_auto_passed"] == 1.0
    assert r["cases"][0]["wrong_fields"]["vendor_name"] == {"got": "Globex Corporation", "expected": "Globex Corp"}
    assert r["field_accuracy"]["vendor_name"] == 0.0 and r["field_accuracy"]["grand_total"] == 1.0
    assert "**1**" in render_markdown(r) and "vendor_name: got 'Globex Corporation'" in render_markdown(r)


def test_unneeded_review_when_the_reader_correctly_says_unclear():
    expected = {**EXPECTED, "invoice_metadata": {**EXPECTED["invoice_metadata"], "issue_date": None}}
    unclear = {**GOOD_RAW, "invoice_metadata": {**GOOD_RAW["invoice_metadata"], "issue_date": None},
               "ocr_warnings": ["invoice_metadata.issue_date: '12/05/23' could be day-first or month-first; set to null"]}
    r = run(unclear, expected)
    assert r["counts"]["flagged_correct"] == 1 and r["unneeded_reviews"] == 1


def test_read_failures_are_counted_not_hidden():
    r = evaluate([Case("x.pdf", pdf(1), EXPECTED)], FakeExtractor(RuntimeError("provider down")))
    assert r["read_failed"] == 1 and r["auto_pass_rate"] is None and r["cases"][0]["outcome"] == "read_failed"


def test_only_fields_in_the_expected_file_are_scored():
    r = run(GOOD_RAW, {"financial_summary": {"grand_total": 825.0}})
    assert list(r["field_accuracy"]) == ["grand_total"]


def test_load_cases_reports_what_it_skipped(tmp_path):
    (tmp_path / "ok.pdf").write_bytes(pdf(1))
    (tmp_path / "ok.expected.json").write_text(json.dumps(EXPECTED))
    (tmp_path / "nolabel.pdf").write_bytes(pdf(2))
    (tmp_path / "badjson.pdf").write_bytes(pdf(3))
    (tmp_path / "badjson.expected.json").write_text("{nope")
    (tmp_path / "text.pdf").write_bytes(b"not a pdf")
    (tmp_path / "text.expected.json").write_text("{}")
    (tmp_path / "notes.txt").write_text("ignored")
    cases, problems = load_cases(tmp_path)
    assert [c.name for c in cases] == ["ok.pdf"] and len(problems) == 3
    assert any("no nolabel.expected.json" in p for p in problems)


def test_the_shipped_sample_case_loads_and_documents_the_format():
    cases, problems = load_cases(ROOT / "eval_cases")
    assert [c.name for c in cases] == ["sample-001.pdf"] and problems == []
    r = evaluate(cases, FakeExtractor({"invoice_metadata": cases[0].expected["invoice_metadata"], "line_items": cases[0].expected["line_items"],
                                       "financial_summary": cases[0].expected["financial_summary"], "ocr_warnings": []}))
    assert r["counts"]["clean_correct"] == 1


def test_new_amount_fields_are_scored_when_labelled():
    reader = {**GOOD_RAW, "financial_summary": {**GOOD_RAW["financial_summary"], "discount_amount": 50.0, "grand_total": 775.0}}
    expected = {"financial_summary": {"discount_amount": 40.0, "grand_total": 775.0}}
    r = evaluate([Case("d.pdf", pdf(1), expected)], FakeExtractor(reader))
    assert r["field_accuracy"] == {"discount_amount": 0.0, "grand_total": 1.0} and r["cases"][0]["wrong_fields"]["discount_amount"] == {"got": 50.0, "expected": 40.0}
