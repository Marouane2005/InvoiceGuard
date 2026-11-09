"""The whole job for one uploaded invoice: check file, read it, apply checks, store, route to review."""
from __future__ import annotations

import hashlib
import os
import threading
from contextlib import contextmanager
from contextlib import closing
from pathlib import Path

from . import policy, review_queue as rq, store, tenants
from .config import Settings
from .errors import PipelineError
from .extractor import ExtractionError, ExtractionNotConfigured
from .media import detect_media_type
from .validator import validate_invoice


class ReadGate:
    """Caps how many invoices are being read by the AI provider at once (protects cost and provider limits)."""

    def __init__(self, size: int):
        self._sem = threading.BoundedSemaphore(size)
        self._lock = threading.Lock()
        self.in_flight = 0

    @contextmanager
    def slot(self):
        if not self._sem.acquire(blocking=False):
            raise PipelineError("server_busy")
        with self._lock:
            self.in_flight += 1
        try:
            yield
        finally:
            with self._lock:
                self.in_flight -= 1
            self._sem.release()


def tenant_dir(settings: Settings, tenant_id: str) -> Path:
    return settings.data_dir / "tenants" / tenant_id


def control_path(settings: Settings) -> Path:
    return settings.data_dir / "control.db"


def open_tenant_db(settings: Settings, tenant_id: str):
    tdir = tenant_dir(settings, tenant_id)
    (tdir / "files").mkdir(parents=True, exist_ok=True)
    return store.open_db(str(tdir / "tenant.db"))


def _document_url(settings: Settings, document_id: str):
    if not settings.public_base_url:
        return None
    return f"{settings.public_base_url}/v1/invoices/{document_id}/file"


def _active_item_id(conn, document_id: str):
    row = conn.execute("SELECT id FROM review_queue WHERE document_id = ? AND status IN ('open','in_review')",
                       (document_id,)).fetchone()
    return row["id"] if row else None


def _result(conn, record: dict, cached: bool) -> dict:
    return {
        "document_id": record["document_id"], "status": record["status"], "cached": cached,
        "invoice": record["invoice"], "issues": record["issues"],
        "review_item_id": _active_item_id(conn, record["document_id"]),
        "usage": {"input_tokens": record["input_tokens"], "output_tokens": record["output_tokens"], "model": record["model"]},
    }


def _write_file_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def process_document(*, settings: Settings, tenant: dict, data: bytes, filename: str, extractor, gate=None) -> dict:
    if not data:
        raise PipelineError("empty_file")
    if len(data) > settings.max_upload_bytes:
        raise PipelineError("file_too_large", limit_mb=settings.max_upload_bytes // (1024 * 1024))
    media_type = detect_media_type(data)
    if media_type is None:
        raise PipelineError("unsupported_file")
    document_id = hashlib.sha256(data).hexdigest()

    with closing(open_tenant_db(settings, tenant["id"])) as tconn:
        existing = store.get_invoice(tconn, document_id)
        if existing:  # same file uploaded before: no second model call, no second charge
            return _result(tconn, existing, cached=True)

        with closing(tenants.open_control(control_path(settings))) as cconn:
            month = tenants.current_month()
            limit = tenant.get("monthly_limit")
            if limit is not None and tenants.documents_in_month(cconn, tenant["id"], month) >= limit:
                raise PipelineError("quota_exceeded")
            try:
                if gate is None:
                    extracted = extractor.extract(data, media_type)
                else:
                    with gate.slot():
                        extracted = extractor.extract(data, media_type)
            except ExtractionNotConfigured:
                raise PipelineError("extraction_not_configured") from None
            except ExtractionError as exc:
                raise PipelineError("extraction_failed", detail=str(exc)) from None

            invoice = policy.normalize_invoice(extracted.invoice)
            invoice, policy_issues = policy.apply_policy(invoice, tenant.get("default_currency"))
            issues = validate_invoice(invoice)["issues"] + policy_issues

            meta = invoice["invoice_metadata"]
            vkey, number = store.vendor_key(meta.get("vendor_name")), meta.get("invoice_number")
            if vkey and number:
                dup = store.find_duplicate(tconn, vkey, number, document_id)
                if dup:
                    issues.append(f"possible duplicate of document {dup[:12]} (same vendor and invoice number)")

            requires_review = bool(issues)
            files_dir = tenant_dir(settings, tenant["id"]) / "files"
            _write_file_atomic(store.file_path(files_dir, document_id, media_type), data)
            store.save_invoice(tconn, document_id=document_id, filename=filename or "upload", media_type=media_type,
                               status="needs_review" if requires_review else "clean", invoice=invoice, issues=issues,
                               model=extracted.model, input_tokens=extracted.input_tokens,
                               output_tokens=extracted.output_tokens)
            if requires_review:
                rq.enqueue(tconn, document_id, invoice, {"requires_review": True, "issues": issues},
                           document_url=_document_url(settings, document_id))
            tenants.record_usage(cconn, tenant["id"], document_id, month, extracted.input_tokens, extracted.output_tokens)
        return _result(tconn, store.get_invoice(tconn, document_id), cached=False)


def _require_item(conn, item_id: int):
    row = conn.execute("SELECT * FROM review_queue WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise PipelineError("not_found")
    return row


def resolve_review(conn, tenant: dict, item_id: int, note: str, corrected_invoice=None) -> None:
    """Close a review item. A corrected invoice must pass every check before it is accepted."""
    row = _require_item(conn, item_id)
    corrected = None
    if corrected_invoice is not None:
        corrected = policy.normalize_invoice(corrected_invoice)
        corrected["ocr_warnings"] = []
        corrected, policy_issues = policy.apply_policy(corrected, tenant.get("default_currency"))
        problems = validate_invoice(corrected)["issues"] + policy_issues
        if problems:
            raise PipelineError("invalid_correction", detail="; ".join(problems[:3]))
    try:
        rq.resolve(conn, item_id, note, corrected_invoice=corrected)
    except ValueError as exc:
        raise PipelineError("invalid_request", detail=str(exc)) from None
    store.mark_reviewed(conn, row["document_id"], "reviewed", corrected)


def dismiss_review(conn, item_id: int, note: str) -> None:
    row = _require_item(conn, item_id)
    try:
        rq.dismiss(conn, item_id, note)
    except ValueError as exc:
        raise PipelineError("invalid_request", detail=str(exc)) from None
    store.mark_reviewed(conn, row["document_id"], "dismissed")


def build_extractor(settings: Settings):
    if settings.reader == "local":
        from .local_reader import LocalExtractor
        return LocalExtractor()
    from .extractor import ClaudeExtractor, UnconfiguredExtractor
    if not settings.anthropic_api_key:
        return UnconfiguredExtractor()
    return ClaudeExtractor(settings.anthropic_api_key, settings.model, settings.anthropic_base_url)


def erase_tenant(settings: Settings, control_conn, tenant_id: str, confirm: bool = False) -> dict:
    """Remove ALL of one customer's invoices, files and review items, and switch the account off.

    Dry run unless confirm=True. Usage counts (no invoice content) are kept so billing records stay intact.
    """
    import shutil
    tenant = tenants.get_tenant(control_conn, tenant_id)
    if tenant is None:
        raise PipelineError("tenant_not_found")
    tdir = tenant_dir(settings, tenant_id)
    documents = files = 0
    if (tdir / "tenant.db").exists():
        with closing(store.open_db(str(tdir / "tenant.db"))) as conn:
            documents = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
    if (tdir / "files").is_dir():
        files = sum(1 for _ in (tdir / "files").iterdir())
    if not confirm:
        return {"tenant": tenant_id, "documents": documents, "files": files, "dry_run": True}
    shutil.rmtree(tdir, ignore_errors=False) if tdir.exists() else None
    tenants.deactivate(control_conn, tenant_id)
    return {"tenant": tenant_id, "documents": documents, "files": files, "dry_run": False, "account_active": False}
