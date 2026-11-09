"""End to end over real sockets: real server + real ClaudeExtractor/urllib transport + a local stand-in for the AI provider."""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import uvicorn

from invoiceguard import tenants
from invoiceguard.api import create_app
from invoiceguard.config import Settings
from invoiceguard.pipeline import control_path
from conftest import BAD_RAW, GOOD_RAW, pdf


class StubProvider(BaseHTTPRequestHandler):
    seen: list = []
    script: list = []   # statuses to return before succeeding
    invoice: dict = GOOD_RAW

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        status = type(self).script.pop(0) if type(self).script else 200
        if status != 200:
            self.send_response(status)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        payload = json.dumps({"model": "stub-model", "stop_reason": "tool_use", "usage": {"input_tokens": 900, "output_tokens": 250},
                              "content": [{"type": "tool_use", "name": "record_invoice", "input": type(self).invoice}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(tmp_path):
    StubProvider.seen, StubProvider.script, StubProvider.invoice = [], [], GOOD_RAW
    provider = ThreadingHTTPServer(("127.0.0.1", 0), StubProvider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    settings = Settings(data_dir=tmp_path / "data", anthropic_api_key="sk-ant-e2e-test", model="stub-model",
                        anthropic_base_url=f"http://127.0.0.1:{provider.server_address[1]}", admin_token="admin-token-123456")
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    from contextlib import closing
    with closing(tenants.open_control(control_path(settings))) as conn:
        _, key = tenants.create_tenant(conn, "E2E")
    yield f"http://127.0.0.1:{port}", {"Authorization": f"Bearer {key}"}
    server.should_exit = True
    thread.join(timeout=5)
    provider.shutdown()


def test_real_http_upload_reads_with_the_real_transport(live):
    base, headers = live
    res = httpx.post(f"{base}/v1/invoices", files={"file": ("a.pdf", pdf(1), "application/pdf")}, headers=headers, timeout=20)
    assert res.status_code == 201 and res.json()["status"] == "clean" and res.json()["usage"]["model"] == "stub-model"
    path, sent_headers, body = StubProvider.seen[0]
    assert path == "/v1/messages" and sent_headers["x-api-key"] == "sk-ant-e2e-test"
    assert sent_headers["anthropic-version"] == "2023-06-01" and body["tool_choice"]["name"] == "record_invoice"
    assert body["messages"][0]["content"][0]["type"] == "document"
    assert httpx.get(f"{base}/v1/usage", headers=headers).json()["input_tokens"] == 900


def test_real_http_retries_a_busy_provider(live):
    base, headers = live
    StubProvider.script = [529]
    started = time.monotonic()
    res = httpx.post(f"{base}/v1/invoices", files={"file": ("a.pdf", pdf(2), "application/pdf")}, headers=headers, timeout=30)
    assert res.status_code == 201 and len(StubProvider.seen) == 2 and time.monotonic() - started >= 1.0


def test_real_http_flagged_invoice_then_review_over_the_wire(live):
    base, headers = live
    StubProvider.invoice = BAD_RAW
    res = httpx.post(f"{base}/v1/invoices", files={"file": ("b.pdf", pdf(3), "application/pdf")}, headers=headers, timeout=20).json()
    assert res["status"] == "needs_review"
    done = httpx.post(f"{base}/v1/review-queue/{res['review_item_id']}/resolve", headers=headers,
                      json={"note": "fixed from the PDF", "corrected_invoice": GOOD_RAW})
    assert done.status_code == 200 and "Globex Corp" in httpx.get(f"{base}/v1/export.csv", headers=headers).text
    assert "InvoiceGuard" in httpx.get(f"{base}/").text


def test_real_http_provider_outage_gives_a_clear_error(live):
    base, headers = live
    StubProvider.script = [401]
    res = httpx.post(f"{base}/v1/invoices", files={"file": ("c.pdf", pdf(4), "application/pdf")}, headers=headers, timeout=20)
    assert res.status_code == 502 and "rejected the server's API key" in res.json()["error"]["message"]
    assert "sk-ant-e2e-test" not in res.text
