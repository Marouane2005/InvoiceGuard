import json

import pytest

from invoiceguard import cli, pipeline
from conftest import BAD_RAW, GOOD_RAW, FakeExtractor, pdf


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("INVOICEGUARD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path


def make_tenant(capsys, *extra):
    assert cli.main(["create-tenant", "Acme", *extra]) == 0
    out = capsys.readouterr()
    return json.loads(out.out), out.err


def test_create_tenant_prints_key_once_with_a_warning(env, capsys):
    data, err = make_tenant(capsys, "--currency", "usd", "--monthly-limit", "50")
    assert data["api_key"].startswith("ig_") and data["tenant"]["default_currency"] == "USD" and "cannot be shown again" in err


def test_create_tenant_rejects_bad_input(env, capsys):
    assert cli.main(["create-tenant", "Acme", "--currency", "dollars"]) == 2
    assert "3-letter" in capsys.readouterr().err


def test_bad_configuration_is_reported(env, monkeypatch, capsys):
    monkeypatch.setenv("INVOICEGUARD_MAX_UPLOAD_MB", "lots")
    assert cli.main(["usage"]) == 2 and "configuration error" in capsys.readouterr().err


def test_validate_command_exit_codes(env, tmp_path, capsys):
    good, bad = tmp_path / "g.json", tmp_path / "b.json"
    good.write_text(json.dumps(GOOD_RAW))
    bad.write_text(json.dumps(BAD_RAW))
    assert cli.main(["validate", str(good)]) == 0
    assert cli.main(["validate", str(bad)]) == 1


def test_process_without_api_key_explains_what_to_do(env, capsys):
    data, _ = make_tenant(capsys)
    f = env / "inv.pdf"
    f.write_bytes(pdf(1))
    assert cli.main(["process", str(f), "--tenant", data["tenant"]["id"]]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "extraction_not_configured"


def test_process_unknown_tenant_and_missing_file(env, capsys):
    data, _ = make_tenant(capsys)
    assert cli.main(["process", "x.pdf", "--tenant", "t_nope"]) == 2
    assert cli.main(["process", str(env / "missing.pdf"), "--tenant", data["tenant"]["id"]]) == 2


def test_process_and_usage_with_a_fake_reader(env, monkeypatch, capsys):
    data, _ = make_tenant(capsys)
    tid = data["tenant"]["id"]
    f = env / "inv.pdf"
    f.write_bytes(pdf(1))
    monkeypatch.setattr(pipeline, "build_extractor", lambda s: FakeExtractor(GOOD_RAW))
    assert cli.main(["process", str(f), "--tenant", tid]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "clean"
    g = env / "bad.pdf"
    g.write_bytes(pdf(2))
    monkeypatch.setattr(pipeline, "build_extractor", lambda s: FakeExtractor(BAD_RAW))
    assert cli.main(["process", str(g), "--tenant", tid]) == 1
    capsys.readouterr()
    assert cli.main(["usage"]) == 0
    rows = json.loads(capsys.readouterr().out)["usage"]
    assert rows[0]["documents"] == 2


def test_purge_is_a_dry_run_unless_confirmed(env, monkeypatch, capsys):
    from contextlib import closing
    from invoiceguard import store
    data, _ = make_tenant(capsys)
    tid = data["tenant"]["id"]
    f = env / "inv.pdf"
    f.write_bytes(pdf(1))
    monkeypatch.setattr(pipeline, "build_extractor", lambda s: FakeExtractor(GOOD_RAW))
    cli.main(["process", str(f), "--tenant", tid])
    capsys.readouterr()
    settings = cli.Settings.from_env()
    with closing(pipeline.open_tenant_db(settings, tid)) as conn:
        conn.execute("UPDATE invoices SET created_at = '2000-01-01T00:00:00+00:00'")

    assert cli.main(["purge", "--tenant", tid, "--older-than-days", "30"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out) == {"documents": 1, "files": 0, "dry_run": True} and "Nothing was deleted" in out.err
    assert cli.main(["purge", "--tenant", tid, "--older-than-days", "30", "--confirm"]) == 0
    assert json.loads(capsys.readouterr().out) == {"documents": 1, "files": 1, "dry_run": False}
    assert cli.main(["purge", "--tenant", tid, "--older-than-days", "0"]) == 2


# ---- operations commands -------------------------------------------------------------------------------
def test_onboard_drafts_but_never_sends(env, monkeypatch, capsys):
    assert cli.main(["onboard", "Acme"]) == 2 and "INVOICEGUARD_PUBLIC_URL" in capsys.readouterr().err
    monkeypatch.setenv("INVOICEGUARD_PUBLIC_URL", "https://invoices.example.com")
    assert cli.main(["onboard", "Acme", "--currency", "eur", "--monthly-limit", "100"]) == 0
    out = capsys.readouterr()
    data = json.loads(out.out)
    assert "https://invoices.example.com/" in data["welcome_message"] and data["api_key"] not in data["welcome_message"]
    assert "Nothing has been sent" in out.err


def test_usage_csv_for_invoicing_blocks_formula_injection(env, capsys):
    cli.main(["create-tenant", "=cmd|' /C calc'!A0"])
    capsys.readouterr()
    assert cli.main(["usage", "--csv"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "customer_id,customer,month,documents,input_tokens,output_tokens,monthly_limit"
    assert "'=cmd" in lines[1]


def test_backup_and_restore_commands(env, capsys):
    data, _ = make_tenant(capsys)
    assert cli.main(["backup", "--dest", str(env / "bk"), "--keep", "3"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["backup"].endswith(".tar.gz") and info["old_backups_removed"] == []
    assert cli.main(["restore", info["backup"], "--into", str(env / "restored")]) == 0
    assert json.loads(capsys.readouterr().out)["databases"] == 1   # the customer has not uploaded yet, so only the control database exists
    assert cli.main(["restore", info["backup"], "--into", str(env / "restored")]) == 2   # now not empty
    assert "not empty" in capsys.readouterr().err


def test_backup_with_nothing_to_back_up(env, capsys):
    assert cli.main(["backup", "--dest", str(env / "bk")]) == 2 and "nothing to back up" in capsys.readouterr().err


def test_backup_copies_only_when_a_second_folder_is_configured(env, capsys, monkeypatch):
    make_tenant(capsys)
    capsys.readouterr()
    monkeypatch.setenv("INVOICEGUARD_OFFSITE_BACKUP_DIR", str(env / "other"))
    assert cli.main(["backup", "--dest", str(env / "bk")]) == 0
    copied = json.loads(capsys.readouterr().out)["copied_to"]
    assert copied and open(copied, "rb").read(2) == b"\x1f\x8b"
    monkeypatch.delenv("INVOICEGUARD_OFFSITE_BACKUP_DIR", raising=False)
    assert cli.main(["backup", "--dest", str(env / "bk2")]) == 0
    assert json.loads(capsys.readouterr().out)["copied_to"] is None


def test_doctor_exit_codes_and_no_secret_output(env, monkeypatch, capsys):
    assert cli.main(["doctor"]) == 1
    assert "[FAIL] provider key" in capsys.readouterr().out
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-never-print-this")
    monkeypatch.setenv("INVOICEGUARD_ADMIN_TOKEN", "a-long-random-admin-token-value")
    monkeypatch.setenv("INVOICEGUARD_PUBLIC_URL", "https://invoices.example.com")
    monkeypatch.setenv("INVOICEGUARD_TRUST_PROXY", "1")
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out and "[WARN] backups" in out and "never-print-this" not in out


class Boom:
    def extract(self, *a):
        raise AssertionError("the provider must not be called without --yes")


def labelled_dir(env, reader_output=None):
    d = env / "cases"
    d.mkdir()
    (d / "a.pdf").write_bytes(pdf(1))
    (d / "a.expected.json").write_text(json.dumps({"invoice_metadata": {"vendor_name": "Globex Corp"}, "financial_summary": {"grand_total": 825.0}}))
    return d


def test_eval_refuses_to_spend_quota_without_yes(env, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "build_extractor", lambda s: Boom())
    assert cli.main(["eval", "--dir", str(labelled_dir(env))]) == 2
    assert "Add --yes" in capsys.readouterr().err


def test_eval_reports_and_can_gate_on_false_clean(env, monkeypatch, capsys):
    d = labelled_dir(env)
    monkeypatch.setattr(pipeline, "build_extractor", lambda s: FakeExtractor(GOOD_RAW))
    out_file = env / "accuracy.md"
    assert cli.main(["eval", "--dir", str(d), "--yes", "--out", str(out_file), "--fail-on-false-clean"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["clean_correct"] == 1 and "Accuracy evaluation" in out_file.read_text()
    wrong = {**GOOD_RAW, "invoice_metadata": {**GOOD_RAW["invoice_metadata"], "vendor_name": "Someone Else"}}
    monkeypatch.setattr(pipeline, "build_extractor", lambda s: FakeExtractor(wrong))
    assert cli.main(["eval", "--dir", str(d), "--yes", "--fail-on-false-clean"]) == 1
    assert cli.main(["eval", "--dir", str(d), "--yes"]) == 0   # without the gate it only reports


def test_eval_with_no_labelled_files(env, capsys):
    empty = env / "empty"
    empty.mkdir()
    assert cli.main(["eval", "--dir", str(empty), "--yes"]) == 2 and "no labelled invoices" in capsys.readouterr().err


def test_erase_tenant_is_a_dry_run_unless_confirmed(env, monkeypatch, capsys):
    data, _ = make_tenant(capsys)
    tid = data["tenant"]["id"]
    f = env / "inv.pdf"
    f.write_bytes(pdf(1))
    monkeypatch.setattr(pipeline, "build_extractor", lambda s: FakeExtractor(GOOD_RAW))
    cli.main(["process", str(f), "--tenant", tid])
    capsys.readouterr()
    assert cli.main(["erase-tenant", "--tenant", tid]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["dry_run"] is True and "Nothing was deleted" in out.err
    assert cli.main(["erase-tenant", "--tenant", tid, "--confirm"]) == 0
    assert json.loads(capsys.readouterr().out)["account_active"] is False
    assert cli.main(["erase-tenant", "--tenant", "t_nope", "--confirm"]) == 2


def test_doctor_backup_dir_defaults_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("INVOICEGUARD_BACKUP_DIR", str(tmp_path))
    args = cli.build_parser().parse_args(["doctor"])
    assert args.backup_dir == str(tmp_path)
    other = tmp_path / "other"
    flagged = cli.build_parser().parse_args(["doctor", "--backup-dir", str(other)])
    assert flagged.backup_dir == str(other)
