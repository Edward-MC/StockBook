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
| `calc.py` | **纯计算引擎**(无框架依赖):持仓、市值、占比、偏离、状态、再平衡、未分配池、均价、盈亏 |
| `services.py` | ORM ↔ calc 的胶水:把 DB 行映射成 calc 输入,组装仪表盘载荷 |
| `quotes.py` | 行情:代码→市场前缀映射、腾讯返回解析、交易时段判断(解析与网络分离,便于测试) |
| `schemas.py` | Pydantic 请求/响应模型 |
| `seed.py` | 建表、示例数据播种、轻量迁移(`ALTER TABLE`)、`reset_to_default` |
| `routers/api.py` | JSON API |
| `routers/pages.py` | 单页外壳渲染(`/`)+ `/entry` 重定向 |
| `templates/` | `base.html`(外壳)+ `index.html`(单页:仪表盘 / 持仓 两个页内 tab) |
| `static/` | `css/style.css`、`js/common.js`(工具)+ `js/app.js`(全部交互) |

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

## 5. JSON API 一览
- `GET /api/dashboard` — 一次性返回页面所需(各大类 + 标的 + 再平衡 + 总资产/估值日期/price_state 等)。
- 大类:`POST /api/asset-classes`、`PUT/DELETE /api/asset-classes/{id}`。
- 标的:`POST /api/securities`、`PUT/DELETE /api/securities/{id}`、`PUT /api/securities/{id}/price`、`GET /api/securities/{id}/transactions`。
- 交易:`POST /api/transactions`(支持按 code 自动建标的 + 每笔买入的预期卖价)、`PUT/DELETE /api/transactions/{id}`。
- 目标:`PUT /api/strategy/targets`(校验覆盖全部大类且合计 100)。
- 再平衡留痕:`POST /api/strategy/rebalanced`。
- 行情:`POST /api/prices/refresh`(多源 failover,写 `source=auto`,顺带补全占位名称)。
- 记录/现金:`GET /api/ledger`、`POST/DELETE /api/cashflows`。
- 备份:`POST /api/backup`、`GET /api/backups`、`POST /api/restore`。
- 重置:`POST /api/reset`(先自动备份)。

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

## 8. 约定
- **新增功能 = 同时更新本文档**(关键决策 / API 一览 / 功能日志)与对应测试。
- 测试或本地起服务时**用临时库**(`STOCKBOOK_DATABASE_URL=sqlite:////tmp/...`),绝不动用户的 `stockbook.db`。
