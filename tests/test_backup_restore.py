"""Boundary cases for the restore path (app.backup.restore_backup).

The happy paths (local / offsite) and the encrypted failures (wrong passphrase,
no-passphrase integrity net) are covered in test_api / test_backup_crypto. Here
we pin the *remaining* edges that protect against data loss:

  - an offsite copy that can't be materialized  → FileNotFoundError (not a crash)
  - a destination filter that excludes the file → FileNotFoundError
  - a corrupt PLAINTEXT backup                  → abort, live DB untouched
  - reversibility: the pre-restore auto-backup is itself restorable
  - content fidelity: a restore brings back actual row values, not just schema

Unit-level cases monkeypatch live_db_path/get_destinations (no network, no real
DB); the API-level cases ride the isolated `client` fixture (temp SQLite).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import backup


def _make_sqlite(path: Path, value: str = "hello") -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    con.execute("INSERT INTO t VALUES (?)", (value,))
    con.commit()
    con.close()


class _FakeOffsite:
    """An offsite destination whose synced file may be unmaterialized (evicted)."""
    def __init__(self, name="offsite", materialized=True):
        self.name = name
        self.metas = {}
        self.blobs = {}
        self._materialized = materialized

    def store(self, src, meta):
        self.blobs[meta.name] = Path(src).read_bytes()
        self.metas[meta.name] = meta

    def list(self):
        return list(self.metas.values())

    def fetch(self, name, dest):
        Path(dest).write_bytes(self.blobs[name])

    def prune(self, keep):
        return []

    def path_of(self, name):
        return Path("/in-memory") / name

    def is_local(self, name):
        return self._materialized

    def ensure_materialized(self, name, timeout):
        return self._materialized


def _store_meta(dest, src: Path, name: str) -> str:
    meta = backup.BackupMeta(name, backup.file_sha256(src), src.stat().st_size,
                             "2026-01-01T00:00:00", "h", True)
    dest.store(src, meta)
    return name


# --------------------------------------------------------------------------- #
# Unit-level: failures that must raise *before* touching the live DB.
# --------------------------------------------------------------------------- #
def test_restore_unmaterialized_offsite_raises_not_found(tmp_path, monkeypatch):
    # The offsite copy exists in the manifest but can't be pulled (offline /
    # evicted from the synced folder) → FileNotFoundError, never a half-restore.
    src = tmp_path / "art.db"; _make_sqlite(src)
    fake = _FakeOffsite("offsite", materialized=False)
    name = _store_meta(fake, src, "stockbook-x.db")

    live = tmp_path / "live.db"; _make_sqlite(live, "orig")
    before = live.read_bytes()
    monkeypatch.setattr(backup, "live_db_path", lambda: live)
    monkeypatch.setattr(backup, "get_destinations", lambda: [fake])

    with pytest.raises(FileNotFoundError):
        backup.restore_backup(name, "offsite")
    assert live.read_bytes() == before                  # live DB untouched


def test_restore_destination_without_file_raises_not_found(tmp_path, monkeypatch):
    # The backup exists locally, but the caller asked to restore from "offsite":
    # the filtered destination set has no such file → FileNotFoundError.
    src = tmp_path / "art.db"; _make_sqlite(src)
    local = backup.LocalDirDestination(tmp_path / "b", "local")
    name = _store_meta(local, src, "stockbook-y.db")

    live = tmp_path / "live.db"; _make_sqlite(live, "orig")
    monkeypatch.setattr(backup, "live_db_path", lambda: live)
    monkeypatch.setattr(backup, "get_destinations", lambda: [local])

    before = live.read_bytes()
    with pytest.raises(FileNotFoundError):
        backup.restore_backup(name, "offsite")          # wrong destination
    assert live.read_bytes() == before                  # live DB untouched


# --------------------------------------------------------------------------- #
# API-level (isolated temp SQLite via `client`): live-DB safety + reversibility.
# --------------------------------------------------------------------------- #
def _secs(client):
    return {s["code"]: s
            for ac in client.get("/api/dashboard").json()["asset_classes"]
            for s in ac["securities"]}


def _class_count(client):
    return len(client.get("/api/dashboard").json()["asset_classes"])


def test_restore_corrupt_plaintext_backup_aborts_live_untouched(client, tmp_path):
    # A corrupt/truncated backup must NOT clobber the live DB: the artifact
    # integrity check aborts with the live state intact (here: a mutated 4-class
    # state, proving restore neither completes to 5 nor wipes the DB).
    client.post("/api/backup")
    file = client.get("/api/backups").json()[0]["file"]

    victim = client.get("/api/dashboard").json()["asset_classes"][0]["id"]
    client.delete(f"/api/asset-classes/{victim}")
    assert _class_count(client) == 4                     # live now differs from backup

    bad = tmp_path / "backups" / file                    # client DB lives under tmp_path
    bad.write_bytes(b"\x00" * bad.stat().st_size)        # same size, not a SQLite file

    r = client.post("/api/restore", json={"file": file})
    assert r.status_code == 400                           # integrity safety net → ValueError
    assert _class_count(client) == 4                      # live DB untouched (not half-restored)


def test_restore_is_reversible_via_pre_restore_backup(client):
    # Restoring snapshots the CURRENT state first, so a restore is itself
    # reversible: the pre-restore backup recovers the state we restored *over*.
    client.post("/api/backup")
    file_a = client.get("/api/backups").json()[0]["file"]   # state A: 5 classes

    victim = client.get("/api/dashboard").json()["asset_classes"][0]["id"]
    client.delete(f"/api/asset-classes/{victim}")           # state B: 4 classes
    assert _class_count(client) == 4

    assert client.post("/api/restore", json={"file": file_a}).status_code == 200
    assert _class_count(client) == 5                        # rolled back to A

    backups = client.get("/api/backups").json()
    assert len(backups) >= 2                                # A + the pre-restore snapshot of B
    file_b = next(b["file"] for b in backups if b["file"] != file_a)

    # That auto-captured pre-restore backup is a real, restorable snapshot of B.
    assert client.post("/api/restore", json={"file": file_b}).status_code == 200
    assert _class_count(client) == 4


def test_restore_round_trips_actual_data_content(client):
    # Beyond schema/counts: a restore brings back specific row values (page-level
    # content fidelity), here a security's manual price.
    assert _secs(client)["510300"]["price"] == pytest.approx(4.0)   # seed price
    client.post("/api/backup")
    file = client.get("/api/backups").json()[0]["file"]

    sid = _secs(client)["510300"]["id"]
    client.put(f"/api/securities/{sid}/price", json={"price": 99.0})
    assert _secs(client)["510300"]["price"] == pytest.approx(99.0)

    assert client.post("/api/restore", json={"file": file}).status_code == 200
    assert _secs(client)["510300"]["price"] == pytest.approx(4.0)    # value restored
