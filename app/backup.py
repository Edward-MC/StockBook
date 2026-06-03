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
from typing import Dict, List, Optional, Protocol

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


def _verify_one(dest: BackupDestination, name: str, *, allow_pull: bool,
                timeout: float = _VERIFY_PULL_TIMEOUT_SECS) -> dict:
    """Verify one backup. status ∈ {ok, mismatch, unavailable}. A pull/partial-
    materialization failure is ALWAYS 'unavailable', never a false 'mismatch'."""
    meta = {m.name: m for m in dest.list()}.get(name)
    if meta is None or not meta.sha256:
        return {"file": name, "destination": dest.name,
                "status": "unavailable", "reason": "无 manifest 记录"}
    materialized = dest.ensure_materialized(name, timeout) if allow_pull else dest.is_local(name)
    if not materialized:
        return {"file": name, "destination": dest.name,
                "status": "unavailable", "reason": "文件未物化(离线/驱逐)"}
    p = dest.path_of(name)
    if not p.exists() or p.stat().st_size != meta.size:  # partial → unavailable, not mismatch
        return {"file": name, "destination": dest.name,
                "status": "unavailable", "reason": "文件不完整"}
    ok = file_sha256(p) == meta.sha256 and _sqlite_integrity_check(p)
    return {"file": name, "destination": dest.name,
            "status": "ok" if ok else "mismatch",
            "reason": "" if ok else "哈希或完整性校验不通过"}


# --------------------------------------------------------------------------- #
# Live DB path + destination selection
# --------------------------------------------------------------------------- #
def live_db_path() -> Path:
    """Path of the live SQLite file from the *currently bound* engine. Reads
    database.engine (which the test fixtures rebind to a temp DB) — NOT
    config.DATABASE_URL — so tests never touch the real stockbook.db."""
    from . import database
    url = database.engine.url
    if url.get_backend_name() != "sqlite" or not url.database:
        raise ValueError("仅支持 SQLite 数据库备份")
    return Path(url.database)


def get_destinations() -> List[BackupDestination]:
    backups_dir = live_db_path().parent / "backups"
    dests: List[BackupDestination] = [LocalDirDestination(backups_dir, "local")]
    if config.BACKUP_DIR:
        dests.append(LocalDirDestination(Path(config.BACKUP_DIR), "offsite"))
    return dests


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _timestamp_name(existing: List[str]) -> str:
    base = f"stockbook-{datetime.now():%Y%m%d-%H%M%S}"
    name, i = f"{base}.db", 2
    while name in existing:
        name = f"{base}-{i}.db"
        i += 1
    return name


def make_backup(*, force: bool = False) -> dict:
    """Snapshot → integrity-check → (change-detect) → store to all destinations →
    prune → verify newest. Returns a result dict; never writes a corrupt backup."""
    live = live_db_path()
    source_hash = file_sha256(live)
    dests = get_destinations()
    primary = dests[0]
    existing = primary.list()
    if not force and existing and existing[0].source_hash == source_hash:
        return {"skipped": True, "reason": "unchanged", "written": [], "verified": {}}

    backups_dir = live.parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    tmp = backups_dir / f".tmp-{os.getpid()}-{int(time.time()*1000)}.db"
    try:
        _sqlite_snapshot(live, tmp)
        if not _sqlite_integrity_check(tmp):
            raise RuntimeError("快照完整性校验失败,放弃产出")
        meta = BackupMeta(
            name=_timestamp_name([m.name for m in existing]),
            sha256=file_sha256(tmp), size=tmp.stat().st_size,
            created_at=datetime.now().isoformat(), source_hash=source_hash,
            integrity_ok=True,
        )
        for d in dests:
            d.store(tmp, meta)
            d.prune(config.BACKUP_KEEP)
    finally:
        if tmp.exists():
            tmp.unlink()

    verified = {d.name: _verify_one(d, meta.name, allow_pull=False)["status"] for d in dests}
    return {"skipped": False, "written": [d.name for d in dests],
            "file": meta.name, "verified": verified}


def run_cycle() -> dict:
    """One backup cycle for scheduler/CLI. Never raises — failures are logged."""
    try:
        return make_backup(force=False)
    except Exception as e:  # backup must never crash the app
        return {"skipped": False, "error": str(e)}


def verify(*, name: Optional[str] = None, destination: Optional[str] = None,
           allow_pull: bool = True) -> List[dict]:
    """Verify a named backup (or each destination's newest) — tri-state results."""
    results: List[dict] = []
    for d in get_destinations():
        if destination and d.name != destination:
            continue
        targets = [name] if name else ([d.list()[0].name] if d.list() else [])
        for n in targets:
            results.append(_verify_one(d, n, allow_pull=allow_pull))
    return results


def restore_backup(name: str, destination: Optional[str] = None) -> dict:
    """Restore the live DB from a backup (snapshot current first → page-level
    write-back). Raises FileNotFoundError if the named backup is unavailable."""
    live = live_db_path()
    dests = get_destinations()
    if destination:
        dests = [d for d in dests if d.name == destination]
    src_dest = next((d for d in dests if name in {m.name for m in d.list()}), None)
    if src_dest is None or Path(name).name != name:
        raise FileNotFoundError(name)
    if not src_dest.ensure_materialized(name, _VERIFY_PULL_TIMEOUT_SECS):
        raise FileNotFoundError(name)
    try:  # current state is itself snapshotted, so a restore is reversible
        make_backup(force=True)
    except Exception:
        pass
    from . import database
    from .seed import create_schema
    artifact = live.parent / "backups" / f".restore-{os.getpid()}-{int(time.time()*1000)}.db"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    src_dest.fetch(name, artifact)
    try:
        database.engine.dispose()          # release connections before overwrite
        _sqlite_restore(artifact, live)
    finally:
        if artifact.exists():
            artifact.unlink()
    create_schema()                         # additive migrations if an old backup
    return {"ok": True, "restored": name}


# --------------------------------------------------------------------------- #
# In-process scheduler (started/stopped by main lifespan)
# --------------------------------------------------------------------------- #
_STARTUP_DELAY_SECS = 5.0


async def _scheduler_loop() -> None:
    import asyncio
    import logging
    from starlette.concurrency import run_in_threadpool
    await asyncio.sleep(_STARTUP_DELAY_SECS)
    while True:
        try:
            if not config.READONLY:
                await run_in_threadpool(run_cycle)
        except Exception as exc:  # never let the scheduler die silently
            logging.getLogger(__name__).warning("backup scheduler error: %s", exc)
        hours = config.BACKUP_INTERVAL_HOURS
        if hours <= 0:
            return
        await asyncio.sleep(hours * 3600)


def start_scheduler():
    """Return an asyncio.Task running the loop, or None if auto-backup disabled."""
    import asyncio
    if config.BACKUP_INTERVAL_HOURS <= 0:
        return None
    return asyncio.create_task(_scheduler_loop())


async def stop_scheduler(task) -> None:
    """Cancel the scheduler and do a best-effort final backup — only if a
    scheduler was actually running (auto-backup enabled)."""
    import asyncio
    from starlette.concurrency import run_in_threadpool
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    if not config.READONLY:
        try:
            await run_in_threadpool(run_cycle)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# CLI:  python -m app.backup   (one cycle, for cron / ops / manual)
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from .seed import init_db
    init_db()
    print(json.dumps(run_cycle(), ensure_ascii=False, default=str))
