"""Pre-launch and pre-upgrade checks. Never prints secrets."""
from __future__ import annotations

import shutil
import socket
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

from . import backup, migrate, pipeline, tenants
from .config import Settings

GIB = 1024 ** 3


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str


def _tcp(host: str, port: int = 443, timeout: float = 5.0) -> None:
    with socket.create_connection((host, port), timeout=timeout):
        pass


def _http_get(url: str, timeout: float = 3.0) -> None:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        if resp.status >= 400:
            raise OSError(f"HTTP {resp.status}")


def run_checks(settings: Settings, backup_dir: Optional[Path] = None, online: bool = False,
               connect: Callable[[str], None] = _tcp, fetch: Callable[[str], None] = _http_get) -> list:
    out = []

    def add(name, status, detail):
        out.append(Check(name, status, detail))

    if settings.reader == "local":
        add("provider key", "warn", "INVOICEGUARD_READER=local reads text already inside a PDF, and images only when "
            "tesseract is installed. No cloud key is used. Accuracy on real invoices has not been measured")
    elif settings.anthropic_api_key:
        add("provider key", "ok", "ANTHROPIC_API_KEY is set")
    else:
        add("provider key", "fail", "ANTHROPIC_API_KEY is not set, so invoices cannot be read")

    if not settings.admin_token:
        add("admin token", "warn", "not set: /admin is switched off. Create customers with the command line instead")
    elif len(settings.admin_token) < 24:
        add("admin token", "warn", "set, but shorter than 24 characters. Use a long random value")
    else:
        add("admin token", "ok", "set")

    if not settings.public_base_url:
        add("public address", "warn", "INVOICEGUARD_PUBLIC_URL is not set: Slack links and the welcome message need it")
    elif settings.public_base_url.startswith("https://"):
        add("public address", "ok", settings.public_base_url)
    elif (urlparse(settings.public_base_url).hostname or "").lower() in _LOCAL_HOSTS:
        add("public address", "warn", f"{settings.public_base_url} is only for this machine. A customer-facing server needs https://")
    else:
        add("public address", "fail", "must start with https:// in production")

    if settings.public_base_url and settings.public_base_url.startswith("https://") and not settings.trust_proxy:
        add("reverse proxy", "warn", "set INVOICEGUARD_TRUST_PROXY=1 when running behind your proxy. Otherwise every "
            "customer shares the proxy's address and one customer's wrong keys can lock everyone out")
    else:
        add("reverse proxy", "ok", "trust_proxy=%s" % settings.trust_proxy)

    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.data_dir / ".doctor"
        probe.write_text("ok")
        probe.unlink()
        free = shutil.disk_usage(settings.data_dir).free
        if free < GIB // 10:
            add("data folder", "fail", f"only {free // (1024 ** 2)} MB free")
        elif free < GIB:
            add("data folder", "warn", f"writable, but only {free // (1024 ** 2)} MB free")
        else:
            add("data folder", "ok", f"writable, {free // GIB} GB free")
    except OSError as exc:
        add("data folder", "fail", f"not writable ({exc.__class__.__name__})")

    try:
        with closing(tenants.open_control(pipeline.control_path(settings))):
            pass
        add("control database", "ok", "opens and is at the expected schema version")
    except migrate.SchemaTooNew as exc:
        add("control database", "fail", str(exc))
    except Exception as exc:
        add("control database", "fail", f"cannot open ({exc.__class__.__name__})")

    if backup_dir is None:
        add("backups", "warn", "no backup folder given (--backup-dir), so freshness was not checked")
    else:
        age = backup.latest_backup_age_hours(backup_dir)
        if age is None:
            add("backups", "fail", f"no backup found in {backup_dir}")
        elif age > 36:
            add("backups", "warn", f"newest backup is {age:.0f} hours old")
        else:
            add("backups", "ok", f"newest backup is {age:.0f} hours old")

    if not settings.monitor_url:
        add("monitoring", "warn", "not configured. Point a checker you run at GET /readyz. This program does not send an alert.")
    else:
        try:
            fetch(settings.monitor_url)
            add("monitoring", "ok", "the configured checker URL answered")
        except Exception as exc:
            add("monitoring", "fail", f"the configured checker URL did not answer ({exc.__class__.__name__})")

    if online:
        host = urlparse(settings.anthropic_base_url).hostname or ""
        try:
            connect(host)
            add("provider reachable", "ok", f"connected to {host}")
        except OSError as exc:
            add("provider reachable", "fail", f"cannot connect to {host} ({exc.__class__.__name__})")
    return out


def exit_code(checks: list) -> int:
    return 1 if any(c.status == "fail" for c in checks) else 0
