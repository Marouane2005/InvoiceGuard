"""Runs the REAL deploy/Caddyfile in front of the REAL server and checks the properties the security design relies on.

    python scripts/proxy_check.py [--caddy /path/to/caddy]

Needs the caddy binary (https://caddyserver.com/download). Only the site address and the upstream are swapped for local
ports (a real certificate needs a real domain); every other directive is the file as shipped. No API key needed.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
FAILED = []


def step(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(url: str, tries: int = 100) -> bool:
    for _ in range(tries):
        try:
            httpx.get(url, timeout=1)
            return True
        except httpx.HTTPError:
            time.sleep(0.1)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--caddy", default="caddy")
    caddy = parser.parse_args().caddy
    real = (ROOT / "deploy" / "Caddyfile").read_text()
    app_port, proxy_port = free_port(), free_port()
    token = "proxy-check-admin-token-0123456789"

    validate = subprocess.run([caddy, "validate", "--config", str(ROOT / "deploy" / "Caddyfile"), "--adapter", "caddyfile"],
                              capture_output=True, text=True, env={**os.environ, "DOMAIN": "invoices.example.test"})
    step("the shipped Caddyfile is valid Caddy configuration", validate.returncode == 0, validate.stderr.strip()[-200:])

    test_file = re.sub(r"^\{\$DOMAIN\} \{", "{\n\tauto_https off\n\tadmin off\n}\n\nhttp://127.0.0.1:%d {" % proxy_port, real, count=1, flags=re.M)
    test_file = test_file.replace("reverse_proxy invoiceguard:8000", f"reverse_proxy 127.0.0.1:{app_port}")
    assert test_file != real and f"127.0.0.1:{app_port}" in test_file, "the Caddyfile layout changed; update this check"

    with tempfile.TemporaryDirectory() as tmp:
        cfg = pathlib.Path(tmp) / "Caddyfile"
        cfg.write_text(test_file)
        env = {**os.environ, "INVOICEGUARD_DATA_DIR": tmp, "INVOICEGUARD_ADMIN_TOKEN": token, "INVOICEGUARD_TRUST_PROXY": "1",
               "INVOICEGUARD_AUTH_FAILURES_PER_MIN": "5"}
        app = subprocess.Popen([sys.executable, "-m", "invoiceguard.cli", "serve", "--port", str(app_port)], env=env,
                               cwd=tempfile.gettempdir(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proxies = []

        def start_proxy(admin_allow=None):
            for p in proxies:
                p.terminate(); p.wait(10)
            proxies.clear()
            penv = {k: v for k, v in os.environ.items() if k != "ADMIN_ALLOW"}
            if admin_allow:
                penv["ADMIN_ALLOW"] = admin_allow
            proxies.append(subprocess.Popen([caddy, "run", "--config", str(cfg), "--adapter", "caddyfile"], env=penv,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            return wait_for(f"http://127.0.0.1:{proxy_port}/healthz")

        via, direct, admin = f"http://127.0.0.1:{proxy_port}", f"http://127.0.0.1:{app_port}", {"X-Admin-Token": token}
        try:
            step("the app starts", wait_for(f"{direct}/readyz"))
            step("the proxy starts with the default admin rule", start_proxy())

            r = httpx.get(f"{via}/admin/usage", headers=admin)
            step("admin works from this server itself (the default allowed address)", r.status_code == 200, str(r.status_code))
            page = httpx.get(f"{via}/")
            step("the dashboard passes through with its security policy intact", page.status_code == 200 and "script-src 'self'" in page.headers.get("content-security-policy", ""))
            step("the proxy adds HSTS and hides the server name", "max-age=31536000" in page.headers.get("strict-transport-security", "") and "server" not in page.headers, str(dict(page.headers)))

            big = httpx.post(f"{via}/v1/invoices", content=b"x" * (13 * 1024 * 1024), headers={"Authorization": "Bearer none"}, timeout=30)
            step("a 13 MB upload is refused at the proxy (limit 12 MB)", big.status_code == 413, str(big.status_code))

            step("the proxy restarts with ADMIN_ALLOW set to another address", start_proxy("203.0.113.9"))
            denied = httpx.get(f"{via}/admin/usage", headers=admin)
            step("admin is refused with 404 from any other address, even with the right token", denied.status_code == 404 and denied.content == b"", f"{denied.status_code} {denied.content[:40]!r}")
            step("the same call straight to the app still works (the block is at the proxy)", httpx.get(f"{direct}/admin/usage", headers=admin).status_code == 200)
            step("non-admin traffic is unaffected by the admin rule", httpx.get(f"{via}/healthz").status_code == 200)

            # last, because a lockout (correctly) blocks that address for a minute, including later calls from it
            spoofed = [httpx.get(f"{via}/v1/usage", headers={"Authorization": "Bearer wrong", "X-Forwarded-For": f"203.0.113.{i}"}).status_code for i in range(7)]
            step("a spoofed X-Forwarded-For cannot dodge the lockout (the proxy overwrites it)", spoofed[:5] == [401] * 5 and spoofed[5] == 429, str(spoofed))
        finally:
            for p in proxies:
                p.terminate(); p.wait(10)
            app.terminate(); app.wait(10)
    print("\n" + ("PROXY CHECK PASSED" if not FAILED else f"PROXY CHECK FAILED: {FAILED}"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
