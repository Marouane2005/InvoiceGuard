import json
import sqlite3
import tempfile
import threading
from contextlib import closing
from pathlib import Path

import pytest

from invoiceguard import review_queue as rq, store
from invoiceguard.policy import normalize_invoice
from conftest import GOOD_RAW

OLD = "2000-01-01T00:00:00+00:00"


@pytest.fixture
def conn(tmp_path):
    with closing(store.open_db(str(tmp_path / "t.db"))) as c:
        yield c


def save(conn, doc, raw=GOOD_RAW, status="clean", **kw):
    inv = normalize_invoice(raw)
    store.save_invoice(conn, document_id=doc, filename=kw.get("filename", "a.pdf"), media_type="application/pdf",
                       status=status, invoice=inv, issues=[], model="m", input_tokens=1, output_tokens=2)


def test_opening_one_new_database_from_several_threads_does_not_crash():
    errors = []

    def worker(path: str) -> None:
        try:
            with closing(store.open_db(path)) as conn:
                conn.execute("SELECT 1")
        except sqlite3.OperationalError as exc:
            errors.append(str(exc))

    for _ in range(40):
        path = str(Path(tempfile.mkdtemp()) / "tenant.db")
        threads = [threading.Thread(target=worker, args=(path,)) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []


def test_vendor_key_normalizes():
    assert store.vendor_key(" ACME,  Corp. ") == "acmecorp" and store.vendor_key("!!!") is None and store.vendor_key(None) is None


def test_roundtrip(conn):
    save(conn, "d1")
    rec = store.get_invoice(conn, "d1")
    assert rec["status"] == "clean" and rec["invoice"]["invoice_metadata"]["vendor_name"] == "Globex Corp"
    assert store.get_invoice(conn, "nope") is None


def test_duplicate_detection_ignores_self_dismissed_and_other_numbers(conn):
    save(conn, "d1")
    key = store.vendor_key("Globex Corp")
    assert store.find_duplicate(conn, key, "9942", "d1") is None
    assert store.find_duplicate(conn, key, "9942", "d2") == "d1"
    assert store.find_duplicate(conn, key, "0001", "d2") is None
    store.mark_reviewed(conn, "d1", "dismissed")
    assert store.find_duplicate(conn, key, "9942", "d2") is None


def test_csv_export_defaults_to_clean_and_reviewed_only(conn):
    save(conn, "d1")
    save(conn, "d2", status="needs_review")
    rows = store.export_csv(conn).strip().splitlines()
    assert len(rows) == 2 and rows[1].startswith("d1,")
    assert len(store.export_csv(conn, statuses=None).strip().splitlines()) == 3
    assert store.export_csv(conn, month="1999-01").strip().splitlines() == [",".join(store.EXPORT_COLUMNS)]


def test_csv_blocks_formula_injection_but_keeps_negative_numbers(conn):
    raw = json.loads(json.dumps(GOOD_RAW))
    raw["invoice_metadata"]["vendor_name"] = "=HYPERLINK(\"http://evil\")"
    raw["financial_summary"]["grand_total"] = -50.0
    save(conn, "d1", raw)
    line = store.export_csv(conn).splitlines()[1]
    assert "'=HYPERLINK" in line and ",-50.0," in line


def test_purge_is_a_dry_run_by_default_and_spares_active_reviews(conn, tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    for doc in ("old-clean", "old-flagged", "new"):
        save(conn, doc)
        (files / f"{doc}.pdf").write_bytes(b"%PDF-")
    inv = normalize_invoice(GOOD_RAW)
    rq.enqueue(conn, "old-flagged", inv, {"requires_review": True, "issues": ["x"]})
    conn.execute("UPDATE invoices SET created_at = ? WHERE document_id IN ('old-clean','old-flagged')", (OLD,))
    cutoff = "2020-01-01T00:00:00+00:00"

    result = store.purge(conn, files, cutoff)
    assert result == {"documents": 1, "files": 0, "dry_run": True}
    assert store.get_invoice(conn, "old-clean") and (files / "old-clean.pdf").exists()

    result = store.purge(conn, files, cutoff, dry_run=False)
    assert result == {"documents": 1, "files": 1, "dry_run": False}
    assert store.get_invoice(conn, "old-clean") is None and not (files / "old-clean.pdf").exists()
    assert store.get_invoice(conn, "old-flagged") and (files / "old-flagged.pdf").exists()
    assert store.get_invoice(conn, "new")
