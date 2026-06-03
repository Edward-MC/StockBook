"""app/backup.py 单测:产物哈希/完整性、LocalDirDestination 存取轮转(零网络)。"""
import sqlite3
from pathlib import Path

import pytest

from app import backup, config


def _make_sqlite(path: Path, value: str = "hello") -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    con.execute("INSERT INTO t VALUES (?)", (value,))
    con.commit()
    con.close()


def test_file_sha256_matches_hashlib(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"abc123")
    import hashlib
    assert backup.file_sha256(p) == hashlib.sha256(b"abc123").hexdigest()


def test_integrity_check_ok_on_good_db_bad_on_corrupt(tmp_path):
    good = tmp_path / "good.db"
    _make_sqlite(good)
    assert backup._sqlite_integrity_check(good) is True
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a sqlite file at all")
    assert backup._sqlite_integrity_check(bad) is False


def test_snapshot_is_consistent_copy(tmp_path):
    src = tmp_path / "src.db"
    _make_sqlite(src, "v1")
    dest = tmp_path / "snap.db"
    backup._sqlite_snapshot(src, dest)
    con = sqlite3.connect(str(dest))
    assert con.execute("SELECT v FROM t").fetchone()[0] == "v1"
    con.close()


def test_localdir_store_list_roundtrip_and_prune(tmp_path):
    d = backup.LocalDirDestination(tmp_path / "backups", "local")
    src = tmp_path / "art.db"
    _make_sqlite(src)
    metas = []
    for i in range(3):
        m = backup.BackupMeta(
            name=f"stockbook-2026010{i}.db", sha256=backup.file_sha256(src),
            size=src.stat().st_size, created_at=f"2026-01-0{i}T00:00:00",
            source_hash="h", integrity_ok=True,
        )
        d.store(src, m)
        metas.append(m)
    listed = d.list()
    assert {m.name for m in listed} == {m.name for m in metas}
    deleted = d.prune(keep=2)
    assert deleted == ["stockbook-20260100.db"]  # oldest by created_at
    assert {m.name for m in d.list()} == {"stockbook-20260101.db", "stockbook-20260102.db"}
    assert not (tmp_path / "backups" / "stockbook-20260100.db").exists()


class FakeDestination:
    """In-memory destination with selectable materialization state."""
    def __init__(self, name="fake", materialized=True):
        self.name = name
        self.metas = {}
        self.blobs = {}
        self._materialized = materialized

    def store(self, src, meta):
        self.blobs[meta.name] = Path(src).read_bytes()
        self.metas[meta.name] = meta

    def list(self):
        return sorted(self.metas.values(), key=lambda m: m.created_at, reverse=True)

    def fetch(self, name, dest):
        Path(dest).write_bytes(self.blobs[name])

    def prune(self, keep):
        doomed = [m.name for m in self.list()[keep:]]
        for n in doomed:
            self.metas.pop(n, None)
            self.blobs.pop(n, None)
        return doomed

    def path_of(self, name):
        return Path("/in-memory") / name

    def is_local(self, name):
        return self._materialized

    def ensure_materialized(self, name, timeout):
        return self._materialized


def test_make_backup_writes_all_destinations(tmp_path, monkeypatch):
    live = tmp_path / "live.db"
    _make_sqlite(live, "v1")
    monkeypatch.setattr(backup, "live_db_path", lambda: live)
    d1 = backup.LocalDirDestination(tmp_path / "b1", "local")
    d2 = FakeDestination("offsite")
    monkeypatch.setattr(backup, "get_destinations", lambda: [d1, d2])
    res = backup.make_backup(force=True)
    assert res["skipped"] is False
    assert len(d1.list()) == 1 and len(d2.list()) == 1
    assert res["verified"]["local"] == "ok"


def test_make_backup_change_detection_skips_unchanged(tmp_path, monkeypatch):
    live = tmp_path / "live.db"
    _make_sqlite(live, "v1")
    monkeypatch.setattr(backup, "live_db_path", lambda: live)
    d1 = backup.LocalDirDestination(tmp_path / "b1", "local")
    monkeypatch.setattr(backup, "get_destinations", lambda: [d1])
    backup.make_backup(force=True)
    res2 = backup.make_backup(force=False)          # unchanged → skip
    assert res2["skipped"] is True
    assert len(d1.list()) == 1
    _make_sqlite(live, "v2")                          # change → no skip
    res3 = backup.make_backup(force=False)
    assert res3["skipped"] is False and len(d1.list()) == 2


def test_make_backup_prunes_to_keep(tmp_path, monkeypatch):
    live = tmp_path / "live.db"
    _make_sqlite(live)
    monkeypatch.setattr(backup, "live_db_path", lambda: live)
    d1 = backup.LocalDirDestination(tmp_path / "b1", "local")
    monkeypatch.setattr(backup, "get_destinations", lambda: [d1])
    monkeypatch.setattr(config, "BACKUP_KEEP", 3)
    for i in range(5):
        _make_sqlite(live, f"v{i}")  # change each time so it isn't skipped
        backup.make_backup(force=True)
    assert len(d1.list()) == 3


def test_verify_tristate(tmp_path):
    d = backup.LocalDirDestination(tmp_path / "b", "local")
    src = tmp_path / "a.db"; _make_sqlite(src)
    meta = backup.BackupMeta("stockbook-x.db", backup.file_sha256(src), src.stat().st_size,
                             "2026-01-01T00:00:00", "h", True)
    d.store(src, meta)
    # ok — stored file is an intact copy
    assert backup._verify_one(d, "stockbook-x.db", allow_pull=False)["status"] == "ok"
    # mismatch — SAME size but tampered content on a fully-present file
    p = d.path_of("stockbook-x.db")
    p.write_bytes(b"\x00" * meta.size)
    assert backup._verify_one(d, "stockbook-x.db", allow_pull=False)["status"] == "mismatch"
    # unavailable — not materialized & cannot pull
    fake = FakeDestination("offsite", materialized=False)
    fake.store(src, meta)
    assert backup._verify_one(fake, "stockbook-x.db", allow_pull=True)["status"] == "unavailable"
    # unavailable — auto-verify (allow_pull=False) on an unmaterialized file: no download triggered
    assert backup._verify_one(fake, "stockbook-x.db", allow_pull=False)["status"] == "unavailable"
    # unavailable — partial materialization (size < recorded): never a false mismatch
    p.write_bytes(b"\x01" * (meta.size - 1))
    assert backup._verify_one(d, "stockbook-x.db", allow_pull=False)["status"] == "unavailable"
    # unavailable — no manifest entry
    assert backup._verify_one(d, "nope.db", allow_pull=False)["status"] == "unavailable"
