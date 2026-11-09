"""HTTP API and reviewer dashboard."""
from __future__ import annotations

import hmac
import json
import logging
import re
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Callable, Optional

from fastapi import Depends, FastAPI, File, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import __version__, exports, metrics as metrics_module, pipeline, review_queue as rq, store, tenants
from .config import Settings
from .errors import PipelineError

STATIC = Path(__file__).parent / "static"
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DOC_ID = re.compile(r"^[0-9a-f]{64}$")
_STATUSES = {"clean", "needs_review", "reviewed", "dismissed"}
DASHBOARD_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                 "img-src 'self' blob:; frame-src blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
ASSETS = {"dashboard.css": "text/css; charset=utf-8", "dashboard.js": "text/javascript; charset=utf-8"}


class ClaimBody(BaseModel):
    assignee: str = Field(min_length=1, max_length=100)


class NoteBody(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class ResolveBody(NoteBody):
    corrected_invoice: Optional[dict] = None


class SettingsBody(BaseModel):
    default_currency: Optional[str] = None
    slack_webhook_url: Optional[str] = None
    export_template: Optional[dict] = None


class TenantBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    default_currency: Optional[str] = None
    monthly_limit: Optional[int] = Field(default=None, ge=0)


def _month(value: Optional[str]) -> str:
    if value is None:
        return tenants.current_month()
    if not _MONTH.match(value):
        raise PipelineError("invalid_request", detail="month must look like 2026-10")
    return value


def _item(row, detail: bool = False) -> dict:
    invoice = json.loads(row["invoice_json"])
    meta, summary = invoice.get("invoice_metadata") or {}, invoice.get("financial_summary") or {}
    out = {
        "id": row["id"], "document_id": row["document_id"], "status": row["status"], "severity": row["severity"],
        "issues": json.loads(row["issues_json"]), "assignee": row["assignee"], "resolution_note": row["resolution_note"],
        "vendor_name": meta.get("vendor_name"), "invoice_number": meta.get("invoice_number"),
        "grand_total": summary.get("grand_total"), "currency": meta.get("currency"),
        "created_at": row["created_at"], "updated_at": row["updated_at"], "notified_at": row["notified_at"],
    }
    if detail:
        out["invoice"] = invoice
    return out


API_DESCRIPTION = (
    "Reads invoices, checks the numbers with code, and sends anything doubtful to a person for review.\n\n"
    "**Authentication:** send your key as `Authorization: Bearer <key>`. Admin endpoints use the `X-Admin-Token` header.\n\n"
    "**Errors** always look like `{\"error\": {\"code\", \"message\", \"hint\"}}`; the hint says what to do next."
)
_TAGS = [("/v1/invoices", "Invoices", "Upload, search, fetch, and sync processed invoices."),
         ("/v1/review-queue", "Review queue", "Invoices that need a person to confirm them."),
         ("/v1/notifications", "Notifications", "Slack messages. Sent only when you call dispatch."),
         ("/v1/export", "Export", "CSV for your accounting software."),
         ("/v1/usage", "Usage", "Documents and tokens used this month."),
         ("/v1/settings", "Settings", "Currency, Slack webhook, export layout, key rotation."),
         ("/admin", "Admin", "For the company running the service. Needs the admin token.")]
_SUMMARIES = {
    ("POST", "/v1/invoices"): "Upload and read one invoice (201 new, 200 already processed)",
    ("GET", "/v1/invoices"): "Search and list processed invoices",
    ("GET", "/v1/invoices/changes"): "Change feed for syncing into another system",
    ("GET", "/v1/invoices/{document_id}"): "Get one processed invoice",
    ("GET", "/v1/invoices/{document_id}/file"): "Download the original file",
    ("GET", "/v1/review-queue"): "List invoices waiting for review",
    ("POST", "/v1/review-queue/{item_id}/claim"): "Take an item for review",
    ("POST", "/v1/review-queue/{item_id}/resolve"): "Resolve an item, optionally with a corrected invoice",
    ("POST", "/v1/review-queue/{item_id}/dismiss"): "Reject an item",
    ("POST", "/v1/notifications/dispatch"): "Send Slack messages for new review items",
    ("GET", "/v1/export.csv"): "Download checked invoices as CSV",
    ("PATCH", "/v1/settings"): "Change settings",
    ("POST", "/v1/settings/rotate-key"): "Replace your API key",
    ("GET", "/admin/metrics"): "Prometheus metrics",
}


def _document_api(app: FastAPI) -> None:
    from fastapi.openapi.utils import get_openapi
    from fastapi.routing import APIRoute
    for route in app.routes:
        if isinstance(route, APIRoute):
            for prefix, tag, _ in _TAGS:
                if route.path.startswith(prefix):
                    route.tags = [tag]
                    break
            method = sorted(route.methods)[0]
            route.summary = _SUMMARIES.get((method, route.path), route.summary)

    def openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes,
                             tags=[{"name": t, "description": d} for _, t, d in _TAGS])
        schema["components"]["securitySchemes"] = {
            "ApiKey": {"type": "http", "scheme": "bearer", "description": "Your customer API key."},
            "AdminToken": {"type": "apiKey", "in": "header", "name": "X-Admin-Token"}}
        for path, item in schema["paths"].items():
            for op in item.values():
                op["security"] = [{"AdminToken": []}] if path.startswith("/admin") else [{"ApiKey": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = openapi


def create_app(settings: Settings, extractor=None, notifier_factory: Callable = rq.slack_webhook_sender,
               limiter=None) -> FastAPI:
    from .ratelimit import RateLimiter

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    extractor = extractor or pipeline.build_extractor(settings)
    limiter = limiter or RateLimiter(settings.rate_limit_per_minute)
    failures = RateLimiter(settings.auth_failures_per_minute)  # failed logins per client address
    gate = pipeline.ReadGate(settings.max_concurrent_reads)
    metrics = metrics_module.Metrics(__version__, {"invoiceguard_reads_in_flight": lambda: gate.in_flight})
    access_log = logging.getLogger("invoiceguard.access")
    with closing(tenants.open_control(pipeline.control_path(settings))):
        pass  # create tables on startup

    app = FastAPI(title="InvoiceGuard", version=__version__, description=API_DESCRIPTION)

    # ---- errors -----------------------------------------------------------------------------
    @app.exception_handler(PipelineError)
    async def _pipeline_error(request: Request, exc: PipelineError):
        headers = {"Retry-After": "60"} if exc.code == "rate_limited" else None
        metrics.inc("invoiceguard_errors_total", {"code": exc.code})
        return JSONResponse(exc.as_dict(), status_code=exc.status, headers=headers)

    @app.exception_handler(rq.QueueError)
    async def _queue_error(request: Request, exc: rq.QueueError):
        return await _pipeline_error(request, PipelineError("conflict", detail=str(exc)))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'][1:]) or 'body'}: {e['msg']}" for e in exc.errors())
        return await _pipeline_error(request, PipelineError("invalid_request", detail=detail[:300]))

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        started = time.monotonic()
        request_id = uuid.uuid4().hex[:16]
        request.state.tenant_id = None
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.max_upload_bytes + 64 * 1024:
            err = PipelineError("file_too_large", limit_mb=settings.max_upload_bytes // (1024 * 1024))
            response = JSONResponse(err.as_dict(), status_code=err.status)
        else:
            try:
                response = await call_next(request)
            except Exception as exc:  # never show internals; log only the error type and request id
                access_log.error(json.dumps({"request_id": request_id, "path": request.url.path, "error": exc.__class__.__name__}))
                err = PipelineError("internal_error")
                response = JSONResponse(err.as_dict(), status_code=err.status)
        metrics.inc("invoiceguard_http_requests_total", {"method": request.method, "status_class": f"{response.status_code // 100}xx"})
        response.headers["X-Request-ID"] = request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith(("/v1", "/admin")):
            response.headers.setdefault("Cache-Control", "no-store")
        # One JSON line per request. No query string, headers, bodies, keys, or invoice contents.
        access_log.info(json.dumps({"request_id": request_id, "method": request.method, "path": request.url.path,
                                    "status": response.status_code, "ms": round((time.monotonic() - started) * 1000, 1),
                                    "tenant": getattr(request.state, "tenant_id", None)}))
        return response

    # ---- auth and per-request resources -----------------------------------------------------
    def client_ip(request: Request) -> str:
        if settings.trust_proxy:  # only behind your own reverse proxy, which appends the real address last
            last = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
            if last:
                return last
        return request.client.host if request.client else "unknown"

    def not_locked_out(request: Request) -> str:
        ip = client_ip(request)
        if failures.count(ip) >= settings.auth_failures_per_minute:
            raise PipelineError("rate_limited")
        return ip

    def current_tenant(request: Request, authorization: Optional[str] = Header(default=None)) -> dict:
        ip = not_locked_out(request)
        parts = (authorization or "").split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            failures.record(ip)
            raise PipelineError("missing_api_key")
        with closing(tenants.open_control(pipeline.control_path(settings))) as conn:
            tenant = tenants.authenticate(conn, parts[1].strip())
        if tenant is None:
            failures.record(ip)
            raise PipelineError("invalid_api_key")
        if not limiter.allow(tenant["id"]):
            raise PipelineError("rate_limited")
        request.state.tenant_id = tenant["id"]
        return tenant

    @contextmanager
    def tenant_db(tenant: dict):
        with closing(pipeline.open_tenant_db(settings, tenant["id"])) as conn:
            yield conn

    @contextmanager
    def control_db():
        with closing(tenants.open_control(pipeline.control_path(settings))) as conn:
            yield conn

    def admin_guard(request: Request, x_admin_token: Optional[str] = Header(default=None)) -> None:
        if not settings.admin_token:
            raise PipelineError("not_found")
        ip = not_locked_out(request)
        if not x_admin_token or not hmac.compare_digest(x_admin_token.encode(), settings.admin_token.encode()):
            failures.record(ip)
            raise PipelineError("invalid_admin_token")

    def doc_id(value: str) -> str:
        if not _DOC_ID.match(value):
            raise PipelineError("not_found")
        return value

    # ---- public --------------------------------------------------------------------------
    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        """Ready only if the control database opens and the data folder is writable. Never reveals why it is not."""
        try:
            with closing(tenants.open_control(pipeline.control_path(settings))) as conn:
                conn.execute("SELECT 1").fetchone()
            probe = settings.data_dir / ".ready"
            probe.write_text("ok")
            probe.unlink()
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready"}

    @app.get("/", include_in_schema=False)
    def dashboard():
        return Response((STATIC / "dashboard.html").read_bytes(), media_type="text/html; charset=utf-8",
                        headers={"Content-Security-Policy": DASHBOARD_CSP, "Cache-Control": "no-store"})

    @app.get("/assets/{name}", include_in_schema=False)
    def asset(name: str):
        if name not in ASSETS:
            raise PipelineError("not_found")
        return Response((STATIC / name).read_bytes(), media_type=ASSETS[name],
                        headers={"Content-Security-Policy": DASHBOARD_CSP})

    # ---- invoices ------------------------------------------------------------------------
    @app.post("/v1/invoices")
    def upload_invoice(file: UploadFile = File(...), tenant: dict = Depends(current_tenant)):
        data = file.file.read(settings.max_upload_bytes + 1)
        started = time.monotonic()
        result = pipeline.process_document(settings=settings, tenant=tenant, data=data,
                                           filename=file.filename or "upload", extractor=extractor, gate=gate)
        metrics.inc("invoiceguard_invoices_total", {"result": "cached" if result["cached"] else result["status"]})
        metrics.observe("invoiceguard_upload_seconds", time.monotonic() - started)
        return JSONResponse(result, status_code=200 if result["cached"] else 201)

    @app.get("/v1/invoices")
    def list_invoices(status: Optional[str] = None, month: Optional[str] = None, q: Optional[str] = None, limit: int = 100,
                      offset: int = 0, tenant: dict = Depends(current_tenant)):
        if status is not None and status not in _STATUSES:
            raise PipelineError("invalid_request", detail=f"status must be one of {sorted(_STATUSES)}")
        if offset < 0:
            raise PipelineError("invalid_request", detail="offset must be 0 or more")
        limit = max(1, min(limit, 200))
        with tenant_db(tenant) as conn:
            rows, total = store.search_invoices(conn, [status] if status else None, _month(month) if month else None, q, limit, offset)
        return {"invoices": rows, "total": total, "limit": limit, "offset": offset}

    @app.get("/v1/invoices/changes")
    def invoice_changes(cursor: Optional[str] = None, limit: int = 100, include: Optional[str] = None,
                        tenant: dict = Depends(current_tenant)):
        if include not in (None, "all"):
            raise PipelineError("invalid_request", detail="include must be 'all' or left out")
        try:
            with tenant_db(tenant) as conn:
                rows, next_cursor = store.changes(conn, None if include == "all" else store.EXPORTABLE, cursor, max(1, min(limit, 200)))
        except ValueError as exc:
            raise PipelineError("invalid_request", detail=str(exc)) from None
        return {"invoices": rows, "next_cursor": next_cursor}

    @app.get("/v1/invoices/{document_id}")
    def get_invoice(document_id: str, tenant: dict = Depends(current_tenant)):
        with tenant_db(tenant) as conn:
            record = store.get_invoice(conn, doc_id(document_id))
        if record is None:
            raise PipelineError("not_found")
        return record

    @app.get("/v1/invoices/{document_id}/file")
    def get_invoice_file(document_id: str, tenant: dict = Depends(current_tenant)):
        with tenant_db(tenant) as conn:
            record = store.get_invoice(conn, doc_id(document_id))
        if record is None:
            raise PipelineError("not_found")
        files_dir = pipeline.tenant_dir(settings, tenant["id"]) / "files"
        path = store.file_path(files_dir, record["document_id"], record["media_type"])
        if not path.exists():
            raise PipelineError("not_found")
        return FileResponse(path, media_type=record["media_type"],
                            headers={"Content-Disposition": f'inline; filename="{path.name}"'})

    # ---- review queue --------------------------------------------------------------------
    @app.get("/v1/review-queue")
    def review_queue(limit: int = 50, tenant: dict = Depends(current_tenant)):
        with tenant_db(tenant) as conn:
            rows = rq.list_active(conn, max(1, min(limit, 200)))
            return {"items": [_item(r) for r in rows]}

    def _load_item(conn, item_id: int):
        row = conn.execute("SELECT * FROM review_queue WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise PipelineError("not_found")
        return row

    @app.get("/v1/review-queue/{item_id}")
    def review_item(item_id: int, tenant: dict = Depends(current_tenant)):
        with tenant_db(tenant) as conn:
            return _item(_load_item(conn, item_id), detail=True)

    @app.post("/v1/review-queue/{item_id}/claim")
    def claim(item_id: int, body: ClaimBody, tenant: dict = Depends(current_tenant)):
        with tenant_db(tenant) as conn:
            _load_item(conn, item_id)
            rq.claim(conn, item_id, body.assignee.strip())
            return _item(_load_item(conn, item_id))

    @app.post("/v1/review-queue/{item_id}/release")
    def release(item_id: int, tenant: dict = Depends(current_tenant)):
        with tenant_db(tenant) as conn:
            _load_item(conn, item_id)
            rq.release(conn, item_id)
            return _item(_load_item(conn, item_id))

    @app.post("/v1/review-queue/{item_id}/resolve")
    def resolve(item_id: int, body: ResolveBody, tenant: dict = Depends(current_tenant)):
        with tenant_db(tenant) as conn:
            pipeline.resolve_review(conn, tenant, item_id, body.note, body.corrected_invoice)
            return _item(_load_item(conn, item_id), detail=True)

    @app.post("/v1/review-queue/{item_id}/dismiss")
    def dismiss(item_id: int, body: NoteBody, tenant: dict = Depends(current_tenant)):
        with tenant_db(tenant) as conn:
            pipeline.dismiss_review(conn, item_id, body.note)
            return _item(_load_item(conn, item_id))

    @app.post("/v1/notifications/dispatch")
    def dispatch(tenant: dict = Depends(current_tenant)):
        """Sends Slack messages for new review items. Runs only when someone calls it."""
        url = tenant.get("slack_webhook_url")
        if not url:
            raise PipelineError("notifications_not_configured")
        sender = notifier_factory(url)
        with tenant_db(tenant) as conn:
            result = rq.dispatch_pending(conn, sender)
        for failure in result["failed"]:
            failure["error"] = failure["error"].replace(url, "[webhook]")
        return result

    # ---- export, usage, settings ---------------------------------------------------------
    @app.get("/v1/export.csv")
    def export_csv(month: Optional[str] = None, status: Optional[str] = None, template: Optional[str] = None,
                   tenant: dict = Depends(current_tenant)):
        if status not in (None, "all") and status not in _STATUSES:
            raise PipelineError("invalid_request", detail=f"status must be 'all' or one of {sorted(_STATUSES)}")
        if template not in (None, "default", "custom"):
            raise PipelineError("invalid_request", detail="template must be 'default' or 'custom'")
        custom = tenants.export_template(tenant)
        if template == "custom" and custom is None:
            raise PipelineError("invalid_request", detail="no custom export template is set; add one with PATCH /v1/settings")
        statuses = None if status == "all" else ([status] if status else store.EXPORTABLE)
        with tenant_db(tenant) as conn:
            if custom is not None and template != "default":
                records = list(reversed(store.list_invoices(conn, statuses, _month(month) if month else None, 100000)))
                body = exports.render(records, custom)
            else:
                body = store.export_csv(conn, _month(month) if month else None, statuses)
        return Response(body, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="invoices.csv"'})

    @app.get("/v1/usage")
    def usage(month: Optional[str] = None, tenant: dict = Depends(current_tenant)):
        with control_db() as conn:
            summary = tenants.usage_summary(conn, tenant["id"], _month(month))
        summary["monthly_limit"] = tenant["monthly_limit"]
        return summary

    @app.post("/v1/settings/rotate-key")
    def rotate_own_key(tenant: dict = Depends(current_tenant)):
        with control_db() as conn:
            key = tenants.rotate_key(conn, tenant["id"])
        return {"api_key": key, "note": "Save this key now. The old key no longer works."}

    @app.get("/v1/settings")
    def get_settings(tenant: dict = Depends(current_tenant)):
        return tenants.public(tenant)

    @app.patch("/v1/settings")
    def patch_settings(body: SettingsBody, tenant: dict = Depends(current_tenant)):
        changes = {k: getattr(body, k) for k in body.model_fields_set}
        try:
            with control_db() as conn:
                tenants.update_settings(conn, tenant["id"], **changes)
                return tenants.public(tenants.get_tenant(conn, tenant["id"]))
        except ValueError as exc:
            raise PipelineError("invalid_setting", detail=str(exc)) from None

    # ---- admin (for the company running this service) ------------------------------------
    @app.post("/admin/tenants", status_code=201)
    def admin_create_tenant(body: TenantBody, _: None = Depends(admin_guard)):
        try:
            with control_db() as conn:
                tenant, key = tenants.create_tenant(conn, body.name, body.default_currency, body.monthly_limit)
        except ValueError as exc:
            raise PipelineError("invalid_setting", detail=str(exc)) from None
        return {"tenant": tenants.public(tenant), "api_key": key,
                "note": "Save this key now. It cannot be shown again."}

    @app.post("/admin/tenants/{tenant_id}/rotate-key")
    def admin_rotate(tenant_id: str, _: None = Depends(admin_guard)):
        with control_db() as conn:
            key = tenants.rotate_key(conn, tenant_id)
        if key is None:
            raise PipelineError("tenant_not_found")
        return {"api_key": key, "note": "Save this key now. The old key no longer works."}

    @app.post("/admin/tenants/{tenant_id}/deactivate")
    def admin_deactivate(tenant_id: str, _: None = Depends(admin_guard)):
        with control_db() as conn:
            if not tenants.deactivate(conn, tenant_id):
                raise PipelineError("tenant_not_found")
        return {"id": tenant_id, "active": False}

    @app.get("/admin/metrics")
    def admin_metrics(_: None = Depends(admin_guard)):
        return Response(metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/admin/usage")
    def admin_usage(month: Optional[str] = None, _: None = Depends(admin_guard)):
        with control_db() as conn:
            return {"usage": tenants.usage_all(conn, _month(month))}

    _document_api(app)
    return app
