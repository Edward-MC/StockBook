# 历史净值 + 绩效分析(走势板块)设计

> 给 StockBook 补「时间维度」:现在只有此刻快照,本设计加每日净值快照、净值曲线、绩效指标(XIRR/TWR/最大回撤/波动率)与基准对比(vs 沪深300)。
>
> 落地后一并更新 `docs/architecture.md`(关键决策 / API 一览 / 功能日志)。

## 1. 背景与动机

StockBook 所有衍生值都是「此刻」:总资产、占比、盈亏都按当前行情实时算,**没有历史**。一个投资追踪器没有净值曲线/年化收益,只做了一半。本设计加时间序列与绩效分析,并复用已有的进程内调度(备份调度器)做每日自动快照——一个干净的自动化(DBRE)+ 纯计算(可测)故事。

## 2. 目标 / 非目标

**目标**
- 每日净值快照:总资产、净投入(累计本金)、各大类市值、基准指数点位。
- 调度:**每日自动 + 启动补当天 + 手动**,每天最多一条(按 `date` 去重/upsert)。
- 绩效指标(纯函数,配 Hypothesis 不变量):**XIRR**(资金加权年化)、**TWR**(时间加权)、**最大回撤**、**年化波动率**;基准同口径算一份对比。
- 走势页(综合页):指标卡 + 主图(总资产/净投入/基准三条可切)+ 大类市值堆叠面积图。零 Node、纸质感自绘 SVG。

**非目标(本轮)**
- 历史回填(只从现在正向攒;早期数据少是预期)。
- 分红/送股/拆股(单独板块)、多基准、数据导出。
- TWR 精确到现金流时点(用日快照分段近似,见 §5)。
- 不规则采样的统计校正:本地优先、应用非天天开,快照天然有跳空(周末/漏开),波动率/TWR 等基于日收益的指标采样稀疏时仅供参考(见 §5 诚实声明)。

## 3. 数据模型(关键决策)

新表 `Snapshot`:
```
id            PK
date          DATE  UNIQUE        # 每天一条
total_assets  FLOAT               # 持仓市值(当日行情)+ 现金余额(= build_ledger 口径)
net_invested  FLOAT               # 累计净投入 = Σ注入 − Σ移出(本金)
benchmark     FLOAT NULL          # 基准指数点位(抓不到存 null)
class_values  TEXT(JSON)          # {asset_class_id: market_value} 当日各大类市值(供堆叠图)
created_at    DATETIME
```
**决策(写进 architecture)**:这是对项目「**推导而非存储**」铁律的**有意例外** —— 过去某天的市值无法重算(行情已变),时间序列必须落盘。建表走现有 `create_all`+`seed._migrate()`,保持单文件、零运维。`reset_to_default` 一并清 `Snapshot`(reset 即干净起点)。

## 4. 架构分层

| 文件 | 职责 |
|---|---|
| `app/models.py` | 加 `Snapshot` 表 |
| `app/snapshot_service.py`(新) | ORM↔calc 胶水:从 `build_ledger`/`build_dashboard` 取总资产/净投入/各大类市值 + 抓基准 → upsert 当日 `Snapshot`;读序列;调 calc 指标组装 `/api/history` 载荷 |
| `app/calc.py` | 加纯绩效函数:`xirr` / `twr` / `max_drawdown` / `annualized_volatility`(无框架依赖,输入 dataclass/序列) |
| `app/snapshot_service.py`(调度部分) | 进程内 lifespan 调度,**仿** `app/backup.py` 的 `_scheduler_loop`/`start_scheduler`/`stop_scheduler` **另起一个独立 asyncio task**(不是复用同一个 —— 备份认 `BACKUP_INTERVAL_HOURS`=12h、快照认 `SNAPSHOT_INTERVAL_HOURS`=24h,两条 cadence):启动补当天 + 每 `SNAPSHOT_INTERVAL_HOURS` 一次 `run_snapshot()`;`READONLY`/间隔=0 不自动 |
| `main.py` | lifespan 里在 backup 调度之外**新增** snapshot 调度任务的 start/stop(目前只起停 backup 一个) |
| `app/routers/api.py` | `POST /api/snapshot`、`GET /api/history` |
| `app/config.py` | `BENCHMARK_CODE`、`SNAPSHOT_INTERVAL_HOURS` |
| `templates/index.html`、`static/js/app.js`、`static/css/style.css` | 新 tab「走势」+ 自绘 SVG 图 |

## 5. 绩效指标(`calc.py` 纯函数)

输入:快照序列(`[(date, total_assets, net_invested, benchmark, class_values)]`)+ 现金流(`[(date, amount)]`,注入为正、移出为负)+ 期末市值。

- **`xirr(flows, end_value, end_date) -> Optional[float]`**:资金加权内部收益率。现金流(含期末市值作为一笔正流出)按 `Σ cf/(1+r)^(days/365) = 0` 求根(二分,区间 `[-0.999, 10]`;无解/单点/全零返回 None)。
- **`twr(snapshots, cashflows) -> Optional[float]`**:时间加权。相邻快照分段收益 `r_i = (V_end − V_begin − netflow_in_period)/V_begin`,链乘 `Π(1+r_i) − 1`,再年化。日快照只能把现金流归到区间端点 —— **近似,文档注明**。
- **`max_drawdown(nav) -> float`**:净值序列峰谷最大跌幅 ∈ [0,1]。
- **`annualized_volatility(nav) -> Optional[float]`**:日收益标准差 × √252。**诚实声明**:√252 假设等间隔交易日采样;本地优先下快照有跳空(周末/漏开),不规则采样会放大噪声 —— 采样稀疏时该指标仅供参考(同 TWR 的近似声明)。
- **基准**:把 `benchmark` 序列当一个「净值」,同口径算归一化增长 + 基准 **CAGR**(点对点年化)/ 回撤,供对比。**注意不是 XIRR**:纯价格序列无现金流,XIRR 无定义,基准用 CAGR;组合用资金加权(XIRR)、基准用价格年化,口径本就不完全可比,前端对照小字点一句即可。

**Hypothesis 不变量(每条变异检查)**:回撤 ∈ [0,1];空/单点/全相等序列不崩、返回 None 或 0;净值单调升 → 回撤=0、波动=0;XIRR 对「翻倍且无中途流入」≈ 期间复合年化;TWR 与「忽略流入的简单收益」同号;任意输入不出 NaN/不抛。

## 6. 快照捕获 + 调度

- `run_snapshot(db)`:
  1. **先刷新持仓行情**再取市值 —— 否则 `build_ledger`/`build_dashboard` 读到的是上次缓存的 `PriceQuote`,「每日净值」会记成陈价。`run_snapshot` 内先对持仓标的跑一次 `fetch_quotes` 写 `source=auto`(失败则退化用最近已知价,并记日志,不阻断快照)。
  2. 算当日 `total_assets`/`net_invested`(复用 `build_ledger`)+ 各大类市值(复用 `build_dashboard` 的 per-class)。
  3. 抓 `config.BENCHMARK_CODE` 点位(走现有多源 `fetch_quotes`;失败存 null)。**指数 ≠ 个股**:沪深300 在腾讯是 `sh000300`,而现有 code→市场映射是给**个股**设计的(`000`/`399` 段会判成深市个股 `sz…`,对指数错误)。因此 `BENCHMARK_CODE` **默认显式带市场前缀** `sh000300`,snapshot_service 抓基准时按「已带前缀则直用、否则才走个股映射」处理;配指数解析测试(腾讯/新浪指数返回格式)防回归。
  4. **按今天 `date` upsert**(已存在则更新,实现「每天最多一条 + 手动可刷新当天」)。纯读 live 库 + 写一行(`Snapshot` 表)+ 顺带刷 `PriceQuote`,绝不改交易数据。
- 调度:lifespan 启动 → 若**今天还没快照**补一个 → 每 `config.SNAPSHOT_INTERVAL_HOURS`(默认 24,`0` 关)再来;`config.READONLY` 下不自动。同备份调度的「绝不拖垮主流程/可静默失败」。
- 测试隔离:conftest autouse 关 `SNAPSHOT_INTERVAL_HOURS=0`(同备份做法),避免自动快照污染既有测试。

## 7. 配置

| 环境变量 | config | 默认 | 含义 |
|---|---|---|---|
| `STOCKBOOK_BENCHMARK_CODE` | `BENCHMARK_CODE` | `sh000300` | 基准指数代码(沪深300);**默认带市场前缀**(指数非个股,前缀映射不可靠,见 §6);空=不抓基准 |
| `STOCKBOOK_SNAPSHOT_INTERVAL_HOURS` | `SNAPSHOT_INTERVAL_HOURS` | `24` | 自动快照间隔;`0` 关闭 |

## 8. API

- `POST /api/snapshot`(写操作,`require_writable`)→ upsert 今日快照,返回该行。
- `GET /api/history?range=3m|1y|all` → `{"series":[{date,total_assets,net_invested,benchmark,class_values}], "metrics":{xirr,twr,max_drawdown,volatility, benchmark:{growth,cagr,max_drawdown}}, "class_names":{id:{name,color}}}`。
  - **range 同时过滤 series 和 metrics**:指标按**选中区间**计算(与主图一致),即「3月最大回撤」算的是近 3 月窗口、`all` 才是全历史。XIRR 的现金流也只取落在窗口内的(窗口起点的持仓市值作为期初一笔流出)。这样指标卡随区间切换联动、自洽。
  - `benchmark.cagr` 是基准点对点年化(非 XIRR,见 §5);`benchmark.growth` 是归一化增长。
  - `class_names` 仅含**当前存在**的大类;历史 `class_values` 里指向**已删除**大类的 id 在此 map 中缺失(见 §9 前端兜底)。

## 9. 前端(综合页,新 tab「走势」)

- 第 4 个 tab「走势」(`#trends` 深链;沿用现有页内 tab 机制)。
- **指标卡**一排:年化(XIRR)· TWR · 最大回撤 · 波动率,各带基准对照小字。
- **主图**:自绘 SVG 折线;三条线 = 总资产(实)/ 净投入(虚)/ 基准归一化;按钮切显哪几条;区间切换 3月/1年/全部;hover tooltip 显示某日数值。
- **下方**:大类市值**堆叠面积图**(SVG),看配置随时间漂移;图例用大类配色。**已删除大类兜底**:历史 `class_values` 可能含已不在 `class_names` 里的 id(大类被删/改名),前端给它兜底标签「已删除大类」+ 中性灰,不丢图层、不报错(金额仍计入堆叠总高,避免历史总资产对不上)。
- 纸质感、零 Node、与现有自绘视觉一致。空/单点数据态:提示「攒几天就有曲线了」。隐藏金额(`hideAmounts`)时金额轴掩码、百分比/形状仍显示。

## 10. 测试

- `calc` 四个指标:边界(空/单点/全等/单调)+ 已知数值用例 + Hypothesis 不变量(§5),每条变异检查。
- `snapshot_service`:临时库 + 假 `fetch_quotes`:同日两次 `run_snapshot` → 一条(upsert);基准抓不到 → benchmark=null 不崩;class_values JSON 往返;range 过滤。
- API:TestClient 造 N 条快照 → `GET /api/history` 结构 + 指标键齐全;`POST /api/snapshot` upsert。
- 调度:直接测 `run_snapshot` 单次,不测定时器;conftest 关自动快照。
- 覆盖率:`calc.py` 进核心硬线(`--fail-under=95`),`snapshot_service` 外围只报告。

## 11. 关键决策小结(待并入 architecture.md)

1. **快照是「推导而非存储」的有意例外**:过去市值不可重算,时间序列落盘(`Snapshot` 表,JSON 存各大类市值,单文件不变)。
2. **每日快照走进程内调度**(**仿**备份调度器另起一个独立 asyncio task —— 两条 cadence 不同,不是复用同一个;启动补当天 + 每日 + 手动,按 date upsert),非 cron/常驻——契合本地优先、单文件。快照前先刷持仓行情避免记陈价;基准用显式带前缀的指数代码(`sh000300`)绕开个股前缀映射。
3. **基准正向累积**:每日顺带快照指数点位(`fetch_quotes`),无需历史数据 API;归一化对比,早期数据少是预期。
4. **绩效指标是 calc 纯函数**:XIRR(资金加权)/ TWR(时间加权,日快照近似)/ 最大回撤 / 年化波动,配 Hypothesis 不变量;money-weighted 与 time-weighted 是不同的量,分开给。
5. **走势综合页**:指标卡 + 可切主图 + 大类堆叠,自绘 SVG 零 Node。

## 12. 行为兼容

- 纯增量:新表、新 service、新 calc 函数、新 tab、新 API、新配置;现有仪表盘/持仓/记录/备份一字不改。
- 不配 `BENCHMARK_CODE` → 不抓基准(benchmark 列 null),其余照常。
- `Snapshot` 表为空时 `/api/history` 返回空序列 + 指标为 None,前端显示空态。
