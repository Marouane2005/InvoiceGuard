import io
import sqlite3
import tarfile
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from invoiceguard import backup, migrate, pipeline, store, tenants
from invoiceguard.config import Settings
from conftest import GOOD_RAW, FakeExtractor, pdf


def seed(settings, make_tenant):
    out = []
    for i, name in enumerate(("Alpha", "Beta"), start=1):
        tenant, headers = make_tenant(name)
        data = pdf(i)
        res = pipeline.process_document(settings=settings, tenant=tenant, data=data, filename=f"{name}.pdf", extractor=FakeExtractor(GOOD_RAW))
        out.append((tenant, headers["Authorization"].split()[1], res["document_id"], data))
    return out


def test_backup_then_restore_brings_everything_back(settings, make_tenant, tmp_path):
    seeded = seed(settings, make_tenant)
    archive = backup.create_backup(settings.data_dir, tmp_path / "backups")
    assert archive.name.startswith("invoiceguard-backup-") and oct(archive.stat().st_mode & 0o777) == "0o600"

    info = backup.restore(archive, tmp_path / "restored")
    assert info["tenants"] == 2 and info["files"] == 2 and info["databases"] == 3

    restored = Settings(data_dir=tmp_path / "restored")
    with closing(tenants.open_control(pipeline.control_path(restored))) as control:
        for tenant, key, doc_id, data in seeded:
            assert tenants.authenticate(control, key)["id"] == tenant["id"]
            with closing(pipeline.open_tenant_db(restored, tenant["id"])) as conn:
                assert store.get_invoice(conn, doc_id)["status"] == "clean"
            path = store.file_path(pipeline.tenant_dir(restored, tenant["id"]) / "files", doc_id, "application/pdf")
            assert path.read_bytes() == data
        assert tenants.usage_summary(control, seeded[0][0]["id"], tenants.current_month())["documents"] == 1


def test_nothing_to_back_up(tmp_path):
    with pytest.raises(backup.NothingToBackup, match="nothing to back up"):
        backup.create_backup(tmp_path / "empty", tmp_path / "dest")


def test_extra_copy_is_a_second_folder_and_fails_closed(settings, make_tenant, tmp_path):
    seed(settings, make_tenant)
    archive = backup.create_backup(settings.data_dir, tmp_path / "local")
    copied = backup.copy_archive(archive, tmp_path / "other")
    assert copied.name == archive.name and copied.read_bytes() == archive.read_bytes()
    assert oct(copied.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(backup.BackupError, match="different"):
        backup.copy_archive(archive, tmp_path / "local")
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    with pytest.raises(backup.BackupError, match="could not copy"):
        backup.copy_archive(archive, blocker / "child")


def test_same_second_does_not_overwrite_an_existing_backup(settings, make_tenant, tmp_path):
    seed(settings, make_tenant)
    moment = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    backup.create_backup(settings.data_dir, tmp_path / "b", now=moment)
    with pytest.raises(backup.BackupError, match="already exists"):
        backup.create_backup(settings.data_dir, tmp_path / "b", now=moment)


def test_prune_only_touches_our_own_old_archives(settings, make_tenant, tmp_path):
    seed(settings, make_tenant)
    dest = tmp_path / "b"
    for hour in range(4):
        backup.create_backup(settings.data_dir, dest, now=datetime(2026, 10, 1, hour, 0, 0, tzinfo=timezone.utc))
    (dest / "notes.txt").write_text("keep me")
    (dest / "invoiceguard-backup-garbage.tar.gz").write_text("keep me too")
    removed = backup.prune(dest, 2)
    assert removed == ["invoiceguard-backup-20261001T000000Z.tar.gz", "invoiceguard-backup-20261001T010000Z.tar.gz"]
    left = sorted(p.name for p in dest.iterdir())
    assert "notes.txt" in left and "invoiceguard-backup-garbage.tar.gz" in left and len(left) == 4
    with pytest.raises(backup.BackupError):
        backup.prune(dest, 0)


def test_latest_backup_age(settings, make_tenant, tmp_path):
    seed(settings, make_tenant)
    dest = tmp_path / "b"
    assert backup.latest_backup_age_hours(dest) is None
    made = datetime(2026, 10, 1, 6, 0, 0, tzinfo=timezone.utc)
    backup.create_backup(settings.data_dir, dest, now=made)
    assert backup.latest_backup_age_hours(dest, now=made + timedelta(hours=5)) == pytest.approx(5.0)


def test_unreadable_backup_stamp_does_not_crash_and_older_valid_archive_still_counts(settings, make_tenant, tmp_path):
    seed(settings, make_tenant)
    dest = tmp_path / "b"
    made = datetime(2026, 10, 1, 6, 0, 0, tzinfo=timezone.utc)
    backup.create_backup(settings.data_dir, dest, now=made)
    # The filename pattern allows this. The calendar date is impossible, so it is not a backup age.
    (dest / "invoiceguard-backup-20261340T000000Z.tar.gz").write_bytes(b"not-a-real-archive")
    assert backup.latest_backup_age_hours(dest, now=made + timedelta(hours=5)) == pytest.approx(5.0)
    for path in dest.glob("invoiceguard-backup-*.tar.gz"):
        path.unlink()
    (dest / "invoiceguard-backup-20261340T000000Z.tar.gz").write_bytes(b"still-not")
    assert backup.latest_backup_age_hours(dest, now=made) is None


def test_restore_reports_an_unreadable_member_instead_of_crashing(tmp_path, monkeypatch):
    archive = tmp_path / "member.tar.gz"
    payload = b'{"format": 1}'
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("MANIFEST.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    monkeypatch.setattr(tarfile.TarFile, "extractfile", lambda self, member: None)
    with pytest.raises(backup.BackupError, match="could not read MANIFEST.json"):
        backup.restore(archive, tmp_path / "out")


def test_restore_never_overwrites(settings, make_tenant, tmp_path):
    seed(settings, make_tenant)
    archive = backup.create_backup(settings.data_dir, tmp_path / "b")
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "precious.txt").write_text("mine")
    with pytest.raises(backup.BackupError, match="not empty"):
        backup.restore(archive, busy)
    assert (busy / "precious.txt").read_text() == "mine"
    backup.restore(archive, tmp_path / "ok")  # a new folder is fine


def _tar(path, entries):
    with tarfile.open(path, "w:gz") as tar:
        for name, payload, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                tar.addfile(info)
            else:
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))


@pytest.mark.parametrize("entries", [
    [("../evil.txt", b"x", "file"), ("MANIFEST.json", b"{}", "file")],
    [("/abs/evil.txt", b"x", "file"), ("MANIFEST.json", b"{}", "file")],
    [("link", b"", "symlink"), ("MANIFEST.json", b"{}", "file")],
])
def test_malicious_archives_are_refused_before_anything_is_written(tmp_path, entries):
    archive = tmp_path / "bad.tar.gz"
    _tar(archive, entries)
    target = tmp_path / "into"
    with pytest.raises(backup.BackupError, match="unsafe"):
        backup.restore(archive, target)
    assert not (tmp_path / "evil.txt").exists() and not target.exists()


def test_non_backups_are_refused(tmp_path):
    plain = tmp_path / "x.tar.gz"
    _tar(plain, [("hello.txt", b"hi", "file")])
    with pytest.raises(backup.BackupError, match="no manifest"):
        backup.restore(plain, tmp_path / "a")
    junk = tmp_path / "junk.tar.gz"
    junk.write_bytes(b"not a tar")
    with pytest.raises(backup.BackupError, match="not a readable backup"):
        backup.restore(junk, tmp_path / "b")


def test_a_backup_from_a_newer_version_is_refused_on_restore(settings, make_tenant, tmp_path):
    seed(settings, make_tenant)
    with closing(sqlite3.connect(pipeline.control_path(settings))) as raw:
        raw.execute("PRAGMA user_version = 99")
    archive = backup.create_backup(settings.data_dir, tmp_path / "b")
    with pytest.raises(migrate.SchemaTooNew):
        backup.restore(archive, tmp_path / "r")
