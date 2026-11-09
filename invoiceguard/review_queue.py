"""SQLite-backed review queue for invoices flagged by validate_invoice().

Contract
--------
* One invoice (document_id) == one ACTIVE queue item (status open / in_review).
  Enforced by a partial unique index, not just application code.
* The table is the source of truth. Slack/email are notifications that point at a row.
* Notification delivery is at-least-once: a row is marked notified only after a
  successful send, so a failed send is retried on the next dispatch.
* No secrets are stored. The Slack webhook URL is passed in by the caller.

Stdlib only (sqlite3, urllib, json, hashlib).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_queue (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id            TEXT    NOT NULL CHECK (length(document_id) > 0),
    content_hash           TEXT    NOT NULL,
    status                 TEXT    NOT NULL DEFAULT 'open'
                           CHECK (status IN ('open','in_review','resolved','dismissed')),
    severity               TEXT    NOT NULL CHECK (severity IN ('low','medium','high')),
    issues_json            TEXT    NOT NULL,
    invoice_json           TEXT    NOT NULL,
    corrected_invoice_json TEXT,
    document_url           TEXT,
    assignee               TEXT,
    resolution_note        TEXT,
    created_at             TEXT    NOT NULL,
    updated_at             TEXT    NOT NULL,
    resolved_at            TEXT,
    notified_at            TEXT,
    CHECK ((status IN ('resolved','dismissed')) = (resolved_at IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_review_queue_active_doc
    ON review_queue(document_id) WHERE status IN ('open','in_review');
CREATE INDEX IF NOT EXISTS idx_review_queue_status_sev_created
    ON review_queue(status, severity, created_at);
"""

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}
MAX_ISSUES_IN_MESSAGE = 10
AUTO_RESOLVE_NOTE = "auto: re-extraction passed validation"


class QueueError(Exception):
    """Base class for queue errors."""


class InvalidTransition(QueueError):
    """Raised when an item is not in a state that allows the requested change."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_when_unlocked(conn: sqlite3.Connection, sql: str, *, script: bool = False) -> None:
    """Retry statements that SQLite refuses at once when another connection is opening the same file."""
    for attempt in range(40):
        try:
            if script:
                conn.executescript(sql)
            else:
                conn.execute(sql)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 39:
                raise
            time.sleep(0.05)


def connect(path: str = ":memory:", durable: bool = True) -> sqlite3.Connection:
    """durable=True (default) fsyncs every commit (synchronous=FULL). False trades crash-durability for speed."""
    conn = sqlite3.connect(path, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    if path != ":memory:":
        run_when_unlocked(conn, "PRAGMA journal_mode=WAL")
        run_when_unlocked(conn, f"PRAGMA synchronous={'FULL' if durable else 'NORMAL'}")
    run_when_unlocked(conn, SCHEMA, script=True)
    return conn


@contextmanager
def _tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def classify_issue(issue: str) -> str:
    """low = OCR ambiguity warning, medium = missing data, high = math mismatch / bad value."""
    if issue.startswith("ocr_warning:"):
        return "low"
    if "missing" in issue or issue.startswith("no line items"):
        return "medium"
    return "high"


def severity_of(issues: list) -> str:
    if not issues:
        return "low"
    return max((classify_issue(i) for i in issues), key=SEVERITY_RANK.__getitem__)


def _content_hash(invoice: dict, issues: list) -> str:
    blob = json.dumps({"invoice": invoice, "issues": issues}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def enqueue(conn, document_id: str, invoice: dict, validation: dict,
            document_url: Optional[str] = None) -> Optional[int]:
    """Queue an invoice flagged by validate_invoice(). Returns the item id, or None if clean.

    * Clean result: nothing queued; an OPEN item for the same document is auto-resolved
      (in_review items are left alone, a human is working on them).
    * Same document + same content already active: no-op, returns the existing id.
    * Same document + changed content already active: updated in place and notified_at reset.
    """
    if not document_id or not isinstance(document_id, str):
        raise ValueError("document_id must be a non-empty string")
    requires_review = bool(validation.get("requires_review"))
    issues = list(validation.get("issues") or [])
    if requires_review and not issues:
        raise ValueError("requires_review is true but issues is empty")

    now = _now()
    with _tx(conn):
        active = conn.execute(
            "SELECT id, status, content_hash FROM review_queue "
            "WHERE document_id = ? AND status IN ('open','in_review')", (document_id,)
        ).fetchone()

        if not requires_review:
            if active is not None and active["status"] == "open":
                conn.execute(
                    "UPDATE review_queue SET status='resolved', resolved_at=?, updated_at=?, "
                    "resolution_note=? WHERE id=?", (now, now, AUTO_RESOLVE_NOTE, active["id"]))
            return None

        digest = _content_hash(invoice, issues)
        severity = severity_of(issues)
        invoice_json = json.dumps(invoice, sort_keys=True)
        issues_json = json.dumps(issues)

        if active is not None:
            if active["content_hash"] != digest:
                conn.execute(
                    "UPDATE review_queue SET content_hash=?, severity=?, issues_json=?, invoice_json=?, "
                    "document_url=COALESCE(?, document_url), notified_at=NULL, updated_at=? WHERE id=?",
                    (digest, severity, issues_json, invoice_json, document_url, now, active["id"]))
            return active["id"]

        cur = conn.execute(
            "INSERT INTO review_queue (document_id, content_hash, status, severity, issues_json, "
            "invoice_json, document_url, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (document_id, digest, "open", severity, issues_json, invoice_json, document_url, now, now))
        return cur.lastrowid


def _get(conn, item_id: int):
    row = conn.execute("SELECT * FROM review_queue WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise InvalidTransition(f"queue item {item_id} does not exist")
    return row


def claim(conn, item_id: int, assignee: str) -> None:
    """open -> in_review. Atomic: of two concurrent claimers, exactly one wins."""
    if not assignee:
        raise ValueError("assignee is required")
    cur = conn.execute(
        "UPDATE review_queue SET status='in_review', assignee=?, updated_at=? "
        "WHERE id=? AND status='open'", (assignee, _now(), item_id))
    if cur.rowcount == 0:
        row = _get(conn, item_id)
        raise InvalidTransition(f"item {item_id} is {row['status']}, not open")


def release(conn, item_id: int) -> None:
    """in_review -> open, clears assignee and re-arms notification."""
    cur = conn.execute(
        "UPDATE review_queue SET status='open', assignee=NULL, notified_at=NULL, updated_at=? "
        "WHERE id=? AND status='in_review'", (_now(), item_id))
    if cur.rowcount == 0:
        row = _get(conn, item_id)
        raise InvalidTransition(f"item {item_id} is {row['status']}, not in_review")


def _close(conn, item_id: int, status: str, note: str, corrected_invoice: Optional[dict]) -> None:
    if not note or not note.strip():
        raise ValueError("a resolution note is required for the audit trail")
    now = _now()
    corrected = json.dumps(corrected_invoice, sort_keys=True) if corrected_invoice is not None else None
    cur = conn.execute(
        "UPDATE review_queue SET status=?, resolution_note=?, corrected_invoice_json=?, "
        "resolved_at=?, updated_at=? WHERE id=? AND status IN ('open','in_review')",
        (status, note.strip(), corrected, now, now, item_id))
    if cur.rowcount == 0:
        row = _get(conn, item_id)
        raise InvalidTransition(f"item {item_id} is already {row['status']}")


def resolve(conn, item_id: int, note: str, corrected_invoice: Optional[dict] = None) -> None:
    _close(conn, item_id, "resolved", note, corrected_invoice)


def dismiss(conn, item_id: int, note: str) -> None:
    _close(conn, item_id, "dismissed", note, None)


def list_active(conn, limit: int = 50) -> list:
    """Active items, highest severity first, then oldest first."""
    return conn.execute(
        "SELECT * FROM review_queue WHERE status IN ('open','in_review') "
        "ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, created_at, id "
        "LIMIT ?", (limit,)).fetchall()


def pending_notifications(conn, limit: int = 50) -> list:
    return conn.execute(
        "SELECT * FROM review_queue WHERE status='open' AND notified_at IS NULL "
        "ORDER BY id LIMIT ?", (limit,)).fetchall()


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _label(row) -> str:
    """Human label for an item: vendor, invoice number and total; falls back to a short document id."""
    try:
        invoice = json.loads(row["invoice_json"])
        meta, summary = invoice.get("invoice_metadata") or {}, invoice.get("financial_summary") or {}
    except (ValueError, AttributeError):
        meta, summary = {}, {}
    parts = []
    if meta.get("vendor_name"):
        parts.append(str(meta["vendor_name"]))
    if meta.get("invoice_number"):
        parts.append(f"#{meta['invoice_number']}")
    if not parts:  # nothing identifying was read: fall back to the short document id
        doc = row["document_id"]
        parts.append(doc if len(doc) <= 24 else doc[:12])
    total = summary.get("grand_total")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        parts.append(f"{total:,.2f}" + (f" {meta['currency']}" if meta.get("currency") else ""))
    return " ".join(parts)


def format_slack_payload(row) -> dict:
    issues = json.loads(row["issues_json"])
    shown = issues[:MAX_ISSUES_IN_MESSAGE]
    body = "\n".join(f"\u2022 {_esc(i)}" for i in shown)
    if len(issues) > len(shown):
        body += f"\n\u2026and {len(issues) - len(shown)} more"
    body = body[:2900]  # Slack section text limit is 3000
    title = f"[{row['severity'].upper()}] Invoice needs review: {_label(row)}"
    context = f"Queue item #{row['id']}"
    if row["document_url"]:
        safe_url = _esc(row["document_url"]).replace("|", "%7C")
        context += f" | <{safe_url}|Open document>"
    return {
        "text": title[:150],
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": title[:150]}},
            {"type": "section", "text": {"type": "mrkdwn", "text": body}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": context}]},
        ],
    }


def format_email(row) -> dict:
    issues = json.loads(row["issues_json"])
    lines = [f"Invoice {_label(row)} needs review (severity: {row['severity']}).", "", "Issues:"]
    lines += [f"  - {i}" for i in issues[:MAX_ISSUES_IN_MESSAGE]]
    if len(issues) > MAX_ISSUES_IN_MESSAGE:
        lines.append(f"  ...and {len(issues) - MAX_ISSUES_IN_MESSAGE} more")
    if row["document_url"]:
        lines += ["", f"Document: {row['document_url']}"]
    lines += ["", f"Queue item: #{row['id']}"]
    return {"subject": f"[{row['severity'].upper()}] Invoice needs review: {_label(row)}",
            "body": "\n".join(lines)}


def slack_webhook_sender(webhook_url: str, timeout: float = 5.0) -> Callable[[dict], None]:
    """Build a sender for dispatch_pending(). Raises on non-2xx so the row stays un-notified."""
    if not webhook_url.startswith("https://"):
        raise ValueError("webhook URL must use https")

    def send(payload: dict) -> None:
        req = urllib.request.Request(
            webhook_url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https enforced above)
            if not 200 <= resp.status < 300:
                raise QueueError(f"webhook returned HTTP {resp.status}")

    return send


def dispatch_pending(conn, send: Callable[[dict], None], limit: int = 50) -> dict:
    """Send Slack payloads for open, un-notified items. At-least-once delivery."""
    sent, failed = 0, []
    for row in pending_notifications(conn, limit):
        try:
            send(format_slack_payload(row))
        except Exception as exc:  # delivery failure must never lose the queue row
            failed.append({"id": row["id"], "error": str(exc)})
            continue
        now = _now()
        conn.execute("UPDATE review_queue SET notified_at=?, updated_at=? WHERE id=?", (now, now, row["id"]))
        sent += 1
    return {"sent": sent, "failed": failed}
