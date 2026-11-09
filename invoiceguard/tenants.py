"""Customer accounts, API keys (stored only as hashes), settings and usage metering."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from . import migrate

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    api_key_hash      TEXT NOT NULL UNIQUE,
    default_currency  TEXT,
    monthly_limit     INTEGER CHECK (monthly_limit IS NULL OR monthly_limit >= 0),
    slack_webhook_url TEXT,
    active            INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id     TEXT NOT NULL REFERENCES tenants(id),
    document_id   TEXT NOT NULL,
    month         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (tenant_id, document_id)
);
CREATE INDEX IF NOT EXISTS idx_usage_tenant_month ON usage(tenant_id, month);
"""

SCHEMA_VERSION = 2
# Add new entries here when the schema changes; never edit old ones. SCHEMA above stays the version-1 shape on purpose:
# fresh and existing databases both reach the current shape by running these in order.
MIGRATIONS: dict = {2: ["ALTER TABLE tenants ADD COLUMN export_template TEXT"]}

UNSET = object()
_CURRENCY = re.compile(r"^[A-Z]{3}$")


def open_control(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    migrate.ensure_version(conn, SCHEMA_VERSION, MIGRATIONS, "control database")
    return conn


def _hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def validate_currency(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    norm = value.strip().upper()
    if not _CURRENCY.match(norm):
        raise ValueError("currency must be a 3-letter code such as USD, EUR or MAD")
    return norm


def validate_webhook(url: Optional[str]) -> Optional[str]:
    if url is None:
        return None
    if not url.startswith("https://hooks.slack.com/"):
        raise ValueError("Slack webhook URLs start with https://hooks.slack.com/")
    return url


def public(tenant: dict) -> dict:
    """Safe to return to the customer. The key hash and the webhook URL never leave the server."""
    return {"id": tenant["id"], "name": tenant["name"], "default_currency": tenant["default_currency"],
            "monthly_limit": tenant["monthly_limit"], "slack_configured": bool(tenant["slack_webhook_url"]),
            "export_template_configured": bool(tenant.get("export_template")),
            "active": bool(tenant["active"]), "created_at": tenant["created_at"]}


def create_tenant(conn, name: str, default_currency: Optional[str] = None, monthly_limit: Optional[int] = None) -> tuple:
    name = (name or "").strip()
    if not 1 <= len(name) <= 100:
        raise ValueError("name must be 1 to 100 characters")
    if monthly_limit is not None and (not isinstance(monthly_limit, int) or monthly_limit < 0):
        raise ValueError("monthly_limit must be a whole number, 0 or more")
    currency = validate_currency(default_currency)
    tenant_id = "t_" + secrets.token_hex(6)
    api_key = "ig_" + secrets.token_urlsafe(32)
    conn.execute("INSERT INTO tenants (id, name, api_key_hash, default_currency, monthly_limit, created_at) VALUES (?,?,?,?,?,?)",
                 (tenant_id, name, _hash(api_key), currency, monthly_limit, _now()))
    return get_tenant(conn, tenant_id), api_key


def get_tenant(conn, tenant_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    return dict(row) if row else None


def authenticate(conn, api_key: str) -> Optional[dict]:
    if not api_key:
        return None
    row = conn.execute("SELECT * FROM tenants WHERE api_key_hash = ? AND active = 1", (_hash(api_key),)).fetchone()
    return dict(row) if row else None


def rotate_key(conn, tenant_id: str) -> Optional[str]:
    api_key = "ig_" + secrets.token_urlsafe(32)
    cur = conn.execute("UPDATE tenants SET api_key_hash = ? WHERE id = ?", (_hash(api_key), tenant_id))
    return api_key if cur.rowcount else None


def deactivate(conn, tenant_id: str) -> bool:
    return conn.execute("UPDATE tenants SET active = 0 WHERE id = ?", (tenant_id,)).rowcount > 0


def export_template(tenant: dict):
    raw = tenant.get("export_template")
    return json.loads(raw) if raw else None


def update_settings(conn, tenant_id: str, default_currency=UNSET, slack_webhook_url=UNSET, export_template=UNSET) -> None:
    if default_currency is not UNSET:
        conn.execute("UPDATE tenants SET default_currency = ? WHERE id = ?", (validate_currency(default_currency), tenant_id))
    if slack_webhook_url is not UNSET:
        conn.execute("UPDATE tenants SET slack_webhook_url = ? WHERE id = ?", (validate_webhook(slack_webhook_url), tenant_id))
    if export_template is not UNSET:
        from . import exports  # validates; raises TemplateError (a ValueError) with a message that says what to fix
        value = None if export_template is None else json.dumps(exports.validate_template(export_template))
        conn.execute("UPDATE tenants SET export_template = ? WHERE id = ?", (value, tenant_id))


def documents_in_month(conn, tenant_id: str, month: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM usage WHERE tenant_id = ? AND month = ?", (tenant_id, month)).fetchone()[0]


def record_usage(conn, tenant_id: str, document_id: str, month: str, input_tokens: int, output_tokens: int) -> None:
    conn.execute("INSERT OR IGNORE INTO usage (tenant_id, document_id, month, input_tokens, output_tokens, created_at) "
                 "VALUES (?,?,?,?,?,?)", (tenant_id, document_id, month, input_tokens, output_tokens, _now()))


def usage_summary(conn, tenant_id: str, month: str) -> dict:
    row = conn.execute("SELECT COUNT(*) AS documents, COALESCE(SUM(input_tokens),0) AS input_tokens, "
                       "COALESCE(SUM(output_tokens),0) AS output_tokens FROM usage WHERE tenant_id = ? AND month = ?",
                       (tenant_id, month)).fetchone()
    return {"month": month, "documents": row["documents"], "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"]}


def usage_all(conn, month: str) -> list:
    rows = conn.execute(
        "SELECT t.id, t.name, t.monthly_limit, COUNT(u.id) AS documents, COALESCE(SUM(u.input_tokens),0) AS input_tokens, "
        "COALESCE(SUM(u.output_tokens),0) AS output_tokens FROM tenants t LEFT JOIN usage u "
        "ON u.tenant_id = t.id AND u.month = ? GROUP BY t.id ORDER BY t.created_at", (month,)).fetchall()
    return [dict(r, month=month) for r in rows]
