# 备份加密(只加密异地)设计

> 接续《备份加固》(`docs/superpowers/specs/2026-06-01-stockbook-backup-hardening-design.md` §9 把加密列为「留作将来的 wrapper destination 扩展点」)。本设计实现那个扩展点:**给离机的异地备份加密**,本地备份保持明文不变。
>
> 落地后一并更新 `docs/architecture.md`(关键决策 / API 一览 / 功能日志)。

## 1. 背景与动机

备份加固已让异地副本(写进 iCloud/坚果云等同步盘目录)实现「自动 + 可校验」,但**异地是明文** —— 持仓/成本/盈亏一旦同步上云就暴露面变大。本设计在不动本地备份、不引入账号/KMS 的前提下,给**离机的那份**加密。

**为什么只加密异地(范围已定)**:本地 `backups/` 在你自己的盘上,明文可接受;真正离开你掌控的是异地副本。**而且这带来一个关键的安全冗余:本地始终是明文可恢复的备份 —— 即便加密口令丢了,你最多失去异地副本,本地仍能恢复,不会因忘口令而丢掉全部备份。** 这正是 offsite-only 加密优于「全加密」的地方。

## 2. 目标 / 非目标

**目标**
- 异地备份以认证加密(Fernet)落盘;同步盘里只有密文。
- 口令走 `.env`(`STOCKBOOK_BACKUP_PASSPHRASE`),scrypt 派生密钥;零账号、零 KMS、跨平台。
- 异地文件夹**自描述**:`.enc` 密文 + manifest + KDF 参数(salt),换台机器带上口令即可解/恢复。
- verify 对加密备份**解密后再校验**(Fernet 认证 + 明文 `integrity_check`),且**口令问题不误判为数据损坏**。
- 恢复支持从加密异地取回(解密→写回);口令错/缺则明确中止,绝不半恢复。
- 自动启用:设了口令就加密异地;没设则异地明文 + 告警(YAGNI,不加额外开关)。

**非目标(本轮)**
- 加密本地备份(本地保持明文,见 §1 论点)。
- 密钥轮转 / 改口令后重加密旧备份;多口令;非对称/共享密钥。
- 严格模式(「没口令就拒绝写明文异地」)—— 仅日志告警,留作将来一个布尔开关。
- 隐藏文件名/大小等元数据(`.enc` 文件名、size 仍可见;只加密内容)。

## 3. 架构:`EncryptedDestination` 包装层

```
get_destinations():
  [ LocalDirDestination(<db>/backups, "local")              ← 永远明文,verify/恢复原样
    + (若 BACKUP_DIR 配了)
        EncryptedDestination(                               ← 设了口令才套这层
            LocalDirDestination(BACKUP_DIR, "offsite"),
            passphrase)
        或 LocalDirDestination(BACKUP_DIR, "offsite")       ← 没口令:明文 + 告警
  ]
```

`EncryptedDestination` 是包在 offsite `LocalDirDestination` 外面的**装饰器**(spec §9 的扩展点),实现同一个 `BackupDestination` Protocol:
- `name` = 内层名(`"offsite"`);新增只读属性 `encrypted = True`(verify/restore 据此分支)。
- `store(src, meta)`:把明文 `src` 用 Fernet 加密成临时密文 → 以 `<meta.name>.enc` 写入内层目录;manifest 记录 `meta`(**`sha256` 仍是明文哈希**,与 local 统一)外加 `encrypted: true`。
- `fetch(name, dest)`:内层取 `<name>.enc` 密文 → 解密 → 写明文到 `dest`。**这就是"产出明文"的统一入口**(verify 与 restore 都复用它)。
- `prune` / `is_local` / `ensure_materialized` / `list`:透传内层(对 `.enc` 文件名做映射)。
- `path_of(name)`:返回内层 `<name>.enc` 路径(密文,用于 size/存在性检查)。

> 本地目标零改动:它的 `store/fetch/verify/restore` 路径完全不经过加密层。

## 4. 加密原语与密钥

新增依赖:`cryptography`(跨平台,发布预编译 wheel,不绑 OS)。

- **KDF**:`Scrypt(salt, n=2**14, r=8, p=1, length=32)` 从口令派生 32 字节 → `base64.urlsafe_b64encode` → Fernet key。
- **salt**:每个异地目录**首次加密时随机生成 16 字节**,连同 KDF 参数存进该目录的 `enc.json`(`{"kdf":"scrypt","salt":"<hex>","n":16384,"r":8,"p":1}`)。salt 非机密,可明文存;它让异地文件夹自描述。
- **加密**:`Fernet(key).encrypt(plaintext_bytes)` —— AES-128-CBC + HMAC-SHA256 认证,随机 IV 内建。**整文件载入内存**(库 ~4MB,可接受;`.db` 远大时再考虑流式,本轮 YAGNI;在文档注明此限制)。
- **认证语义**:Fernet 的 MAC 使「密文被改 / 用错 key」都触发 `InvalidToken`(二者不可区分 —— 这是 Fernet 设计使然,见 §6 verify 如何处理)。

## 5. 配置

| 环境变量 | config 属性 | 默认 | 含义 |
|---|---|---|---|
| `STOCKBOOK_BACKUP_PASSPHRASE` | `BACKUP_PASSPHRASE` | 空 | 异地加密口令。**设了 → 异地自动加密;空 → 异地明文 + 启动/备份时告警一次**。 |

口令只走 `.env`(已 gitignore),绝不入代码/库/前端/日志(日志只说「已加密/未加密」,不打印口令)。

## 6. verify:加密备份怎么验(tri-state 不变,语义加强)

`_verify_one` 对 `getattr(dest, "encrypted", False)` 分支:

**明文目标(local)**:维持现状(`path_of` 上 `sha256==meta.sha256 && integrity_check`)。

**加密目标(offsite)**:
1. 未物化(`is_local`/`ensure_materialized` 失败)→ `unavailable`(同现状)。
2. **没配口令** → `unavailable`,reason=「未配置口令,无法校验加密备份」(钥匙问题,不是损坏)。
3. 有口令 → 把 `<name>.enc` 解密到临时明文:
   - `InvalidToken`(密文被改 **或** 口令错)→ `mismatch`,reason=「解密失败:口令错误或文件损坏」(二者 Fernet 不可分,但**两种都意味着这份当前不可用**,如实标问题,而非假绿)。
   - 解密成功 → 临时明文上跑 `sha256==meta.sha256 && integrity_check`:
     - 都过 → `ok`;否则 → `mismatch`。
   - 临时明文用完即删。

> 净效果:加密让 verify **更强**(带认证);唯一的诚实让步是「口令错」与「密文损坏」在有口令时会都归 `mismatch`(都得换一份恢复点,处理方式一致)。「没口令」则单独归 `unavailable`(钥匙问题)。

## 7. 恢复:从加密异地取回

`restore_backup(name, destination="offsite")`:
- 若目标是加密目标且**没配口令** → 抛错「需要 STOCKBOOK_BACKUP_PASSPHRASE 才能从加密异地恢复」,中止(不动 live 库)。
- `fetch`(内含解密)到临时明文:
  - `InvalidToken` → 抛错「解密失败:口令错误或备份损坏」,中止(live 库未动,可逆性不受影响)。
  - 成功 → 沿用现有流程(先快照当前→`engine.dispose()`→`_sqlite_restore`→`create_schema`)。

## 8. 元数据与磁盘布局

异地目录(同步盘):
```
enc.json                       # KDF 参数 + salt(非机密)
manifest.json                  # {name: {…meta…, encrypted: true}}  (sha256=明文哈希)
stockbook-<ts>.db.enc          # Fernet 密文
…
```
- `meta.sha256` = **明文** SHA-256(与 local 统一;解密后校验)。
- `BackupMeta` 新增字段 `encrypted: bool = False`。`make_backup` 把**同一个** meta 传给所有目标;`EncryptedDestination.store` 用 `dataclasses.replace(meta, encrypted=True)` 写自己的 manifest 条目(**不就地改动共享 meta**,local 仍记 false)。`GET /api/backups` 行增 `encrypted` 字段。
- 改口令后旧 `.enc` 解不开(key 变)→ 本轮不支持轮转,文档注明「改口令前先用旧口令把异地恢复/迁出」。

## 9. 前端(小改动)

- 异地状态行:`异地:已加密 🔒 → <dir>` / `已配置(未加密)⚠ → <dir>` / `未配置`。
- 备份行若 `encrypted` 为真,加一把小锁标记。
- verify 结果 `mismatch` 且 reason 含「解密失败」时,提示语点明「口令错误或文件损坏」,引导先确认 `.env` 口令再判定损坏。
- 不在前端收集/显示口令。

## 10. 测试

`tests/test_backup_crypto.py`(新),临时目录 + 临时口令,**不碰真实库/真钥匙**:
- 加解密往返:加密 `store` → `fetch` 出来的明文与原文逐字节相等。
- 错口令:用 A 口令加密、B 口令 `fetch` → `InvalidToken`;`restore_backup` 据此**中止且 live 未动**;`_verify_one` 报 `mismatch(解密失败)`。
- 没口令:加密目标 + 无口令 `_verify_one` → `unavailable(未配置口令)`;`restore_backup` → 抛明确错。
- 篡改密文:改 `.enc` 一字节 → `_verify_one` → `mismatch`。
- 完好:正确口令 → `_verify_one` → `ok`;`integrity_check` 在解密明文上跑。
- 配了 offsite 但没口令 → 异地写**明文** + 告警(断言文件非密文 / 日志含告警)。
- salt 持久化:`enc.json` 生成一次,二次加密复用同一 salt/key(同口令解得开)。
- `get_destinations()`:设口令 → offsite 是 `EncryptedDestination`;不设 → 是 `LocalDirDestination`。
- 端到端(TestClient + 临时库 + 临时 `BACKUP_DIR`/口令):`POST /api/backup` 写出 `.enc`,`POST /api/backup/verify` 报 ok,`POST /api/restore destination=offsite` 成功。

覆盖率:加密模块归「外围只报告不卡」一档(与备份同档),但加解密/派生/分支等纯逻辑测透。

## 11. 关键决策小结(待并入 architecture.md)

1. **只加密异地、本地留明文** —— 本地明文是「忘口令也能恢复」的安全冗余;离机副本才需加密。
2. **加密 = offsite 目标外的 `EncryptedDestination` 装饰器**(spec §9 扩展点落地),本地路径零改动;复用 `fetch` 作为"产出明文"统一入口(verify/restore 共用)。
3. **Fernet(认证加密)+ scrypt 派生**,口令走 `.env`;salt + KDF 参数存异地目录 `enc.json` → 文件夹自描述、跨平台、带上口令即可解。
4. **verify 解密后再校验**:Fernet 认证使「篡改/错口令」→ `mismatch`,「没口令」→ `unavailable`;加密让校验更强,且口令问题不假报损坏。
5. **设口令即加密,无额外开关**(YAGNI);没口令配了异地 → 明文 + 告警(不静默)。
6. 加密是**目标层**关注点,与 `_sqlite_*` / 将来 `BackupSource` 接缝正交。

## 12. 行为兼容

- 不设 `STOCKBOOK_BACKUP_PASSPHRASE` → 行为与现状完全一致(异地明文,或无异地)。
- 既有本地备份/verify/restore 路径一字不改。
- `GET /api/backups` 仅**新增** `encrypted` 字段(向后兼容)。
- 新依赖 `cryptography` 进 `requirements.txt`(预编译 wheel,`pip install` 即得,不破坏「他机最小化复现」)。
