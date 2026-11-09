import sqlite3

import pytest

from invoiceguard import migrate, store, tenants


def mem():
    return sqlite3.connect(":memory:", isolation_level=None)


def test_new_databases_are_stamped_and_reopen_cleanly(tmp_path):
    for opener, version in ((lambda p: store.open_db(str(p)), store.SCHEMA_VERSION), (tenants.open_control, tenants.SCHEMA_VERSION)):
        path = tmp_path / f"{version}.db"
        opener(path).close()
        conn = opener(path)
        assert migrate.current_version(conn) == version
        conn.close()


def test_migrations_apply_in_order_and_bump_the_version():
    conn = mem()
    conn.execute("CREATE TABLE t (a)")
    final = migrate.ensure_version(conn, 3, {2: ["ALTER TABLE t ADD COLUMN b"], 3: ["ALTER TABLE t ADD COLUMN c", "UPDATE t SET a = 1"]})
    assert final == 3 and migrate.current_version(conn) == 3
    assert [r[1] for r in conn.execute("PRAGMA table_info(t)")] == ["a", "b", "c"]


def test_a_failing_migration_changes_nothing():
    conn = mem()
    conn.execute("CREATE TABLE t (a)")
    migrate.ensure_version(conn, 1)
    with pytest.raises(sqlite3.OperationalError):
        migrate.ensure_version(conn, 2, {2: ["ALTER TABLE t ADD COLUMN b", "THIS IS NOT SQL"]})
    assert migrate.current_version(conn) == 1
    assert [r[1] for r in conn.execute("PRAGMA table_info(t)")] == ["a"]


def test_missing_migration_and_newer_database_are_refused():
    conn = mem()
    migrate.ensure_version(conn, 1)
    with pytest.raises(RuntimeError, match="no migration"):
        migrate.ensure_version(conn, 2, {})
    conn.execute("PRAGMA user_version = 9")
    with pytest.raises(migrate.SchemaTooNew, match="Upgrade InvoiceGuard"):
        migrate.ensure_version(conn, 1)


def test_opening_a_database_from_the_future_is_refused(tmp_path):
    path = tmp_path / "future.db"
    store.open_db(str(path)).close()
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 99")
    raw.commit()
    raw.close()
    with pytest.raises(migrate.SchemaTooNew):
        store.open_db(str(path))
