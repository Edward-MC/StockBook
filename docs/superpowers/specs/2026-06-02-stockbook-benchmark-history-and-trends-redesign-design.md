# 沪深300 历史基准线 + 走势页视觉重做 设计

> 走势板块的两点迭代:① 抓 沪深300 历史日线,让基准线第一天就有(不用等攒快照);② 走势页换成 Robinhood 极简风(大数字当头 + 渐变填充曲线 + 区间药丸),不再死白难看。
>
> 落地后一并更新 `docs/architecture.md`(关键决策 / API 一览 / 功能日志)。

## 1. 背景与动机

现状:走势页的基准/净值曲线**只随每日快照正向累积**,新库头几天图里空空、白底难看。用户要求:基准线(沪深300)从第一天就有「东西可看」,且整页视觉要更耐看。**不做持仓-基准对齐**(两条线各画各的,持仓线随时间自己长出来)。

## 2. 目标 / 非目标

**目标**
- **沪深300 历史线**:抓历史日线落库,基准线与基准指标卡(年化 CAGR / 最大回撤)立即有数,独立于持仓快照数量。
- **区间可选**:顶部按钮 `3月 / 6月 / 1年 / 3年 / 全部`,选哪个两条线就画多长。
- **视觉重做(Robinhood 极简风)**:大数字当头(总资产 + 区间涨跌)、主曲线带暖色渐变填充、沪深300 细线叠加、区间药丸按钮、暖纸底替代死白、末点圆点。
- 空/稀疏态仍优雅(网格框 + 基准线 + 提示)。

**非目标(本轮)**
- 持仓线与基准线的精确对齐/同起点归一(用户明确「不用对齐」)。基准按自身范围自适应缩放,仅作形状参照。
- 盘中实时基准、分红调整、多基准。
- hover 十字光标/tooltip(末点圆点即可;tooltip 留后续)。

## 3. 数据模型(关键决策)

新表 `BenchmarkPoint`(与 `Snapshot` 解耦——指数有「你没快照的交易日」,塞进 Snapshot 不合适):
```
date    DATE  PK     # 交易日
close   FLOAT        # 沪深300 收盘点位
```
**决策**:历史指数收盘同样是「过去不可重算」的事实,持久化落库(对齐项目「推导而非存储」的既有例外口径,如 Snapshot)。`reset_to_default` 一并清 `BenchmarkPoint`。建表走 `create_all`。

## 4. 数据源与抓取(`quotes.py`)

- 新增 `fetch_index_history(code, market, days) -> List[Tuple[date, float]]`:走东方财富 K 线历史接口 `https://push2his.eastmoney.com/api/qt/stock/kline/get`(复用 `to_em_secid` → `1.000300`;`klt=101` 日线、`fqt=0`、`lmt=days`)。**解析与网络分离**:`parse_em_kline(text) -> List[(date, close)]`(纯函数,易测)+ `_fetch_index_history_em(...)`(网络)。失败抛 `httpx.HTTPError`,上层吞掉不阻断。
- 仅东财一个源即可(指数 K 线东财稳定);失败不致命(基准线缺失,页面照常)。

## 5. 抓取编排 + 缓存(`snapshot_service.py`)

- `backfill_benchmark(db, days=≈750)`:表空或最新点早于昨天时,拉 ~3 年日线 `upsert` 进 `BenchmarkPoint`(按 date)。**不在请求路径上**:放进**快照调度器的启动步**(lifespan 起后台 task 时顺带 backfill 一次),以及每日 `run_snapshot` 末尾补当天 close(复用已抓到的基准点写入 `BenchmarkPoint`,顺带保持每日新鲜)。
- `/api/history` **只读 `BenchmarkPoint` 表**,绝不在页面加载时打网络 → 页面快。表空时(首次、backfill 未完)基准线暂缺,前端显示稀疏态。
- 测试隔离:`fetch_index_history` 用 `FakeIndexSource`/monkeypatch,绝不打真实网络;conftest 已关调度(`SNAPSHOT_INTERVAL_HOURS=0`),backfill 不会自动跑。

## 6. API

- `GET /api/history?range=3m|6m|1y|3y|all`:
  - `range` 现在驱动一个**日期窗口**(相对今天:90/180/365/1095 天;`all`=不设下界)。
  - 响应在原有基础上**新增** `benchmark_series: [{date, close}]`(密集,来自 `BenchmarkPoint`,按窗口裁剪)。
  - 原 `series`(持仓快照,稀疏)保持不变。
  - **基准指标 `metrics.benchmark`(growth/cagr/max_drawdown)改为按 `benchmark_series` 在窗口内计算**(立即有数)。持仓指标(xirr/twr/volatility/max_drawdown)仍来自快照、需 ≥2 条。
  - 非法 `range` 回退 `all`。
- `Snapshot.benchmark` 列保留(每日仍写,作冗余),但图与基准指标改以 `BenchmarkPoint` 为准。

## 7. 前端视觉重做(走势页,Robinhood 极简风)

**布局**
- **抬头(大数字)**:`总资产 ¥263,512` 大号 + 一行 `▲ +4.9% · 近1年`(区间涨跌 = 窗口内总资产末/首,色按涨跌;`hideAmounts` 下金额掩码、百分比仍显)。取代原 “走势 · 绩效” 标题位。
- **主图**:
  - **总资产**线:暖棕实线 + **线下渐变填充**(棕→透明,`<linearGradient>`,自绘 SVG)。≥2 条快照才画;否则主图以 **沪深300 线**为主体。
  - **净投入**:淡虚线。
  - **沪深300**:细实线(次要色),**按自身 min/max 自适应缩放**(不与 ¥ 对齐,仅形状参照),默认显示。
  - 末点小圆点标当前值。
  - 底色用暖纸卡调(非死白);浅横向网格(已抽 `_gridFrame`)。
- **区间药丸**:`3月 / 6月 / 1年 / 3年 / 全部`,选中=实心暖棕。
- **序列开关**:总资产 / 净投入 / 基准 复选(保留,样式收敛)。
- **指标卡**:沿用 5 张(XIRR/TWR/最大回撤/年化波动/基准CAGR),样式精修(标签更小、数值更大、留白)。
- **堆叠图**:保留,仍需 ≥2 快照(保留网格框 + 提示);配色/圆角微调。
- 空/稀疏态:网格框 + 基准线(若有)+ 居中提示「攒几天就有曲线了」。

**取舍说明**:不对齐 → 持仓与基准各自缩放,共用绘图框但不强求同刻度。抬头大数字始终是「总资产」,涨跌按所选区间算,给用户「一眼看现状」。

## 8. 架构分层(改动文件)

| 文件 | 改动 |
|---|---|
| `app/models.py` | 加 `BenchmarkPoint` 表 |
| `app/quotes.py` | `parse_em_kline` + `fetch_index_history`(东财日线,解析/网络分离) |
| `app/snapshot_service.py` | `backfill_benchmark` + 调度器启动补抓 + `run_snapshot` 末尾写当天 close;`build_history` 读 `BenchmarkPoint`、按窗口算基准指标、返回 `benchmark_series`;区间 → 日期窗口 |
| `app/seed.py` | `reset_to_default` 清 `BenchmarkPoint` |
| `app/routers/api.py` | `/api/history` 接受新 range 值(6m/3y),透传 |
| `static/js/app.js` | 走势页重做:抬头大数字、渐变填充主线、日期轴、沪深300 自适应叠线、药丸区间、末点圆点 |
| `templates/index.html` | 走势面板结构调整(抬头大数字区 + 药丸按钮组) |
| `static/css/style.css` | Robinhood 极简风样式(渐变、药丸、抬头、卡片精修) |

## 9. 测试

- `quotes.parse_em_kline`:假东财 K 线 JSON → `[(date, close)]`;空/坏数据不崩。
- `snapshot_service.backfill_benchmark`:`FakeIndexSource` → upsert 进表;再跑一次幂等(按 date 不重复)。
- `build_history`:造 `BenchmarkPoint` + `Snapshot`,断言 `benchmark_series` 按窗口裁剪、基准指标来自 `BenchmarkPoint`(≥2 点即有数,哪怕 0 持仓快照);range 6m/3y 窗口正确;空表稀疏态。
- API:`GET /api/history?range=6m`/`3y` 结构含 `benchmark_series`;非法 range 回退。
- 前端:`node --check` + headless-Chrome 渲染(渐变 `<linearGradient>`/`fill`、药丸数=5、沪深300 线在 0 快照时也出现、抬头大数字、hideAmounts 掩码)。
- 全程假数据,绝不打真实网络;核心覆盖率 gate 保持(`calc`/`services` ≥95%)。

## 10. 关键决策小结(待并入 architecture.md)

1. **沪深300 历史线落 `BenchmarkPoint` 表**(与快照解耦,持久化「不可重算」的指数收盘);东财日线抓取,解析/网络分离;backfill 在调度器启动跑、`/api/history` 只读表(页面零网络)。
2. **基准独立于持仓**:基准线 + 基准指标按历史线算、立即有数;**不与持仓对齐**(各自缩放,形状参照)。
3. **区间驱动日期窗口**:`3月/6月/1年/3年/全部`,持仓与基准同窗口。
4. **走势页 Robinhood 极简风**:大数字抬头 + 渐变填充主线 + 药丸区间 + 暖纸底 + 末点圆点;空/稀疏态网格框 + 基准线兜底。

## 11. 行为兼容

- 纯增量 + 走势页视觉重做;仪表盘/持仓/记录/备份一字不改。
- 不配 `BENCHMARK_CODE` 或抓取失败 → 无基准线,页面照常(持仓线照画)。
- `BenchmarkPoint` 空(首次、backfill 未完)→ 基准线暂缺,稀疏态提示。
