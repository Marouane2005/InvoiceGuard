import json
import logging
import threading

import pytest

from invoiceguard import tenants
from conftest import GOOD_RAW, FakeExtractor, pdf


def upload(client, headers, data):
    return client.post("/v1/invoices", files={"file": ("a.pdf", data, "application/pdf")}, headers=headers)


def test_readiness_and_failure_do_not_reveal_details(build_client, monkeypatch):
    c = build_client()
    assert c.get("/readyz").json() == {"status": "ready"}
    monkeypatch.setattr(tenants, "open_control", lambda path: (_ for _ in ()).throw(RuntimeError("disk /secret/path exploded")))
    res = c.get("/readyz")
    assert res.status_code == 503 and "secret" not in res.text and res.json() == {"status": "unavailable"}


def test_repeated_wrong_keys_lock_out_even_a_correct_key_for_a_minute(build_client, make_tenant):
    c = build_client(settings_overrides={"auth_failures_per_minute": 3})
    _, good = make_tenant()
    for _ in range(3):
        assert c.get("/v1/usage", headers={"Authorization": "Bearer wrong"}).status_code == 401
    locked = c.get("/v1/usage", headers=good)
    assert locked.status_code == 429 and locked.json()["error"]["code"] == "rate_limited"


def test_successful_requests_never_count_as_failures(build_client, make_tenant):
    c = build_client(settings_overrides={"auth_failures_per_minute": 2}, limiter=None)
    _, good = make_tenant()
    assert all(c.get("/v1/usage", headers=good).status_code == 200 for _ in range(10))


def test_forwarded_header_is_ignored_unless_behind_a_trusted_proxy(build_client):
    spoof = lambda i: {"Authorization": "Bearer wrong", "X-Forwarded-For": f"10.0.0.{i}"}
    c = build_client(settings_overrides={"auth_failures_per_minute": 3})
    codes = [c.get("/v1/usage", headers=spoof(i)).status_code for i in range(4)]
    assert codes == [401, 401, 401, 429]   # rotating the header does not escape the lockout

    proxied = build_client(settings_overrides={"auth_failures_per_minute": 3, "trust_proxy": True})
    for _ in range(3):
        proxied.get("/v1/usage", headers=spoof(1))
    assert proxied.get("/v1/usage", headers=spoof(1)).status_code == 429       # that client is locked out
    assert proxied.get("/v1/usage", headers=spoof(2)).status_code == 401       # a different client is not


def test_admin_token_cannot_be_brute_forced(build_client):
    c = build_client(settings_overrides={"auth_failures_per_minute": 3})
    for _ in range(3):
        assert c.post("/admin/tenants", json={"name": "X"}, headers={"X-Admin-Token": "nope"}).status_code == 401
    assert c.post("/admin/tenants", json={"name": "X"}, headers={"X-Admin-Token": "admin-token-123456"}).status_code == 429


def test_access_log_has_one_clean_json_line_per_request(build_client, make_tenant, caplog):
    c = build_client(FakeExtractor(GOOD_RAW))
    _, h = make_tenant()
    with caplog.at_level(logging.INFO, logger="invoiceguard.access"):
        res = c.get("/v1/usage?month=2026-10", headers=h)
    line = json.loads(caplog.records[-1].getMessage())
    assert line["path"] == "/v1/usage" and line["status"] == 200 and line["tenant"].startswith("t_")
    assert line["request_id"] == res.headers["x-request-id"] and set(line) == {"request_id", "method", "path", "status", "ms", "tenant"}
    text = caplog.text
    assert "2026-10" not in text and "Bearer" not in text and h["Authorization"].split()[1] not in text


def test_unexpected_errors_return_a_clean_500_and_log_no_content(build_client, make_tenant, monkeypatch, caplog):
    from invoiceguard import pipeline
    monkeypatch.setattr(pipeline, "process_document", lambda **kw: (_ for _ in ()).throw(RuntimeError("secret invoice text 4242")))
    c = build_client()
    _, h = make_tenant()
    with caplog.at_level(logging.INFO, logger="invoiceguard.access"):
        res = upload(c, h, pdf(1))
    assert res.status_code == 500 and res.json()["error"]["code"] == "internal_error" and "x-request-id" in res.headers
    assert "secret invoice text" not in res.text and "secret invoice text" not in caplog.text and "RuntimeError" in caplog.text


class BlockingExtractor(FakeExtractor):
    def __init__(self):
        super().__init__(GOOD_RAW)
        self.started, self.release = threading.Event(), threading.Event()

    def extract(self, data, media_type):
        self.started.set()
        assert self.release.wait(10)
        return super().extract(data, media_type)


def test_only_a_limited_number_of_invoices_are_read_at_once(build_client, make_tenant):
    ex = BlockingExtractor()
    c = build_client(ex, settings_overrides={"max_concurrent_reads": 1})
    _, h = make_tenant()
    first = {}
    t = threading.Thread(target=lambda: first.update(res=upload(c, h, pdf(1))))
    t.start()
    assert ex.started.wait(10)
    busy = upload(c, h, pdf(2))
    assert busy.status_code == 503 and busy.json()["error"]["code"] == "server_busy" and busy.json()["error"]["hint"]
    ex.release.set()
    t.join(10)
    assert first["res"].status_code == 201 and len(ex.calls) == 1
