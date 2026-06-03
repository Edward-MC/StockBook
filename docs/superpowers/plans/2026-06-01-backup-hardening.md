# 数据安全:备份加固 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把备份从「同盘、明文、无校验、无自动、无轮转」升级为**自动化 + 可校验(tri-state)+ 异地**,且对外端点行为兼容、单文件可打包不变。

**Architecture:** 抽出独立 `app/backup.py`(纯逻辑/框架无关,对齐 `calc`/`services` 分层);`BackupDestination` Protocol(`LocalDirDestination` 复用为本地主目标 + 同步盘异地目标,复用子项目 B 的「Protocol + 实现 + 选择器」惯例);SHA-256 + `PRAGMA integrity_check` + 目录内 `manifest.json` + 不经恢复的 `verify`(`ok/mismatch/unavailable`);进程内 lifespan 调度(启动备 + 每 12h + 退出备)+ `python -m app.backup` CLI;变更检测跳过无变化、计数式保留(默认 30)。SQLite 耦合收敛在 `_sqlite_*` 几个命名函数里(为将来 `BackupSource` 抽离铺路,本轮不建接口)。

**Tech Stack:** Python 3.9、`typing.Protocol`、`sqlite3` 在线备份 API、`hashlib`、`asyncio`、`starlette.concurrency.run_in_threadpool`、pytest + FastAPI TestClient。

**Spec:** `docs/superpowers/specs/2026-06-01-stockbook-backup-hardening-design.md`

**前置约定(贯穿全程):**
- 已在分支 `feat/datasource-interfaces`;commit 末尾附 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。
- venv:`source .venv/bin/activate`。**绝不碰真实 `stockbook.db`**;一切走临时库。
- Python 3.9:`typing.Optional/List/Dict/Tuple/Protocol`,不用 `X | None`。
- **绝不改写 git 历史**:不 `--amend`/`rebase`/`squash`;每个 Task 一个独立 commit;review/验证在 commit 之前(确认绿了再提交)。
- 现有 117 测试是回归网,每 Task 跑 `pytest -q` 保持全绿。

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `app/backup.py` | 备份纯逻辑 + 目标接口 + 编排 + 调度 + CLI | 新建 |
| `app/config.py` | 追加 `BACKUP_DIR`/`BACKUP_INTERVAL_HOURS`/`BACKUP_KEEP` | 改 |
| `app/routers/api.py` | 备份/列表/恢复/重置端点改薄壳委托 `app/backup.py`;新增 `verify` 端点 | 改 |
| `main.py` | lifespan 启停调度器 | 改 |
| `tests/conftest.py` | autouse 关掉自动备份(`BACKUP_INTERVAL_HOURS=0`、`BACKUP_DIR=""`) | 改 |
| `tests/test_backup.py` | backup.py 单测(纯逻辑 + Fake 目标 + tri-state) | 新建 |
| `tests/test_api.py` | 保留现有备份测试;新增 verify/异地恢复 API 测试 | 改 |
| `templates/index.html`、`static/js/app.js` | 校验徽标 / 立即校验 / 异地状态 / 恢复来源 | 改 |
| `docs/architecture.md`、`README.md` | 决策 + API + changelog + 结构 | 改 |

---

## Task A:`app/backup.py` 基石(产物/校验/目标,DB 无关编排还没来)

**Files:**
- Create: `app/backup.py`
- Create: `tests/test_backup.py`

- [ ] **Step 1: 写失败测试 `tests/test_backup.py`(checksum + integrity + 目标 store/list/prune)**

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `source .venv/bin/activate && pytest tests/test_backup.py -q`
Expected: FAIL（`ModuleNotFoundError: app.backup` 或属性缺失）。

- [ ] **Step 3: 写 `app/backup.py`(本任务只到目标层,不含 make_backup/调度)**

```python
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
        placeholder = p.parent / ("." + p.name + ".icloud")
        if placeholder.exists():
            try:  # macOS iCloud: ask for an explicit download, bounded
                subprocess.run(["brctl", "download", str(p)], timeout=timeout,
                               check=False, capture_output=True)
            except (OSError, subprocess.SubprocessError):
                return False
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self.is_local(name):
                return True
            time.sleep(0.5)
        return self.is_local(name)


def _mtime_iso(p: Path) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(p.stat().st_mtime).isoformat()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `source .venv/bin/activate && pytest tests/test_backup.py -q`
Expected: PASS（4 个测试)。

- [ ] **Step 5: 跑全套确认无回归**

Run: `source .venv/bin/activate && pytest -q`
Expected: 117 + 4 = 121 passed。

- [ ] **Step 6: Commit(模块 A)**

```bash
git add app/backup.py tests/test_backup.py
git commit -m "feat(backup): app/backup.py core — snapshot/integrity/checksum + LocalDirDestination

SQLite primitives confined to _sqlite_* (snapshot/integrity_check/restore) per
spec §13. BackupMeta + file_sha256, and a BackupDestination Protocol with a
LocalDirDestination (manifest.json-backed store/list/prune/fetch + is_local/
ensure_materialized for synced-folder pull). Pure-logic unit tests, no network.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task B:编排 + 调度 + CLI + 变更检测/轮转(端点改薄壳)

**Files:**
- Modify: `app/config.py`(追加 3 个配置)
- Modify: `app/backup.py`(加 `live_db_path`/`get_destinations`/`make_backup`/`run_cycle`/调度/CLI)
- Modify: `app/routers/api.py`(`/backup`、`/backups`、`/restore`、`/reset` 改委托;删除内联 `_sqlite_snapshot`/`_make_backup`/`_db_path`)
- Modify: `main.py`(lifespan 启停调度)
- Modify: `tests/conftest.py`(autouse 关自动备份)
- Modify: `tests/test_backup.py`(加 make_backup/变更检测/Fake 目标测试)

- [ ] **Step 1: `app/config.py` 末尾追加备份配置**

```python
# Backups (data-safety hardening). BACKUP_DIR is an offsite/synced-folder path
# (e.g. inside iCloud/坚果云); empty = local primary only. INTERVAL 0 disables
# the in-process auto-backup scheduler.
BACKUP_DIR = os.getenv("STOCKBOOK_BACKUP_DIR", "")
BACKUP_INTERVAL_HOURS = int(os.getenv("STOCKBOOK_BACKUP_INTERVAL_HOURS", "12"))
BACKUP_KEEP = int(os.getenv("STOCKBOOK_BACKUP_KEEP", "30"))
```

- [ ] **Step 2: `tests/conftest.py` 的 autouse fixture 关掉自动备份**

在 `_clean_rag_flags` 里(`monkeypatch.setattr(config, "READONLY", False)` 之后)追加两行,使整套测试默认不触发 lifespan 自动备份(保住 `test_backup_creates_and_lists_file` 的「初始为空」假设);测调度的用例自行翻开:

```python
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)  # no auto-backup in tests
    monkeypatch.setattr(config, "BACKUP_DIR", "")            # local-only in tests
```

- [ ] **Step 3: 写失败测试(make_backup / 变更检测 / 轮转 / Fake 目标写全部)** 追加到 `tests/test_backup.py`

```python
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
            self.metas.pop(n, None); self.blobs.pop(n, None)
        return doomed
    def path_of(self, name):
        return Path("/in-memory") / name
    def is_local(self, name):
        return self._materialized
    def ensure_materialized(self, name, timeout):
        return self._materialized


def test_make_backup_writes_all_destinations(tmp_path, monkeypatch):
    live = tmp_path / "live.db"; _make_sqlite(live, "v1")
    monkeypatch.setattr(backup, "live_db_path", lambda: live)
    d1, d2 = backup.LocalDirDestination(tmp_path / "b1", "local"), FakeDestination("offsite")
    monkeypatch.setattr(backup, "get_destinations", lambda: [d1, d2])
    res = backup.make_backup(force=True)
    assert res["skipped"] is False
    assert len(d1.list()) == 1 and len(d2.list()) == 1
    assert res["verified"]["local"] == "ok"


def test_make_backup_change_detection_skips_unchanged(tmp_path, monkeypatch):
    live = tmp_path / "live.db"; _make_sqlite(live, "v1")
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
    live = tmp_path / "live.db"; _make_sqlite(live)
    monkeypatch.setattr(backup, "live_db_path", lambda: live)
    d1 = backup.LocalDirDestination(tmp_path / "b1", "local")
    monkeypatch.setattr(backup, "get_destinations", lambda: [d1])
    monkeypatch.setattr(config, "BACKUP_KEEP", 3)
    for i in range(5):
        _make_sqlite(live, f"v{i}")  # change each time so it isn't skipped
        backup.make_backup(force=True)
    assert len(d1.list()) == 3
```
(在文件顶部补 `from app import config`。)

- [ ] **Step 4: 跑测试确认失败**

Run: `source .venv/bin/activate && pytest tests/test_backup.py -q`
Expected: FAIL（`make_backup`/`live_db_path`/`get_destinations` 未定义）。

- [ ] **Step 5: `app/backup.py` 追加编排 + 调度 + CLI**

```python
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
    from datetime import datetime
    base = f"stockbook-{datetime.now():%Y%m%d-%H%M%S}"
    name, i = f"{base}.db", 2
    while name in existing:
        name = f"{base}-{i}.db"; i += 1
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
        from datetime import datetime
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


# --------------------------------------------------------------------------- #
# In-process scheduler (started/stopped by main lifespan)
# --------------------------------------------------------------------------- #
_STARTUP_DELAY_SECS = 5.0


async def _scheduler_loop() -> None:
    import asyncio
    from starlette.concurrency import run_in_threadpool
    await asyncio.sleep(_STARTUP_DELAY_SECS)
    while True:
        if not config.READONLY:
            await run_in_threadpool(run_cycle)
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
    import asyncio
    from starlette.concurrency import run_in_threadpool
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if not config.READONLY:  # best-effort final backup on shutdown
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
```

> 注:`_verify_one` 在 Task C 定义。本任务为让 `make_backup` 的 `verified` 可用,**先在 Task C 前**把 `_verify_one` 的最小版加入(见 Step 6);若按顺序执行,Task C 会把它扩展为完整 tri-state。为避免前向引用,这里在 Task B 就落一个完整可用的 `_verify_one`(Task C 不再改它,只加 `verify()` 公共函数与端点)。

- [ ] **Step 6: 在 `app/backup.py` 加入完整 `_verify_one`(tri-state 核心)**

放在 `make_backup` 之前(供其调用):

```python
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
```

- [ ] **Step 7: `app/routers/api.py` 端点改薄壳**

删除内联的 `_db_path`/`_sqlite_snapshot`/`_make_backup`(及顶部 `import sqlite3`、`from pathlib import Path` 若已无其它用处则保留——先 `grep` 确认),改为 `from .. import backup`。四个端点替换为:

```python
from .. import backup  # 顶部 import 区

@router.post("/backup", dependencies=[Depends(require_writable)])
def backup_now():
    return backup.make_backup(force=True)


@router.get("/backups")
def list_backups():
    out = []
    seen = {}
    for d in backup.get_destinations():
        for m in d.list():
            row = seen.get(m.name)
            if row is None:
                row = {"file": m.name, "size": m.size, "modified": m.created_at,
                       "integrity_ok": m.integrity_ok, "destinations": []}
                seen[m.name] = row
                out.append(row)
            row["destinations"].append(d.name)
    return out


@router.post("/backup/verify", dependencies=[Depends(require_writable)])
def verify_backups(file: Optional[str] = Query(None), destination: Optional[str] = Query(None)):
    return backup.verify(name=file, destination=destination, allow_pull=True)


@router.post("/restore", dependencies=[Depends(require_writable)])
def restore(payload: schemas.RestoreRequest):
    try:
        return backup.restore_backup(payload.file, getattr(payload, "destination", None))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="备份文件不存在")


@router.post("/reset", dependencies=[Depends(require_writable)])
def reset(db: Session = Depends(get_db)):
    try:
        backup.make_backup(force=True)
    except Exception:
        pass
    reset_to_default(db)
    return {"ok": True}
```

> `Query` 已在 api.py 顶部 import;`Optional` 需确保从 `typing` 引入(`from typing import Optional`,若无则加)。`backup.verify` 与 `backup.restore_backup` 在 Task C 定义——**本 Task 先加它们的可用实现**(见 Step 8),使端点即刻可用、测试可跑。

- [ ] **Step 8: 在 `app/backup.py` 加 `verify` 与 `restore_backup`**

```python
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
    artifact = live.parent / "backups" / f".restore-{os.getpid()}.db"
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
```

- [ ] **Step 9: `main.py` lifespan 启停调度器**

```python
from app import backup, config
...
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = backup.start_scheduler()
    try:
        yield
    finally:
        await backup.stop_scheduler(task)
```

- [ ] **Step 10: 跑测试(backup 单测 + 全套,确认现有备份测试仍绿)**

Run: `source .venv/bin/activate && pytest tests/test_backup.py tests/test_api.py -q && pytest -q`
Expected: backup 单测新增全过;`test_api.py` 现有 `test_backup_creates_and_lists_file`/`test_reset_*`/`test_restore_*` 全绿(端点行为兼容);全套通过(121 + 3 新 = 124)。`grep -rn "_make_backup\|_db_path\b" app/` 应只剩历史无引用。

- [ ] **Step 11: Commit(模块 B)**

```bash
git add app/backup.py app/config.py app/routers/api.py main.py tests/conftest.py tests/test_backup.py
git commit -m "feat(backup): orchestration + 12h scheduler + CLI + change-detection/retention

make_backup (snapshot→integrity→change-detect→store-all→prune→verify), run_cycle,
in-process lifespan scheduler (startup + every BACKUP_INTERVAL_HOURS, default 12,
0=off) and python -m app.backup CLI. Endpoints become thin shims over app/backup.
Tests force interval=0 so the suite's empty-start assumptions hold.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task C:verify tri-state + 异地恢复 的 API 测试(逻辑已在 B 落地)

> 说明:`verify`/`_verify_one`/`restore_backup` 与端点已在 Task B 落地(避免前向引用)。本 Task **专做测试**把 tri-state 各分支与异地恢复钉死,并补 `RestoreRequest.destination` 可选字段。

**Files:**
- Modify: `app/schemas.py`(`RestoreRequest` 加可选 `destination`)
- Modify: `tests/test_backup.py`(tri-state 四分支)
- Modify: `tests/test_api.py`(verify 端点 + 异地恢复)

- [ ] **Step 1: `app/schemas.py` 给 `RestoreRequest` 加可选字段**

```python
class RestoreRequest(BaseModel):
    file: str = Field(..., min_length=1)
    destination: Optional[str] = None
```
(确保该文件已 `from typing import Optional`。)

- [ ] **Step 2: 写 tri-state 失败测试** 追加 `tests/test_backup.py`

```python
def test_verify_tristate(tmp_path):
    d = backup.LocalDirDestination(tmp_path / "b", "local")
    src = tmp_path / "a.db"; _make_sqlite(src)
    meta = backup.BackupMeta("stockbook-x.db", backup.file_sha256(src), src.stat().st_size,
                             "2026-01-01T00:00:00", "h", True)
    d.store(src, meta)
    # ok
    assert backup._verify_one(d, "stockbook-x.db", allow_pull=False)["status"] == "ok"
    # mismatch — tamper bytes of a fully-materialized file
    d.path_of("stockbook-x.db").write_bytes(b"corrupted-but-present-and-full-size__")
    assert backup._verify_one(d, "stockbook-x.db", allow_pull=False)["status"] == "mismatch"
    # unavailable — not materialized & cannot pull
    fake = FakeDestination("offsite", materialized=False)
    fake.store(src, meta)
    assert backup._verify_one(fake, "stockbook-x.db", allow_pull=True)["status"] == "unavailable"
    # unavailable — no manifest entry
    assert backup._verify_one(d, "nope.db", allow_pull=False)["status"] == "unavailable"
```

- [ ] **Step 3: 写 API 测试** 追加 `tests/test_api.py`

```python
def test_verify_endpoint_reports_ok(client):
    client.post("/api/backup")
    res = client.post("/api/backup/verify").json()
    assert res and all(r["status"] == "ok" for r in res)


def test_verify_endpoint_detects_mismatch(client, tmp_path):
    client.post("/api/backup")
    f = client.get("/api/backups").json()[0]["file"]
    # tamper the on-disk backup (live DB is tmp_path/test.db → backups under tmp_path)
    bad = tmp_path / "backups" / f
    bad.write_bytes(b"x" * bad.stat().st_size)
    res = client.post("/api/backup/verify", params={"file": f}).json()
    assert any(r["status"] == "mismatch" for r in res)


def test_restore_from_offsite(client, tmp_path, monkeypatch):
    from app import config
    offsite = tmp_path / "offsite"
    monkeypatch.setattr(config, "BACKUP_DIR", str(offsite))
    client.post("/api/backup")                       # writes local + offsite
    f = client.get("/api/backups").json()[0]["file"]
    r = client.post("/api/restore", json={"file": f, "destination": "offsite"})
    assert r.json()["ok"] is True
```

- [ ] **Step 4: 跑测试**

Run: `source .venv/bin/activate && pytest tests/test_backup.py tests/test_api.py -q && pytest -q`
Expected: 全绿;全套 124 + 4 = 128。

- [ ] **Step 5: Commit(模块 C)**

```bash
git add app/schemas.py tests/test_backup.py tests/test_api.py
git commit -m "test(backup): pin verify tri-state + offsite restore; RestoreRequest.destination

ok/mismatch/unavailable branches (tamper, not-materialized, no-manifest) and a
restore-from-offsite API test. RestoreRequest gains an optional destination.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task D:前端(校验徽标 / 立即校验 / 异地状态 / 恢复来源)

**Files:**
- Modify: `static/js/app.js`(备份区渲染 + 校验/恢复交互)
- Modify: `templates/index.html`(备份区域新增按钮/状态位,如有)

- [ ] **Step 1: 先读现有备份 UI**

Run: `grep -n "backup\|restore\|/api/backups\|备份" static/js/app.js templates/index.html`
找到现有「备份列表渲染 / 恢复按钮」代码块,**照其既有风格**(`api()` 工具、DOM 构造方式)增改,不重写整块。

- [ ] **Step 2: 备份列表每行加 tri-state 徽标**

在渲染每条备份的地方,用 `GET /api/backups` 返回的 `integrity_ok` 与 `destinations` 字段加徽标。示例(并入现有行模板):

```javascript
function backupBadge(item) {
  // item.verified 为前端点「立即校验」后写入的状态;未校验时按 integrity_ok 兜底
  const s = item.verified;
  if (s === 'ok') return '<span class="badge ok">✓ 已校验</span>';
  if (s === 'mismatch') return '<span class="badge warn">⚠ 不一致</span>';
  if (s === 'unavailable') return '<span class="badge muted">☁ 暂不可验</span>';
  return '<span class="badge muted">… 未校验</span>';
}
function destBadge(item) {
  return (item.destinations || []).map(d => `<span class="badge muted">${d}</span>`).join(' ');
}
```

- [ ] **Step 3: 「立即校验」按钮 → `POST /api/backup/verify`**

在备份区加一个按钮,点击后校验并把结果回填到对应行(`unavailable` 用中性提示,不红):

```javascript
async function verifyBackups() {
  const results = await api('/api/backup/verify', { method: 'POST' });
  const byFile = Object.fromEntries(results.map(r => [r.file, r.status]));
  state.backups.forEach(b => { b.verified = byFile[b.file] || b.verified; });
  renderBackups();           // 复用现有渲染函数名;按实际命名调整
  const bad = results.filter(r => r.status === 'mismatch');
  if (bad.length) toast(`⚠ ${bad.length} 份备份校验不一致,请尽快换一份恢复点`);
  else toast('校验完成');
}
```
(`api`/`toast`/`state`/`renderBackups` 用项目里已有的同名工具/状态;若名称不同,按实际替换。)

- [ ] **Step 4: 恢复弹窗加来源选择(本地/异地)+ 顶部异地状态**

- 恢复时若该备份 `destinations` 含 `offsite`,允许选来源,`POST /api/restore` body 带 `destination`。
- 备份区顶部显示「异地:已配置 → <BACKUP_DIR> / 未配置」与「上次自动备份:<最新 modified>」。`BACKUP_DIR` 是否配置可由 `/api/backups` 返回是否出现 `offsite` 目标推断,或新增 `GET /api/backup/status`(可选,YAGNI 下可不加,用现有数据推断)。

- [ ] **Step 5: 起服务手点验证**

Run: `STOCKBOOK_DATABASE_URL=sqlite:////tmp/sb_demo.db uvicorn main:app --port 8099`(临时库!)
手动:点「立即备份」→ 列表出现新行 → 点「立即校验」→ 徽标变 ✓;手动改坏 `/tmp` 下备份再校验 → ⚠。确认无 JS 报错。

- [ ] **Step 6: 跑全套(前端改动不应影响后端测试)**

Run: `source .venv/bin/activate && pytest -q`
Expected: 128 全绿。

- [ ] **Step 7: Commit(模块 D)**

```bash
git add static/js/app.js templates/index.html
git commit -m "feat(backup): frontend — verify badges, verify button, offsite status & restore source

Tri-state badge (✓ ok / ⚠ mismatch / ☁ unavailable / … untested), a 立即校验
button calling POST /api/backup/verify (mismatch warns, unavailable stays
neutral), offsite/last-auto-backup status, and restore-source selection.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task E:文档同步

**Files:**
- Modify: `docs/architecture.md`(关键决策 #19 + API 一览 + 功能日志)
- Modify: `README.md`(环境变量 + 结构)

- [ ] **Step 1: `docs/architecture.md` 决策末尾加 #19**

```markdown
19. **备份加固(数据安全)**:备份从「同盘/明文/无校验/无自动/无轮转」升级为**自动化 + 可校验 + 异地**。抽出 `app/backup.py`(对齐 calc/services 分层),`BackupDestination` Protocol + `LocalDirDestination` 复用为本地主目标 + 同步盘异地目标(`STOCKBOOK_BACKUP_DIR` 指向 iCloud/坚果云即得离机副本,零密钥);每份备份 SHA-256 + `PRAGMA integrity_check` 写入目录内 `manifest.json`;`verify` 返回 **tri-state**(`ok/mismatch/unavailable`)——显式校验会有界拉取未物化的同步盘文件、拉不下来即 `unavailable`,**任何拉取/部分物化失败都不假报 mismatch**;只能校验本机物化副本(抓得到云端损坏、抓不到「一致地变旧」,服务端校验留给将来 `S3Destination`)。进程内 lifespan 调度(启动备 + 每 12h + 退出备,`STOCKBOOK_BACKUP_INTERVAL_HOURS=0` 关闭)+ `python -m app.backup` CLI;变更检测(live 文件哈希)跳过无变化、计数式保留(`STOCKBOOK_BACKUP_KEEP`,默认 30)。SQLite 耦合收敛在 `_sqlite_*` 几个函数,换库时按 spec §13 抽 `BackupSource`(本轮不建)。
```

- [ ] **Step 2: `docs/architecture.md` API 一览「备份」行更新**

把备份那行改为:
```markdown
- 备份:`POST /api/backup`(强制一次,多目标)、`GET /api/backups`(带 integrity/destinations)、`POST /api/backup/verify`(tri-state,显式会拉取异地)、`POST /api/restore`(可选 `destination`,本地/异地)。
```

- [ ] **Step 3: `docs/architecture.md` 功能日志加一行**

```markdown
- **2026-06-01** 备份加固(数据安全):抽 `app/backup.py` + `BackupDestination` 接口(本地 + 同步盘异地)+ SHA-256/`integrity_check`/manifest + `verify` tri-state(异地按需拉取,失败不假报 mismatch)+ 进程内 12h 调度/`python -m app.backup` CLI + 变更检测/保留(30);SQLite 耦合收敛 `_sqlite_*` 留 `BackupSource` 接缝。设计见 `docs/superpowers/specs/2026-06-01-stockbook-backup-hardening-design.md`,计划见 `docs/superpowers/plans/2026-06-01-backup-hardening.md`。
```

- [ ] **Step 4: `README.md` 环境变量 + 结构补充**

环境变量表加三行:`STOCKBOOK_BACKUP_DIR`(异地/同步盘目录,空=仅本地)、`STOCKBOOK_BACKUP_INTERVAL_HOURS`(默认 12,0 关闭自动)、`STOCKBOOK_BACKUP_KEEP`(默认 30)。结构树 `app/` 加一行 `backup.py 备份引擎(BackupDestination 接口 + 校验 + 异地 + 调度/CLI)`。并提一句 `python -m app.backup` 可手动/cron 触发一轮。

- [ ] **Step 5: 跑全套确认仍绿**

Run: `source .venv/bin/activate && pytest -q`
Expected: 128 全绿。

- [ ] **Step 6: Commit(模块 E)**

```bash
git add docs/architecture.md README.md
git commit -m "docs: record backup hardening (decision #19 + API + changelog + README)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 收尾

- `pytest -q` 全绿(128);现有备份/恢复/重置端点行为兼容 = 不破坏旧用户。
- 用 `superpowers:finishing-a-development-branch` 决定整合方式。

## Self-Review 结果(写计划时已核对)

- **Spec 覆盖**:模块拆分(A)、`BackupDestination`+选择器(A/B)、checksum+integrity+manifest(A)、make_backup/变更检测/轮转(B)、调度+CLI(B)、verify tri-state + 异地拉取边界(B 逻辑 / C 测试)、异地恢复(B/C)、前端(D)、配置(B)、行为兼容(B 端点薄壳)、§13 换库接缝(A 的 `_sqlite_*` 收敛 + E 文档)。全覆盖。
- **测试安全**:`live_db_path()` 读 `database.engine`(被 conftest 重绑到临时库),非 `config.DATABASE_URL`;autouse 关 `BACKUP_INTERVAL_HOURS`/`BACKUP_DIR` → lifespan 不自动备 → 保住 `test_backup_creates_and_lists_file` 初始为空。绝不碰真实库。
- **前向引用**:`_verify_one`/`verify`/`restore_backup` 都在 Task B 落地(C 只加测试与 schema 字段),端点在 B 即可用。
- **命名一致**:`make_backup`/`run_cycle`/`live_db_path`/`get_destinations`/`_verify_one`/`verify`/`restore_backup`/`start_scheduler`/`stop_scheduler` 全程一致;tri-state 字面量 `ok/mismatch/unavailable` 统一。
- **Python 3.9**:`Optional/List/Dict/Protocol`,无 `X | None`。
```
