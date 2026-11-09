"""Release rehearsal: the whole customer journey against the real server process, with a stand-in AI provider.

Needs no API key and spends nothing. Run before every release:   python scripts/release_smoke.py
Exit code 0 means every step passed.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from make_sample_pdf import make_pdf  # noqa: E402

CLEAN = {"invoice_metadata": {"invoice_number": "R-100", "vendor_name": "Rehearsal Supplies", "issue_date": "2025-03-01",
                              "due_date": "2025-03-31", "currency": "USD"},
         "line_items": [{"description": "Item", "quantity": 4, "unit_price": 25.0, "total_amount": 100.0}],
         "financial_summary": {"subtotal": 100.0, "tax_amount": 10.0, "grand_total": 110.0}, "ocr_warnings": []}
FLAGGED = {**CLEAN, "invoice_metadata": {**CLEAN["invoice_metadata"], "invoice_number": "R-200"},
           "line_items": [{"description": "Item", "quantity": 4, "unit_price": 25.0, "total_amount": 25.0}],
           "financial_summary": {"subtotal": 25.0, "tax_amount": 10.0, "grand_total": 110.0}}


class Provider(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        doc = base64.b64decode(body["messages"][0]["content"][0]["source"]["data"])
        payload = json.dumps({"model": "stand-in", "stop_reason": "tool_use", "usage": {"input_tokens": 1000, "output_tokens": 200},
                              "content": [{"type": "tool_use", "name": "record_invoice", "input": FLAGGED if b"FLAG" in doc else CLEAN}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


FAILED = []
NEUTRAL = tempfile.gettempdir()  # run from outside the source tree so the INSTALLED package is what gets tested


def step(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def cli(env, *args):
    return subprocess.run([sys.executable, "-m", "invoiceguard.cli", *args], env=env, capture_output=True, text=True, cwd=NEUTRAL)


def main() -> int:
    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    port = free_port()
    secret_key = "sk-ant-rehearsal-secret"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        env = {**os.environ, "INVOICEGUARD_DATA_DIR": str(tmp / "data"), "INVOICEGUARD_ADMIN_TOKEN": "rehearsal-admin-token-0123456789",
               "ANTHROPIC_API_KEY": secret_key, "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{provider.server_address[1]}",
               "INVOICEGUARD_PUBLIC_URL": "https://invoices.example.test", "INVOICEGUARD_TRUST_PROXY": "1",
               "INVOICEGUARD_AUTH_FAILURES_PER_MIN": "5"}
        doc = cli(env, "doctor")
        step("doctor finds no blocking problem", doc.returncode == 0, doc.stdout)
        step("doctor never prints the provider key", secret_key not in doc.stdout + doc.stderr)

        log_path = tmp / "server.log"
        with open(log_path, "w") as log:
            server = subprocess.Popen([sys.executable, "-m", "invoiceguard.cli", "serve", "--port", str(port)], env=env, cwd=NEUTRAL,
                                      stdout=log, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(100):
                try:
                    if httpx.get(f"{base}/readyz", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.1)
            step("server reports ready", httpx.get(f"{base}/readyz").json() == {"status": "ready"})

            admin = {"X-Admin-Token": env["INVOICEGUARD_ADMIN_TOKEN"]}
            created = httpx.post(f"{base}/admin/tenants", json={"name": "Rehearsal Co", "default_currency": "usd", "monthly_limit": 10}, headers=admin)
            key = created.json()["api_key"]
            h = {"Authorization": f"Bearer {key}"}
            step("admin creates a customer and gets a key once", created.status_code == 201 and key.startswith("ig_"))

            up = lambda data, name: httpx.post(f"{base}/v1/invoices", files={"file": (name, data, "application/pdf")}, headers=h, timeout=30)
            clean_pdf, flag_pdf = make_pdf(["clean invoice"]), make_pdf(["FLAG invoice"])
            r1 = up(clean_pdf, "clean.pdf")
            step("clean invoice is accepted and not queued", r1.status_code == 201 and r1.json()["status"] == "clean" and r1.json()["review_item_id"] is None)
            r2 = up(flag_pdf, "flag.pdf")
            item = r2.json().get("review_item_id")
            step("misread invoice is flagged for review with reasons", r2.json()["status"] == "needs_review" and any("4 x 25" in i for i in r2.json()["issues"]))
            step("same file again is free (cached)", up(clean_pdf, "clean.pdf").status_code == 200)
            step("queue shows exactly the flagged invoice", [i["id"] for i in httpx.get(f"{base}/v1/review-queue", headers=h).json()["items"]] == [item])
            step("original file can be downloaded for the reviewer", httpx.get(f"{base}/v1/invoices/{r2.json()['document_id']}/file", headers=h).content == flag_pdf)

            c = httpx.post(f"{base}/v1/review-queue/{item}/claim", json={"assignee": "Amina"}, headers=h)
            bad = httpx.post(f"{base}/v1/review-queue/{item}/resolve", json={"note": "x", "corrected_invoice": FLAGGED}, headers=h)
            fixed = {**FLAGGED, "line_items": CLEAN["line_items"], "financial_summary": CLEAN["financial_summary"]}
            ok = httpx.post(f"{base}/v1/review-queue/{item}/resolve", json={"note": "checked against the PDF", "corrected_invoice": fixed}, headers=h)
            step("reviewer claims, a bad fix is refused, a good fix resolves", c.status_code == 200 and bad.status_code == 422 and ok.status_code == 200)
            csv = httpx.get(f"{base}/v1/export.csv", headers=h).text
            step("export contains both checked invoices", "R-100" in csv and "R-200" in csv and csv.count("\n") == 3)
            usage = httpx.get(f"{base}/v1/usage", headers=h).json()
            step("usage counts 2 documents (the repeat was free)", usage["documents"] == 2 and usage["input_tokens"] == 2000)

            page = httpx.get(f"{base}/")
            step("dashboard served with a strict policy", page.status_code == 200 and "script-src 'self'" in page.headers["content-security-policy"])
            attacker = {"Authorization": "Bearer wrong", "X-Forwarded-For": "203.0.113.9"}   # behind the proxy, each client has its own address
            codes = [httpx.get(f"{base}/v1/usage", headers=attacker, timeout=5).status_code for _ in range(6)]
            step("repeated wrong keys get locked out", codes[:5] == [401] * 5 and codes[5] == 429, str(codes))
            step("a locked-out address does not block other customers", httpx.get(f"{base}/v1/usage", headers=h).status_code == 200)

            bk = cli(env, "backup", "--dest", str(tmp / "bk"))
            archive = json.loads(bk.stdout)["backup"] if bk.returncode == 0 else ""
            step("backup works while the service is running", bk.returncode == 0 and pathlib.Path(archive).exists(), bk.stderr)
            rs = cli(env, "restore", archive, "--into", str(tmp / "restored"))
            step("restore into an empty folder works", rs.returncode == 0 and json.loads(rs.stdout)["tenants"] == 1, rs.stderr)
            step("restore refuses to overwrite", cli(env, "restore", archive, "--into", str(tmp / "restored")).returncode == 2)
            eco = cli(env, "economics", "--input-price-per-mtok", "3", "--output-price-per-mtok", "15")
            step("economics prices from measured usage", eco.returncode == 0 and json.loads(eco.stdout)["customers"][0]["documents"] == 2)
            step("billing CSV lists the customer", "Rehearsal Co" in cli(env, "usage", "--csv").stdout)

            tid = created.json()["tenant"]["id"]
            tpl = tmp / "template.json"
            tpl.write_text(json.dumps({"columns": [{"header": "Supplier", "field": "vendor_name"}, {"header": "Amount", "field": "grand_total"}],
                                       "delimiter": ";", "decimal": ","}))
            chk = cli(env, "template-check", str(tpl))
            step("template preview uses invented data only", chk.returncode == 0 and "Example Supplies Ltd;1234,50" in chk.stdout)
            st = cli(env, "set-export-template", "--tenant", tid, "--file", str(tpl))
            exp = httpx.get(f"{base}/v1/export.csv", headers=h).text
            step("customer export now follows their template", st.returncode == 0 and exp.splitlines()[0] == "Supplier;Amount" and "Rehearsal Supplies;110,00" in exp)
            step("standard layout is still available", httpx.get(f"{base}/v1/export.csv?template=default", headers=h).text.startswith("document_id,"))

            probe = httpx.get(f"{base}/v1/usage", headers=h)
            tr = cli(env, "trace", str(log_path), "--request-id", probe.headers["x-request-id"])
            found = json.loads(tr.stdout) if tr.returncode == 0 else {}
            step("a request can be found in the log by the id the customer quotes", found.get("path") == "/v1/usage" and found.get("tenant") == tid, tr.stderr)

            doc = r2.json()["document_id"][:10]
            shown = cli(env, "show", "--tenant", tid, "--document", doc)
            step("support can view a stored invoice (with a privacy warning)", shown.returncode == 0 and json.loads(shown.stdout)["status"] == "reviewed" and "customer data" in shown.stderr)
            refused = cli(env, "export-case", "--tenant", tid, "--document", doc, "--into", str(tmp / "cases"))
            step("a case cannot be exported without the customer's permission", refused.returncode == 2 and not (tmp / "cases").exists())
            made = cli(env, "export-case", "--tenant", tid, "--document", doc, "--into", str(tmp / "cases"), "--permission-confirmed")
            step("a reviewer-confirmed invoice becomes an accuracy test case", made.returncode == 0 and len(list((tmp / "cases").glob("*"))) == 2, made.stderr)
            found = httpx.get(f"{base}/v1/invoices", params={"q": "rehearsal"}, headers=h).json()
            step("search finds invoices by vendor name", found["total"] == 2, str(found["total"]))
            first = httpx.get(f"{base}/v1/invoices/changes", params={"limit": 1}, headers=h).json()
            second = httpx.get(f"{base}/v1/invoices/changes?limit=1&cursor={first['next_cursor']}", headers=h).json()   # cursor pasted as is
            third = httpx.get(f"{base}/v1/invoices/changes?limit=1&cursor={second['next_cursor']}", headers=h).json()
            ids = {first["invoices"][0]["document_id"], second["invoices"][0]["document_id"]}
            step("change feed pages through every invoice exactly once", len(ids) == 2 and third["invoices"] == [])
            metrics = httpx.get(f"{base}/admin/metrics", headers=admin).text
            step("metrics count the work done", 'invoiceguard_invoices_total{result="clean"} 1' in metrics and 'invoiceguard_invoices_total{result="needs_review"} 1' in metrics)
            bill = cli(env, "billing", "--price-per-document", "0.25", "--included", "1", "--base-fee", "20", "--currency", "usd")
            rot = httpx.post(f"{base}/v1/settings/rotate-key", headers=h).json()
            step("a customer can replace their own key", httpx.get(f"{base}/v1/usage", headers=h).status_code == 401
                 and httpx.get(f"{base}/v1/usage", headers={"Authorization": f"Bearer {rot['api_key']}"}).status_code == 200)
            step("billing drafts exact statements (nothing is sent)", bill.returncode == 0 and json.loads(bill.stdout)["statements"][0]["total"] == "20.25", bill.stdout)
        finally:
            server.terminate()
            server.wait(10)

        dry = cli(env, "erase-tenant", "--tenant", tid)
        step("erase is a dry run by default", json.loads(dry.stdout)["dry_run"] is True and (tmp / "data" / "tenants" / tid).exists())
        real = cli(env, "erase-tenant", "--tenant", tid, "--confirm")
        step("confirmed erase removes the customer's data", json.loads(real.stdout)["documents"] == 2 and not (tmp / "data" / "tenants" / tid).exists())

        logs = log_path.read_text()
        lines = [json.loads(l) for l in logs.splitlines() if l.startswith("{")]
        step("server logged JSON request lines", len(lines) > 10 and all("request_id" in l for l in lines if "path" in l))
        leaked = [w for w in (key, rot["api_key"], secret_key, "Rehearsal Supplies", "R-100", "R-200", "checked against the PDF", "Amina") if w in logs]
        step("logs contain no keys and no invoice content", not leaked, str(leaked))
    provider.shutdown()
    print("\n" + ("REHEARSAL PASSED" if not FAILED else f"REHEARSAL FAILED: {FAILED}"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
