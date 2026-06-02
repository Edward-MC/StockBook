"""app/backup.py 单测:产物哈希/完整性、LocalDirDestination 存取轮转(零网络)。"""
import sqlite3
from pathlib import Path

import pytest

from app import backup


def _make_sqlite(path: Path, value: str = "hello") -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE t (v TEXT)")
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
