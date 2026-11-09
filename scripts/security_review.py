"""Automated security review for a release.   python scripts/security_review.py [--audit] [--root DIR]

Checks what a machine can check. It does not replace a person reading the diff and thinking like an attacker.
Exit code 1 if anything fails. --audit also runs pip-audit (needs network).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SECRET = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}|ig_[A-Za-z0-9_-]{35,}|hooks\.slack\.com/services/T[A-Z0-9]{6,}/B[A-Z0-9]{6,}/[A-Za-z0-9]{12,}")
RISKY_NAMES = re.compile(r"(^|/)\.env$|\.db($|-wal$|-shm$)|^data/|^backups/|\.pem$|id_rsa")
TEXT_SUFFIXES = {".py", ".md", ".js", ".json", ".yml", ".yaml", ".toml", ".html", ".css", ".txt", ".example", ""}


def tracked_files(root: Path) -> list:
    out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True)
    if out.returncode == 0 and out.stdout.strip():
        return [root / line for line in out.stdout.splitlines()]
    return [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts and "node_modules" not in p.parts]


def run_checks(root: Path, check_app: bool = True, audit: bool = False) -> list:
    root = Path(root)
    results = []

    def add(name, ok, detail=""):
        results.append((name, ok, detail))

    files = tracked_files(root)
    rel = [str(p.relative_to(root)) for p in files]
    leaked = [r for p, r in zip(files, rel) if p.suffix in TEXT_SUFFIXES and p.is_file() and SECRET.search(p.read_text(errors="ignore"))]
    add("no keys or tokens in tracked files", not leaked, "found in: " + ", ".join(leaked) if leaked else "")
    risky = [r for r in rel if RISKY_NAMES.search(r)]
    add("no .env, database, backup or key files tracked", not risky, ", ".join(risky))
    ignore = (root / ".gitignore").read_text() if (root / ".gitignore").exists() else ""
    missing = [x for x in (".env", "*.db", "data/", "node_modules/") if x not in ignore.splitlines()]
    add(".gitignore covers .env, databases, data and node_modules", not missing, "missing: " + ", ".join(missing))

    docker = (root / "Dockerfile").read_text() if (root / "Dockerfile").exists() else ""
    users = re.findall(r"^USER\s+(\S+)", docker, re.M)
    add("the container runs as a non-root user", bool(users) and users[-1] not in ("root", "0"), "no USER line" if not users else f"USER {users[-1]}")
    add("the container pins its dependencies", "-c constraints.txt" in docker, "")

    try:
        import yaml
        compose = yaml.safe_load((root / "docker-compose.prod.yml").read_text())
        published = sorted(n for n, s in compose["services"].items() if s.get("ports"))
        add("only the proxy publishes ports in production", published == ["caddy"], f"publishing ports: {published}")
    except Exception as exc:  # missing file or invalid YAML is itself a failure
        add("only the proxy publishes ports in production", False, f"cannot read docker-compose.prod.yml ({exc.__class__.__name__})")

    caddy = (root / "deploy" / "Caddyfile").read_text() if (root / "deploy" / "Caddyfile").exists() else ""
    add("the admin area is closed to the internet at the proxy", "path /admin/*" in caddy and "remote_ip" in caddy and "respond @admin_denied 404" in caddy, "")

    constraints = (root / "constraints.txt").read_text() if (root / "constraints.txt").exists() else ""
    loose = [l for l in constraints.splitlines() if l.strip() and not l.startswith("#") and "==" not in l]
    add("every dependency is pinned exactly", bool(constraints) and not loose, "unpinned: " + ", ".join(loose) if loose else "constraints.txt missing" if not constraints else "")

    if check_app:
        sys.path.insert(0, str(root))
        try:
            from invoiceguard.config import Settings
            s = Settings.from_env({})
            add("defaults are safe (admin off, proxy not trusted, no provider key)", s.admin_token is None and not s.trust_proxy and s.anthropic_api_key is None, "")
            from fastapi.testclient import TestClient
            from invoiceguard.api import create_app
            with tempfile.TemporaryDirectory() as tmp:
                client = TestClient(create_app(Settings(data_dir=Path(tmp))))
                page, api, health = client.get("/"), client.get("/v1/usage"), client.get("/healthz")
            csp = page.headers.get("content-security-policy", "")
            add("the dashboard forbids inline scripts and framing", "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp, csp)
            add("responses carry nosniff and no-referrer", all(r.headers.get("x-content-type-options") == "nosniff" and r.headers.get("referrer-policy") == "no-referrer" for r in (page, api, health)), "")
            add("API responses are not cached", api.headers.get("cache-control") == "no-store", "")
            body = api.text
            add("errors reveal no internals", api.status_code == 401 and "Traceback" not in body and "File \"" not in body and set(api.json()["error"]) == {"code", "message", "hint"}, "")
            add("the admin area is hidden when no admin token is set", client.post("/admin/tenants", json={"name": "x"}).status_code == 404, "")
        except Exception as exc:
            add("application checks could run", False, f"{exc.__class__.__name__}: {exc}")
        finally:
            sys.path.remove(str(root))

    if audit:
        done = subprocess.run([sys.executable, "-m", "pip_audit", "-r", str(root / "constraints.txt"), "--progress-spinner", "off"], capture_output=True, text=True)
        output = (done.stdout + done.stderr).strip()
        if "No module named pip_audit" in output:
            output = "pip-audit is not installed. Run: pip install pip-audit   (then run this again)"
        add("no known vulnerabilities in the pinned dependencies (pip-audit)", done.returncode == 0, output[-300:])
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Automated security review.")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--audit", action="store_true", help="also run pip-audit (needs network)")
    parser.add_argument("--skip-app", action="store_true", help="skip the checks that start the application")
    args = parser.parse_args(argv)
    results = run_checks(Path(args.root), check_app=not args.skip_app, audit=args.audit)
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))
    failed = [n for n, ok, _ in results if not ok]
    print("\n" + (f"{len(failed)} check(s) failed. This is not a substitute for a person reading the diff." if failed else "All automated checks passed. A person still has to read the diff and think like an attacker."))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
