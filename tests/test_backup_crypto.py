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
