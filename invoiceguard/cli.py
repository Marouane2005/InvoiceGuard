"""Command line. One function per command; `main` parses and dispatches."""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import backup, billing, cases, doctor, economics, evaluation, exports, onboarding, pipeline, store, tenants, validator
from .config import ConfigError, Settings
from .errors import PipelineError


def _print(obj) -> None:
    print(json.dumps(obj, indent=2))


def _err(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def _control(settings):
    return closing(tenants.open_control(pipeline.control_path(settings)))


def cmd_serve(args, settings) -> int:
    import uvicorn
    from .api import create_app
    logging.basicConfig(level=logging.INFO, format="%(message)s")  # access lines are already JSON
    uvicorn.run(create_app(settings), host=args.host, port=args.port, access_log=False, log_level="info")
    return 0


def cmd_create_tenant(args, settings) -> int:
    with _control(settings) as control:
        try:
            tenant, key = tenants.create_tenant(control, args.name, args.currency, args.monthly_limit)
        except ValueError as exc:
            return _err(str(exc))
    print("Save this API key now. It cannot be shown again.", file=sys.stderr)
    _print({"tenant": tenants.public(tenant), "api_key": key})
    return 0


def cmd_onboard(args, settings) -> int:
    with _control(settings) as control:
        try:
            result = onboarding.onboard(control, settings, args.name, args.currency, args.monthly_limit)
        except ValueError as exc:
            return _err(str(exc))
    print("Save this API key now. It cannot be shown again. Nothing has been sent to anyone.", file=sys.stderr)
    _print(result)
    return 0


def cmd_process(args, settings) -> int:
    with _control(settings) as control:
        tenant = tenants.get_tenant(control, args.tenant)
    if tenant is None:
        return _err(f"no customer with id {args.tenant}")
    try:
        data = Path(args.path).read_bytes()
        result = pipeline.process_document(settings=settings, tenant=tenant, data=data, filename=Path(args.path).name,
                                           extractor=pipeline.build_extractor(settings))
    except OSError as exc:
        return _err(str(exc))
    except PipelineError as exc:
        _print(exc.as_dict())
        return 2
    _print(result)
    return 1 if result["status"] == "needs_review" else 0


SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
STOP_ON = {"quota_exceeded", "extraction_not_configured"}   # no point sending the rest of the folder


def cmd_process_folder(args, settings) -> int:
    if not 1 <= args.concurrency <= 8:
        return _err("--concurrency must be between 1 and 8")
    with _control(settings) as control:
        tenant = tenants.get_tenant(control, args.tenant)
    if tenant is None:
        return _err(f"no customer with id {args.tenant}")
    folder = Path(args.folder)
    if not folder.is_dir():
        return _err(f"{folder} is not a folder")
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED)
    ignored = sorted(p.name for p in folder.iterdir() if p.is_file() and p.suffix.lower() not in SUPPORTED)
    if ignored:
        print(f"note: ignoring {len(ignored)} file(s) that are not PDF or images (for example {ignored[0]})", file=sys.stderr)
    if not files:
        return _err("no PDF or image files found in that folder")
    if not args.yes:
        print(f"This will send up to {len(files)} document(s) to the AI provider and use paid quota "
              "(files already processed are free). Add --yes to run.", file=sys.stderr)
        return 2
    extractor, stop = pipeline.build_extractor(settings), threading.Event()

    def work(path: Path) -> dict:
        if stop.is_set():
            return {"file": path.name, "result": "skipped"}
        try:
            res = pipeline.process_document(settings=settings, tenant=tenant, data=path.read_bytes(), filename=path.name, extractor=extractor)
        except PipelineError as exc:
            if exc.code in STOP_ON:
                stop.set()
            return {"file": path.name, "result": "failed", "error": exc.code}
        except OSError as exc:
            return {"file": path.name, "result": "failed", "error": exc.__class__.__name__}
        return {"file": path.name, "result": "cached" if res["cached"] else res["status"], "issues": len(res["issues"]),
                "document_id": res["document_id"]}

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        rows = list(pool.map(work, files))
    summary = {k: sum(1 for r in rows if r["result"] == k) for k in ("clean", "needs_review", "cached", "failed", "skipped")}
    if args.report:
        with open(args.report, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["file", "result", "issues", "document_id", "error"])
            for r in rows:
                writer.writerow([store._safe_cell(r["file"]), r["result"], r.get("issues", ""), r.get("document_id", ""), r.get("error", "")])
    _print({"folder": str(folder), "files": len(files), **summary, "failures": [r for r in rows if r["result"] == "failed"],
            "report": args.report})
    if summary["skipped"]:
        print("Stopped early: the customer's quota is used up or reading is not configured. Fix that and run the same command again; finished files are free.", file=sys.stderr)
    return 1 if summary["failed"] or summary["skipped"] else 0


def cmd_usage(args, settings) -> int:
    month = args.month or tenants.current_month()
    with _control(settings) as control:
        rows = tenants.usage_all(control, month)
    if not args.csv:
        _print({"usage": rows})
        return 0
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["customer_id", "customer", "month", "documents", "input_tokens", "output_tokens", "monthly_limit"])
    for r in rows:
        writer.writerow([r["id"], store._safe_cell(r["name"]), r["month"], r["documents"], r["input_tokens"],
                         r["output_tokens"], "" if r["monthly_limit"] is None else r["monthly_limit"]])
    print(out.getvalue(), end="")
    return 0


def cmd_economics(args, settings) -> int:
    with _control(settings) as control:
        try:
            rows = economics.report(tenants.usage_all(control, args.month or tenants.current_month()),
                                    args.input_price_per_mtok, args.output_price_per_mtok, args.target_margin)
        except ValueError as exc:
            return _err(str(exc))
    _print({"note": "Provider cost only. Add hosting, support and payment fees before setting prices.", "customers": rows})
    return 0


def cmd_billing(args, settings) -> int:
    try:
        price = billing.parse_amount(args.price_per_document, "--price-per-document")
        base = billing.parse_amount(args.base_fee, "--base-fee")
        currency = tenants.validate_currency(args.currency)
        with _control(settings) as control:
            rows = tenants.usage_all(control, args.month or tenants.current_month())
        statements = billing.draft_statements(rows, price, args.included, base, currency)
    except ValueError as exc:
        return _err(str(exc))
    if not args.csv:
        _print({"note": billing.NOTE, "statements": statements})
        return 0
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["customer_id", "customer", "month", "documents", "included", "billable_documents", "base_fee", "usage_charge", "total", "currency"])
    for r in statements:
        writer.writerow([r["customer_id"], store._safe_cell(r["customer"]), r["month"], r["documents"], r["included"],
                         r["billable_documents"], r["base_fee"], r["usage_charge"], r["total"], r["currency"]])
    print(out.getvalue(), end="")
    print(billing.NOTE, file=sys.stderr)
    return 0


def _with_tenant_db(args, settings, action):
    with _control(settings) as control:
        tenant = tenants.get_tenant(control, args.tenant)
    if tenant is None:
        return _err(f"no customer with id {args.tenant}")
    with closing(pipeline.open_tenant_db(settings, tenant["id"])) as conn:
        try:
            return action(conn, tenant)
        except cases.CaseError as exc:
            return _err(str(exc))


def cmd_show(args, settings) -> int:
    def action(conn, tenant):
        doc = cases.resolve_document(conn, args.document)
        record = store.get_invoice(conn, doc)
        item = conn.execute("SELECT id, status, severity, assignee, resolution_note FROM review_queue WHERE document_id = ? "
                            "ORDER BY id DESC LIMIT 1", (doc,)).fetchone()
        _print({**{k: record[k] for k in ("document_id", "filename", "status", "created_at", "issues", "invoice")},
                "review_item": dict(item) if item else None})
        print("This is customer data. Do not paste it into chats, tickets, or public places.", file=sys.stderr)
        return 0
    return _with_tenant_db(args, settings, action)


def cmd_export_case(args, settings) -> int:
    if not args.permission_confirmed:
        return _err("add --permission-confirmed to confirm the customer agreed that this invoice may be used as a test case")

    def action(conn, tenant):
        doc = cases.resolve_document(conn, args.document)
        result = cases.export_case(conn, pipeline.tenant_dir(settings, tenant["id"]) / "files", doc, Path(args.into))
        _print(result)
        print("Keep this folder out of git (eval_cases is ignored) and out of public places.", file=sys.stderr)
        return 0
    return _with_tenant_db(args, settings, action)


def _read_template(path: str) -> dict:
    try:
        return exports.validate_template(json.loads(Path(path).read_text(encoding="utf-8")))
    except OSError as exc:
        raise exports.TemplateError(f"cannot read {path}: {exc.__class__.__name__}") from None
    except ValueError as exc:
        if isinstance(exc, exports.TemplateError):
            raise
        raise exports.TemplateError(f"{path} is not valid JSON") from None


def cmd_template_check(args) -> int:
    try:
        template = _read_template(args.file)
    except exports.TemplateError as exc:
        return _err(str(exc))
    print(exports.render(exports.SAMPLE_RECORDS, template), end="")
    print("Preview with invented sample data. Nothing was saved.", file=sys.stderr)
    return 0


def cmd_set_export_template(args, settings) -> int:
    try:
        template = None if args.clear else _read_template(args.file)
    except exports.TemplateError as exc:
        return _err(str(exc))
    with _control(settings) as control:
        if tenants.get_tenant(control, args.tenant) is None:
            return _err(f"no customer with id {args.tenant}")
        tenants.update_settings(control, args.tenant, export_template=template)
    _print({"tenant": args.tenant, "export_template_configured": template is not None})
    return 0


def cmd_trace(args) -> int:
    try:
        if args.logfile == "-":
            found = cases.trace(sys.stdin, args.request_id, args.tenant)
        else:
            with open(args.logfile, encoding="utf-8", errors="replace") as fh:
                found = cases.trace(fh, args.request_id, args.tenant)
    except (OSError, cases.CaseError) as exc:
        return _err(str(exc))
    for entry in found:
        print(json.dumps(entry))
    if not found:
        print("No matching log lines.", file=sys.stderr)
        return 1
    return 0


def cmd_purge(args, settings) -> int:
    if args.older_than_days < 1:
        return _err("--older-than-days must be at least 1")
    with _control(settings) as control:
        tenant = tenants.get_tenant(control, args.tenant)
    if tenant is None:
        return _err(f"no customer with id {args.tenant}")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.older_than_days)).isoformat(timespec="seconds")
    with closing(pipeline.open_tenant_db(settings, tenant["id"])) as conn:
        result = store.purge(conn, pipeline.tenant_dir(settings, tenant["id"]) / "files", cutoff, dry_run=not args.confirm)
    _print(result)
    if result["dry_run"]:
        print("Nothing was deleted. Add --confirm to delete these documents.", file=sys.stderr)
    return 0


def cmd_erase_tenant(args, settings) -> int:
    with _control(settings) as control:
        try:
            result = pipeline.erase_tenant(settings, control, args.tenant, confirm=args.confirm)
        except PipelineError as exc:
            return _err(exc.message)
    _print(result)
    if result["dry_run"]:
        print("Nothing was deleted. Add --confirm to permanently erase this customer's invoices and switch the account off.", file=sys.stderr)
    return 0


def cmd_backup(args, settings) -> int:
    try:
        archive = backup.create_backup(settings.data_dir, Path(args.dest))
        removed = backup.prune(Path(args.dest), args.keep) if args.keep else []
        copied = backup.copy_archive(archive, settings.offsite_backup_dir) if settings.offsite_backup_dir else None
    except backup.NothingToBackup as exc:
        print(f"waiting: {exc}", file=sys.stderr)
        return 2
    except backup.BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print({"backup": str(archive), "size_bytes": archive.stat().st_size, "old_backups_removed": removed,
            "copied_to": str(copied) if copied else None})
    return 0


def cmd_restore(args) -> int:
    try:
        _print(backup.restore(Path(args.archive), Path(args.into)))
    except (backup.BackupError, Exception) as exc:  # includes SchemaTooNew
        return _err(str(exc))
    print(f"Restored into {args.into}. Start the service with INVOICEGUARD_DATA_DIR={args.into} after checking it.", file=sys.stderr)
    return 0


def cmd_doctor(args, settings) -> int:
    checks = doctor.run_checks(settings, Path(args.backup_dir) if args.backup_dir else None, online=args.online)
    marks = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    for c in checks:
        print(f"[{marks[c.status]}] {c.name}: {c.detail}")
    code = doctor.exit_code(checks)
    print("\nNot ready: fix the FAIL lines first." if code else "\nNo blocking problems found. Read any WARN lines before going live.")
    return code


def cmd_eval(args, settings) -> int:
    try:
        cases, problems = evaluation.load_cases(args.dir)
    except OSError as exc:
        return _err(str(exc))
    for p in problems:
        print(f"note: {p}", file=sys.stderr)
    if not cases:
        return _err("no labelled invoices found (each file needs a matching <name>.expected.json)")
    if not args.yes:
        print(f"This will send {len(cases)} document(s) to the AI provider and use paid API quota. Add --yes to run.", file=sys.stderr)
        return 2
    result = evaluation.evaluate(cases, pipeline.build_extractor(settings), args.currency)
    if args.out:
        Path(args.out).write_text(evaluation.render_markdown(result))
    _print({k: v for k, v in result.items() if k != "cases"} | {"report": args.out})
    return 1 if args.fail_on_false_clean and result["counts"]["clean_wrong"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invoiceguard", description="InvoiceGuard command line")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="start the web service")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)

    for name, text in (("create-tenant", "create a customer account and print its API key once"),
                       ("onboard", "create a customer and draft the welcome message (nothing is sent)")):
        s = sub.add_parser(name, help=text)
        s.add_argument("name")
        s.add_argument("--currency", help="default currency, e.g. USD")
        s.add_argument("--monthly-limit", type=int, help="invoices per month; leave out for no limit")

    s = sub.add_parser("validate", help="check the math of an extracted invoice JSON file")
    s.add_argument("path")

    s = sub.add_parser("process", help="read one invoice file for a customer")
    s.add_argument("path")
    s.add_argument("--tenant", required=True)

    s = sub.add_parser("process-folder", help="read every PDF/image in a folder for a customer (uses paid quota; safe to run again)")
    s.add_argument("folder")
    s.add_argument("--tenant", required=True)
    s.add_argument("--concurrency", type=int, default=3, help="documents read at the same time (1 to 8)")
    s.add_argument("--report", default=None, help="write a CSV of the result per file")
    s.add_argument("--yes", action="store_true", help="confirm that sending these documents to the provider is OK")

    s = sub.add_parser("usage", help="documents and tokens per customer for a month")
    s.add_argument("--month", default=None, help="YYYY-MM, default this month")
    s.add_argument("--csv", action="store_true", help="print CSV for invoicing your customers")

    s = sub.add_parser("economics", help="provider cost per invoice and the price that reaches a target margin")
    s.add_argument("--month", default=None)
    s.add_argument("--input-price-per-mtok", type=float, required=True, help="dollars per million input tokens (provider price list)")
    s.add_argument("--output-price-per-mtok", type=float, required=True, help="dollars per million output tokens")
    s.add_argument("--target-margin", type=float, default=0.8, help="for example 0.8 for 80 percent (default)")

    s = sub.add_parser("billing", help="draft monthly statements per customer from usage (drafts only; nothing is sent or charged)")
    s.add_argument("--month", default=None, help="YYYY-MM, default this month")
    s.add_argument("--price-per-document", required=True, help="price per billable document, e.g. 0.25")
    s.add_argument("--included", type=int, default=0, help="documents included in the base fee")
    s.add_argument("--base-fee", default="0", help="fixed monthly fee per customer")
    s.add_argument("--currency", default="USD")
    s.add_argument("--csv", action="store_true")

    s = sub.add_parser("show", help="print one stored invoice, its issues and review state (customer data)")
    s.add_argument("--tenant", required=True)
    s.add_argument("--document", required=True, help="document id or its first 8+ characters")

    s = sub.add_parser("export-case", help="turn a reviewer-confirmed invoice into an accuracy test case")
    s.add_argument("--tenant", required=True)
    s.add_argument("--document", required=True)
    s.add_argument("--into", default="eval_cases")
    s.add_argument("--permission-confirmed", action="store_true", help="the customer agreed their invoice may be used as a test case")

    s = sub.add_parser("template-check", help="preview a custom CSV export template with invented sample data")
    s.add_argument("file", help="template JSON file")

    s = sub.add_parser("set-export-template", help="set (or clear) a customer's custom CSV export template")
    s.add_argument("--tenant", required=True)
    group = s.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", help="template JSON file")
    group.add_argument("--clear", action="store_true", help="go back to the default layout")

    s = sub.add_parser("trace", help="find a request in the server log by the X-Request-ID a customer quotes")
    s.add_argument("logfile", help="log file, or - to read standard input")
    s.add_argument("--request-id")
    s.add_argument("--tenant", help="customer id")

    s = sub.add_parser("purge", help="delete old documents (dry run unless --confirm)")
    s.add_argument("--tenant", required=True)
    s.add_argument("--older-than-days", type=int, required=True)
    s.add_argument("--confirm", action="store_true", help="actually delete")

    s = sub.add_parser("erase-tenant", help="erase ALL of one customer's invoices and switch the account off (dry run unless --confirm)")
    s.add_argument("--tenant", required=True)
    s.add_argument("--confirm", action="store_true")

    s = sub.add_parser("backup", help="write one backup file holding every database and upload")
    s.add_argument("--dest", required=True, help="folder for backup files (copy them off this machine too)")
    s.add_argument("--keep", type=int, default=None, help="afterwards delete the oldest backups beyond this many (only our own files)")

    s = sub.add_parser("restore", help="restore a backup into an EMPTY folder (never overwrites)")
    s.add_argument("archive")
    s.add_argument("--into", required=True)

    s = sub.add_parser("doctor", help="check this installation before going live")
    s.add_argument("--backup-dir", default=os.environ.get("INVOICEGUARD_BACKUP_DIR") or None,
                   help="check that a recent backup exists here (default: $INVOICEGUARD_BACKUP_DIR)")
    s.add_argument("--online", action="store_true", help="also check that the AI provider can be reached")

    s = sub.add_parser("eval", help="measure accuracy on your own labelled invoices (uses paid API quota)")
    s.add_argument("--dir", required=True, help="folder of invoices, each with <name>.expected.json")
    s.add_argument("--out", default=None, help="write a markdown report here")
    s.add_argument("--currency", default=None, help="default currency to apply, like a customer setting")
    s.add_argument("--yes", action="store_true", help="confirm that sending these documents to the provider is OK")
    s.add_argument("--fail-on-false-clean", action="store_true", help="exit 1 if any invoice passed the checks but was wrong")
    return parser


HANDLERS = {"serve": cmd_serve, "create-tenant": cmd_create_tenant, "onboard": cmd_onboard, "process": cmd_process,
            "usage": cmd_usage, "economics": cmd_economics, "purge": cmd_purge, "erase-tenant": cmd_erase_tenant,
            "backup": cmd_backup, "doctor": cmd_doctor, "eval": cmd_eval, "billing": cmd_billing, "show": cmd_show,
            "export-case": cmd_export_case, "set-export-template": cmd_set_export_template,
            "process-folder": cmd_process_folder}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "validate":
        return validator.main([args.path])
    if args.cmd == "restore":
        return cmd_restore(args)
    if args.cmd == "trace":
        return cmd_trace(args)
    if args.cmd == "template-check":
        return cmd_template_check(args)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return HANDLERS[args.cmd](args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
