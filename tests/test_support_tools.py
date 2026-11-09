import json
from contextlib import closing

import pytest

from invoiceguard import cases, cli, pipeline, store, tenants
from conftest import BAD_RAW, GOOD_RAW, FakeExtractor, pdf


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("INVOICEGUARD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path


def create(capsys, name="Acme"):
    assert cli.main(["create-tenant", name]) == 0
    return json.loads(capsys.readouterr().out)["tenant"]["id"]


def test_usage_csv_neutralises_formula_names(env, capsys):
    create(capsys, "=cmd|calc")
    assert cli.main(["usage", "--csv"]) == 0
    assert "'=cmd" in capsys.readouterr().out


# ---- support: show, export-case, trace ------------------------------------------------------------------
def processed(env, capsys, monkeypatch, raw=BAD_RAW, n=1):
    tid = create(capsys)
    data = pdf(n)
    f = env / "inv.pdf"
    f.write_bytes(data)
    monkeypatch.setattr(pipeline, "build_extractor", lambda s: FakeExtractor(raw))
    cli.main(["process", str(f), "--tenant", tid])
    res = json.loads(capsys.readouterr().out)
    return tid, res, data


def test_show_prints_the_record_with_a_privacy_reminder(env, capsys, monkeypatch):
    tid, res, _ = processed(env, capsys, monkeypatch)
    assert cli.main(["show", "--tenant", tid, "--document", res["document_id"][:10]]) == 0
    out = capsys.readouterr()
    shown = json.loads(out.out)
    assert shown["status"] == "needs_review" and shown["review_item"]["severity"] == "high" and "customer data" in out.err
    for bad in ("zz", "G" * 10, "%" * 8, "0" * 12):
        assert cli.main(["show", "--tenant", tid, "--document", bad]) == 2
    assert cli.main(["show", "--tenant", "t_nope", "--document", res["document_id"][:10]]) == 2
    capsys.readouterr()


def test_export_case_needs_permission_and_a_reviewer_confirmed_invoice(env, capsys, monkeypatch):
    tid, res, data = processed(env, capsys, monkeypatch)
    target = env / "cases"
    args = ["export-case", "--tenant", tid, "--document", res["document_id"], "--into", str(target)]
    assert cli.main(args) == 2 and "--permission-confirmed" in capsys.readouterr().err
    assert cli.main([*args, "--permission-confirmed"]) == 2          # still flagged, not reviewed
    assert "reviewer has confirmed" in capsys.readouterr().err and not target.exists()

    settings = cli.Settings.from_env()
    with closing(pipeline.open_tenant_db(settings, tid)) as conn:
        tenant = {"default_currency": "USD"}
        pipeline.resolve_review(conn, tenant, res["review_item_id"], "checked against the PDF", GOOD_RAW)
    assert cli.main([*args, "--permission-confirmed"]) == 0
    made = json.loads(capsys.readouterr().out)
    assert (target / f"case-{res['document_id'][:12]}.pdf").read_bytes() == data
    assert json.loads(open(made["expected"]).read())["financial_summary"]["grand_total"] == 825.0
    assert cli.main([*args, "--permission-confirmed"]) == 2 and "nothing was overwritten" in capsys.readouterr().err
    # and the exported folder is directly usable by the accuracy tool
    from invoiceguard.evaluation import load_cases
    loaded, problems = load_cases(target)
    assert len(loaded) == 1 and problems == []


def test_trace_finds_requests_by_id_and_customer(env, tmp_path, capsys):
    log = tmp_path / "server.log"
    log.write_text("\n".join(["INFO startup", json.dumps({"request_id": "aaa", "path": "/v1/invoices", "status": 502, "tenant": "t_1"}),
                              json.dumps({"request_id": "bbb", "path": "/v1/usage", "status": 200, "tenant": "t_2"}), "{broken", "[1,2]"]))
    assert cli.main(["trace", str(log), "--request-id", "aaa"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == 502
    assert cli.main(["trace", str(log), "--tenant", "t_2"]) == 0
    assert json.loads(capsys.readouterr().out)["request_id"] == "bbb"
    assert cli.main(["trace", str(log), "--request-id", "zzz"]) == 1
    assert cli.main(["trace", str(log)]) == 2 and cli.main(["trace", str(tmp_path / "missing.log"), "--request-id", "a"]) == 2
    capsys.readouterr()


def test_document_prefix_must_be_unique(tmp_path):
    with closing(store.open_db(str(tmp_path / "t.db"))) as conn:
        for doc in ("abcdef0123", "abcdef0199"):
            store.save_invoice(conn, document_id=doc, filename="a", media_type="application/pdf", status="clean",
                               invoice={"invoice_metadata": {}, "financial_summary": {}, "line_items": []}, issues=[], model="m", input_tokens=1, output_tokens=1)
        assert cases.resolve_document(conn, "abcdef0123") == "abcdef0123"
        with pytest.raises(cases.CaseError, match="more than one"):
            cases.resolve_document(conn, "abcdef01")
        with pytest.raises(cases.CaseError, match="no invoice"):
            cases.resolve_document(conn, "ffffffff")


# ---- export template commands ---------------------------------------------------------------------------
EU = {"columns": [{"header": "Supplier", "field": "vendor_name"}, {"header": "Date", "field": "issue_date", "date_format": "%d/%m/%Y"},
                  {"header": "Account", "value": "4000"}, {"header": "Amount", "field": "grand_total"}], "delimiter": ";", "decimal": ","}


def test_template_check_previews_with_invented_data_only(tmp_path, capsys):
    f = tmp_path / "t.json"
    f.write_text(json.dumps(EU))
    assert cli.main(["template-check", str(f)]) == 0
    out = capsys.readouterr()
    assert out.out.splitlines() == ["Supplier;Date;Account;Amount", "Example Supplies Ltd;01/10/2026;4000;1234,50", "Sample Office Co;15/10/2026;4000;99,99"]
    assert "invented sample data" in out.err


def test_template_check_explains_problems(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"columns": [{"header": "A", "field": "password"}]}))
    assert cli.main(["template-check", str(bad)]) == 2 and "unknown field" in capsys.readouterr().err
    broken = tmp_path / "broken.json"
    broken.write_text("{nope")
    assert cli.main(["template-check", str(broken)]) == 2 and "not valid JSON" in capsys.readouterr().err
    assert cli.main(["template-check", str(tmp_path / "missing.json")]) == 2 and "cannot read" in capsys.readouterr().err


def test_set_and_clear_a_customers_template(env, tmp_path, capsys):
    tid = create(capsys)
    f = tmp_path / "t.json"
    f.write_text(json.dumps(EU))
    assert cli.main(["set-export-template", "--tenant", tid, "--file", str(f)]) == 0
    assert json.loads(capsys.readouterr().out)["export_template_configured"] is True
    with closing(tenants.open_control(pipeline.control_path(cli.Settings.from_env()))) as c:
        assert tenants.export_template(tenants.get_tenant(c, tid))["delimiter"] == ";"
    assert cli.main(["set-export-template", "--tenant", tid, "--clear"]) == 0
    assert json.loads(capsys.readouterr().out)["export_template_configured"] is False
    assert cli.main(["set-export-template", "--tenant", "t_nope", "--file", str(f)]) == 2
    f.write_text("{}")
    assert cli.main(["set-export-template", "--tenant", tid, "--file", str(f)]) == 2   # invalid template changes nothing
    capsys.readouterr()


# ---- bulk processing ------------------------------------------------------------------------------------
import threading


class Numbered:
    """Thread-safe stand-in reader that gives every document a different invoice number."""
    def __init__(self):
        self.lock, self.calls = threading.Lock(), 0

    def extract(self, data, media_type):
        from invoiceguard.extractor import ExtractionResult
        with self.lock:
            self.calls += 1
            n = self.calls
        raw = {**GOOD_RAW, "invoice_metadata": {**GOOD_RAW["invoice_metadata"], "invoice_number": f"N-{n}"}}
        return ExtractionResult(invoice=raw, input_tokens=10, output_tokens=5, model="m")


def folder_of(env, n=5, extra=True):
    d = env / "inbox"
    d.mkdir()
    for i in range(n):
        (d / f"inv{i}.pdf").write_bytes(pdf(100 + i))
    if extra:
        (d / "notes.txt").write_text("not an invoice")
    return d


def test_folder_run_needs_yes_then_reads_everything_and_is_free_to_repeat(env, capsys, monkeypatch):
    tid = create(capsys)
    d = folder_of(env)
    reader = Numbered()
    monkeypatch.setattr(pipeline, "build_extractor", lambda s: reader)
    assert cli.main(["process-folder", str(d), "--tenant", tid]) == 2
    assert "Add --yes" in capsys.readouterr().err and reader.calls == 0

    report = env / "report.csv"
    assert cli.main(["process-folder", str(d), "--tenant", tid, "--yes", "--report", str(report), "--concurrency", "4"]) == 0
    out = capsys.readouterr()
    summary = json.loads(out.out)
    assert summary["files"] == 5 and summary["clean"] == 5 and summary["failed"] == 0 and "ignoring 1 file" in out.err
    assert reader.calls == 5 and len(report.read_text().strip().splitlines()) == 6

    assert cli.main(["process-folder", str(d), "--tenant", tid, "--yes"]) == 0
    assert json.loads(capsys.readouterr().out)["cached"] == 5 and reader.calls == 5      # nothing was sent again


def test_folder_run_stops_when_the_quota_is_used_up(env, capsys, monkeypatch):
    assert cli.main(["create-tenant", "Small", "--monthly-limit", "2"]) == 0
    tid = json.loads(capsys.readouterr().out)["tenant"]["id"]
    d = folder_of(env, 6, extra=False)
    reader = Numbered()
    monkeypatch.setattr(pipeline, "build_extractor", lambda s: reader)
    assert cli.main(["process-folder", str(d), "--tenant", tid, "--yes", "--concurrency", "1"]) == 1
    out = capsys.readouterr()
    summary = json.loads(out.out)
    assert summary["clean"] == 2 and summary["failed"] == 1 and summary["skipped"] == 3 and reader.calls == 2
    assert summary["failures"][0]["error"] == "quota_exceeded" and "Stopped early" in out.err


def test_folder_command_input_checks(env, capsys):
    tid = create(capsys)
    assert cli.main(["process-folder", str(env / "nope"), "--tenant", tid, "--yes"]) == 2
    empty = env / "empty"
    empty.mkdir()
    assert cli.main(["process-folder", str(empty), "--tenant", tid, "--yes"]) == 2 and "no PDF or image" in capsys.readouterr().err
    assert cli.main(["process-folder", str(empty), "--tenant", tid, "--yes", "--concurrency", "99"]) == 2
    assert cli.main(["process-folder", str(empty), "--tenant", "t_nope", "--yes"]) == 2
