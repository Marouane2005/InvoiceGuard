"""Versioned schema changes for the SQLite files, using PRAGMA user_version.

Rules: a migration is a list of SQL statements. Each runs in one transaction together with the version bump,
so a failed migration changes nothing. A database newer than this program is refused, never "downgraded".
"""
from __future__ import annotations

from typing import Optional


class SchemaTooNew(RuntimeError):
    """The database was written by a newer version of InvoiceGuard."""


def current_version(conn) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def ensure_version(conn, target: int, migrations: Optional[dict] = None, name: str = "database") -> int:
    migrations = migrations or {}
    version = current_version(conn)
    if version > target:
        raise SchemaTooNew(f"{name} is at schema version {version}, but this program only understands up to {target}. "
                           "Upgrade InvoiceGuard before opening it.")
    while version < target:
        step = version + 1
        statements = migrations.get(step)
        if statements is None and step > 1:
            raise RuntimeError(f"{name}: no migration defined for schema version {step}")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements or []:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {int(step)}")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        version = step
    return version
