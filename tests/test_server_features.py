import json
import re
from contextlib import closing

import pytest

from invoiceguard import __version__, store
from invoiceguard.policy import normalize_invoice
from conftest import BAD_RAW, GOOD_RAW, FakeExtractor, pdf
from export_openapi import current as current_contract, TARGET


def upload(client, headers, data):
    return client.post("/v1/invoices", files={"file": ("a.pdf", data, "application/pdf")}, headers=headers)


def error(res):
    return res.json()["error"]


# ---- search --------------------------------------------------------------------------------------------
@pytest.fixture
def conn(tmp_path):
    with closing(store.open_db(str(tmp_path / "t.db"))) as c:
        yield c


def save(conn, doc, vendor, number, filename="x.pdf", status="clean"):
    raw = {**GOOD_RAW, "invoice_metadata": {**GOOD_RAW["invoice_metadata"], "vendor_name": vendor, "invoice_number": number}}
    store.save_invoice(conn, document_id=doc, filename=filename, media_type="application/pdf", status=status,
                       invoice=normalize_invoice(raw), issues=[], model="m", input_tokens=1, output_tokens=1)


@pytest.fixture
def five(conn):
    save(conn, "d1", "Globex Corp", "9942", "globex-oct.pdf")
    save(conn, "d2", "Acme Supplies Ltd", "A-17", "acme.pdf")
    save(conn, "d3", "Wayne_Enterprises", "W-1", "100%-scan.pdf")
    save(conn, "d4", "Stark Industries", "S-9", "stark.pdf", status="needs_review")
    save(conn, "d5", "Globex Corp", "9943", "globex-nov.pdf")
    return conn


def found(conn, q, **kw):
    rows, total = store.search_invoices(conn, q=q, **kw)
    return sorted(r["document_id"] for r in rows), total


def test_search_finds_by_vendor_number_or_file_ignoring_case_and_punctuation(five):
    assert found(five, "globex")[0] == ["d1", "d5"]
    assert found(five, "GLOBEX  corp.")[0] == ["d1", "d5"]
    assert found(five, "a-17")[0] == ["d2"] and found(five, "9943")[0] == ["d5"] and found(five, "stark.pdf")[0] == ["d4"]
    assert found(five, "wayne enterprises")[0] == ["d3"]
    assert found(five, "nothing like this") == ([], 0)
    assert found(five, "  ")[1] == 5 and found(five, None)[1] == 5


def test_search_treats_sql_wildcards_as_plain_characters(five):
    assert found(five, "%") == (["d3"], 1)        # only the file literally named 100%-scan.pdf
    assert found(five, "_") == ([], 0)             # an underscore is a plain character here, not "any one character"
    assert found(five, "' OR 1=1 --") == ([], 0)


def test_search_paging_reports_the_true_total(five):
    rows, total = store.search_invoices(five, limit=2, offset=4)
    assert total == 5 and len(rows) == 1
    rows, total = store.search_invoices(five, statuses=["needs_review"])
    assert total == 1 and rows[0]["document_id"] == "d4"


# ---- change feed ---------------------------------------------------------------------------------------
def test_feed_pages_never_skip_or_repeat_even_with_identical_timestamps(conn):
    for i in range(5):
        save(conn, f"doc{i}", "V", str(i))
    conn.execute("UPDATE invoices SET updated_at = '2026-10-01T00:00:00.000000+00:00'")
    seen, cursor = [], None
    for _ in range(10):
        rows, cursor = store.changes(conn, store.EXPORTABLE, cursor, 2)
        if not rows:
            break
        seen += [r["document_id"] for r in rows]
    assert seen == ["doc0", "doc1", "doc2", "doc3", "doc4"]
    rows, again = store.changes(conn, store.EXPORTABLE, cursor, 2)
    assert rows == [] and again == cursor      # an empty page keeps the cursor, so polling later picks up new work
    assert re.fullmatch(r"[A-Za-z0-9_-]+", cursor)   # safe to paste into a URL as is


@pytest.mark.parametrize("bad", ["x' OR 1=1 --", "2026-10-01", "abc|5", "2026-10-01T00:00:00+00:00|x", "|", "2026-10-01T00:00:00+00:00|5; DROP TABLE invoices"])
def test_bad_cursors_are_refused(conn, bad):
    with pytest.raises(ValueError, match="cursor"):
        store.changes(conn, None, bad, 10)


def test_feed_over_the_api_and_a_record_that_changes_appears_again(build_client, make_tenant):
    c = build_client(FakeExtractor(BAD_RAW))
    _, h = make_tenant()
    res = upload(c, h, pdf(1)).json()
    assert c.get("/v1/invoices/changes", headers=h).json() == {"invoices": [], "next_cursor": None}      # flagged: not exportable yet
    assert len(c.get("/v1/invoices/changes?include=all", headers=h).json()["invoices"]) == 1
    c.post(f"/v1/review-queue/{res['review_item_id']}/resolve", json={"note": "checked", "corrected_invoice": GOOD_RAW}, headers=h)
    feed = c.get("/v1/invoices/changes", headers=h).json()
    assert [i["status"] for i in feed["invoices"]] == ["reviewed"] and feed["next_cursor"]
    assert c.get(f"/v1/invoices/changes?cursor={feed['next_cursor']}", headers=h).json()["invoices"] == []
    assert error(c.get("/v1/invoices/changes?cursor=nope", headers=h))["code"] == "invalid_request"
    assert c.get("/v1/invoices/changes?include=x", headers=h).status_code == 422


# ---- listing with search ------------------------------------------------------------------------------
def test_listing_supports_search_paging_and_reports_totals(build_client, make_tenant):
    c = build_client(FakeExtractor(GOOD_RAW))
    _, h = make_tenant()
    assert upload(c, h, pdf(1)).status_code == 201
    out = c.get("/v1/invoices?q=globex", headers=h).json()
    assert out["total"] == 1 and out["limit"] == 100 and out["offset"] == 0 and out["invoices"][0]["invoice"]["invoice_metadata"]["vendor_name"] == "Globex Corp"
    assert c.get("/v1/invoices?q=nobody", headers=h).json() == {"invoices": [], "total": 0, "limit": 100, "offset": 0}
    assert c.get("/v1/invoices?limit=999&offset=5", headers=h).json()["limit"] == 200
    assert c.get("/v1/invoices?offset=-1", headers=h).status_code == 422


# ---- key rotation --------------------------------------------------------------------------------------
def test_a_customer_can_replace_their_own_key(build_client, make_tenant):
    c = build_client()
    _, h = make_tenant()
    out = c.post("/v1/settings/rotate-key", headers=h).json()
    assert out["api_key"].startswith("ig_") and "no longer works" in out["note"]
    assert c.get("/v1/usage", headers=h).status_code == 401
    assert c.get("/v1/usage", headers={"Authorization": f"Bearer {out['api_key']}"}).status_code == 200


# ---- metrics -------------------------------------------------------------------------------------------
def test_metrics_are_admin_only(build_client):
    assert build_client(settings_overrides={"admin_token": None}).get("/admin/metrics").status_code == 404
    c = build_client()
    assert c.get("/admin/metrics").status_code == 401 and c.get("/admin/metrics", headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_metrics_count_what_happened_without_leaking_who_or_what(build_client, make_tenant):
    c = build_client(FakeExtractor(GOOD_RAW, BAD_RAW))
    tenant, h = make_tenant()
    upload(c, h, pdf(1)); upload(c, h, pdf(1)); upload(c, h, pdf(2)); upload(c, h, b"plain text")
    res = c.get("/admin/metrics", headers={"X-Admin-Token": "admin-token-123456"})
    text = res.text
    assert res.headers["content-type"].startswith("text/plain; version=0.0.4")
    for line in ('invoiceguard_invoices_total{result="clean"} 1', 'invoiceguard_invoices_total{result="cached"} 1',
                 'invoiceguard_invoices_total{result="needs_review"} 1', 'invoiceguard_errors_total{code="unsupported_file"} 1',
                 f'invoiceguard_info{{version="{__version__}"}} 1', "invoiceguard_reads_in_flight 0", "invoiceguard_upload_seconds_count 3"):
        assert line in text, line
    assert 'invoiceguard_http_requests_total{method="POST",status_class="2xx"} 3' in text
    assert tenant["id"] not in text and "/v1/" not in text and "pdf" not in text.lower().replace("# ", "")
    sample = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})? [-+0-9.eE]+$")
    for line in text.strip().splitlines():
        assert line.startswith("# HELP ") or line.startswith("# TYPE ") or sample.match(line), line


# ---- API documentation and contract --------------------------------------------------------------------
def test_every_operation_is_tagged_summarised_and_declares_how_to_authenticate(build_client):
    spec = build_client().get("/openapi.json").json()
    assert set(spec["components"]["securitySchemes"]) == {"ApiKey", "AdminToken"}
    for path, item in spec["paths"].items():
        for method, op in item.items():
            assert op["tags"] and op["summary"] and op["security"], (method, path)
            assert op["security"] == ([{"AdminToken": []}] if path.startswith("/admin") else [{"ApiKey": []}])
    assert "/v1/invoices/changes" in spec["paths"] and "/v1/settings/rotate-key" in spec["paths"]


def test_the_api_contract_matches_the_committed_file():
    assert TARGET.read_text() == current_contract(), "the API changed. If that is intended: python scripts/export_openapi.py"
