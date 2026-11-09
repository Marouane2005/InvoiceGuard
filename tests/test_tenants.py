from contextlib import closing

import pytest

from invoiceguard import tenants


@pytest.fixture
def conn(tmp_path):
    with closing(tenants.open_control(tmp_path / "c.db")) as c:
        yield c


def test_key_is_returned_once_and_only_its_hash_is_stored(conn):
    tenant, key = tenants.create_tenant(conn, "Acme", "usd", 10)
    assert key.startswith("ig_") and len(key) > 40 and tenant["default_currency"] == "USD"
    dump = " ".join(str(v) for row in conn.execute("SELECT * FROM tenants") for v in tuple(row))
    assert key not in dump
    assert tenants.authenticate(conn, key)["id"] == tenant["id"]


def test_authenticate_rejects_wrong_empty_and_inactive(conn):
    tenant, key = tenants.create_tenant(conn, "Acme")
    assert tenants.authenticate(conn, key + "x") is None and tenants.authenticate(conn, "") is None
    tenants.deactivate(conn, tenant["id"])
    assert tenants.authenticate(conn, key) is None


def test_rotate_key_invalidates_old_key(conn):
    tenant, old = tenants.create_tenant(conn, "Acme")
    new = tenants.rotate_key(conn, tenant["id"])
    assert tenants.authenticate(conn, old) is None and tenants.authenticate(conn, new)
    assert tenants.rotate_key(conn, "t_missing") is None


@pytest.mark.parametrize("kwargs", [{"name": ""}, {"name": "x" * 101}, {"name": "A", "default_currency": "US"},
                                    {"name": "A", "monthly_limit": -1}])
def test_create_validation(conn, kwargs):
    with pytest.raises(ValueError):
        tenants.create_tenant(conn, **kwargs)


def test_settings_update_and_unset_semantics(conn):
    tenant, _ = tenants.create_tenant(conn, "Acme", "USD")
    tenants.update_settings(conn, tenant["id"], slack_webhook_url="https://hooks.slack.com/services/T/B/X")
    row = tenants.get_tenant(conn, tenant["id"])
    assert row["default_currency"] == "USD" and row["slack_webhook_url"]
    tenants.update_settings(conn, tenant["id"], default_currency=None)
    assert tenants.get_tenant(conn, tenant["id"])["default_currency"] is None
    with pytest.raises(ValueError):
        tenants.update_settings(conn, tenant["id"], slack_webhook_url="https://evil.example/hook")


def test_public_view_hides_hash_and_webhook(conn):
    tenant, _ = tenants.create_tenant(conn, "Acme")
    tenants.update_settings(conn, tenant["id"], slack_webhook_url="https://hooks.slack.com/services/T/B/X")
    view = tenants.public(tenants.get_tenant(conn, tenant["id"]))
    assert view["slack_configured"] is True and "api_key_hash" not in view and "slack_webhook_url" not in view


def test_usage_is_idempotent_and_listed_for_every_tenant(conn):
    a, _ = tenants.create_tenant(conn, "A")
    b, _ = tenants.create_tenant(conn, "B")
    tenants.record_usage(conn, a["id"], "d1", "2026-10", 100, 20)
    tenants.record_usage(conn, a["id"], "d1", "2026-10", 100, 20)
    tenants.record_usage(conn, a["id"], "d2", "2026-10", 50, 10)
    assert tenants.documents_in_month(conn, a["id"], "2026-10") == 2
    assert tenants.usage_summary(conn, a["id"], "2026-10") == {"month": "2026-10", "documents": 2, "input_tokens": 150, "output_tokens": 30}
    rows = {r["name"]: r for r in tenants.usage_all(conn, "2026-10")}
    assert rows["A"]["documents"] == 2 and rows["B"]["documents"] == 0
