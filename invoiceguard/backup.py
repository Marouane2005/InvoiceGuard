"""Backups and restores.

A backup is ONE .tar.gz file holding a consistent copy of every database (made with SQLite's online backup, safe while
the service runs) plus the uploaded files. Restores never overwrite: they only write into an empty folder.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

from . import __version__, migrate, store, tenants

ARCHIVE_RE = re.compile(r"^invoiceguard-backup-\d{8}T\d{6}Z\.tar\.gz$")
MANIFEST = "MANIFEST.json"


class BackupError(Exception):
    pass


class NothingToBackup(BackupError):
    """The data directory has no database yet. Not a failed backup."""


def _copy_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(src, timeout=30)) as source, closing(sqlite3.connect(dst)) as target:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError(f"{src.name} failed its integrity check; the backup was not made")


def create_backup(data_dir: Path, dest_dir: Path, now: Optional[datetime] = None) -> Path:
    data_dir, dest_dir = Path(data_dir), Path(dest_dir)
    if not (data_dir / "control.db").exists():
        raise NothingToBackup(f"nothing to back up: {data_dir / 'control.db'} does not exist")
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    archive = dest_dir / f"invoiceguard-backup-{stamp}.tar.gz"
    if archive.exists():
        raise BackupError(f"{archive.name} already exists; wait a second and try again")
    counts = {"tenants": 0, "files": 0}
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        _copy_db(data_dir / "control.db", stage / "control.db")
        for tdir in sorted((data_dir / "tenants").glob("*")):
            if not (tdir / "tenant.db").exists():
                continue
            _copy_db(tdir / "tenant.db", stage / "tenants" / tdir.name / "tenant.db")
            counts["tenants"] += 1
            if (tdir / "files").is_dir():
                shutil.copytree(tdir / "files", stage / "tenants" / tdir.name / "files")
                counts["files"] += sum(1 for _ in (tdir / "files").iterdir())
        (stage / MANIFEST).write_text(json.dumps({"format": 1, "invoiceguard_version": __version__,
                                                  "created_at": stamp, **counts}))
        partial = archive.with_suffix(".partial")
        with tarfile.open(partial, "w:gz") as tar:
            for path in sorted(stage.rglob("*")):
                tar.add(path, arcname=str(path.relative_to(stage)), recursive=False)
        os.chmod(partial, 0o600)  # holds customer data
        os.replace(partial, archive)
    return archive


def copy_archive(archive: Path, dest_dir: Path) -> Path:
    """Copy one archive to another folder. That folder is another machine only when the operator mounted it so."""
    archive, dest_dir = Path(archive), Path(dest_dir)
    if dest_dir.resolve() == archive.parent.resolve():
        raise BackupError("the extra copy folder must be different from the local backup folder")
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / archive.name
        if target.exists():
            raise BackupError(f"{target.name} already exists in the extra copy folder")
        shutil.copyfile(archive, target)
        os.chmod(target, 0o600)
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(f"could not copy the backup to the extra folder ({exc.__class__.__name__})") from None
    return target


def prune(dest_dir: Path, keep: int) -> list:
    """Delete the oldest backups beyond `keep`. Only files named like our own archives are ever touched."""
    if keep < 1:
        raise BackupError("keep must be at least 1")
    archives = sorted(p for p in Path(dest_dir).iterdir() if ARCHIVE_RE.match(p.name))
    removed = archives[:-keep] if len(archives) > keep else []
    for path in removed:
        path.unlink()
    return [p.name for p in removed]


def latest_backup_age_hours(dest_dir: Path, now: Optional[datetime] = None) -> Optional[float]:
    dest_dir = Path(dest_dir)
    if not dest_dir.is_dir():
        return None
    archives = sorted(p for p in dest_dir.iterdir() if ARCHIVE_RE.match(p.name))
    current = now or datetime.now(timezone.utc)
    for path in reversed(archives):
        found = re.search(r"(\d{8}T\d{6}Z)", path.name)
        if found is None:
            continue
        try:
            made = datetime.strptime(found.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return (current - made).total_seconds() / 3600
    return None


def restore(archive: Path, into: Path) -> dict:
    archive, into = Path(archive), Path(into)
    if into.exists() and any(into.iterdir()):
        raise BackupError(f"{into} is not empty. Restore into a new or empty folder; nothing is ever overwritten")
    try:
        tar = tarfile.open(archive, "r:gz")
    except (OSError, tarfile.TarError):
        raise BackupError(f"{archive} is not a readable backup file") from None
    with tar:
        members = tar.getmembers()
        for m in members:
            parts = PurePosixPath(m.name).parts
            if PurePosixPath(m.name).is_absolute() or ".." in parts or not (m.isfile() or m.isdir()):
                raise BackupError("the archive contains an unsafe entry; nothing was restored")
        if MANIFEST not in {m.name for m in members}:
            raise BackupError(f"{archive.name} is not an InvoiceGuard backup (no manifest)")
        into.mkdir(parents=True, exist_ok=True)
        for m in members:
            target = into / m.name
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src_file = tar.extractfile(m)
            if src_file is None:
                raise BackupError(f"could not read {m.name} from the archive")
            with src_file as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    databases = [into / "control.db", *sorted((into / "tenants").glob("*/tenant.db"))]
    for db in databases:
        with closing(sqlite3.connect(db)) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError(f"{db.name} in the restored copy failed its integrity check")
            supported = tenants.SCHEMA_VERSION if db.name == "control.db" else store.SCHEMA_VERSION
            if migrate.current_version(conn) > supported:
                raise migrate.SchemaTooNew("this backup was made by a newer InvoiceGuard. Upgrade before restoring it")
    manifest = json.loads((into / MANIFEST).read_text())
    return {"databases": len(databases), "tenants": manifest.get("tenants"), "files": manifest.get("files"),
            "made_by": manifest.get("invoiceguard_version")}
