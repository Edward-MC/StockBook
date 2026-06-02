"""Backup engine — data-safety hardening (spec 2026-06-01-…-backup-hardening).

Pure-ish, framework-light (like calc/services): snapshot + integrity + checksum,
a BackupDestination Protocol with a LocalDirDestination used twice (local primary
+ synced-folder offsite), plus orchestration/scheduler/CLI (added in later tasks).

SQLite coupling is intentionally confined to the `_sqlite_*` helpers so a future
BackupSource extraction (spec §13) is a mechanical move — not built now (YAGNI).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Protocol

from . import config

_VERIFY_PULL_TIMEOUT_SECS = 30.0  # bounded wait when materializing a synced file


# --------------------------------------------------------------------------- #
# SQLite-coupled primitives (the only DB-specific code — see spec §13).
# --------------------------------------------------------------------------- #
def _sqlite_snapshot(src: Path, dest: Path) -> None:
    """Consistent page-level copy via SQLite's online backup API (handles
    concurrent writers / WAL, unlike a raw file copy)."""
    source = sqlite3.connect(str(src))
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _sqlite_integrity_check(path: Path) -> bool:
    """True iff `path` is a structurally sound SQLite DB (PRAGMA integrity_check)."""
    try:
        con = sqlite3.connect(str(path))
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return False
    return bool(row) and row[0] == "ok"


def _sqlite_restore(artifact: Path, live: Path) -> None:
    """Write a backup artifact back into the live DB (page-level, consistent)."""
    _sqlite_snapshot(artifact, live)


# --------------------------------------------------------------------------- #
# Checksum + metadata
# --------------------------------------------------------------------------- #
def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class BackupMeta:
    name: str            # stockbook-YYYYmmdd-HHMMSS.db
    sha256: str          # backup file bytes (integrity)
    size: int
    created_at: str      # ISO
    source_hash: str     # live DB bytes at backup time (change detection)
    integrity_ok: bool


# --------------------------------------------------------------------------- #
# Destination interface + local/synced-folder implementation
# --------------------------------------------------------------------------- #
class BackupDestination(Protocol):
    name: str
    def store(self, src: Path, meta: BackupMeta) -> None: ...
    def list(self) -> List[BackupMeta]: ...
    def fetch(self, name: str, dest: Path) -> None: ...
    def prune(self, keep: int) -> List[str]: ...
    def path_of(self, name: str) -> Path: ...
    def is_local(self, name: str) -> bool: ...                       # present & readable, no pull
    def ensure_materialized(self, name: str, timeout: float) -> bool: ...  # pull synced file


class LocalDirDestination:
    """A directory of backups + a manifest.json. Used for the on-disk primary AND
    for a synced-folder offsite copy (the path living inside iCloud/坚果云 makes it
    offsite — the interface is identical)."""

    def __init__(self, directory: Path, name: str) -> None:
        self.dir = Path(directory)
        self.name = name

    # -- manifest helpers --
    @property
    def _manifest_path(self) -> Path:
        return self.dir / "manifest.json"

    def _load(self) -> Dict[str, dict]:
        if not self._manifest_path.exists():
            return {}
        try:
            return json.loads(self._manifest_path.read_text("utf-8"))
        except (ValueError, OSError):
            return {}

    def _save(self, data: Dict[str, dict]) -> None:
        self._manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    # -- interface --
    def path_of(self, name: str) -> Path:
        return self.dir / Path(name).name  # basename only — no traversal

    def store(self, src: Path, meta: BackupMeta) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, self.path_of(meta.name))
        data = self._load()
        data[meta.name] = asdict(meta)
        self._save(data)

    def list(self) -> List[BackupMeta]:
        data = self._load()
        metas = [BackupMeta(**v) for v in data.values()]
        # Tolerate pre-existing .db files not in the manifest (e.g. legacy backups).
        if self.dir.exists():
            for p in self.dir.glob("*.db"):
                if p.name not in data:
                    metas.append(BackupMeta(p.name, "", p.stat().st_size,
                                            _mtime_iso(p), "", False))
        return sorted(metas, key=lambda m: m.created_at, reverse=True)

    def fetch(self, name: str, dest: Path) -> None:
        shutil.copy2(self.path_of(name), dest)

    def prune(self, keep: int) -> List[str]:
        metas = self.list()
        doomed = metas[keep:]
        data = self._load()
        deleted: List[str] = []
        for m in doomed:
            p = self.path_of(m.name)
            if p.exists():
                p.unlink()
            data.pop(m.name, None)
            deleted.append(m.name)
        self._save(data)
        return deleted

    def is_local(self, name: str) -> bool:
        p = self.path_of(name)
        if not (p.exists() and p.stat().st_size > 0 and os.access(p, os.R_OK)):
            return False
        try:
            with open(p, "rb") as fh:
                fh.read(1)  # probe — forces transparent materialization if dataless
            return True
        except OSError:
            return False

    def ensure_materialized(self, name: str, timeout: float) -> bool:
        if self.is_local(name):
            return True
        p = self.path_of(name)
        deadline = time.monotonic() + max(0.0, timeout)
        placeholder = p.parent / ("." + p.name + ".icloud")
        if placeholder.exists():
            try:  # macOS iCloud: explicit download, bounded by the REMAINING budget
                remaining = deadline - time.monotonic()
                subprocess.run(["brctl", "download", str(p)], timeout=max(0.0, remaining),
                               check=False, capture_output=True)
            except (OSError, subprocess.SubprocessError):
                return False
        while time.monotonic() < deadline:
            if self.is_local(name):
                return True
            time.sleep(0.5)
        return self.is_local(name)


def _mtime_iso(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime).isoformat()
