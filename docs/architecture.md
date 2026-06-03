# StockBook 架构与实现说明

> 本文记录技术选型、架构分层、关键决策与功能日志。**新增功能时一并更新本文档**(尤其是「关键决策」与「功能日志」两节)。
>
> 设计源头见 `docs/superpowers/specs/2026-05-29-stockbook-strategy-tracker-design.md`。

## 1. 定位
个人、单用户、本地优先的投资策略追踪器,围绕「大类资产配置 + 区间再平衡」。A 股优先,人民币计价。

## 2. 技术栈 / Infra
- **语言/运行时**:Python 3.9。
- **后端**:FastAPI(`0.128`)+ SQLAlchemy 2.0(声明式 `Mapped`)+ Pydantic v2 校验。
- **数据库**:SQLite 单文件(`stockbook.db`,项目根)。零运维、可打包给别人 `pip install -r requirements.txt && uvicorn main:app`。
- **前端**:Jinja2 渲染外壳 + 原生 JS(`fetch` 调 JSON API)+ 一套「衡」纸质感 CSS。**零 Node 构建**。
- **外部行情**:腾讯 `qt.gtimg.cn`(纯文本、免密钥、可批量),通过 `httpx` 拉取。
- **测试**:pytest + FastAPI TestClient(`tests/` 每个用例用 `tmp_path` 独立 SQLite,互不污染)。
- **入口**:应用对象定义在根 `main.py`(`app = FastAPI(...)`,便于 IDE/uvicorn 识别);`app/main.py` 是兼容垫片 `from main import app`。

运行 / 环境变量见 `README.md`。

## 3. 架构分层(`app/`)
| 模块 | 职责 |
|---|---|
| `config.py` | 环境变量配置(DB 路径、只读、隐藏金额、自动刷新) |
| `database.py` | SQLAlchemy engine / SessionLocal / Base / `get_db` 依赖 |
| `models.py` | 领域模型(Strategy / AssetClass / Security / Transaction / PriceQuote) |
| `calc.py` | **纯计算引擎**(无框架依赖):持仓、市值、占比、偏离、状态、再平衡、未分配池、均价、盈亏;**绩效纯函数**(`xirr`/`twr`/`max_drawdown`/`annualized_volatility`) |
| `services.py` | ORM ↔ calc 的胶水:把 DB 行映射成 calc 输入,组装仪表盘载荷;`apply_fetched_quotes`(行情写回 `PriceQuote`,刷新接口与快照共用) |
| `backup.py` | 备份引擎:快照/SHA-256/`integrity_check`、`BackupDestination` 接口(本地+同步盘异地)、编排/校验(tri-state)、进程内调度 + `python -m app.backup` CLI |
| `snapshot_service.py` | 历史净值:`run_snapshot`(先刷行情→`build_ledger`/`build_dashboard` 取总额/各大类市值→抓基准→按 date upsert)、`build_history`(区间过滤序列 + 调 calc 组装绩效)、进程内调度(仿 backup,独立 task) |
| `quotes.py` | 行情:代码→市场前缀映射、腾讯返回解析、交易时段判断(解析与网络分离,便于测试) |
| `schemas.py` | Pydantic 请求/响应模型 |
| `seed.py` | 建表、示例数据播种、轻量迁移(`ALTER TABLE`)、`reset_to_default` |
| `routers/api.py` | JSON API |
| `routers/pages.py` | 单页外壳渲染(`/`)+ `/entry` 重定向 |
| `routers/rag.py` | RAG 问答 API(总开关 / 只读 403 / 每日限流 三重护栏) |
| `rag/notion.py` | Notion 抓取 + block→纯文本 + 切块(网络与解析分离,便于测试) |
| `rag/embed.py` | 本地 fastembed 向量化(中文 BGE,ONNX,零成本,惰性加载) |
| `rag/store.py` | KnowledgeChunk 存取 + numpy 余弦检索 + `sync_source` 删旧重建 |
| `rag/snapshot.py` | 精简持仓快照(复用 `build_dashboard`),注入问答 prompt |
| `rag/ask.py` | prompt 组装(纯函数)+ Claude 调用(缓存 system 块) |
| `rag/limiter.py` | 进程内每日调用计数(成本护栏) |
| `templates/` | `base.html`(外壳)+ `index.html`(单页:仪表盘 / 持仓 / 记录 tab)+ `_rag_widget.html`(浮动问答小窗) |
| `static/` | `css/style.css`、`js/common.js`(工具)+ `js/app.js`(全部交互)+ `js/rag.js`(问答小窗) |

## 4. 关键决策(Decisions)
1. **持仓"推导而非存储"**:只存交易(Transaction)与现价(PriceQuote)这两类原始事实,持仓股数/成本/市值/占比/盈亏全部由 `calc.py` 实时算。→ 改一笔交易,所有衍生值自动一致,不会算漏更新。
2. **计算引擎是纯函数、与框架解耦**:`calc.py` 不依赖 FastAPI/SQLAlchemy,输入输出都是 dataclass,易单测(`tests/test_calc.py`)。`services.py` 负责 ORM→calc 的转换。
3. **市场无关 / 策略感知**:`Security.market` 默认 `CN` 为美股/港股预留;目标/区间挂在 `Strategy` 下,多策略是「加行」而非改表。
4. **缺现价兜底**:未填现价的标的标「待估值」,**不计入市值汇总**(避免污染占比);记交易时若无现价则用成交价作**临时现价**(可被实时行情覆盖)。
5. **未分配池模型(替代原型的"按比例吸收 + 锁定")**:拨杆自由拖动、互不联动,未分配可为负(红色提示),仅在「保存目标」时要求归 0。
6. **单页 + 页内 tab**:仪表盘 / 持仓 客户端切换,无跳转(`#holdings` 深链);避免"没有返回按钮"。
7. **现价/计算值只读,仅原始输入可改**:现价(实时)、市值/盈亏/占比/成本(计算)纯展示;可改的只有交易、预期卖价、大类设置、标的归属、目标拨杆。
8. **实时行情独立于渲染 + 多源 failover**:不在 `GET /api/dashboard` 内同步拉行情(避免拖慢、强耦合)。前端「先渲染缓存价 → 静默刷新 → 重渲染」;`PriceQuote.source` 用 `manual`/`auto` 区分。取价按 `STOCKBOOK_QUOTE_SOURCES`(默认 腾讯→新浪→东方财富)**依次尝试,先成功者用之**,整链全挂才报 502;每源「解析/网络」分离,刷新结果回传实际命中的源与 `unresolved` 未取到代码。
9. **SQLite + 轻量加列迁移,不上 Alembic**:`seed._migrate()` 在启动时用 `PRAGMA`/`ALTER TABLE` 补列。单用户本地,保持零运维。
10. **备份/恢复用 SQLite 在线备份 API**:`sqlite3.Connection.backup()` 做页级一致快照(可应对并发写/WAL,优于裸文件复制)到 `backups/`;恢复时释放引擎连接后用同一 API 把备份内容写回主库(不会出现半写文件);恢复/重置前都先自动备份(可逆)。备份名带时间戳并防同秒冲突。
11. **只读分享**:`?readonly=1` 隐藏所有写操作、拨杆禁用;`&hideAmounts=1` 把金额/股数等掩码为 `•••••`(百分比仍显示)。
12. **现金作为大类 + 现金流推导**:某大类可标 `is_cash`(全局唯一),它无标的、市值 = 现金余额 = `Σ注入 − Σ移出 + Σ卖出额 − Σ买入额`(仍是推导而非存储)。买入/卖出对现金的影响隐含在该公式里,无需单独记转账。`is_cash` 类参与总资产/占比/再平衡;**不标则现金只在「记录」板块展示、不进仪表盘**(解耦,改动小)。资金注入/移出存 `CashFlow` 表。
13. **记录板块(交易总账)**:`GET /api/ledger` 合并 买入/卖出/资金注入/移出 为按时间倒序的总账 + 资金概览(注入/移出/净投入/买入/卖出/现金余额/已实现盈亏);筛选(标的/类型/时间/盈亏)在前端做。
14. **卖出按批次配对(specific-lot)**:卖出必带 `matched_buy_id`,指向某买入批次;已实现盈亏 = (卖价 − 该批次买价) × 股数(精确,非平均)。买入批次「剩余 = 原股数 − Σ指向它的卖出」,均价/盈亏按未平仓批次的剩余加权;**剩余为 0 的批次从持仓视图隐藏**(数据不删,记录可查)。删除带卖出的买入批次、把批次股数改到已卖出量之下,均被拒。

15. **RAG 问答(Phase 2)**:独立 `app/rag/` 子包(notion 解析/抓取、fastembed 本地向量化、numpy 暴力余弦检索、prompt 组装 + Claude)。两张新表 `NotionSource`/`KnowledgeChunk`(向量以 JSON 存 Text 列,不依赖向量扩展,保持单文件可打包)。三重成本/安全护栏:总开关 `STOCKBOOK_RAG_ENABLED`、只读模式 403、每日限流(默认 50,`STOCKBOOK_RAG_DAILY_LIMIT` 可改);key 仅后端、走 .env、不下发前端;同步阶段不调 LLM(仅 Notion + 本地 embed)。默认模型 Haiku(`STOCKBOOK_RAG_MODEL` 可切)。检索接口封装在 `store.search`,日后上万片段可平滑换 sqlite-vec。

## 5. JSON API 一览
- `GET /api/dashboard` — 一次性返回页面所需(各大类 + 标的 + 再平衡 + 总资产/估值日期/price_state 等)。
- 大类:`POST /api/asset-classes`、`PUT/DELETE /api/asset-classes/{id}`。
- 标的:`POST /api/securities`、`PUT/DELETE /api/securities/{id}`、`PUT /api/securities/{id}/price`、`GET /api/securities/{id}/transactions`。
- 交易:`POST /api/transactions`(支持按 code 自动建标的 + 每笔买入的预期卖价)、`PUT/DELETE /api/transactions/{id}`。
- 目标:`PUT /api/strategy/targets`(校验覆盖全部大类且合计 100)。
- 再平衡留痕:`POST /api/strategy/rebalanced`。
- 行情:`POST /api/prices/refresh`(多源 failover,写 `source=auto`,顺带补全占位名称)。
- 记录/现金:`GET /api/ledger`、`POST/DELETE /api/cashflows`。
- 备份:`POST /api/backup`(强制一次,多目标)、`GET /api/backups`(带 integrity/destinations/encrypted)、`POST /api/backup/verify`(tri-state,显式会拉取异地)、`POST /api/restore`(可选 `destination`,本地/异地;解密失败→400)。
- 历史/绩效:`POST /api/snapshot`(写,upsert 今日快照)、`GET /api/history?range=3m|1y|all`(序列 + 窗口指标 xirr/twr/max_drawdown/volatility + 基准 growth/cagr/max_drawdown + 当前大类名色;非法 range 回退 all)。
- 重置:`POST /api/reset`(先自动备份)。
- RAG 问答:`GET /api/rag/status`(始终可用,前端据此决定是否显示问答窗)、`POST /api/rag/ask`(三重护栏:总开关/只读/限流)、`POST /api/rag/sync`(删旧重建,不调 LLM)、`POST/DELETE /api/rag/sources`。

16. **负现金不污染占比**:现金大类余额为负(通常=未记录资金注入)时,`compute_dashboard` 把各大类对总资产分母的贡献**按 0 取下限**(`max(0, class_value)`),占比恒在 0–100%,现金类显示其真实负余额但占 0%;仪表盘弹警告提示去「记录」补记入金。现金为正时行为不变。

17. **测试基建(子项目 E)**:CI(GitHub Actions,Python 3.9 单版本)跑全套 pytest;Hypothesis 对纯函数 `calc.py` 验**核心不变量**(实际占比和≈100%、占比∈[0,100]、再平衡后落目标、`net_shares`=未平仓批次剩余之和、均价∈[买价区间]、再平衡金额符号、任意输入不崩/无 NaN、目标未分配=100−Σtarget)。每条不变量用**变异检查**确认有牙(改坏 calc 能被抓到)。覆盖率**分层 gate**:`pytest --cov=app` 出全量报告,但只对 `calc.py`+`services.py` 用 `coverage report --include=… --fail-under=95` 设硬失败线(外围网络/模型代码只报告不卡)。dev 工具(pytest-cov/coverage/hypothesis)独立于 `requirements-dev.txt`,不污染运行时 `requirements.txt`。**实际占比(I1,构造恒等 100%)与目标占比(I8,带未分配池、只在保存时归 0)是不同的量,分两条不变量**。**评估后不加 lint**:3.9 语法守卫无可靠静态工具(ruff `UP` 是旧→新升级、不报 `X|None`),且本地+CI 都跑 3.9 已是天然闸。

18. **数据源接口化(子项目 B)**:三处「可替换后端」——行情源、embedding、检索——各抽出一个 `typing.Protocol` 接口(`QuoteSource`/`Embedder`/`Retriever`),现有实现包成类(`TencentSource`/`SinaSource`/`EastmoneySource`、`FastembedEmbedder`、`NumpyCosineRetriever`)。三者干的事不同、**接口各自独立**,统一的只是「Protocol + 注册/选择」这套做法。行情源用注册表 `QUOTE_SOURCES`(替代 `_FETCHERS`);embedding/检索用 `get_embedder()`/`get_retriever()` 选择器。**纯重构、零新行为**:`fetch_quotes`/`embed_texts`/`store.search` 等被消费的模块级函数保留为兼容垫片,消费方一行不改。**不加配置开关**(YAGNI):embedding/检索各仅一个实现,选择器现在直接返回默认对象,留作将来第二实现的单一改动点(如检索换 sqlite-vec)。收益:加新后端不动老代码;测试用 fake 实现替代网络/模型(`FakeQuoteSource`/`FakeEmbedder`/`FakeRetriever`),failover/检索测试不再依赖外部。

19. **备份加固(数据安全)**:备份从「同盘/明文/无校验/无自动/无轮转」升级为**自动化 + 可校验 + 异地**。抽出 `app/backup.py`(对齐 calc/services 分层),`BackupDestination` Protocol + `LocalDirDestination` 复用为本地主目标 + 同步盘异地目标(`STOCKBOOK_BACKUP_DIR` 指向 iCloud/坚果云即得离机副本,零密钥);每份备份 SHA-256 + `PRAGMA integrity_check` 写入目录内 `manifest.json`;`verify` 返回 **tri-state**(`ok/mismatch/unavailable`)——显式校验会有界拉取未物化的同步盘文件、拉不下来即 `unavailable`,**任何拉取/部分物化失败都不假报 mismatch**;只能校验本机物化副本(抓得到云端损坏、抓不到「一致地变旧」,服务端校验留给将来 `S3Destination`)。进程内 lifespan 调度(启动备 + 每 12h + 退出备,`STOCKBOOK_BACKUP_INTERVAL_HOURS=0` 关闭)+ `python -m app.backup` CLI;变更检测(live 文件哈希)跳过无变化、计数式保留(`STOCKBOOK_BACKUP_KEEP`,默认 30)。SQLite 耦合收敛在 `_sqlite_*` 几个函数,换库时按 spec §13 抽 `BackupSource`(本轮不建)。

20. **备份加密(只加密异地)**:给离机的异地备份加 Fernet 认证加密,**本地保持明文**(本地明文是「忘口令也能恢复」的安全冗余 —— 忘口令最多失去异地、不丢全部)。`EncryptedDestination` 装饰器包在 offsite `LocalDirDestination` 外:`store` 加密成 `<name>.enc`、`fetch` 解密,manifest 透传内层(逻辑名↔`.enc` 映射,`sha256` 存明文哈希)。密钥 = `scrypt(STOCKBOOK_BACKUP_PASSPHRASE, salt)`→Fernet,salt/KDF 参数存异地目录 `enc.json`(文件夹自描述,带口令即可解,跨平台,原子写)。**设口令即加密、无额外开关**(YAGNI);配了异地但没口令 → 明文 + 告警。`verify` 对加密备份**解密到本地临时文件再校验**(Fernet 认证使篡改/错口令→`mismatch`、读取失败→`unavailable`,明文绝不落进同步盘);恢复解密或干净中止(错口令→400,live 库未动)。改口令不支持轮转(改前先用旧口令把异地迁出)。 **(v2 修订)** offsite 永远包加密层、加密改为**逐备份**(有口令→`.enc` 密文、无口令→明文,均记在 `meta.encrypted`),`list()` 永远报逻辑名 → 同一备份永远合并一行(修口令删除后的重复显示,现有 `.enc` 免迁移);`verify`/`restore` 按逐文件 `meta.encrypted` 解密,加密+无口令→unavailable/400;备份列表加分页。

21. **历史净值 + 绩效(走势板块)**:新增 `Snapshot` 表(每日总资产/净投入/基准点位/各大类市值 JSON),是对「**推导而非存储**」的**有意例外** —— 过去某天的市值无法用现价重算,时间序列必须落盘。`snapshot_service.run_snapshot` 捕获前**先刷持仓行情**避免记陈价(写回逻辑抽到 `services.apply_fetched_quotes`,与 `/api/prices/refresh` 共用),按 `date` upsert(每天一条/手动可刷当天)。调度**仿备份调度器另起一个独立 asyncio task**(两条 cadence 不同:备份 12h、快照 `SNAPSHOT_INTERVAL_HOURS` 默认 24h;启动补当天 + 每日,`READONLY`/间隔=0 不自动)。基准(沪深300)**正向累积**:每日顺带快照指数点位(走多源 `fetch_quotes`,抓不到存 null);`BENCHMARK_CODE` 默认 `sh000300` **带市场前缀**——指数非个股,个股 code→市场启发式会误判,故显式前缀。绩效指标是 `calc` 纯函数:**XIRR**(资金加权,二分求根;期初以窗口起点市值为一笔流出,**排除落在起点当天的现金流**避免与起点市值重复计)/**TWR**(时间加权,日快照只能把现金流归到区间端点 → 近似;极端单日跳变年化溢出→None)/**最大回撤**/**年化波动**(√252 假设规则采样,稀疏时仅供参考;单调升仅蕴含回撤=0,**不**蕴含波动=0,只有常数/等比序列波动=0);基准用 **CAGR**(纯价格序列无现金流,XIRR 无定义),并按基准自身数据跨度年化。`GET /api/history?range=` 的**指标随选中区间计算**(与图一致,区间相对最后一条快照日)。`reset_to_default` 一并清 `Snapshot`。走势页自绘 SVG(净值折线三条可切 + 大类堆叠面积,已删除大类兜底中性灰「已删除大类」),`hideAmounts` 掩码金额轴、形状/日期仍显示,零 Node。

## 6. 数据模型
两层:Security 归入 AssetClass,AssetClass 归入 Strategy。持仓由 Transaction 推导;PriceQuote 每标的一条最新价。详见 `models.py` 与设计文档 §3。

## 7. 功能日志(Changelog)
- **2026-05-29** v1:数据模型 + 纯计算引擎 + JSON API + 种子数据 + 「衡」前端(仪表盘:未分配池拨杆、目标/实际堆叠条、持仓偏离轨道、再平衡建议)+ pytest。
- **2026-05-30** 接入原型视觉;改为单页 + 页内 tab;大类增删改改为弹窗;录入 tab → **持仓 tab**(标的卡片:单价/持仓/成本/市值/盈亏/占比,展开看交易明细);再平衡增强(回目标/回边缘、仅加仓、忽略零碎、再平衡提醒);标的均价与盈亏;按 code 记交易自动建标的 + 每笔买入预期卖价;修弹窗内边距。
- **2026-05-31** 实时行情接入(腾讯,按 code,自动/手动刷新,临时现价,补名);备份/恢复 + 重置前自动备份;估值日期旁标「实时/收盘」(交易时段判断);现价/计算值改为只读;`.gitignore` + 本架构文档。
- **2026-05-31** 交易明细可编辑:每笔可改买入价/股数/日期(`PUT /api/transactions/{id}` 扩展,改后重算衍生值并做卖出不超持仓校验);持仓详情 UI 重做——「持仓汇总」卡 + 分隔线 + 「交易明细」每笔独立卡片;每笔字段顺序:买入价 · 现价 · 股数 · 当前盈亏 · 价值 · 预期卖价。
- **2026-05-31** code-review 修复:行情代码映射更稳健(SH 可转债 110/111/113/118 → sh、920 段归北交所不误判、`market="SH"/"SZ"` 显式覆盖);刷新返回 `unresolved` 未取到的代码并在前端提示;加权预期卖价改为按 FIFO 剩余持仓加权(卖出不再虚增);成交价校验文案中性化;抽出 `calc.derive_holding` 统一成本/市值/盈亏(去重 `_security_out`);删除无用 `dotHtml`/`markNav`;备份/恢复改用 SQLite 在线备份 API(`sqlite3.Connection.backup()`)替代 `shutil.copy2`。
- **2026-05-31** 行情多源 failover:新增新浪、东方财富两个备选源(各自 `parse_*`/`_fetch_*`),`fetch_quotes` 按 `STOCKBOOK_QUOTE_SOURCES` 顺序尝试,先成功者用之,全挂才 502;刷新返回实际命中源(前端提示「源:tencent」)。
- **2026-05-31** 交易记录板块(Step 1):现金作为大类(`AssetClass.is_cash`,唯一)+ 资金注入/移出(新表 `CashFlow`)+ 第三个 tab「记录」(资金概览 + 总账 + 标的/类型/时间/盈亏筛选);现金余额推导并入 `is_cash` 大类的市值。
- **2026-05-31** 交易记录板块(Step 2):卖出按批次配对(`Transaction.matched_buy_id`)——持仓 tab 改为展示未平仓买入批次(原股数/剩余/卖出按钮),全局「记一笔」改为买入专用,卖出走「选批次卖出」弹窗;`average_cost`/holdings 改为按未平仓批次剩余加权;记录里卖出盈亏升级为精确批次配对;删/改批次受卖出约束保护。
- **2026-05-31** 资金概览补全:加 **总资产**(持仓市值今日行情 + 现金余额)、**持仓市值**、**总收益**(总资产 − 净投入);明确「净投入 = 注入 − 移出(本金)」「买入≠注入」;各项加 tooltip;净投入为 0 时总收益提示「先记资金注入」。
- **2026-05-31** 记录买卖配对:卖出记录带「对应买入」日期/买价 + 该笔已实现盈亏;持仓 tab 提示语点明「卖出」入口在批次卡片右上角。
- **2026-05-31** 配对显示改版:弃用满屏「配对#N」彩色徽标(需跨行扫视,费劲),改为**卖出行内自带**「↳ 平仓自 <买入日期> @<买价>」子行 + 该笔盈亏,自解释、无需对照。批次卡片字段改名「买入数量(原)」「当前持有(已卖部分)」以消除"卖出后股数没变"的误解(原买入量不变、剩余/持有才是当前持仓;经浏览器自动化验证卖 2000 后持仓 10000→8000 正确)。
- **2026-05-31** 记录加「时间 / 配对分组」视图开关:配对分组下每笔买入是一个可折叠批次组(组头:买入日期/价/量 · 当前持有/已清仓 · **该批次累计已实现盈亏**),展开列出它的所有卖出(日期·股数·卖价·盈亏);资金流水在批次下另列。前端 `buildBatches` 由 ledger entries 按 `matched_buy_id` 聚合,UI 测试用 puppeteer-core 驱动系统 Chrome 验证。
- **2026-05-31** 大类自动配色:新建大类不再手动选色,后端 `_auto_color` 按色相黄金角生成候选、挑离现有大类色相最远的一个(套统一暖色调 S/L),distinct 且无数量上限;颜色存为 hex,`colorVar` 已兼容(`--cN` 旧值走 CSS 变量,hex 直接用);弹窗去掉配色选择。
- **2026-05-31** 配色改进:纯色相黄金角会把紫/品红排太近(人眼难分),改为**精挑配色盘 `_PALETTE`(16 色,色相+明度都拉开)+ 按 RGB 感知距离挑离现有最远**;新增 `POST /api/asset-classes/recolor`(贪心重排全部大类为最分散配色)+ 仪表盘「重新配色」按钮,一键修好已撞色的旧数据。
- **2026-06-01** 修负现金导致占比 >100%:现金大类为负时把各大类对总资产分母的贡献按 0 取下限,占比恒 0–100%(现金显示真实负值、占 0%),并加负现金警告横幅引导补记入金。
- **2026-06-01** Notion RAG 问答(Phase 2):`app/rag/`(notion/embed/store/snapshot/ask/limiter)+ `routers/rag.py` + 浮动问答小窗(`_rag_widget.html`/`rag.js`)。手动「同步」删旧重建,fastembed 本地向量化,numpy 余弦 top-k,持仓快照注入 prompt,Claude(默认 Haiku)摘要 + 原文引用 + Notion 链接;总开关 + 只读 403 + 每日限流三重护栏;`.env`/`.env.*` 入 `.gitignore`。设计见 `docs/superpowers/specs/2026-05-31-stockbook-rag-qa-design.md`,实现计划见 `docs/superpowers/plans/2026-05-31-notion-rag-qa.md`。
- **2026-06-01** code-review 修复(数据安全/正确性):①空 crawl 不再删旧重建(否则会悄悄清空已建索引),返回 −1 让上层提示「未抓到内容」;②`/sync` 全失败/全空时返回 `error` 字段且 phase=error,前端不再无脑报「成功」;③负现金大类不再进再平衡建议(金额会无视亏空误导);④`total_assets` 改为**真实带符号合计**(与记录总账一致),另用 floored `weight_denom` 专供占比/再平衡分母(两者解耦,占比仍 0–100%);⑤每日限额改为 `allow()` 只检查、`record()` 只在成功后计数(失败调用不再烧额度);⑥`/sync` 用 `threading.Lock` 守卫 check-and-set(杜绝并发 sync 竞态)。
- **2026-06-01** code-review 修复(清理/性能):⑦`reset_to_default` 一并清 `NotionSource`/`KnowledgeChunk`(reset 即干净起点);⑧数据库行标题改从 `databases.query` 响应直接取(`fetch_database_rows`),省掉每行一次 `pages.retrieve`;⑨`store.search` 按 `(count,max_id)` sentinel 缓存嵌入矩阵、命中后只取 top-k 文本(不再每次全表载入+解析);⑩`rag.js` 改用 `common.js` 的 `api()`(统一错误处理,校验错误数组不再显示 `[object Object]`)。
- **2026-06-01** 测试基建(子项目 E):GitHub Actions CI(pytest + 分层覆盖率 gate)+ Hypothesis 核心不变量套件(`tests/test_calc_properties.py`,I1–I8,每条经变异检查)+ 核心模块覆盖率硬线(`calc.py`/`services.py` 合并 `--fail-under=95`,实测 99%)+ 独立 `requirements-dev.txt`/`pyproject.toml`。评估后排除 lint(3.9 守卫无可靠工具)。设计见 `docs/superpowers/specs/2026-06-01-stockbook-test-infra-design.md`,计划见 `docs/superpowers/plans/2026-06-01-test-infra.md`。
- **2026-06-01** 数据源接口化(子项目 B):行情源/embedding/检索三处各抽 `Protocol` 接口 + 实现类 + 注册/选择(`QuoteSource`/`Embedder`/`Retriever`);`_FETCHERS`→`QUOTE_SOURCES` 注册表,`get_embedder()`/`get_retriever()` 选择器;被消费函数保留为垫片,纯重构零新行为;新增 fake 实现解耦网络/模型测试。设计见 `docs/superpowers/specs/2026-06-01-stockbook-datasource-interfaces-design.md`,计划见 `docs/superpowers/plans/2026-06-01-datasource-interfaces.md`。
- **2026-06-02** 备份加固(数据安全):抽 `app/backup.py` + `BackupDestination` 接口(本地 + 同步盘异地)+ SHA-256/`integrity_check`/manifest + `verify` tri-state(异地按需拉取,失败不假报 mismatch)+ 进程内 12h 调度/`python -m app.backup` CLI + 变更检测/保留(30);SQLite 耦合收敛 `_sqlite_*` 留 `BackupSource` 接缝。设计见 `docs/superpowers/specs/2026-06-01-stockbook-backup-hardening-design.md`,计划见 `docs/superpowers/plans/2026-06-01-backup-hardening.md`。
- **2026-06-02** 备份加密(只加密异地):`EncryptedDestination`(Fernet + scrypt 口令派生)包 offsite,`<name>.enc` + 自描述 `enc.json`(原子写);设 `STOCKBOOK_BACKUP_PASSPHRASE` 即加密、否则明文+告警;verify 解密后再校验(篡改/错口令→mismatch、读失败→unavailable,明文不落同步盘)、恢复错口令→400 且 live 不动;前端加锁标记 🔒。设计见 `docs/superpowers/specs/2026-06-02-stockbook-backup-encryption-design.md`,计划见 `docs/superpowers/plans/2026-06-02-backup-encryption.md`。
- **2026-06-02** 备份加密 v2:offsite 永远包加密层、加密逐备份(有口令 `.enc`、无口令明文,记 `meta.encrypted`),`list()` 报逻辑名永远合并一行(修口令删除后重复、现有 `.enc` 免迁移);verify/restore 按逐文件标记解密;备份列表加分页。计划见 `docs/superpowers/plans/2026-06-02-backup-encryption-v2.md`。
- **2026-06-02** 历史净值 + 绩效分析(走势板块):新 `Snapshot` 表(推导而非存储的有意例外)+ `app/snapshot_service.py`(run_snapshot 先刷价→捕获→按 date upsert;build_history 区间+指标)+ `calc` 四个绩效纯函数(XIRR/TWR/最大回撤/年化波动,配 Hypothesis 不变量、每条变异检查)+ 进程内每日调度(仿备份、独立 task)+ 基准沪深300 正向累积(`sh000300` 带前缀绕开个股映射)+ 新 tab「走势」(指标卡 + 可切 SVG 净值线 + 大类堆叠,零 Node,headless-Chrome 验证)+ 配置 `BENCHMARK_CODE`/`SNAPSHOT_INTERVAL_HOURS`。行情写回逻辑抽 `services.apply_fetched_quotes`(刷新接口与快照共用,DRY)。设计见 `docs/superpowers/specs/2026-06-02-stockbook-history-performance-design.md`,计划见 `docs/superpowers/plans/2026-06-02-history-performance.md`。

## 8. 约定
- **新增功能 = 同时更新本文档**(关键决策 / API 一览 / 功能日志)与对应测试。
- 测试或本地起服务时**用临时库**(`STOCKBOOK_DATABASE_URL=sqlite:////tmp/...`),绝不动用户的 `stockbook.db`。
