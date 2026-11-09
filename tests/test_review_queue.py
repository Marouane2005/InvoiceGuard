import json
import sqlite3

import pytest

from invoiceguard import review_queue as rq
from invoiceguard.validator import validate_invoice

BAD = {  # the 750 -> 150 misread
    "line_items": [{"quantity": 5, "unit_price": 150.0, "total_amount": 150.0}],
    "financial_summary": {"subtotal": 150.0, "tax_amount": 75.0, "grand_total": 825.0},
    "ocr_warnings": [],
}
BAD2 = {  # same document, re-extracted with a different (still wrong) reading
    "line_items": [{"quantity": 5, "unit_price": 150.0, "total_amount": 700.0}],
    "financial_summary": {"subtotal": 700.0, "tax_amount": 75.0, "grand_total": 825.0},
    "ocr_warnings": [],
}
GOOD = {
    "line_items": [{"quantity": 5, "unit_price": 150.0, "total_amount": 750.0}],
    "financial_summary": {"subtotal": 750.0, "tax_amount": 75.0, "grand_total": 825.0},
    "ocr_warnings": [],
}


@pytest.fixture
def conn(tmp_path):
    c = rq.connect(str(tmp_path / "queue.db"))
    yield c
    c.close()


def enq(conn, doc, inv=BAD, **kw):
    return rq.enqueue(conn, doc, inv, validate_invoice(inv), **kw)


def count(conn, where="1=1"):
    return conn.execute(f"SELECT COUNT(*) FROM review_queue WHERE {where}").fetchone()[0]


def test_clean_invoice_is_not_queued(conn):
    assert enq(conn, "doc-1", GOOD) is None
    assert count(conn) == 0


def test_flagged_invoice_is_queued_with_high_severity(conn):
    item_id = enq(conn, "doc-1", document_url="https://files.example/doc-1.pdf")
    row = conn.execute("SELECT * FROM review_queue WHERE id=?", (item_id,)).fetchone()
    assert row["status"] == "open" and row["severity"] == "high"
    assert row["notified_at"] is None and row["resolved_at"] is None
    assert any("5 x 150" in i for i in json.loads(row["issues_json"]))
    assert json.loads(row["invoice_json"]) == BAD


def test_same_content_is_idempotent(conn):
    a, b = enq(conn, "doc-1"), enq(conn, "doc-1")
    assert a == b and count(conn) == 1


def test_changed_content_updates_in_place_and_rearms_notification(conn):
    a = enq(conn, "doc-1")
    rq.dispatch_pending(conn, lambda p: None)
    assert conn.execute("SELECT notified_at FROM review_queue WHERE id=?", (a,)).fetchone()[0] is not None
    b = enq(conn, "doc-1", BAD2)
    row = conn.execute("SELECT * FROM review_queue WHERE id=?", (b,)).fetchone()
    assert a == b and count(conn) == 1
    assert row["notified_at"] is None
    assert json.loads(row["invoice_json"]) == BAD2


def test_reenqueue_after_resolution_creates_new_item(conn):
    a = enq(conn, "doc-1")
    rq.resolve(conn, a, "fixed unit total", corrected_invoice=GOOD)
    b = enq(conn, "doc-1")
    assert a != b and count(conn) == 2 and count(conn, "status='open'") == 1


def test_partial_unique_index_enforced_by_database(conn):
    enq(conn, "doc-1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO review_queue (document_id, content_hash, severity, issues_json, invoice_json, "
            "created_at, updated_at) VALUES ('doc-1','x','low','[]','{}','t','t')")


def test_resolved_at_check_constraint(conn):
    a = enq(conn, "doc-1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE review_queue SET status='resolved' WHERE id=?", (a,))


def test_claim_is_exclusive(conn):
    a = enq(conn, "doc-1")
    rq.claim(conn, a, "amina")
    with pytest.raises(rq.InvalidTransition):
        rq.claim(conn, a, "youssef")
    assert conn.execute("SELECT assignee FROM review_queue WHERE id=?", (a,)).fetchone()[0] == "amina"


def test_claim_missing_item_raises(conn):
    with pytest.raises(rq.InvalidTransition):
        rq.claim(conn, 999, "amina")


def test_release_returns_item_to_open(conn):
    a = enq(conn, "doc-1")
    rq.claim(conn, a, "amina")
    rq.release(conn, a)
    row = conn.execute("SELECT * FROM review_queue WHERE id=?", (a,)).fetchone()
    assert row["status"] == "open" and row["assignee"] is None
    with pytest.raises(rq.InvalidTransition):
        rq.release(conn, a)


def test_resolve_requires_note_and_stores_correction(conn):
    a = enq(conn, "doc-1")
    with pytest.raises(ValueError):
        rq.resolve(conn, a, "   ")
    rq.resolve(conn, a, "OCR read 750 as 150", corrected_invoice=GOOD)
    row = conn.execute("SELECT * FROM review_queue WHERE id=?", (a,)).fetchone()
    assert row["status"] == "resolved" and row["resolved_at"] is not None
    assert json.loads(row["corrected_invoice_json"]) == GOOD


def test_closed_items_cannot_be_closed_again(conn):
    a = enq(conn, "doc-1")
    rq.dismiss(conn, a, "duplicate of doc-0")
    with pytest.raises(rq.InvalidTransition):
        rq.resolve(conn, a, "again")
    with pytest.raises(rq.InvalidTransition):
        rq.dismiss(conn, a, "again")


def test_clean_reextraction_auto_resolves_open_item_only(conn):
    a = enq(conn, "doc-1")
    enq(conn, "doc-1", GOOD)
    row = conn.execute("SELECT * FROM review_queue WHERE id=?", (a,)).fetchone()
    assert row["status"] == "resolved" and row["resolution_note"] == rq.AUTO_RESOLVE_NOTE

    b = enq(conn, "doc-2")
    rq.claim(conn, b, "amina")
    enq(conn, "doc-2", GOOD)
    assert conn.execute("SELECT status FROM review_queue WHERE id=?", (b,)).fetchone()[0] == "in_review"


@pytest.mark.parametrize("issue,expected", [
    ("ocr_warning: total looks missing", "low"),
    ("grand_total is missing", "medium"),
    ("subtotal is missing", "medium"),
    ("no line items extracted", "medium"),
    ("line items sum to 1.00, but subtotal is 2.00", "high"),
    ("expected total 1.00, but grand_total is 2.00", "high"),
    ("line_items[0]: 5 x 150 = 750.00, but total_amount is 150.00", "high"),
    ("line_items[0].total_amount is not a valid number: '1S0'", "high"),
])
def test_classify_issue(issue, expected):
    assert rq.classify_issue(issue) == expected


def test_invoice_severity_is_the_maximum(conn):
    assert rq.severity_of(["ocr_warning: x", "grand_total is missing"]) == "medium"
    assert rq.severity_of(["ocr_warning: x", "grand_total is missing", "expected total 1.00, but grand_total is 2.00"]) == "high"


def test_list_active_orders_by_severity_then_age(conn):
    low = {"line_items": [{"quantity": 1, "unit_price": 10.0, "total_amount": 10.0}],
           "financial_summary": {"subtotal": 10.0, "tax_amount": 0.0, "grand_total": 10.0},
           "ocr_warnings": ["issue_date ambiguous"]}
    medium = {"line_items": [{"quantity": 1, "unit_price": 10.0, "total_amount": 10.0}],
              "financial_summary": {"subtotal": 10.0, "tax_amount": 0.0, "grand_total": None},
              "ocr_warnings": []}
    enq(conn, "doc-low", low)
    enq(conn, "doc-medium", medium)
    enq(conn, "doc-high", BAD)
    assert [r["document_id"] for r in rq.list_active(conn)] == ["doc-high", "doc-medium", "doc-low"]
    assert [r["severity"] for r in rq.list_active(conn)] == ["high", "medium", "low"]


def test_dispatch_marks_notified_and_does_not_resend(conn):
    enq(conn, "doc-1")
    sent = []
    assert rq.dispatch_pending(conn, sent.append) == {"sent": 1, "failed": []}
    assert rq.dispatch_pending(conn, sent.append) == {"sent": 0, "failed": []}
    assert len(sent) == 1 and sent[0]["text"] == "[HIGH] Invoice needs review: doc-1 825.00"


def test_failed_send_is_retried_not_lost(conn):
    a = enq(conn, "doc-1")

    def boom(payload):
        raise RuntimeError("slack down")

    res = rq.dispatch_pending(conn, boom)
    assert res["sent"] == 0 and res["failed"] == [{"id": a, "error": "slack down"}]
    assert conn.execute("SELECT notified_at FROM review_queue WHERE id=?", (a,)).fetchone()[0] is None
    assert rq.dispatch_pending(conn, lambda p: None)["sent"] == 1


def test_in_review_items_are_not_renotified(conn):
    a = enq(conn, "doc-1")
    rq.claim(conn, a, "amina")
    assert rq.pending_notifications(conn) == []


def test_slack_payload_truncates_escapes_and_links(conn):
    many = {"line_items": [{"quantity": 1, "unit_price": 1.0, "total_amount": 1.0}],
            "financial_summary": {"subtotal": 1.0, "tax_amount": 0.0, "grand_total": 1.0},
            "ocr_warnings": [f"w{i} <b>&" for i in range(14)]}
    a = enq(conn, "d" * 200, many, document_url="https://files.example/a|b?x=1&y=2")
    row = conn.execute("SELECT * FROM review_queue WHERE id=?", (a,)).fetchone()
    payload = rq.format_slack_payload(row)
    body = payload["blocks"][1]["text"]["text"]
    assert body.count("\u2022") == 10 and body.endswith("\u2026and 4 more")
    assert "&lt;b&gt;&amp;" in body and "<b>" not in body
    assert len(payload["blocks"][0]["text"]["text"]) <= 150
    ctx = payload["blocks"][2]["elements"][0]["text"]
    assert "a%7Cb?x=1&amp;y=2" in ctx and f"#{a}" in ctx


def test_email_format(conn):
    a = enq(conn, "doc-1", document_url="https://files.example/doc-1.pdf")
    mail = rq.format_email(conn.execute("SELECT * FROM review_queue WHERE id=?", (a,)).fetchone())
    assert mail["subject"] == "[HIGH] Invoice needs review: doc-1 825.00"
    assert "Document: https://files.example/doc-1.pdf" in mail["body"] and f"#{a}" in mail["body"]


def test_input_validation(conn):
    with pytest.raises(ValueError):
        rq.enqueue(conn, "", BAD, validate_invoice(BAD))
    with pytest.raises(ValueError):
        rq.enqueue(conn, "doc-1", BAD, {"requires_review": True, "issues": []})


def test_webhook_sender_requires_https():
    with pytest.raises(ValueError):
        rq.slack_webhook_sender("http://insecure.example/hook")


def test_titles_use_vendor_number_and_total_not_a_file_hash(conn):
    inv = {"invoice_metadata": {"invoice_number": "9942", "vendor_name": "Globex Corp", "currency": "USD"},
           "line_items": [{"quantity": 5, "unit_price": 150.0, "total_amount": 150.0}],
           "financial_summary": {"subtotal": 150.0, "tax_amount": 75.0, "grand_total": 825.0}, "ocr_warnings": []}
    a = rq.enqueue(conn, "a" * 64, inv, validate_invoice(inv))
    row = conn.execute("SELECT * FROM review_queue WHERE id=?", (a,)).fetchone()
    assert rq.format_slack_payload(row)["text"] == "[HIGH] Invoice needs review: Globex Corp #9942 825.00 USD"
    assert rq.format_email(row)["subject"] == "[HIGH] Invoice needs review: Globex Corp #9942 825.00 USD"
    plain = rq.enqueue(conn, "b" * 64, BAD, validate_invoice(BAD))
    row = conn.execute("SELECT * FROM review_queue WHERE id=?", (plain,)).fetchone()
    assert rq.format_slack_payload(row)["text"] == "[HIGH] Invoice needs review: " + "b" * 12 + " 825.00"
