"""Per-customer storage of processed invoices, duplicate detection, CSV export and purge."""
from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from . import migrate, review_queue as rq
from .media import MEDIA_EXT

STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    document_id   TEXT PRIMARY KEY,
    filename      TEXT NOT NULL,
    media_type    TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('clean','needs_review','reviewed','dismissed')),
    invoice_json  TEXT NOT NULL,
    issues_json   TEXT NOT NULL,
    vendor_key    TEXT,
    invoice_number TEXT,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invoices_vendor_number ON invoices(vendor_key, invoice_number);
CREATE INDEX IF NOT EXISTS idx_invoices_created ON invoices(created_at);
"""

EXPORT_COLUMNS = ["document_id", "filename", "status", "invoice_number", "vendor_name", "issue_date", "due_date",
                  "currency", "subtotal", "tax_amount", "grand_total", "created_at"]
EXPORTABLE = ("clean", "reviewed")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


SCHEMA_VERSION = 1
MIGRATIONS: dict = {}  # {2: ["ALTER TABLE ..."], ...} add here when the schema changes; never edit old entries


def open_db(path: str):
    conn = rq.connect(path)
    rq.run_when_unlocked(conn, STORE_SCHEMA, script=True)
    migrate.ensure_version(conn, SCHEMA_VERSION, MIGRATIONS, "customer database")
    return conn


def vendor_key(name) -> Optional[str]:
    if not isinstance(name, str):
        return None
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    return key or None


def _row(row) -> dict:
    return {
        "document_id": row["document_id"], "filename": row["filename"], "media_type": row["media_type"],
        "status": row["status"], "invoice": json.loads(row["invoice_json"]), "issues": json.loads(row["issues_json"]),
        "model": row["model"], "input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def get_invoice(conn, document_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM invoices WHERE document_id = ?", (document_id,)).fetchone()
    return _row(row) if row else None


def save_invoice(conn, *, document_id: str, filename: str, media_type: str, status: str, invoice: dict,
                 issues: list, model: str, input_tokens: int, output_tokens: int) -> None:
    meta = invoice.get("invoice_metadata") or {}
    now = now_iso()
    conn.execute(
        "INSERT INTO invoices (document_id, filename, media_type, status, invoice_json, issues_json, vendor_key, "
        "invoice_number, model, input_tokens, output_tokens, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (document_id, filename[:200], media_type, status, json.dumps(invoice, sort_keys=True), json.dumps(issues),
         vendor_key(meta.get("vendor_name")), meta.get("invoice_number"), model, input_tokens, output_tokens, now, now))


def find_duplicate(conn, vkey: str, invoice_number: str, exclude_document_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT document_id FROM invoices WHERE vendor_key = ? AND invoice_number = ? AND document_id != ? "
        "AND status != 'dismissed' ORDER BY created_at LIMIT 1", (vkey, invoice_number, exclude_document_id)).fetchone()
    return row["document_id"] if row else None


def mark_reviewed(conn, document_id: str, status: str, invoice: Optional[dict] = None) -> None:
    now = now_iso()
    if invoice is not None:
        meta = invoice.get("invoice_metadata") or {}
        # The correction already passed every check. Drop the old issue text so it cannot
        # describe numbers that are no longer stored.
        conn.execute("UPDATE invoices SET status=?, invoice_json=?, issues_json=?, vendor_key=?, invoice_number=?, updated_at=? "
                     "WHERE document_id=?", (status, json.dumps(invoice, sort_keys=True), json.dumps([]),
                                             vendor_key(meta.get("vendor_name")), meta.get("invoice_number"), now, document_id))
    else:
        conn.execute("UPDATE invoices SET status=?, updated_at=? WHERE document_id=?", (status, now, document_id))


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search_invoices(conn, statuses: Optional[Iterable[str]] = None, month: Optional[str] = None, q: Optional[str] = None,
                    limit: int = 50, offset: int = 0) -> tuple:
    """Newest first. Returns (records, total matching). q matches vendor, invoice number or file name, case-insensitively."""
    where, args = "1=1", []
    if statuses:
        statuses = list(statuses)
        where += f" AND status IN ({','.join('?' * len(statuses))})"
        args += statuses
    if month:
        where += " AND created_at LIKE ?"
        args.append(month + "%")
    if q and q.strip():
        needle = q.strip().lower()[:100]
        clauses = ["lower(filename) LIKE ? ESCAPE '\\'", "lower(COALESCE(invoice_number, '')) LIKE ? ESCAPE '\\'"]
        args += [_like(needle), _like(needle)]
        key = vendor_key(needle)
        if key:
            clauses.append("vendor_key LIKE ? ESCAPE '\\'")
            args.append(_like(key))
        where += " AND (" + " OR ".join(clauses) + ")"
    total = conn.execute(f"SELECT COUNT(*) FROM invoices WHERE {where}", args).fetchone()[0]
    rows = conn.execute(f"SELECT * FROM invoices WHERE {where} ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                        [*args, limit, offset]).fetchall()
    return [_row(r) for r in rows], total


def list_invoices(conn, statuses: Optional[Iterable[str]] = None, month: Optional[str] = None, limit: int = 100) -> list:
    return search_invoices(conn, statuses, month, None, limit, 0)[0]


_CURSOR = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?\+00:00\|\d+$")


def _encode_cursor(stamp: str, rowid: int) -> str:
    """Opaque and URL-safe: a client can paste it straight into a query string."""
    return base64.urlsafe_b64encode(f"{stamp}|{rowid}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple:
    try:
        text = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        text = ""
    if not _CURSOR.match(text):
        raise ValueError("cursor is not valid; use the next_cursor from the previous page")
    stamp, rowid = text.rsplit("|", 1)
    return stamp, int(rowid)


def changes(conn, statuses: Optional[Iterable[str]], cursor: Optional[str], limit: int) -> tuple:
    """Invoices in the order they were created or changed, for syncing into another system.

    The cursor is (time of change, row number), so invoices that changed in the same instant are neither skipped nor repeated.
    A record that changes again (for example after review) appears again: consumers should upsert by document_id.
    """
    where, args = "1=1", []
    if statuses:
        statuses = list(statuses)
        where += f" AND status IN ({','.join('?' * len(statuses))})"
        args += statuses
    if cursor:
        stamp, rowid = _decode_cursor(cursor)
        where += " AND (updated_at > ? OR (updated_at = ? AND rowid > ?))"
        args += [stamp, stamp, int(rowid)]
    rows = conn.execute(f"SELECT rowid AS _rid, * FROM invoices WHERE {where} ORDER BY updated_at ASC, rowid ASC LIMIT ?",
                        [*args, limit]).fetchall()
    next_cursor = _encode_cursor(rows[-1]["updated_at"], rows[-1]["_rid"]) if rows else cursor
    return [_row(r) for r in rows], next_cursor


def _safe_cell(value):
    """Stop spreadsheet formula injection: text starting with = + - @ is prefixed with a quote."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def export_csv(conn, month: Optional[str] = None, statuses: Optional[Iterable[str]] = EXPORTABLE) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(EXPORT_COLUMNS)
    for rec in reversed(list_invoices(conn, statuses, month, limit=100000)):
        meta, summ = rec["invoice"]["invoice_metadata"], rec["invoice"]["financial_summary"]
        writer.writerow([_safe_cell(rec["document_id"]), _safe_cell(rec["filename"]), rec["status"],
                         _safe_cell(meta.get("invoice_number")), _safe_cell(meta.get("vendor_name")),
                         meta.get("issue_date"), meta.get("due_date"), meta.get("currency"),
                         summ.get("subtotal"), summ.get("tax_amount"), summ.get("grand_total"), rec["created_at"]])
    return out.getvalue()


def file_path(files_dir: Path, document_id: str, media_type: str) -> Path:
    return files_dir / f"{document_id}{MEDIA_EXT[media_type]}"


def purge(conn, files_dir: Path, cutoff_iso: str, dry_run: bool = True) -> dict:
    """Delete documents created before cutoff that are not waiting for review.

    Dry run by default: nothing is deleted unless dry_run=False is passed explicitly.
    """
    rows = conn.execute(
        "SELECT i.document_id, i.media_type FROM invoices i WHERE i.created_at < ? AND NOT EXISTS "
        "(SELECT 1 FROM review_queue q WHERE q.document_id = i.document_id AND q.status IN ('open','in_review'))",
        (cutoff_iso,)).fetchall()
    if dry_run:
        return {"documents": len(rows), "files": 0, "dry_run": True}
    files_removed = 0
    with rq._tx(conn):
        for r in rows:
            conn.execute("DELETE FROM review_queue WHERE document_id = ?", (r["document_id"],))
            conn.execute("DELETE FROM invoices WHERE document_id = ?", (r["document_id"],))
    for r in rows:
        path = file_path(files_dir, r["document_id"], r["media_type"])
        if path.exists():
            os.remove(path)
            files_removed += 1
    return {"documents": len(rows), "files": files_removed, "dry_run": False}
