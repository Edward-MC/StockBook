"""Offsite backup encryption: round-trip, salt persistence, wrapping, warning."""
import logging
import sqlite3
from pathlib import Path

from app import backup, config


def _make_sqlite(path: Path, value: str = "hello") -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    con.execute("INSERT INTO t VALUES (?)", (value,))
    con.commit()
    con.close()


def _meta(src: Path):
    return backup.BackupMeta("stockbook-x.db", backup.file_sha256(src), src.stat().st_size,
                             "2026-01-01T00:00:00", "h", True)


def test_encrypted_store_writes_ciphertext_enc_file(tmp_path):
    src = tmp_path / "live.db"; _make_sqlite(src, "secret")
    inner = backup.LocalDirDestination(tmp_path / "off", "offsite")
    enc = backup.EncryptedDestination(inner, "pw-123")
    enc.store(src, _meta(src))
    f = tmp_path / "off" / "stockbook-x.db.enc"
    assert f.exists()
    assert f.read_bytes() != src.read_bytes()
    assert b"secret" not in f.read_bytes()
    assert (tmp_path / "off" / "enc.json").exists()


def test_encrypted_roundtrip_fetch_decrypts(tmp_path):
    src = tmp_path / "live.db"; _make_sqlite(src, "v1")
    enc = backup.EncryptedDestination(backup.LocalDirDestination(tmp_path / "off", "offsite"), "pw")
    enc.store(src, _meta(src))
    out = tmp_path / "restored.db"
    enc.fetch("stockbook-x.db", out)
    assert out.read_bytes() == src.read_bytes()


def test_encrypted_list_uses_logical_names(tmp_path):
    src = tmp_path / "live.db"; _make_sqlite(src)
    enc = backup.EncryptedDestination(backup.LocalDirDestination(tmp_path / "off", "offsite"), "pw")
    enc.store(src, _meta(src))
    names = [m.name for m in enc.list()]
    assert names == ["stockbook-x.db"]
    assert all(m.encrypted for m in enc.list())


def test_salt_persists_same_key_decrypts(tmp_path):
    src = tmp_path / "live.db"; _make_sqlite(src, "v1")
    d = tmp_path / "off"
    backup.EncryptedDestination(backup.LocalDirDestination(d, "offsite"), "pw").store(src, _meta(src))
    salt1 = (d / "enc.json").read_text()
    enc2 = backup.EncryptedDestination(backup.LocalDirDestination(d, "offsite"), "pw")
    out = tmp_path / "r.db"; enc2.fetch("stockbook-x.db", out)
    assert out.read_bytes() == src.read_bytes()
    assert (d / "enc.json").read_text() == salt1


def test_get_destinations_wraps_offsite_only_with_passphrase(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "off"))
    monkeypatch.setattr(backup, "live_db_path", lambda: tmp_path / "live.db")
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "pw")
    dests = backup.get_destinations()
    assert dests[0].name == "local" and not getattr(dests[0], "encrypted", False)
    assert dests[1].name == "offsite" and getattr(dests[1], "encrypted", False) is True
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "")
    dests2 = backup.get_destinations()
    assert getattr(dests2[1], "encrypted", False) is False


def test_wrong_passphrase_raises_and_cleans_temp(tmp_path):
    import pytest
    from cryptography.fernet import InvalidToken
    src = tmp_path / "live.db"; _make_sqlite(src)
    enc = backup.EncryptedDestination(backup.LocalDirDestination(tmp_path / "off", "offsite"), "correct")
    enc.store(src, _meta(src))
    enc_wrong = backup.EncryptedDestination(backup.LocalDirDestination(tmp_path / "off", "offsite"), "wrong")
    out = tmp_path / "out.db"
    with pytest.raises(InvalidToken):
        enc_wrong.fetch("stockbook-x.db", out)
    assert not Path(str(out) + ".ct").exists()   # ciphertext temp cleaned even on failure


def test_offsite_without_passphrase_logs_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "off"))
    monkeypatch.setattr(backup, "live_db_path", lambda: tmp_path / "live.db")
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "")
    with caplog.at_level(logging.WARNING):
        dests = backup.get_destinations()
    assert getattr(dests[1], "encrypted", False) is False
    assert any("PLAINTEXT" in r.message for r in caplog.records)


def test_verify_encrypted_ok_tamper_wrongkey(tmp_path):
    src = tmp_path / "live.db"; _make_sqlite(src)
    d = tmp_path / "off"
    enc = backup.EncryptedDestination(backup.LocalDirDestination(d, "offsite"), "pw")
    enc.store(src, _meta(src))
    # ok
    assert backup._verify_one(enc, "stockbook-x.db", allow_pull=False)["status"] == "ok"
    # tamper ciphertext → decrypt fails → mismatch (never a false ok)
    f = d / "stockbook-x.db.enc"; b = bytearray(f.read_bytes()); b[-1] ^= 0x01; f.write_bytes(bytes(b))
    assert backup._verify_one(enc, "stockbook-x.db", allow_pull=False)["status"] == "mismatch"
    # wrong key → decrypt fails → mismatch (restore the good ciphertext first)
    _make_sqlite(src); enc.store(src, _meta(src))
    enc_wrong = backup.EncryptedDestination(backup.LocalDirDestination(d, "offsite"), "WRONG")
    assert backup._verify_one(enc_wrong, "stockbook-x.db", allow_pull=False)["status"] == "mismatch"


def test_restore_from_encrypted_offsite_and_wrongkey_aborts(tmp_path, monkeypatch):
    import pytest
    live = tmp_path / "live.db"; _make_sqlite(live, "orig")
    monkeypatch.setattr(backup, "live_db_path", lambda: live)
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "off"))
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "pw")
    backup.make_backup(force=True)                       # writes encrypted offsite
    name = backup.get_destinations()[1].list()[0].name
    assert backup.restore_backup(name, "offsite")["ok"] is True
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "WRONG")
    before = live.read_bytes()
    with pytest.raises(ValueError):
        backup.restore_backup(name, "offsite")
    assert live.read_bytes() == before                   # not half-restored


def test_api_backups_exposes_encrypted_flag(client, tmp_path, monkeypatch):
    from app import config as cfg
    monkeypatch.setattr(cfg, "BACKUP_DIR", str(tmp_path / "off"))
    monkeypatch.setattr(cfg, "BACKUP_PASSPHRASE", "pw")
    client.post("/api/backup")
    rows = client.get("/api/backups").json()
    assert any(r.get("encrypted") for r in rows)


def test_verify_encrypted_read_error_is_unavailable(tmp_path, monkeypatch):
    # A transient read/fetch failure (file present per is_local, but fetch errors)
    # must be 'unavailable', never a false 'mismatch'.
    src = tmp_path / "live.db"; _make_sqlite(src)
    enc = backup.EncryptedDestination(backup.LocalDirDestination(tmp_path / "off", "offsite"), "pw")
    enc.store(src, _meta(src))

    def boom(name, dest):
        raise OSError("simulated read failure")
    monkeypatch.setattr(enc, "fetch", boom)
    res = backup._verify_one(enc, "stockbook-x.db", allow_pull=False)
    assert res["status"] == "unavailable"
