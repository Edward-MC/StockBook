# 备份加密(只加密异地)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给离机的异地备份加 Fernet 认证加密(本地备份保持明文),口令走 `.env`,verify 解密后再校验,恢复可解密或干净中止。

**Architecture:** 一个 `EncryptedDestination` 装饰器包在 offsite `LocalDirDestination` 外面——`store` 加密成 `<name>.enc`、`fetch` 解密,manifest/材料化全透传内层(逻辑名↔`.enc` 名映射)。密钥 = `scrypt(口令, salt)`→Fernet;salt/KDF 参数存异地目录 `enc.json`。`get_destinations()` 在设了 `STOCKBOOK_BACKUP_PASSPHRASE` 时套这层;没设则异地明文 + 告警。`_verify_one` 对加密目标走「解密到**本地**临时文件再校验」分支(明文绝不落进同步盘)。

**Tech Stack:** Python 3.9、`cryptography`(Fernet + Scrypt)、pytest。

**Spec:** `docs/superpowers/specs/2026-06-02-stockbook-backup-encryption-design.md`

**前置约定(贯穿全程):**
- 分支 `feat/datasource-interfaces`,HEAD `d8466d1`。commit 末尾附 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。
- 命令用 `.venv/bin/pytest` / `.venv/bin/pip`,**不要 `source`**。
- **🚨 绝不碰真实 `stockbook.db`**;测试用临时目录/临时口令;**绝不运行裸 `python -m app.backup`**(它打真实库)。
- **绝不改写 git 历史**:每 Task 一个独立 commit,**review 前置**(实现子代理只暂存不提交,审查通过后才提交)。
- Python 3.9:`typing.Optional/List/Dict`,不用 `X | None`。
- 现有 128 测试是回归网,每 Task 跑 `.venv/bin/pytest -q` 保持全绿。
- **简化决策(spec 的可观察行为不变)**:`get_destinations()` 仅在**设了口令**时把 offsite 套加密。没设口令 → 异地明文 + 告警。若曾加密、后来口令被移除 → 异地退化成明文目标,加密条目校验为 `unavailable`(spec §6「没口令→无法校验→unavailable」的可观察结果一致),不特殊处理。

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `requirements.txt` | 加 `cryptography` | 改 |
| `app/config.py` | 加 `BACKUP_PASSPHRASE` | 改 |
| `app/backup.py` | `BackupMeta.encrypted` 字段;`EncryptedDestination`;`get_destinations` 套加密;`_verify_one` 加密分支 + `_verify_encrypted`;`restore_backup` 解密错处理 | 改 |
| `app/routers/api.py` | `GET /api/backups` 加 `encrypted` 字段;restore 端点映射解密错→400 | 改 |
| `tests/test_backup_crypto.py` | 加密往返/错口令/篡改/无口令/salt/wrapping/e2e | 新建 |
| `static/js/app.js`、`templates/index.html`、`static/css/style.css` | 锁标记 + 异地已加密状态 + 解密失败提示 | 改 |
| `docs/architecture.md`、`README.md` | 决策 + API + changelog + 环境变量 | 改 |

---

## Task A:依赖 + 配置 + `BackupMeta.encrypted`

**Files:**
- Modify: `requirements.txt`
- Modify: `app/config.py`
- Modify: `app/backup.py`
- Modify: `tests/test_backup.py`

- [ ] **Step 1: 安装并固定 `cryptography`**

Run: `.venv/bin/pip install cryptography`
然后把实际安装的版本写进 `requirements.txt`(末尾追加一行,版本号用 `.venv/bin/pip show cryptography | grep Version` 的值,例如 `cryptography==43.0.1`):
```
cryptography==<实际版本>
```

- [ ] **Step 2: `app/config.py` 末尾追加口令配置**

```python
# Backup encryption (offsite only). Set this to encrypt the offsite/synced-folder
# copy (Fernet + scrypt). Empty = offsite stays plaintext (a warning is logged).
# Secret — .env only, never committed/logged.
BACKUP_PASSPHRASE = os.getenv("STOCKBOOK_BACKUP_PASSPHRASE", "")
```

- [ ] **Step 3: 写失败测试**(`BackupMeta` 默认 `encrypted=False`)追加到 `tests/test_backup.py`

```python
def test_backupmeta_has_encrypted_default_false():
    m = backup.BackupMeta("x.db", "h", 1, "2026-01-01T00:00:00", "s", True)
    assert m.encrypted is False
```

- [ ] **Step 4: 跑确认失败**

Run: `.venv/bin/pytest tests/test_backup.py::test_backupmeta_has_encrypted_default_false -q`
Expected: FAIL（`TypeError`/`AttributeError`:无 `encrypted`)。

- [ ] **Step 5: 给 `BackupMeta` 加字段**

把 `app/backup.py` 的 `BackupMeta` 改为(在 `integrity_ok` 后加一行,带默认值以兼容现有 6 参构造):

```python
@dataclass
class BackupMeta:
    name: str            # stockbook-YYYYmmdd-HHMMSS.db
    sha256: str          # backup file bytes (integrity)
    size: int
    created_at: str      # ISO
    source_hash: str     # live DB bytes at backup time (change detection)
    integrity_ok: bool
    encrypted: bool = False   # offsite copies encrypted with Fernet (spec 2026-06-02)
```

- [ ] **Step 6: 跑测试 + 全套**

Run: `.venv/bin/pytest tests/test_backup.py -q && .venv/bin/pytest -q`
Expected: 新测试过;全套 128 + 1 = 129 passed。`LocalDirDestination.list()` 用 `BackupMeta(**v)` 反序列化——旧 manifest 无 `encrypted` 键时走默认 False,无碍。

- [ ] **Step 7: Commit(Task A)**

```bash
git add requirements.txt app/config.py app/backup.py tests/test_backup.py
git commit -m "feat(backup): add cryptography dep, BACKUP_PASSPHRASE, BackupMeta.encrypted

Foundation for offsite encryption: the cryptography lib (Fernet+scrypt), the
STOCKBOOK_BACKUP_PASSPHRASE config (.env), and an encrypted flag on BackupMeta
(default False, back-compatible with existing manifests).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task B:`EncryptedDestination` + `get_destinations` 套加密

**Files:**
- Modify: `app/backup.py`
- Create: `tests/test_backup_crypto.py`

- [ ] **Step 1: 写失败测试 `tests/test_backup_crypto.py`**

```python
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
    # the on-disk file is <name>.enc and is NOT a readable sqlite / not the plaintext
    f = tmp_path / "off" / "stockbook-x.db.enc"
    assert f.exists()
    assert f.read_bytes() != src.read_bytes()      # ciphertext, not plaintext
    assert b"secret" not in f.read_bytes()          # content not leaked in clear
    assert (tmp_path / "off" / "enc.json").exists()  # self-describing salt/params


def test_encrypted_roundtrip_fetch_decrypts(tmp_path):
    src = tmp_path / "live.db"; _make_sqlite(src, "v1")
    enc = backup.EncryptedDestination(backup.LocalDirDestination(tmp_path / "off", "offsite"), "pw")
    enc.store(src, _meta(src))
    out = tmp_path / "restored.db"
    enc.fetch("stockbook-x.db", out)               # logical name in, plaintext out
    assert out.read_bytes() == src.read_bytes()


def test_encrypted_list_uses_logical_names(tmp_path):
    src = tmp_path / "live.db"; _make_sqlite(src)
    enc = backup.EncryptedDestination(backup.LocalDirDestination(tmp_path / "off", "offsite"), "pw")
    enc.store(src, _meta(src))
    names = [m.name for m in enc.list()]
    assert names == ["stockbook-x.db"]             # stripped of .enc
    assert all(m.encrypted for m in enc.list())


def test_salt_persists_same_key_decrypts(tmp_path):
    src = tmp_path / "live.db"; _make_sqlite(src, "v1")
    d = tmp_path / "off"
    backup.EncryptedDestination(backup.LocalDirDestination(d, "offsite"), "pw").store(src, _meta(src))
    salt1 = (d / "enc.json").read_text()
    # a fresh EncryptedDestination over the same dir reuses enc.json's salt → can decrypt
    enc2 = backup.EncryptedDestination(backup.LocalDirDestination(d, "offsite"), "pw")
    out = tmp_path / "r.db"; enc2.fetch("stockbook-x.db", out)
    assert out.read_bytes() == src.read_bytes()
    assert (d / "enc.json").read_text() == salt1   # salt not regenerated


def test_get_destinations_wraps_offsite_only_with_passphrase(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "off"))
    # rebind the live engine path via a fake live_db_path
    monkeypatch.setattr(backup, "live_db_path", lambda: tmp_path / "live.db")
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "pw")
    dests = backup.get_destinations()
    assert dests[0].name == "local" and not getattr(dests[0], "encrypted", False)
    assert dests[1].name == "offsite" and getattr(dests[1], "encrypted", False) is True
    # without passphrase → plaintext offsite + a warning
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "")
    dests2 = backup.get_destinations()
    assert getattr(dests2[1], "encrypted", False) is False
```

- [ ] **Step 2: 跑确认失败**

Run: `.venv/bin/pytest tests/test_backup_crypto.py -q`
Expected: FAIL（无 `EncryptedDestination`）。

- [ ] **Step 3: 在 `app/backup.py` 加 `EncryptedDestination`**(放在 `LocalDirDestination` 之后、`_mtime_iso` 附近)

```python
class EncryptedDestination:
    """Decorator over an offsite LocalDirDestination: Fernet-encrypts each backup
    on store, decrypts on fetch. Files land as '<name>.enc'; the inner manifest is
    keyed by the .enc name but list() strips it back to the logical name, and
    sha256 stays the PLAINTEXT hash (uniform with local). Salt + KDF params live in
    enc.json so the folder is self-describing. Decryption always targets a LOCAL
    temp (never the synced dir) so plaintext never lands in iCloud."""
    encrypted = True

    def __init__(self, inner: "LocalDirDestination", passphrase: str) -> None:
        self.inner = inner
        self.name = inner.name
        self._passphrase = passphrase

    def _enc(self, name: str) -> str:
        return name + ".enc"

    def _strip(self, name: str) -> str:
        return name[:-4] if name.endswith(".enc") else name

    def _fernet(self):
        import base64
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        params = self.inner.dir / "enc.json"
        if params.exists():
            d = json.loads(params.read_text("utf-8"))
            salt, n, r, p = bytes.fromhex(d["salt"]), d["n"], d["r"], d["p"]
        else:
            salt, n, r, p = os.urandom(16), 16384, 8, 1
            self.inner.dir.mkdir(parents=True, exist_ok=True)
            params.write_text(json.dumps(
                {"kdf": "scrypt", "salt": salt.hex(), "n": n, "r": r, "p": p}), "utf-8")
        key = base64.urlsafe_b64encode(
            Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(self._passphrase.encode()))
        return Fernet(key)

    def store(self, src: Path, meta: BackupMeta) -> None:
        from dataclasses import replace
        token = self._fernet().encrypt(Path(src).read_bytes())
        self.inner.dir.mkdir(parents=True, exist_ok=True)
        ct = self.inner.dir / f".enc-tmp-{os.getpid()}-{int(time.time()*1000)}"
        ct.write_bytes(token)
        try:
            self.inner.store(ct, replace(meta, name=self._enc(meta.name), encrypted=True))
        finally:
            if ct.exists():
                ct.unlink()

    def list(self) -> List[BackupMeta]:
        from dataclasses import replace
        return [replace(m, name=self._strip(m.name)) for m in self.inner.list()
                if m.name.endswith(".enc")]

    def fetch(self, name: str, dest: Path) -> None:
        """Decrypt <name>.enc into `dest` (caller must pass a LOCAL path)."""
        ct = Path(str(dest) + ".ct")
        self.inner.fetch(self._enc(name), ct)
        try:
            Path(dest).write_bytes(self._fernet().decrypt(ct.read_bytes()))  # InvalidToken on tamper/bad key
        finally:
            if ct.exists():
                ct.unlink()

    def prune(self, keep: int) -> List[str]:
        return [self._strip(n) for n in self.inner.prune(keep)]

    def path_of(self, name: str) -> Path:
        return self.inner.path_of(self._enc(name))

    def is_local(self, name: str) -> bool:
        return self.inner.is_local(self._enc(name))

    def ensure_materialized(self, name: str, timeout: float) -> bool:
        return self.inner.ensure_materialized(self._enc(name), timeout)
```

- [ ] **Step 4: 改 `get_destinations()` 套加密 + 无口令告警**

```python
def get_destinations() -> List[BackupDestination]:
    backups_dir = live_db_path().parent / "backups"
    dests: List[BackupDestination] = [LocalDirDestination(backups_dir, "local")]
    if config.BACKUP_DIR:
        offsite = LocalDirDestination(Path(config.BACKUP_DIR), "offsite")
        if config.BACKUP_PASSPHRASE:
            dests.append(EncryptedDestination(offsite, config.BACKUP_PASSPHRASE))
        else:
            logging.getLogger(__name__).warning(
                "STOCKBOOK_BACKUP_DIR set but STOCKBOOK_BACKUP_PASSPHRASE empty — "
                "offsite backups are PLAINTEXT. Set a passphrase to encrypt them.")
            dests.append(offsite)
    return dests
```
确认 `app/backup.py` 顶部已 `import logging`(若无则加到 import 区)。

- [ ] **Step 5: 跑测试 + 全套**

Run: `.venv/bin/pytest tests/test_backup_crypto.py -q && .venv/bin/pytest -q`
Expected: crypto 测试全过;全套 129 + 5 = 134 passed。

- [ ] **Step 6: Commit(Task B)**

```bash
git add app/backup.py tests/test_backup_crypto.py
git commit -m "feat(backup): EncryptedDestination (Fernet) wrapping offsite when passphrase set

A decorator over the offsite LocalDirDestination: encrypts to <name>.enc on
store, decrypts on fetch; manifest delegated to inner with logical names and
plaintext sha256; salt/KDF params in a self-describing enc.json. get_destinations
wraps offsite only when STOCKBOOK_BACKUP_PASSPHRASE is set (else plaintext +
warning). Decryption targets caller-provided LOCAL paths — plaintext never lands
in the synced folder.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task C:verify 加密分支 + 恢复解密错处理 + API `encrypted` 字段

**Files:**
- Modify: `app/backup.py`
- Modify: `app/routers/api.py`
- Modify: `tests/test_backup_crypto.py`

- [ ] **Step 1: 写失败测试** 追加 `tests/test_backup_crypto.py`

```python
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
    # wrong key → decrypt fails → mismatch
    enc_wrong = backup.EncryptedDestination(backup.LocalDirDestination(d, "offsite"), "WRONG")
    # restore the good ciphertext first
    _make_sqlite(src); enc.store(src, _meta(src))
    assert backup._verify_one(enc_wrong, "stockbook-x.db", allow_pull=False)["status"] == "mismatch"


def test_restore_from_encrypted_offsite_and_wrongkey_aborts(tmp_path, monkeypatch):
    live = tmp_path / "live.db"; _make_sqlite(live, "orig")
    monkeypatch.setattr(backup, "live_db_path", lambda: live)
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "off"))
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "pw")
    backup.make_backup(force=True)                       # writes encrypted offsite
    name = backup.get_destinations()[1].list()[0].name
    # good key → restore ok
    assert backup.restore_backup(name, "offsite")["ok"] is True
    # wrong key → ValueError, live DB untouched (still readable)
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "WRONG")
    import pytest
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
    assert any(r.get("encrypted") for r in rows)         # offsite row flagged encrypted
```

- [ ] **Step 2: 跑确认失败**

Run: `.venv/bin/pytest tests/test_backup_crypto.py -q`
Expected: FAIL（verify 加密分支/restore 解密错/`encrypted` 字段未实现）。

- [ ] **Step 3: `app/backup.py` 加 `_verify_encrypted` 并在 `_verify_one` 分支**

在 `_verify_one` 之前加:

```python
def _verify_encrypted(dest: BackupDestination, name: str, meta: BackupMeta) -> dict:
    """Verify an encrypted backup by decrypting to a LOCAL temp then checking the
    plaintext. Fernet authentication means tamper/wrong-key → InvalidToken →
    mismatch (never a false ok). Plaintext temp is local + deleted immediately."""
    import tempfile
    from cryptography.fernet import InvalidToken
    fd, tmp_name = tempfile.mkstemp(prefix="sb-verify-", suffix=".db")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        try:
            dest.fetch(name, tmp)
        except InvalidToken:
            return {"file": name, "destination": dest.name, "status": "mismatch",
                    "reason": "解密失败:口令错误或文件损坏"}
        ok = (tmp.exists() and tmp.stat().st_size == meta.size
              and file_sha256(tmp) == meta.sha256 and _sqlite_integrity_check(tmp))
        return {"file": name, "destination": dest.name,
                "status": "ok" if ok else "mismatch",
                "reason": "" if ok else "解密后大小/哈希/完整性不符"}
    finally:
        if tmp.exists():
            tmp.unlink()
```

把 `_verify_one` 的「size 检查」之前插入加密分支(整体替换 `_verify_one`):

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
    if getattr(dest, "encrypted", False):
        return _verify_encrypted(dest, name, meta)
    p = dest.path_of(name)
    if not p.exists() or p.stat().st_size != meta.size:  # partial → unavailable, not mismatch
        return {"file": name, "destination": dest.name,
                "status": "unavailable", "reason": "文件不完整"}
    ok = file_sha256(p) == meta.sha256 and _sqlite_integrity_check(p)
    return {"file": name, "destination": dest.name,
            "status": "ok" if ok else "mismatch",
            "reason": "" if ok else "哈希或完整性校验不通过"}
```

- [ ] **Step 4: `restore_backup` 捕获解密失败 → `ValueError`(干净中止)**

在 `restore_backup` 里把 `src_dest.fetch(name, artifact)` 那行包成:

```python
    from cryptography.fernet import InvalidToken
    try:
        src_dest.fetch(name, artifact)
    except InvalidToken:
        if artifact.exists():
            artifact.unlink()
        raise ValueError("解密失败:口令错误或备份损坏")
```
(此时尚未 `dispose()`/写回,live 库未动 → 天然可逆。)

- [ ] **Step 5: `app/routers/api.py` —— `GET /api/backups` 加 `encrypted`,restore 映射 400**

`list_backups` 行模板加字段(`encrypted` 用「任一目标加密即真」):
```python
            if row is None:
                row = {"file": m.name, "size": m.size, "modified": m.created_at,
                       "integrity_ok": m.integrity_ok, "destinations": [],
                       "encrypted": False}
                seen[m.name] = row
                out.append(row)
            row["destinations"].append(d.name)
            if getattr(m, "encrypted", False):
                row["encrypted"] = True
```
`restore` 端点加解密错→400:
```python
@router.post("/restore", dependencies=[Depends(require_writable)])
def restore(payload: schemas.RestoreRequest):
    try:
        return backup.restore_backup(payload.file, getattr(payload, "destination", None))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="备份文件不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
```

- [ ] **Step 6: 跑测试 + 全套**

Run: `.venv/bin/pytest tests/test_backup_crypto.py tests/test_api.py -q && .venv/bin/pytest -q`
Expected: 全绿;全套 134 + 3 = 137 passed。

- [ ] **Step 7: Commit(Task C)**

```bash
git add app/backup.py app/routers/api.py tests/test_backup_crypto.py
git commit -m "feat(backup): verify-decrypts-then-checks for encrypted backups; restore aborts on bad key

_verify_one branches to _verify_encrypted for encrypted destinations: decrypt to
a local temp (Fernet auth → tamper/wrong-key = mismatch, never false ok), then
size/sha256/integrity on the plaintext. restore_backup maps InvalidToken → a
ValueError (live DB untouched). GET /api/backups exposes an encrypted flag;
restore endpoint maps it to HTTP 400.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task D:前端(锁标记 + 异地已加密状态 + 解密失败提示)

**Files:**
- Modify: `static/js/app.js`、`templates/index.html`、`static/css/style.css`

- [ ] **Step 1: 先读现有备份 UI**

Run: `grep -n "destinations\|异地\|verifyBackups\|backupBadge\|_bkBadge\|bk-info\|integrity_ok" static/js/app.js`
照现有风格(`api`/`toast`/`byId`、`_bkBadge`/`_bkSummary` 等真实函数名)做**targeted** 增改,不重写整块。

- [ ] **Step 2: 备份行加锁标记 + 异地加密状态**

- 行渲染处:若该行 `item.encrypted` 为真,加一个锁标记 `<span class="bk-lock" title="异地副本已加密">🔒</span>`。
- `_bkSummary`(异地状态行)按是否有加密行区分文案:
  - 有异地且加密:`异地:已加密 🔒 → <dir 或 "已配置">`
  - 有异地未加密:`异地:已配置(未加密)⚠`
  - 无异地:`异地:未配置`
  (是否加密由 `/api/backups` 任一行 `encrypted` 推断;`dir` 路径前端不一定有,显示「已配置」即可。)

- [ ] **Step 3: 解密失败提示**

`verifyBackups` 里:若某结果 `status==='mismatch'` 且 `reason` 含「解密失败」,toast 文案点明「口令错误或文件损坏 —— 先确认 .env 口令再判定损坏」,与普通 mismatch 区分。

- [ ] **Step 4: CSS**

`static/css/style.css` 加 `.bk-lock`(小字号、与 `.bk-badge` 同一行内对齐;复用现有变量,几行即可)。

- [ ] **Step 5: 起服务手点(临时库 + 临时口令,绝不碰真实库)**

```
STOCKBOOK_DATABASE_URL=sqlite:////tmp/sb_enc_$$.db \
STOCKBOOK_BACKUP_DIR=/tmp/sb_enc_off_$$ \
STOCKBOOK_BACKUP_PASSPHRASE=demo-pass \
.venv/bin/python -m uvicorn main:app --port 8780 --log-level warning &
```
`curl -s -X POST :8780/api/backup` → 看 `/tmp/sb_enc_off_*` 里出现 `.enc` 文件;`curl -s :8780/api/backups` → 异地行 `encrypted:true`;`curl -s -X POST :8780/api/backup/verify` → ok。`GET /` 页面无 JS 报错。完事 `pkill -f "uvicorn main:app --port 8780"`,清 `/tmp/sb_enc_*`。`node --check static/js/app.js`。

- [ ] **Step 6: 全套仍绿**

Run: `.venv/bin/pytest -q` → 137 passed。

- [ ] **Step 7: Commit(Task D)**

```bash
git add static/js/app.js templates/index.html static/css/style.css
git commit -m "feat(backup): frontend — encrypted lock badge, offsite-encrypted status, decrypt-fail hint

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task E:文档

**Files:**
- Modify: `docs/architecture.md`、`README.md`

- [ ] **Step 1: `docs/architecture.md` 决策末尾加 #20**(在 #19 之后)

```markdown
20. **备份加密(只加密异地)**:给离机的异地备份加 Fernet 认证加密,**本地保持明文**(本地明文是「忘口令也能恢复」的安全冗余 —— 忘口令最多失去异地、不丢全部)。`EncryptedDestination` 装饰器包在 offsite `LocalDirDestination` 外:`store` 加密成 `<name>.enc`、`fetch` 解密,manifest 透传内层(逻辑名↔`.enc` 映射,`sha256` 存明文哈希)。密钥 = `scrypt(STOCKBOOK_BACKUP_PASSPHRASE, salt)`→Fernet,salt/KDF 参数存异地目录 `enc.json`(文件夹自描述,带口令即可解,跨平台)。**设口令即加密、无额外开关**(YAGNI);配了异地但没口令 → 明文 + 告警。`verify` 对加密备份**解密到本地临时文件再校验**(Fernet 认证使篡改/错口令→`mismatch`,明文绝不落进同步盘);恢复解密或干净中止(错口令→400,live 库未动)。改口令不支持轮转(改前先用旧口令把异地迁出)。
```

- [ ] **Step 2: `docs/architecture.md` API 一览「备份」行补 `encrypted`**

把备份那行的 `GET /api/backups` 括注改为 `(带 integrity/destinations/encrypted)`。

- [ ] **Step 3: `docs/architecture.md` 功能日志加一行**

```markdown
- **2026-06-02** 备份加密(只加密异地):`EncryptedDestination`(Fernet + scrypt 口令派生)包 offsite,`<name>.enc` + 自描述 `enc.json`;设 `STOCKBOOK_BACKUP_PASSPHRASE` 即加密、否则明文+告警;verify 解密后再校验(篡改/错口令→mismatch,明文不落同步盘)、恢复错口令→400 且 live 不动。设计见 `docs/superpowers/specs/2026-06-02-stockbook-backup-encryption-design.md`,计划见 `docs/superpowers/plans/2026-06-02-backup-encryption.md`。
```

- [ ] **Step 4: `README.md` 环境变量加一行**

`STOCKBOOK_BACKUP_PASSPHRASE` — 异地备份加密口令(Fernet);留空=异地明文 + 告警。**注意:忘口令则加密的异地备份永久无法解开(本地明文备份不受影响)。**

- [ ] **Step 5: 全套确认仍绿**

Run: `.venv/bin/pytest -q` → 137 passed。

- [ ] **Step 6: Commit(Task E)**

```bash
git add docs/architecture.md README.md
git commit -m "docs: record backup encryption (decision #20 + API + changelog + README)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 收尾

- `.venv/bin/pytest -q` 全绿(137);本地备份/verify/恢复路径一字未改 = 向后兼容。
- 用 `superpowers:finishing-a-development-branch` 决定整合方式。

## Self-Review 结果(写计划时已核对)

- **Spec 覆盖**:范围只加密异地(B 的 get_destinations)、`EncryptedDestination` 装饰器 + `.enc` + enc.json(B)、Fernet+scrypt(B)、配置(A)、verify 解密分支 tri-state(C)、恢复解密/中止(C)、`encrypted` 字段(C)、前端(D)、文档(E)、依赖(A)。全覆盖。
- **明文不落同步盘**:`_verify_encrypted` 与 `restore` 的解密目标都是**本地临时/本地 artifact**,不是 offsite 目录。
- **行为兼容**:不设口令 → 与现状一致;本地路径零改;`BackupMeta.encrypted` 带默认值,旧 manifest 反序列化无碍;`GET /api/backups` 仅新增字段。
- **简化点已声明**:仅设口令时套加密;口令移除后加密条目退化为 `unavailable`(spec §6 可观察结果一致),不特殊处理 —— 已在前置约定写明。
- **命名一致**:`EncryptedDestination`/`encrypted`/`_verify_encrypted`/`BACKUP_PASSPHRASE`/`enc.json` 全程一致;tri-state 字面量统一。
- **Python 3.9**:`Optional/List/Dict`,无 `X | None`。
