# 备份加密 v2:逐文件加密 + 永远合并显示 + 分页 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让异地备份「有没有口令都能备」、同一备份永远合并成一行(消除口令删除后的重复显示)、加密与否逐文件记录;并给备份列表加分页。

**Architecture:** `EncryptedDestination` 永远包住 offsite(只要配了 `BACKUP_DIR`,不再看有没有口令)。加密是**逐备份**的:有口令存 `<名>.db.enc`(密文,`encrypted=true`),没口令存 `<名>.db`(明文,`encrypted=false`);`list()` 永远剥掉 `.enc` 还原逻辑名 → 永远和本地同名 → 合并一行。`_verify_one`/`restore` 按**每条备份的 `meta.encrypted`**(而非目标类型)决定是否解密;加密备份遇错口令→`mismatch`、遇没口令→`unavailable/需口令`。现有 `.enc` 文件**无需迁移**(永远包一层后照样被剥名合并)。

**Tech Stack:** Python 3.9、`cryptography`(Fernet+Scrypt)、pytest、原生 JS。

**Spec:** `docs/superpowers/specs/2026-06-02-stockbook-backup-encryption-design.md`(本计划同时更新该 spec 的模型描述,见 Task 3)。

**前置约定:**
- 分支 `feat/datasource-interfaces`,HEAD `271cb3a`。commit 末尾附 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。
- 命令用 `.venv/bin/pytest`,**不要 `source`**。🚨 **绝不碰真实 `stockbook.db`**(测试用临时目录+临时口令);**绝不运行裸 `python -m app.backup`**;起服务必须 `STOCKBOOK_DATABASE_URL`/`STOCKBOOK_BACKUP_DIR`/`STOCKBOOK_BACKUP_PASSPHRASE` 全指临时值。
- **绝不改写 git 历史**:每 Task 一个独立 commit,**review 前置**(实现子代理只暂存不提交,审过才提交)。
- Python 3.9:`Optional/List/Dict`,不用 `X | None`。
- 现有 141 测试是回归网,每 Task 跑 `.venv/bin/pytest -q` 保持全绿。

---

## 文件结构

| 文件 | 改动 |
|---|---|
| `app/backup.py` | 重做 `EncryptedDestination`(永远包、逐文件加密、逻辑名);`get_destinations` 永远包 offsite;`_verify_one` 改按 `meta.encrypted` 分支 | 
| `tests/test_backup_crypto.py` | 改若干受影响测试 + 加「口令删除后合并不重复」「无口令明文异地」两个测试 |
| `static/js/app.js`、`static/css/style.css` | 备份列表分页 |
| `docs/architecture.md`、`docs/superpowers/specs/2026-06-02-stockbook-backup-encryption-design.md` | 更新决策 #20 + spec 模型 + changelog |

---

## Task 1:逐文件加密 + 永远合并(后端重做)

**Files:**
- Modify: `app/backup.py`
- Modify: `tests/test_backup_crypto.py`

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_backup_crypto.py`;同时**替换**两个旧测试)

先**删除**这两个旧测试(行为已变)并用下面替换:`test_get_destinations_wraps_offsite_only_with_passphrase`、`test_offsite_without_passphrase_logs_warning`。

新增/替换为:
```python
def test_get_destinations_always_wraps_offsite(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "off"))
    monkeypatch.setattr(backup, "live_db_path", lambda: tmp_path / "live.db")
    for pw in ("pw", ""):           # wrapped whether or not a passphrase is set
        monkeypatch.setattr(config, "BACKUP_PASSPHRASE", pw)
        dests = backup.get_destinations()
        assert isinstance(dests[1], backup.EncryptedDestination)


def test_offsite_plaintext_without_passphrase_merges_and_flags(tmp_path):
    src = tmp_path / "live.db"; _make_sqlite(src, "v1")
    enc = backup.EncryptedDestination(backup.LocalDirDestination(tmp_path / "off", "offsite"), "")
    enc.store(src, _meta(src))
    # no passphrase → plaintext file under the LOGICAL name (no .enc), encrypted flag False
    assert (tmp_path / "off" / "stockbook-x.db").exists()
    assert not (tmp_path / "off" / "stockbook-x.db.enc").exists()
    metas = enc.list()
    assert [m.name for m in metas] == ["stockbook-x.db"]
    assert metas[0].encrypted is False
    out = tmp_path / "r.db"; enc.fetch("stockbook-x.db", out)   # plaintext fetch = plain copy
    assert out.read_bytes() == src.read_bytes()


def test_passphrase_removed_still_merges_one_row(tmp_path, monkeypatch):
    # THE bug: set passphrase → encrypted offsite; remove passphrase → list must still
    # report the LOGICAL name so it merges with local (one row), not a .enc duplicate.
    live = tmp_path / "live.db"; _make_sqlite(live)
    monkeypatch.setattr(backup, "live_db_path", lambda: live)
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "off"))
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "pw")
    backup.make_backup(force=True)
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "")        # passphrase removed
    offsite_names = [m.name for m in backup.get_destinations()[1].list()]
    assert all(not n.endswith(".enc") for n in offsite_names)   # logical names → merges
    assert offsite_names and all(m.encrypted for m in backup.get_destinations()[1].list())


def test_encrypted_verify_no_passphrase_is_unavailable_not_mismatch(tmp_path, monkeypatch):
    live = tmp_path / "live.db"; _make_sqlite(live)
    monkeypatch.setattr(backup, "live_db_path", lambda: live)
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "off"))
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "pw")
    backup.make_backup(force=True)
    name = backup.get_destinations()[1].list()[0].name
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "")        # can't decrypt now
    res = backup._verify_one(backup.get_destinations()[1], name, allow_pull=False)
    assert res["status"] == "unavailable"                      # NOT a false mismatch
```

确认文件顶部已 `import logging`(本测试用到 caplog 的别处已有;若无则加)。`_make_sqlite`/`_meta`/`config` 已在文件顶部(前序任务已加)。

- [ ] **Step 2: 跑确认失败**

Run: `.venv/bin/pytest tests/test_backup_crypto.py -q`
Expected: FAIL(新行为未实现:`get_destinations` 没口令时还没包、`store` 没口令时还写 `.enc` 等)。

- [ ] **Step 3: 重写 `EncryptedDestination`(整类替换)**

把 `app/backup.py` 里现有的 `EncryptedDestination` 整个类替换为:

```python
class EncryptedDestination:
    """Wraps the offsite LocalDirDestination. Encryption is PER-BACKUP, decided by
    whether a passphrase is set at store time and recorded in meta.encrypted:
      - passphrase set → store Fernet ciphertext as '<name>.enc' (encrypted=True)
      - no passphrase  → store plaintext as '<name>'            (encrypted=False)
    list() always reports the LOGICAL '<name>' (strips '.enc'), so rows always merge
    with the local copy — no duplication regardless of passphrase state, and existing
    '.enc' files need no migration. fetch()/verify()/restore key off each backup's
    meta.encrypted; decryption targets caller-provided LOCAL paths and raises
    ValueError when an encrypted backup is read without a passphrase. enc.json holds
    salt/KDF params (atomic write)."""

    def __init__(self, inner: "LocalDirDestination", passphrase: str) -> None:
        self.inner = inner
        self.name = inner.name
        self._passphrase = passphrase

    def _logical(self, name: str) -> str:
        return name[:-4] if name.endswith(".enc") else name

    def _meta(self, name: str) -> Optional[BackupMeta]:
        return {m.name: m for m in self.list()}.get(name)

    def _phys(self, name: str) -> str:
        m = self._meta(name)
        return name + ".enc" if (m is not None and m.encrypted) else name

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
            tmp_params = params.with_suffix(".json.tmp")
            tmp_params.write_text(json.dumps(
                {"kdf": "scrypt", "salt": salt.hex(), "n": n, "r": r, "p": p}), "utf-8")
            tmp_params.replace(params)   # atomic on POSIX — never leave a partial enc.json
        key = base64.urlsafe_b64encode(
            Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(self._passphrase.encode()))
        return Fernet(key)

    def store(self, src: Path, meta: BackupMeta) -> None:
        from dataclasses import replace
        if self._passphrase:
            token = self._fernet().encrypt(Path(src).read_bytes())
            self.inner.dir.mkdir(parents=True, exist_ok=True)
            ct = self.inner.dir / f".enc-tmp-{os.getpid()}-{int(time.time()*1000)}"
            try:  # write inside try so a failed write never orphans a temp in the synced dir
                ct.write_bytes(token)
                self.inner.store(ct, replace(meta, name=meta.name + ".enc", encrypted=True))
            finally:
                if ct.exists():
                    ct.unlink()
        else:  # no passphrase → plaintext offsite (the UI shows 异地:已配置(未加密))
            logging.getLogger(__name__).warning(
                "offsite backup stored in PLAINTEXT (no STOCKBOOK_BACKUP_PASSPHRASE set)")
            self.inner.store(src, replace(meta, encrypted=False))

    def list(self) -> List[BackupMeta]:
        from dataclasses import replace
        return [replace(m, name=self._logical(m.name)) for m in self.inner.list()]

    def fetch(self, name: str, dest: Path) -> None:
        """Materialize backup `name` (logical) as plaintext into `dest` (a LOCAL path).
        Encrypted backups are decrypted (needs the passphrase); plaintext ones copied."""
        m = self._meta(name)
        if m is not None and m.encrypted:
            if not self._passphrase:
                raise ValueError("需要 STOCKBOOK_BACKUP_PASSPHRASE 才能解密该异地备份")
            ct = Path(str(dest) + ".ct")
            try:
                self.inner.fetch(name + ".enc", ct)
                Path(dest).write_bytes(self._fernet().decrypt(ct.read_bytes()))
            finally:
                if ct.exists():
                    ct.unlink()
        else:
            self.inner.fetch(name, dest)

    def prune(self, keep: int) -> List[str]:
        return [self._logical(n) for n in self.inner.prune(keep)]

    def path_of(self, name: str) -> Path:
        return self.inner.path_of(self._phys(name))

    def is_local(self, name: str) -> bool:
        return self.inner.is_local(self._phys(name))

    def ensure_materialized(self, name: str, timeout: float) -> bool:
        return self.inner.ensure_materialized(self._phys(name), timeout)
```

> 注:旧类有 `encrypted = True` 类属性,**删掉它**(加密现在是逐文件,不再是目标级)。先 `grep -n "getattr(.*\"encrypted\"\|\.encrypted" app/` 确认除 `meta.encrypted`/`m.encrypted`/`row\["encrypted"\]` 外没有别处读 `dest.encrypted`。

- [ ] **Step 4: `get_destinations()` 永远包 offsite**

```python
def get_destinations() -> List[BackupDestination]:
    backups_dir = live_db_path().parent / "backups"
    dests: List[BackupDestination] = [LocalDirDestination(backups_dir, "local")]
    if config.BACKUP_DIR:
        # Always wrap: encryption is per-backup (gated on the passphrase at store time),
        # and the wrapper's logical-name list() keeps rows merged with local.
        dests.append(EncryptedDestination(LocalDirDestination(Path(config.BACKUP_DIR), "offsite"),
                                          config.BACKUP_PASSPHRASE))
    return dests
```

- [ ] **Step 5: `_verify_one` 改按 `meta.encrypted` 分支(整函数替换)**

```python
def _verify_one(dest: BackupDestination, name: str, *, allow_pull: bool,
                timeout: float = _VERIFY_PULL_TIMEOUT_SECS) -> dict:
    """Verify one backup. status ∈ {ok, mismatch, unavailable}. Branches on the
    backup's own meta.encrypted (per-file), not the destination type."""
    meta = {m.name: m for m in dest.list()}.get(name)
    if meta is None or not meta.sha256:
        return {"file": name, "destination": dest.name,
                "status": "unavailable", "reason": "无 manifest 记录"}
    materialized = dest.ensure_materialized(name, timeout) if allow_pull else dest.is_local(name)
    if not materialized:
        return {"file": name, "destination": dest.name,
                "status": "unavailable", "reason": "文件未物化(离线/驱逐)"}
    if meta.encrypted:
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

- [ ] **Step 6: `_verify_encrypted` 增加「没口令→unavailable」分支(整函数替换)**

```python
def _verify_encrypted(dest: BackupDestination, name: str, meta: BackupMeta) -> dict:
    """Verify an encrypted backup by decrypting to a LOCAL temp then checking the
    plaintext. No passphrase → unavailable; tamper/wrong-key (InvalidToken) →
    mismatch; transient read error → unavailable. Plaintext temp is local + deleted."""
    import tempfile
    from cryptography.fernet import InvalidToken
    fd, tmp_name = tempfile.mkstemp(prefix="sb-verify-", suffix=".db")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        try:
            dest.fetch(name, tmp)
        except ValueError as e:   # encrypted backup but no passphrase configured
            return {"file": name, "destination": dest.name,
                    "status": "unavailable", "reason": str(e)}
        except InvalidToken:
            return {"file": name, "destination": dest.name, "status": "mismatch",
                    "reason": "解密失败:口令错误或文件损坏"}
        except OSError as e:      # transient read/fetch error ≠ corruption
            return {"file": name, "destination": dest.name, "status": "unavailable",
                    "reason": "无法读取备份:%s" % e}
        ok = (tmp.exists() and tmp.stat().st_size == meta.size
              and file_sha256(tmp) == meta.sha256 and _sqlite_integrity_check(tmp))
        return {"file": name, "destination": dest.name,
                "status": "ok" if ok else "mismatch",
                "reason": "" if ok else "解密后大小/哈希/完整性不符"}
    finally:
        if tmp.exists():
            tmp.unlink()
```

> `restore_backup` 不需要改:它的 `src_dest.fetch(...)` 现在对加密备份会解密、没口令时抛 `ValueError`(端点已映射 400),且 commit `271cb3a` 加的 artifact integrity-gate 兜底;`InvalidToken`(错口令)路径仍在。确认 `restore_backup` 里仍有 `except InvalidToken` 与 integrity-gate;`ValueError` 会原样冒泡到端点 → 400。

- [ ] **Step 7: 跑测试 + 全套**

Run: `.venv/bin/pytest tests/test_backup_crypto.py tests/test_api.py -q && .venv/bin/pytest -q`
Expected: 受影响测试改后全绿;新增 4 个测试过;旧加密测试(`test_encrypted_store_writes_ciphertext_enc_file`/`roundtrip`/`tamper_wrongkey`/`restore_*`/`read_error`)仍绿(有口令路径不变)。全套 = 141 − 2(删) + 4(新)= 143 passed。报准确数。
若某旧测试因「现在 list 用逻辑名」而断言 `.enc` 名失败,核对该测试断言并按新行为修正(密文文件仍是 `.enc`,但 `list()` 报逻辑名)。

- [ ] **Step 8: Stage(不提交)**

`git add app/backup.py tests/test_backup_crypto.py`;`git --no-pager diff --cached --stat` 确认仅这两个文件;报告。

- [ ] **Step 9: Commit(Task 1)**

```bash
git commit -m "refactor(backup): per-file encryption + always-merge offsite (fix passphrase-removed dup)

Offsite is always wrapped in EncryptedDestination; encryption is per-backup
(passphrase set → <name>.enc ciphertext/encrypted=True, else <name> plaintext/
encrypted=False). list() always reports the logical name so a backup never shows
twice — fixes the duplicate rows after a passphrase is removed, with no migration
of existing .enc files. verify/restore branch on each backup's meta.encrypted;
encrypted-without-passphrase → unavailable (verify) / clean 400 (restore).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2:备份列表分页(前端)

**Files:**
- Modify: `static/js/app.js`、`static/css/style.css`

- [ ] **Step 1: 读现有备份列表渲染**

Run: `grep -n "renderBackupList\|verifyBackups\|_bkSummary\|doRestore\|_bkVerified" static/js/app.js`
读 `renderBackupList`、`verifyBackups`。下面把 `renderBackupList` 拆成「取数据 + 渲染当前页」,新增分页器。

- [ ] **Step 2: 替换 `renderBackupList`,新增 `_renderBackupPage`**

把现有 `async function renderBackupList() {...}` 整体替换为:

```javascript
const BK_PAGE_SIZE = 8;
let _bkAll = [];     // full backup list (fetched once)
let _bkPage = 0;     // current page index

async function renderBackupList() {
  const box = byId("bk-list");
  try {
    _bkAll = await api("GET", "/api/backups");
    _bkPage = 0;
    _renderBackupPage();
  } catch (e) { box.innerHTML = `<div class="bk-empty">${e.message}</div>`; }
}

function _renderBackupPage() {
  const box = byId("bk-list");
  const list = _bkAll;
  if (!list.length) { box.innerHTML = `<div class="bk-empty">还没有备份。点「立即备份」创建一个。</div>`; return; }
  const pages = Math.ceil(list.length / BK_PAGE_SIZE);
  _bkPage = Math.min(Math.max(_bkPage, 0), pages - 1);
  const slice = list.slice(_bkPage * BK_PAGE_SIZE, (_bkPage + 1) * BK_PAGE_SIZE);
  const rows = slice.map(b => {
    const dests = b.destinations || [];
    const hasOffsite = dests.includes("offsite");
    const restoreBtn = hasOffsite
      ? `<select class="mini-btn" data-dest-sel="${b.file}" style="padding:4px 6px">
           <option value="local">local</option>
           <option value="offsite">offsite</option>
         </select>
         <button class="mini-btn primary" data-restore="${b.file}">恢复</button>`
      : `<button class="mini-btn" data-restore="${b.file}">恢复</button>`;
    const lockBadge = b.encrypted ? '<span class="bk-lock" title="异地副本已加密">🔒</span>' : '';
    return `<div class="bk-row">
      <div>
        <div class="bk-file">${b.file}${lockBadge}</div>
        <div class="bk-meta">${b.modified.slice(0, 19).replace("T", " ")} · ${(b.size / 1024).toFixed(0)} KB
          &ensp;${_destBadges(dests)}&ensp;${_bkBadge(b.file)}</div>
      </div>
      <div class="bk-row-right">${restoreBtn}</div>
    </div>`;
  }).join("");
  const pager = pages > 1
    ? `<div class="bk-pager">
         <button class="mini-btn" data-bk-prev ${_bkPage === 0 ? "disabled" : ""}>← 上一页</button>
         <span class="bk-pageno">${_bkPage + 1} / ${pages}</span>
         <button class="mini-btn" data-bk-next ${_bkPage >= pages - 1 ? "disabled" : ""}>下一页 →</button>
       </div>`
    : "";
  box.innerHTML = _bkSummary(list) + rows + pager;
  box.querySelectorAll("[data-restore]").forEach(btn =>
    btn.addEventListener("click", () => {
      const file = btn.dataset.restore;
      const sel = box.querySelector(`[data-dest-sel="${file}"]`);
      doRestore(file, sel ? sel.value : "local");
    }));
  const prev = box.querySelector("[data-bk-prev]");
  if (prev) prev.addEventListener("click", () => { _bkPage--; _renderBackupPage(); });
  const next = box.querySelector("[data-bk-next]");
  if (next) next.addEventListener("click", () => { _bkPage++; _renderBackupPage(); });
}
```

- [ ] **Step 3: `verifyBackups` 重渲染当前页(不重置分页)**

在 `verifyBackups()` 末尾,把对 `renderBackupList()` 的调用改成 `_renderBackupPage()`(保留当前页 + 已取到的 `_bkAll`,只刷新徽标)。`grep` 找到 `verifyBackups` 里调用渲染的那行替换;若它调用的是别的渲染函数名,按实际改为 `_renderBackupPage()`。

- [ ] **Step 4: CSS 分页器**

`static/css/style.css` 加(复用现有变量,放在 `.bk-*` 区域附近):
```css
.bk-pager{display:flex;align-items:center;justify-content:center;gap:12px;margin-top:10px}
.bk-pageno{color:var(--ink-3);font-size:13px}
.bk-pager .mini-btn[disabled]{opacity:.4;cursor:default}
```

- [ ] **Step 5: 起服务手点(临时库/目录/口令,绝不碰真实库)**

```
STOCKBOOK_DATABASE_URL=sqlite:////tmp/sb_pg_$$.db STOCKBOOK_BACKUP_DIR=/tmp/sb_pg_off_$$ STOCKBOOK_BACKUP_PASSPHRASE=demo \
  .venv/bin/python -m uvicorn main:app --port 8782 --log-level warning &
```
连点几次「立即备份」造出 >8 条 → 打开备份弹窗,确认出现「上一页/下一页 · x/y」、翻页正常、每备份一行(不重复)、🔒 在加密行。`node --check static/js/app.js`。完事 `pkill -f "uvicorn main:app --port 8782"`,清 `/tmp/sb_pg_*`。

- [ ] **Step 6: 全套仍绿**

Run: `.venv/bin/pytest -q` → 143 passed(前端改动不影响后端)。

- [ ] **Step 7: Commit(Task 2)**

```bash
git add static/js/app.js static/css/style.css
git commit -m "feat(backup): paginate the backup list (8/page, prev/next)

The list could overflow the screen once backups accumulate. Render one page at a
time with a 上一页/下一页 · x/y pager; verify re-renders the current page.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3:文档同步(更新模型)

**Files:**
- Modify: `docs/superpowers/specs/2026-06-02-stockbook-backup-encryption-design.md`
- Modify: `docs/architecture.md`

- [ ] **Step 1: spec 加一节「v2 修订:逐文件加密 + 永远合并」**

在该 spec 末尾追加:
```markdown
## 13. v2 修订(2026-06-02):逐文件加密 + 永远合并显示

实测发现:口令删除后,异地退化成普通目标、`.enc` 物理名暴露,导致同一备份在列表里显示成两行(本地 `.db` + 异地 `.db.enc`)。修订模型:
- **`EncryptedDestination` 永远包住 offsite**(只要配了 `BACKUP_DIR`,不再看有没有口令)。
- **加密改为逐备份**:有口令存 `<名>.db.enc`(`encrypted=true`),没口令存 `<名>.db`(`encrypted=false`)。两种都是一次成功的异地备份(「有没有口令都能备」)。
- **`list()` 永远报逻辑名**(剥 `.enc`)→ 永远和本地同名 → 合并一行;**现有 `.enc` 文件无需迁移**。
- **`verify`/`restore` 按每条备份的 `meta.encrypted` 决定是否解密**(不再看目标类型):加密 + 没口令 → `verify=unavailable` / `restore` 抛 `ValueError`→400;加密 + 错口令 → `mismatch` / `InvalidToken`→400;明文备份照常。
- 异地目录可同时含 `.db`(某次无口令)与 `.db.enc`(某次有口令),各按标记处理。
```

- [ ] **Step 2: `docs/architecture.md` 决策 #20 末尾补一句**

在决策 #20 文字末尾追加:
```markdown
 **(v2 修订)** offsite 永远包加密层、加密改为**逐备份**(有口令→`.enc` 密文、无口令→明文,均记在 `meta.encrypted`),`list()` 永远报逻辑名 → 同一备份永远合并一行(修口令删除后的重复显示,现有 `.enc` 免迁移);`verify`/`restore` 按逐文件 `meta.encrypted` 解密,加密+无口令→unavailable/400。
```

- [ ] **Step 3: `docs/architecture.md` 功能日志加一行**

```markdown
- **2026-06-02** 备份加密 v2:offsite 永远包加密层、加密逐备份(有口令 `.enc`、无口令明文,记 `meta.encrypted`),`list()` 报逻辑名永远合并一行(修口令删除后重复、现有 `.enc` 免迁移);verify/restore 按逐文件标记解密;备份列表加分页。计划见 `docs/superpowers/plans/2026-06-02-backup-encryption-v2.md`。
```

- [ ] **Step 4: 全套确认仍绿**

Run: `.venv/bin/pytest -q` → 143 passed。

- [ ] **Step 5: Commit(Task 3)**

```bash
git add docs/architecture.md docs/superpowers/specs/2026-06-02-stockbook-backup-encryption-design.md
git commit -m "docs: record backup encryption v2 (per-file encryption, always-merge, pagination)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 收尾

- `.venv/bin/pytest -q` 全绿(143)。
- 重复显示消失、有没有口令都能备、分页可用 = 三项验收点。
- 用 `superpowers:finishing-a-development-branch` 收尾。

## Self-Review 结果(写计划时已核对)

- **覆盖**:永远包(Task1 get_destinations)、逐文件加密 store(Task1)、逻辑名合并 list(Task1)、verify/restore 按 meta.encrypted(Task1)、没口令→unavailable/400(Task1 + 既有端点)、免迁移(逻辑名 list 对现有 `.enc` 同样剥名)、分页(Task2)、文档(Task3)。全覆盖用户三诉求(有无口令都备 / 下拉恢复已存在 / 错口令报错)+ 修重复 + 分页。
- **免迁移验证**:现有 `.enc`(manifest 键 `X.db.enc`)→ 新 `list()` 剥 `.enc` → 逻辑名 → 合并;`fetch(逻辑名)` 经 `_phys` 查 `meta.encrypted=True` → 映射回 `.enc`。无需改盘上文件。
- **明文不落同步盘**:解密目标仍是本地 tempfile / 本地 artifact;新增的明文 store 是「本来就没口令、用户接受的明文异地」。
- **命名一致**:`EncryptedDestination`/`_logical`/`_phys`/`_meta`/`meta.encrypted`/`_verify_encrypted` 全程一致;tri-state 字面量统一。
- **Python 3.9**:`Optional/List/Dict`,无 `X | None`。
- **类属性清理**:删 `EncryptedDestination.encrypted` 类属性(加密改逐文件),Task1 Step3 已 grep 守卫。
