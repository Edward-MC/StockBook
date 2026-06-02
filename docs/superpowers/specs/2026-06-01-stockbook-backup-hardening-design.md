# 数据安全:备份加固(自动化 / 校验 / 异地)设计

> 个人财务数据,「用着不放心」的根源是当前备份**和数据同盘、明文、无校验、无自动、无轮转**。本设计在不改变单文件可打包、零运维定位的前提下,把备份升级为**自动化 + 可校验 + 异地**的三位一体。
>
> 设计源头讨论见对话;落地后一并更新 `docs/architecture.md`(关键决策 / API 一览 / 功能日志)。

## 1. 背景与现状

当前实现(`app/routers/api.py:503–589`,架构决策 #10):
- 用 SQLite 在线备份 API `sqlite3.Connection.backup()` 把库页级一致地快照成 `<db目录>/backups/stockbook-<时间戳>.db`。
- 触发点:手动 `POST /api/backup`;恢复/重置前自动各备一份。
- 恢复:先快照当前态(可逆)→ 释放连接 → 页级写回 → `create_schema()` 补迁移。

**优点**(保留):页级一致(扛并发写/WAL,优于裸 `cp`)、恢复可逆、防路径穿越。

**不安全之处**(本设计要解决的,按用户排序的优先级):
1. **同盘同机**——备份就在项目目录的 `backups/`。磁盘坏 / 电脑丢 / 误删 / 勒索 → live 库与所有备份一起没。违反 3-2-1 原则。**(最高优先)**
2. **全靠手动**——无定时,依赖人记得点。**(高优先)**
3. **无完整性校验**——无 checksum、无 `integrity_check`,备份悄悄损坏不可知,恢复时才暴露。**(高优先)**
4. **无保留/轮转**——备份无限堆积。
5. (明文存储——本轮**不做**加密,见 §9 YAGNI;接口留好扩展点。)

## 2. 目标 / 非目标

**目标**
- 异地容灾:备份除本地外,再写一份到「会被网盘同步的目录」,使副本离开本机本盘。
- 自动化:进程内调度,启动即备 + 运行期每 12 小时备 + 退出 best-effort 备。
- 完整性:每份备份算 SHA-256 + 跑 `PRAGMA integrity_check`,记入 manifest;提供不经恢复的 verify。
- 变更检测:库未变则跳过备份,避免堆重复。
- 保留轮转:每目标保留最新 30 份,超出删最老。
- 可扩展:目标后端走 `Protocol`,将来 S3/rsync/加密 = 加实现类,调用方不改(复用子项目 B 模式)。

**非目标(本轮)**
- 加密(at-rest / 上传前);S3/rsync 等远端实现;GFS 分层保留;KMS;多机一致性。均留扩展点,不实现。

## 3. 架构总览

```
routers/api.py (薄壳端点)
        │  调用
        ▼
app/backup.py  ── 编排 + 纯逻辑(可单测,框架无关)
   ├─ snapshot(src, dest)              页级一致快照(沿用现 _sqlite_snapshot)
   ├─ file_sha256 / integrity_check    完整性
   ├─ make_backup(db) -> BackupResult  编排:快照→校验→变更检测→写各目标→轮转→manifest
   ├─ verify(name)                     重哈希 + 重 integrity_check,比对 manifest
   ├─ BackupDestination (Protocol)     目标后端接口
   │     └─ LocalDirDestination(path)  唯一实现:本地 backups/ + 同步盘目录各一个实例
   ├─ get_destinations()              选择器:主目标 + 可选异地目标
   └─ run_cycle()                      一轮完整备份(供调度器 / CLI 复用)

app/scheduler.py(或并入 main lifespan) 进程内守护任务,周期调 run_cycle()
python -m app.backup                    CLI:跑一轮即退(给 cron / 运维 / 测试)
```

**模块拆分理由**:当前备份逻辑内联在路由里,既难测又与框架耦合。抽出 `app/backup.py` 对齐项目既有 `calc.py`(纯逻辑)/`services.py`(胶水)的分层,是「改到的代码顺手改好」的针对性改进,不做无关重构。

## 4. `BackupDestination` 接口与选择器

```python
from typing import List, Optional, Protocol

class BackupMeta:   # dataclass
    name: str            # 文件名 stockbook-YYYYmmdd-HHMMSS.db
    sha256: str          # 备份文件字节哈希(完整性)
    size: int
    created_at: str      # ISO
    source_hash: str     # 备份时 live 库的字节哈希(变更检测)
    integrity_ok: bool   # PRAGMA integrity_check == "ok"

class BackupDestination(Protocol):
    name: str
    def store(self, src: Path, meta: BackupMeta) -> None: ...   # 拷贝文件 + 更新本目标 manifest
    def list(self) -> List[BackupMeta]: ...                     # 读 manifest
    def fetch(self, name: str, dest: Path) -> None: ...         # 取回一份(供从异地恢复)
    def prune(self, keep: int) -> List[str]: ...                # 删超出 keep 的最老者,返回被删名单
```

**实现 `LocalDirDestination(path, name)`**:
- `store`:把快照文件拷进 `path`,把 `meta` 并入 `path/manifest.json`(JSON,filename→meta)。
- `list`:读 `path/manifest.json`;文件缺失/manifest 无记录时容错(列出磁盘上的 `.db`,标 `integrity_ok=None`)。
- `prune`:按 `created_at` 排序,保留最新 `keep`,删其余文件 + manifest 条目。
- 复用两次:`LocalDirDestination(<db目录>/backups, "local")`(永远在)+ `LocalDirDestination(STOCKBOOK_BACKUP_DIR, "offsite")`(配了才有)。「异地性」来自 `STOCKBOOK_BACKUP_DIR` 指向 iCloud/坚果云等被同步的目录。

**`get_destinations()`**:返回 `[local]`,若 `config.BACKUP_DIR` 非空则追加 `offsite`。对齐 `get_embedder()`/`get_retriever()` 选择器,单一改动点。

> manifest 用**目标目录下的 JSON 文件**,不是数据库表——保持单文件库可打包,且 manifest 跟着备份目录走(异地目录自带自己的 manifest,自洽)。

## 5. 编排:`make_backup` / `run_cycle`

`make_backup(db, *, force=False) -> BackupResult`:
1. 解析 live 库路径,算 `source_hash`(live 文件 SHA-256)。
2. **变更检测**:若 `not force` 且 `source_hash == 主目标最新一份的 source_hash` → 跳过,返回 `skipped`。
3. 快照到临时文件 → 算 `sha256` + `integrity_check`;`integrity_ok` 为假则**报错不写**(不产出坏备份)。
4. 组装 `BackupMeta` → 对 `get_destinations()` 每个目标 `store`。
5. 对每目标 `prune(config.BACKUP_KEEP)`。
6. verify 每目标刚写的这份(读回比对 sha256 + 再跑 integrity_check)。
7. 返回 `BackupResult{written: [...目标], skipped, verified, pruned: [...]}`。

`run_cycle()`:`make_backup` 的无 db-session 包装(自己开一次性连接),供调度器与 CLI 复用,异常吞掉只记日志(备份失败绝不拖垮主流程)。

**变更检测的取舍**:用 live 文件字节哈希判「是否变化」。被动使用下,无写入则文件字节不变 → 跳过;有写入则变 → 备。VACUUM 等物理重排会误判为「变化」(顶多多备一份,不漏备),可接受;不引入持久化 data_version 的复杂度。

## 6. 调度(方案 A:进程内)

- FastAPI **lifespan** 启动守护任务:
  - 启动后延迟少许 → `run_cycle()` 一次。
  - 之后每 `config.BACKUP_INTERVAL_HOURS`(默认 **12**,设 `0` 禁用自动)→ `run_cycle()`。
  - 阻塞的 sqlite 操作走 `run_in_threadpool`,不堵事件循环。
  - `config.READONLY`(分享模式)下**不**自动备(只读分享不该往磁盘写)。
  - 关闭事件:best-effort 再 `run_cycle()` 一次。
- **CLI `python -m app.backup`**:跑一轮 `run_cycle()` 即退,打印 `BackupResult`。给想用 OS cron 的高级用户自接,也方便手动/CI 触发。

## 7. API 与恢复

保留并改为薄壳(行为不变):
- `POST /api/backup` → `make_backup(force=True)`(手动即强制)。
- `GET /api/backups` → 增强:聚合各目标 `list()`,每份带 `integrity_ok`、`destinations:[local,offsite]`、`verified`。
- `POST /api/restore` → 支持 `destination` 参数(默认 local);从该目标 `fetch` 到临时 → 先快照当前(可逆)→ 释放连接 → 页级写回 → `create_schema()`。
- `POST /api/reset` → 不变(仍先 best-effort 备)。

新增:
- `POST /api/backup/verify`(可带 `?file=` / `?destination=`,默认校验各目标最新一份)→ 重哈希 + 重 `integrity_check`,比对 manifest,返回 `{file, ok, reason}`。

## 8. 前端(小改动)

复用现有「备份/恢复」UI(架构 #10/#11):
- 备份列表每行加状态徽标:`✓ 已校验` / `⚠ 不一致` / `… 未校验`。
- 顶部显示「上次自动备份时间」「异地:已配置/未配置(指向 …)」。
- 恢复弹窗可选来源目标(本地 / 异地)。
- 一个「立即校验」按钮 → `POST /api/backup/verify`。

## 9. 配置(`.env` / `config.py`)

| 环境变量 | config 属性 | 默认 | 含义 |
|---|---|---|---|
| `STOCKBOOK_BACKUP_DIR` | `BACKUP_DIR` | 空 | 异地/同步盘目录;空=只写本地主目标 |
| `STOCKBOOK_BACKUP_INTERVAL_HOURS` | `BACKUP_INTERVAL_HOURS` | `12` | 自动备份间隔;`0` 禁用自动 |
| `STOCKBOOK_BACKUP_KEEP` | `BACKUP_KEEP` | `30` | 每目标保留最新份数 |

## 10. 测试

`app/backup.py` 纯逻辑用 `tmp_path` + 临时 SQLite,零网络:
- 快照页级一致(写库→备份→读回值相等)。
- `file_sha256` 正确;`integrity_check`:好库返回 ok、**故意改坏**的文件被判坏且 `make_backup` 拒绝产出。
- 变更检测:连续两次无写入 → 第二次 `skipped`;有写入 → 不跳过。
- 轮转:写 35 份、`keep=30` → 留最新 30、删最老 5(按 `created_at`)。
- manifest 往返:`store` 后 `list` 读回 meta 一致;manifest 缺失时容错列盘。
- `FakeDestination`(内存)证接口缝:`make_backup` 写入**所有**目标 + 各自 `prune`(对齐子项目 B 的 fake 套路)。
- verify 端点:篡改某备份字节 → `POST /api/backup/verify` 报 `ok=false`。
- 调度:直接测 `run_cycle()` 一轮(不测定时器循环);CLI `python -m app.backup` 冒烟。

覆盖率:`backup.py` 含较多 I/O,归「外围只报告不卡」一档(与网络/模型代码同档),但纯部分(checksum/轮转选择/变更检测)仍要测透;核心硬线仍只压 `calc.py`+`services.py`。

## 11. 关键决策小结(待并入 architecture.md)

1. 备份**异地**靠「写一份到同步盘目录」——`BackupDestination` 接口下,异地只是又一个 `LocalDirDestination` 实例,路径在网盘里即得离机副本;零新依赖、零密钥。S3/rsync/加密留作新实现类。
2. 备份**自动化**用**进程内 lifespan 守护任务**(启动备 + 每 12h + 退出备),非 OS cron / APScheduler——契合单文件可打包、他机最小化复现;另暴露 `python -m app.backup` 给 cron 用户自接。
3. 备份**可信**靠 SHA-256 + `PRAGMA integrity_check` 写入 manifest + 不经恢复的 `verify`;坏备份在产出阶段即被拒。
4. **变更检测**(live 文件哈希)避免定时器堆重复;**计数式保留**(默认 30/目标)防无限堆积。
5. 备份逻辑从路由抽到 `app/backup.py`(纯逻辑/框架无关,对齐 calc/services 分层),manifest 用目录内 JSON 不入库,保持单文件可打包。

## 12. 行为兼容

- 现有 4 个端点签名保留;`GET /api/backups` 返回**新增字段**(向后兼容,前端渐进使用)。
- 现有备份文件(无 manifest 记录)被容错列出、标「未校验」,不报错。
- 不配 `STOCKBOOK_BACKUP_DIR` 时行为≈现状(只多了本地的校验/轮转/自动),老用户零感知升级。
