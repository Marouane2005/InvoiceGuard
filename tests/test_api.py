import json

import pytest

from invoiceguard.extractor import ExtractionError
from invoiceguard.ratelimit import RateLimiter
from conftest import BAD_RAW, GOOD_RAW, FakeExtractor, pdf


def upload(client, headers, data, name="inv.pdf"):
    return client.post("/v1/invoices", files={"file": (name, data, "application/pdf")}, headers=headers)


def error(res):
    return res.json()["error"]


# ---- public pages and headers ---------------------------------------------------------------
def test_health_dashboard_and_security_headers(build_client):
    c = build_client()
    assert c.get("/healthz").json()["status"] == "ok"
    page = c.get("/")
    assert page.status_code == 200 and "InvoiceGuard" in page.text
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert page.headers["x-content-type-options"] == "nosniff"
    assert c.get("/assets/dashboard.js").headers["content-type"].startswith("text/javascript")
    assert c.get("/assets/dashboard.css").status_code == 200
    assert c.get("/assets/secrets.txt").status_code == 404 and c.get("/assets/..%2Fapi.py").status_code == 404


def test_api_responses_are_not_cached(build_client, make_tenant):
    c = build_client()
    _, h = make_tenant()
    assert c.get("/v1/usage", headers=h).headers["cache-control"] == "no-store"


# ---- auth -----------------------------------------------------------------------------------
@pytest.mark.parametrize("header,code", [(None, "missing_api_key"), ("Basic abc", "missing_api_key"),
                                         ("Bearer", "missing_api_key"), ("Bearer wrong", "invalid_api_key")])
def test_auth_errors(build_client, header, code):
    res = build_client().get("/v1/usage", headers={"Authorization": header} if header else {})
    assert res.status_code == 401 and error(res)["code"] == code and error(res)["hint"]


def test_rate_limit(build_client, make_tenant):
    c = build_client(limiter=RateLimiter(2))
    _, h = make_tenant()
    assert [c.get("/v1/usage", headers=h).status_code for _ in range(3)] == [200, 200, 429]
    assert c.get("/v1/usage", headers=h).headers["retry-after"] == "60"


# ---- upload ---------------------------------------------------------------------------------
def test_upload_clean_then_repeat(build_client, make_tenant):
    c = build_client(FakeExtractor(GOOD_RAW))
    _, h = make_tenant()
    first = upload(c, h, pdf(1))
    assert first.status_code == 201 and first.json()["status"] == "clean"
    again = upload(c, h, pdf(1))
    assert again.status_code == 200 and again.json()["cached"] is True
    assert c.get("/v1/usage", headers=h).json()["documents"] == 1


def test_upload_rejections(build_client, make_tenant):
    c = build_client(settings_overrides={"max_upload_bytes": 1024})
    _, h = make_tenant()
    assert upload(c, h, b"plain text").status_code == 415
    assert upload(c, h, b"").status_code == 400
    assert error(upload(c, h, b"%PDF-" + b"x" * 2000))["code"] == "file_too_large"
    huge = upload(c, h, b"%PDF-" + b"x" * (1024 + 70 * 1024))
    assert huge.status_code == 413 and error(huge)["code"] == "file_too_large"
    assert c.post("/v1/invoices", headers=h).status_code == 422


def test_reading_failure_is_a_502_with_a_hint(build_client, make_tenant):
    c = build_client(FakeExtractor(ExtractionError("the AI provider returned HTTP 503")))
    _, h = make_tenant()
    res = upload(c, h, pdf(1))
    assert res.status_code == 502 and error(res)["code"] == "extraction_failed" and error(res)["hint"]


def test_server_without_api_key_says_so(build_client, make_tenant, settings):
    import dataclasses
    from fastapi.testclient import TestClient
    from invoiceguard.api import create_app
    c = TestClient(create_app(dataclasses.replace(settings, anthropic_api_key=None)))
    _, h = make_tenant()
    res = upload(c, h, pdf(1))
    assert res.status_code == 503 and error(res)["code"] == "extraction_not_configured"


# ---- review flow ----------------------------------------------------------------------------
def flagged(client, headers):
    res = upload(client, headers, pdf(7)).json()
    assert res["status"] == "needs_review"
    return res


def test_full_review_flow_to_export(build_client, make_tenant):
    c = build_client(FakeExtractor(BAD_RAW))
    _, h = make_tenant()
    res = flagged(c, h)
    item_id = res["review_item_id"]

    queue = c.get("/v1/review-queue", headers=h).json()["items"]
    assert len(queue) == 1 and queue[0]["severity"] == "high" and queue[0]["vendor_name"] == "Globex Corp"
    assert c.get("/v1/export.csv", headers=h).text.count("\n") == 1  # header only: not exportable yet

    assert c.post(f"/v1/review-queue/{item_id}/claim", json={"assignee": "Amina"}, headers=h).json()["status"] == "in_review"
    clash = c.post(f"/v1/review-queue/{item_id}/claim", json={"assignee": "Youssef"}, headers=h)
    assert clash.status_code == 409 and error(clash)["code"] == "conflict" and error(clash)["hint"]

    bad = c.post(f"/v1/review-queue/{item_id}/resolve", json={"note": "tried", "corrected_invoice": BAD_RAW}, headers=h)
    assert bad.status_code == 422 and error(bad)["code"] == "invalid_correction"

    ok = c.post(f"/v1/review-queue/{item_id}/resolve", json={"note": "checked the PDF", "corrected_invoice": GOOD_RAW}, headers=h)
    assert ok.status_code == 200 and ok.json()["status"] == "resolved"
    assert c.get("/v1/review-queue", headers=h).json()["items"] == []
    stored = c.get(f"/v1/invoices/{res['document_id']}", headers=h).json()
    assert stored["status"] == "reviewed" and stored["issues"] == []
    assert stored["invoice"]["financial_summary"]["grand_total"] == GOOD_RAW["financial_summary"]["grand_total"]

    rows = c.get("/v1/export.csv", headers=h)
    assert rows.headers["content-type"].startswith("text/csv") and "Globex Corp" in rows.text and ",825.0," in rows.text


def test_release_dismiss_and_missing_items(build_client, make_tenant):
    c = build_client(FakeExtractor(BAD_RAW))
    _, h = make_tenant()
    item_id = flagged(c, h)["review_item_id"]
    c.post(f"/v1/review-queue/{item_id}/claim", json={"assignee": "A"}, headers=h)
    assert c.post(f"/v1/review-queue/{item_id}/release", headers=h).json()["status"] == "open"
    assert c.post(f"/v1/review-queue/{item_id}/dismiss", json={"note": "not ours"}, headers=h).json()["status"] == "dismissed"
    assert c.post(f"/v1/review-queue/{item_id}/dismiss", json={"note": "again"}, headers=h).status_code == 409
    assert c.get("/v1/review-queue/9999", headers=h).status_code == 404
    assert c.post("/v1/review-queue/9999/claim", json={"assignee": "A"}, headers=h).status_code == 404
    empty = c.post(f"/v1/review-queue/{item_id}/claim", json={"assignee": ""}, headers=h)
    assert empty.status_code == 422 and error(empty)["code"] == "invalid_request"


def test_document_file_and_id_checks(build_client, make_tenant):
    c = build_client(FakeExtractor(GOOD_RAW))
    _, h = make_tenant()
    data = pdf(1)
    doc = upload(c, h, data).json()["document_id"]
    got = c.get(f"/v1/invoices/{doc}/file", headers=h)
    assert got.status_code == 200 and got.content == data and got.headers["content-type"] == "application/pdf"
    for bad in ("../../etc/passwd", "abc", "G" * 64):
        assert c.get(f"/v1/invoices/{bad}", headers=h).status_code == 404
    assert c.get("/v1/invoices?status=bogus", headers=h).status_code == 422
    assert [i["document_id"] for i in c.get("/v1/invoices?status=clean", headers=h).json()["invoices"]] == [doc]


def test_customers_cannot_see_each_others_data(build_client, make_tenant):
    c = build_client(FakeExtractor(BAD_RAW))
    _, ha = make_tenant("A")
    _, hb = make_tenant("B")
    res = flagged(c, ha)
    assert c.get(f"/v1/invoices/{res['document_id']}", headers=hb).status_code == 404
    assert c.get(f"/v1/invoices/{res['document_id']}/file", headers=hb).status_code == 404
    assert c.get("/v1/review-queue", headers=hb).json()["items"] == []
    assert c.get(f"/v1/review-queue/{res['review_item_id']}", headers=hb).status_code == 404
    assert c.post(f"/v1/review-queue/{res['review_item_id']}/dismiss", json={"note": "x"}, headers=hb).status_code == 404
    assert c.get("/v1/export.csv?status=all", headers=hb).text.count("\n") == 1


# ---- settings, notifications, usage ---------------------------------------------------------
def test_settings_validation_and_secret_handling(build_client, make_tenant):
    c = build_client()
    _, h = make_tenant()
    assert error(c.patch("/v1/settings", json={"default_currency": "dollars"}, headers=h))["code"] == "invalid_setting"
    assert error(c.patch("/v1/settings", json={"slack_webhook_url": "https://evil.example/x"}, headers=h))["code"] == "invalid_setting"
    hook = "https://hooks.slack.com/services/T000/B000/SECRETVALUE"
    out = c.patch("/v1/settings", json={"default_currency": "eur", "slack_webhook_url": hook}, headers=h).json()
    assert out["default_currency"] == "EUR" and out["slack_configured"] is True
    assert "SECRETVALUE" not in json.dumps(c.get("/v1/settings", headers=h).json())
    assert c.patch("/v1/settings", json={"slack_webhook_url": None}, headers=h).json()["slack_configured"] is False


def test_notifications_only_send_when_asked_and_never_leak_the_webhook(build_client, make_tenant):
    hook = "https://hooks.slack.com/services/T000/B000/SECRETVALUE"
    sent = []

    def factory(url):
        def send(payload):
            sent.append(payload)
        return send

    c = build_client(FakeExtractor(BAD_RAW), notifier_factory=factory)
    _, h = make_tenant()
    flagged(c, h)
    assert error(c.post("/v1/notifications/dispatch", headers=h))["code"] == "notifications_not_configured"
    assert sent == []  # uploading alone never sends anything
    c.patch("/v1/settings", json={"slack_webhook_url": hook}, headers=h)
    assert c.post("/v1/notifications/dispatch", headers=h).json() == {"sent": 1, "failed": []}
    assert c.post("/v1/notifications/dispatch", headers=h).json()["sent"] == 0 and len(sent) == 1

    def failing(url):
        def send(payload):
            raise RuntimeError(f"cannot post to {url}")
        return send

    c2 = build_client(FakeExtractor(BAD_RAW), notifier_factory=failing)
    _, h2 = make_tenant("Second")
    flagged(c2, h2)
    c2.patch("/v1/settings", json={"slack_webhook_url": hook}, headers=h2)
    out = c2.post("/v1/notifications/dispatch", headers=h2).json()
    assert out["sent"] == 0 and "SECRETVALUE" not in json.dumps(out) and "[webhook]" in out["failed"][0]["error"]


def test_usage_and_month_validation(build_client, make_tenant):
    c = build_client(FakeExtractor(GOOD_RAW))
    _, h = make_tenant(monthly_limit=5)
    upload(c, h, pdf(1))
    use = c.get("/v1/usage", headers=h).json()
    assert use["documents"] == 1 and use["monthly_limit"] == 5 and use["input_tokens"] == 1200
    assert c.get("/v1/usage?month=2020-01", headers=h).json()["documents"] == 0
    assert c.get("/v1/usage?month=2020-13", headers=h).status_code == 422
    assert c.get("/v1/export.csv?month=abc", headers=h).status_code == 422


# ---- admin ----------------------------------------------------------------------------------
def test_admin_is_hidden_when_no_token_is_set(build_client):
    c = build_client(settings_overrides={"admin_token": None})
    assert c.post("/admin/tenants", json={"name": "X"}, headers={"X-Admin-Token": "anything"}).status_code == 404


def test_admin_lifecycle(build_client):
    c = build_client(FakeExtractor(GOOD_RAW))
    admin = {"X-Admin-Token": "admin-token-123456"}
    assert c.post("/admin/tenants", json={"name": "X"}).status_code == 401
    assert error(c.post("/admin/tenants", json={"name": "X"}, headers={"X-Admin-Token": "wrong"}))["code"] == "invalid_admin_token"
    assert c.post("/admin/tenants", json={"name": "X", "default_currency": "dollars"}, headers=admin).status_code == 422

    created = c.post("/admin/tenants", json={"name": "Acme", "default_currency": "usd", "monthly_limit": 100}, headers=admin)
    assert created.status_code == 201
    body = created.json()
    tid, key = body["tenant"]["id"], body["api_key"]
    assert "api_key_hash" not in body["tenant"] and body["tenant"]["default_currency"] == "USD"
    h = {"Authorization": f"Bearer {key}"}
    assert upload(c, h, pdf(1)).status_code == 201

    usage = {u["id"]: u for u in c.get("/admin/usage", headers=admin).json()["usage"]}
    assert usage[tid]["documents"] == 1 and usage[tid]["input_tokens"] == 1200

    new_key = c.post(f"/admin/tenants/{tid}/rotate-key", headers=admin).json()["api_key"]
    assert c.get("/v1/usage", headers=h).status_code == 401
    h2 = {"Authorization": f"Bearer {new_key}"}
    assert c.get("/v1/usage", headers=h2).status_code == 200
    assert c.post(f"/admin/tenants/{tid}/deactivate", headers=admin).json()["active"] is False
    assert c.get("/v1/usage", headers=h2).status_code == 401
    assert c.post("/admin/tenants/t_nope/rotate-key", headers=admin).status_code == 404
    assert c.post("/admin/tenants/t_nope/deactivate", headers=admin).status_code == 404


# ---- export templates -----------------------------------------------------------------------------
def test_customers_can_set_a_template_that_the_export_then_uses(build_client, make_tenant):
    c = build_client(FakeExtractor(GOOD_RAW))
    _, h = make_tenant()
    _, other = make_tenant("Other")
    upload(c, h, pdf(1))
    template = {"columns": [{"header": "Supplier", "field": "vendor_name"}, {"header": "Date", "field": "issue_date", "date_format": "%d/%m/%Y"},
                            {"header": "Amount", "field": "grand_total"}], "delimiter": ";", "decimal": ","}
    out = c.patch("/v1/settings", json={"export_template": template}, headers=h).json()
    assert out["export_template_configured"] is True and "columns" not in json.dumps(out)
    assert c.get("/v1/export.csv", headers=h).text.strip().splitlines() == ["Supplier;Date;Amount", "Globex Corp;05/12/2023;825,00"]
    assert c.get("/v1/export.csv?template=default", headers=h).text.startswith("document_id,")
    assert c.get("/v1/export.csv", headers=other).text.startswith("document_id,")        # another customer is unaffected
    assert c.patch("/v1/settings", json={"export_template": None}, headers=h).json()["export_template_configured"] is False
    assert c.get("/v1/export.csv", headers=h).text.startswith("document_id,")


def test_bad_templates_and_modes_are_refused_with_a_clear_message(build_client, make_tenant):
    c = build_client()
    _, h = make_tenant()
    bad = c.patch("/v1/settings", json={"export_template": {"columns": [{"header": "A", "field": "password"}]}}, headers=h)
    assert bad.status_code == 422 and error(bad)["code"] == "invalid_setting" and "unknown field" in error(bad)["message"]
    assert c.get("/v1/settings", headers=h).json()["export_template_configured"] is False
    assert error(c.get("/v1/export.csv?template=custom", headers=h))["code"] == "invalid_request"
    assert c.get("/v1/export.csv?template=weird", headers=h).status_code == 422
