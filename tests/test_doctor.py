import dataclasses
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import pytest

from invoiceguard import backup, doctor, pipeline, tenants
from invoiceguard.config import Settings


@pytest.fixture
def good(tmp_path):
    return Settings(data_dir=tmp_path / "data", anthropic_api_key="sk-ant-do-not-print", admin_token="a-long-random-admin-token-value",
                    public_base_url="https://invoices.example.com", trust_proxy=True)


def by_name(checks):
    return {c.name: c for c in checks}


def test_a_properly_configured_install_has_no_failures(good, tmp_path):
    good.data_dir.mkdir(parents=True)
    with closing(tenants.open_control(pipeline.control_path(good))):
        pass
    backup.create_backup(good.data_dir, tmp_path / "bk")
    configured = dataclasses.replace(good, monitor_url="https://monitor.example/readyz")
    checks = doctor.run_checks(configured, tmp_path / "bk", fetch=lambda url: None)
    assert doctor.exit_code(checks) == 0 and {c.status for c in checks} == {"ok"}


def test_secrets_are_never_printed(good):
    text = repr(doctor.run_checks(good))
    assert "sk-ant-do-not-print" not in text and "a-long-random-admin-token-value" not in text


@pytest.mark.parametrize("changes,name,status", [
    ({"anthropic_api_key": None}, "provider key", "fail"),
    ({"admin_token": None}, "admin token", "warn"),
    ({"admin_token": "short-but-16-chars"}, "admin token", "warn"),
    ({"public_base_url": None}, "public address", "warn"),
    ({"public_base_url": "http://invoices.example.com"}, "public address", "fail"),
    ({"trust_proxy": False}, "reverse proxy", "warn"),
])
def test_each_setting_problem_is_reported(good, changes, name, status):
    checks = by_name(doctor.run_checks(dataclasses.replace(good, **changes)))
    assert checks[name].status == status
    assert doctor.exit_code(list(checks.values())) == (1 if status == "fail" else 0)


def test_backup_freshness(good, tmp_path):
    good.data_dir.mkdir(parents=True)
    assert by_name(doctor.run_checks(good))["backups"].status == "warn"
    assert by_name(doctor.run_checks(good, tmp_path / "none"))["backups"].status == "fail"
    with closing(tenants.open_control(pipeline.control_path(good))):
        pass
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    backup.create_backup(good.data_dir, tmp_path / "stale", now=old)
    assert by_name(doctor.run_checks(good, tmp_path / "stale"))["backups"].status == "warn"


def test_database_from_a_newer_version_blocks_launch(good):
    good.data_dir.mkdir(parents=True)
    with closing(tenants.open_control(pipeline.control_path(good))):
        pass
    with closing(sqlite3.connect(pipeline.control_path(good))) as raw:
        raw.execute("PRAGMA user_version = 99")
    check = by_name(doctor.run_checks(good))["control database"]
    assert check.status == "fail" and "Upgrade" in check.detail


def test_unwritable_data_folder_fails(good, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    checks = by_name(doctor.run_checks(dataclasses.replace(good, data_dir=blocker / "sub")))
    assert checks["data folder"].status == "fail"


def test_localhost_http_is_a_warning_not_a_launch_failure(good):
    checks = by_name(doctor.run_checks(dataclasses.replace(good, public_base_url="http://127.0.0.1:8741")))
    assert checks["public address"].status == "warn"
    assert doctor.exit_code(list(checks.values())) == 0
    again = by_name(doctor.run_checks(dataclasses.replace(good, public_base_url="http://localhost:8000")))
    assert again["public address"].status == "warn"


def test_local_reader_does_not_require_a_cloud_key(good):
    checks = by_name(doctor.run_checks(dataclasses.replace(good, reader="local", anthropic_api_key=None)))
    assert checks["provider key"].status == "warn"
    assert "not been measured" in checks["provider key"].detail
    assert "sk-ant" not in checks["provider key"].detail


def test_monitoring_is_unconfigured_until_a_url_answers(good):
    missing = by_name(doctor.run_checks(good))["monitoring"]
    assert missing.status == "warn" and "not configured" in missing.detail and "does not send an alert" in missing.detail
    refused = by_name(doctor.run_checks(dataclasses.replace(good, monitor_url="https://monitor.example/readyz"),
                                        fetch=lambda url: (_ for _ in ()).throw(ConnectionRefusedError())))
    assert refused["monitoring"].status == "fail"
    ok = by_name(doctor.run_checks(dataclasses.replace(good, monitor_url="https://monitor.example/readyz"),
                                   fetch=lambda url: None))
    assert ok["monitoring"].status == "ok"


def test_provider_reachability_is_only_checked_on_request(good):
    calls = []
    assert "provider reachable" not in by_name(doctor.run_checks(good, connect=calls.append)) and calls == []
    assert by_name(doctor.run_checks(good, online=True, connect=calls.append))["provider reachable"].status == "ok"
    assert calls == ["api.anthropic.com"]

    def down(host):
        raise ConnectionRefusedError()
    assert by_name(doctor.run_checks(good, online=True, connect=down))["provider reachable"].status == "fail"
