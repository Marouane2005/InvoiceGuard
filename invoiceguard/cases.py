"""Support tools: turn a reviewer-confirmed customer invoice into a regression test, find a request in the logs."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, Optional

from . import store
from .media import MEDIA_EXT

_PREFIX = re.compile(r"^[0-9a-f]{8,64}$")


class CaseError(Exception):
    pass


def resolve_document(conn, text: str) -> str:
    """Accepts a full document id or an unambiguous prefix of at least 8 hex characters."""
    if not _PREFIX.match(text or ""):
        raise CaseError("the document id must be 8 to 64 characters of 0-9 and a-f")
    rows = conn.execute("SELECT document_id FROM invoices WHERE document_id LIKE ? ESCAPE '\\' LIMIT 2", (text + "%",)).fetchall()
    if not rows:
        raise CaseError(f"no invoice starts with {text}")
    if len(rows) > 1:
        raise CaseError(f"{text} matches more than one invoice; use more characters")
    return rows[0]["document_id"]


def export_case(conn, files_dir: Path, document_id: str, into: Path) -> dict:
    record = store.get_invoice(conn, document_id)
    if record is None:
        raise CaseError("no such invoice")
    if record["status"] != "reviewed":
        raise CaseError(f"only invoices a reviewer has confirmed can become test cases (this one is '{record['status']}'): "
                        "an unreviewed result could copy the reader's own mistake into the expected answer")
    source = store.file_path(files_dir, document_id, record["media_type"])
    if not source.exists():
        raise CaseError("the original file is no longer stored (it may have been purged)")
    into.mkdir(parents=True, exist_ok=True)
    stem = f"case-{document_id[:12]}"
    target, expected = into / f"{stem}{MEDIA_EXT[record['media_type']]}", into / f"{stem}.expected.json"
    if target.exists() or expected.exists():
        raise CaseError(f"{stem} already exists in {into}; nothing was overwritten")
    target.write_bytes(source.read_bytes())
    inv = record["invoice"]
    expected.write_text(json.dumps({k: inv[k] for k in ("invoice_metadata", "line_items", "financial_summary")}, indent=2))
    return {"file": str(target), "expected": str(expected)}


def trace(lines: Iterable[str], request_id: Optional[str] = None, tenant: Optional[str] = None) -> list:
    """Matching JSON log lines. Lines that are not JSON are ignored."""
    if not request_id and not tenant:
        raise CaseError("give a request id or a customer id")
    found = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if (request_id is None or entry.get("request_id") == request_id) and (tenant is None or entry.get("tenant") == tenant):
            found.append(entry)
    return found
