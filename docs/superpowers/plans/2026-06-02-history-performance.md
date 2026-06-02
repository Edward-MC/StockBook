# 历史净值 + 绩效分析(走势板块)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 StockBook 加每日净值快照、净值/绩效曲线与基准对比(沪深300),把"只有此刻"的追踪器补上时间维度。

**Architecture:** 新增 `Snapshot` 表(对"推导而非存储"的有意例外——过去市值不可重算)落每日净值;`app/snapshot_service.py` 做 ORM↔calc 胶水 + 进程内调度(仿备份调度器、另起独立 asyncio task);`app/calc.py` 加四个纯绩效函数(XIRR/TWR/最大回撤/年化波动)配 Hypothesis 不变量;新 tab「走势」自绘 SVG、零 Node。

**Tech Stack:** Python 3.9(`typing.Optional/List/Dict`,**不用** `X|None`)、FastAPI、SQLAlchemy 2.0、SQLite、pytest + Hypothesis、原生 JS + 自绘 SVG。

---

## 关键约束(每个 Task 都要守)

- **绝不碰真实 `stockbook.db`**:测试一律走 conftest 的临时库;绝不裸跑打真实库的脚本。
- **Python 3.9**:`Optional[X]`/`List[X]`/`Dict[K,V]`,不写 `X | None`。
- **review 前置于 commit**:subagent-driven 下,实现子代理只 `git add` 暂存、**绝不 commit**;控制方核验 + 两段 review 通过后才 commit。本计划每个 Task 的「Commit」步给控制方用。
- **跑测试**:`.venv/bin/pytest -q`(不要 `source`;pytest 已在白名单)。
- 提交信息末尾附:`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

## 文件结构(本计划要动的文件)

| 文件 | 创建/修改 | 职责 |
|---|---|---|
| `app/calc.py` | 修改(追加) | 加纯绩效函数 `xirr`/`twr`/`max_drawdown`/`annualized_volatility`(无框架依赖) |
| `app/models.py` | 修改(追加) | 加 `Snapshot` 表 |
| `app/seed.py` | 修改 | `reset_to_default` 一并清 `Snapshot`(`Snapshot` 表无加列迁移,`create_all` 建即可) |
| `app/config.py` | 修改 | `BENCHMARK_CODE`、`SNAPSHOT_INTERVAL_HOURS` |
| `app/snapshot_service.py` | **创建** | `run_snapshot`(刷价→算总额→抓基准→按 date upsert)、`build_history`(读序列+区间过滤+组装指标)、调度器三函数(仿 backup) |
| `main.py` | 修改 | lifespan 里在 backup 之外**新增** snapshot 调度任务 start/stop |
| `app/routers/api.py` | 修改(追加) | `POST /api/snapshot`、`GET /api/history` |
| `tests/conftest.py` | 修改 | autouse 里加 `SNAPSHOT_INTERVAL_HOURS=0` |
| `tests/test_calc_performance.py` | **创建** | 四个绩效函数的 example 测试 |
| `tests/test_calc_properties.py` | 修改(追加) | 绩效函数的 Hypothesis 不变量(每条变异检查) |
| `tests/test_snapshot_service.py` | **创建** | `run_snapshot` upsert / 基准 null / class_values 往返 / `build_history` 区间+指标 |
| `tests/test_api.py` 或新 `tests/test_history_api.py` | **创建/追加** | `POST /api/snapshot`、`GET /api/history` 结构与键 |
| `templates/index.html` | 修改 | 新 tab 按钮 + `panel-trends` 面板 |
| `static/js/app.js` | 修改 | tab 接线 + `renderTrends()` + SVG 折线/堆叠/指标卡 |
| `static/css/style.css` | 修改(追加) | 走势页样式(指标卡、图、图例) |
| `docs/architecture.md` | 修改 | 关键决策 / API 一览 / 功能日志三节 |

---

## Task 1: calc — 四个纯绩效函数

**Files:**
- Modify: `app/calc.py`(顶部 imports + 文件末尾追加函数)
- Test: `tests/test_calc_performance.py`(创建)

**设计要点(实现前读一遍):**
- `xirr(flows)`:`flows` 是 `List[Tuple[date, float]]`,金额**已带符号**(投资人视角:投入为负、取回为正)。求 `Σ amount/(1+r)^(days/365)=0` 的根,`days` 相对最早日期。二分区间 `[-0.9999, 10.0]`;两端 NPV 同号(无符号变化)→ 无解返回 `None`;<2 笔或全零返回 `None`。**符号约定放在调用方(snapshot_service),calc 只解数值。**
- `twr(values, flows)`:`values` 是 `List[Tuple[date, float]]`(各快照日净值,按日期升序);`flows` 是 `List[Tuple[date, float]]`,**注入为正、移出为负**(组合视角的外部净流入)。相邻快照分段 `r_i=(V_end−V_begin−net_flow_in_(begin,end])/V_begin`,链乘后**年化**。`V_begin<=0` 的段跳过;<2 个净值或跨期 0 天返回 `None`。
- `max_drawdown(nav)`:`nav` 是 `List[float]`,返回峰谷最大跌幅 ∈ `[0,1]`。空/单点返回 `0.0`。
- `annualized_volatility(nav)`:日收益 `r_i=nav[i]/nav[i-1]−1` 的样本标准差 × `√252`。<2 点或出现非正净值返回 `None`。

- [ ] **Step 1: 写失败测试 `tests/test_calc_performance.py`**

```python
"""Unit tests for the pure performance functions (history+performance spec §5)."""
import datetime as dt
import math

import pytest

from app.calc import xirr, twr, max_drawdown, annualized_volatility

D0 = dt.date(2025, 1, 1)


def d(days):
    return D0 + dt.timedelta(days=days)


# ----------------------------- xirr ------------------------------------- #
def test_xirr_double_in_one_year_is_100pct():
    # Put in 100 at t0, portfolio worth 200 a year later → IRR ≈ 100%.
    r = xirr([(d(0), -100.0), (d(365), 200.0)])
    assert r is not None
    assert abs(r - 1.0) < 1e-3


def test_xirr_no_sign_change_returns_none():
    # All outflows, never any return → no IRR.
    assert xirr([(d(0), -100.0), (d(365), -50.0)]) is None


def test_xirr_too_few_or_zero_flows_returns_none():
    assert xirr([(d(0), -100.0)]) is None
    assert xirr([]) is None
    assert xirr([(d(0), 0.0), (d(365), 0.0)]) is None


# ----------------------------- twr -------------------------------------- #
def test_twr_no_flows_matches_simple_return_sign():
    # 100 → 110 → 121, no external flows: positive TWR.
    r = twr([(d(0), 100.0), (d(182), 110.0), (d(365), 121.0)], [])
    assert r is not None and r > 0


def test_twr_strips_external_flow_from_segment():
    # Value jumps 100 → 200 but 100 of that was a deposit → segment return 0.
    r = twr([(d(0), 100.0), (d(365), 200.0)], [(d(100), 100.0)])
    assert r is not None and abs(r) < 1e-9


def test_twr_too_few_points_returns_none():
    assert twr([(d(0), 100.0)], []) is None
    assert twr([], []) is None


# ------------------------- max_drawdown --------------------------------- #
def test_max_drawdown_basic():
    # peak 100 → trough 60 → recover: max DD = 40%.
    assert abs(max_drawdown([100, 120, 60, 90]) - (120 - 60) / 120) < 1e-9


def test_max_drawdown_monotonic_up_is_zero():
    assert max_drawdown([1, 2, 3, 4]) == 0.0


def test_max_drawdown_empty_or_single_is_zero():
    assert max_drawdown([]) == 0.0
    assert max_drawdown([42.0]) == 0.0


# --------------------- annualized_volatility ---------------------------- #
def test_volatility_constant_series_is_zero():
    assert annualized_volatility([100, 100, 100, 100]) == 0.0


def test_volatility_constant_ratio_is_zero():
    # Equal periodic returns → zero dispersion → zero volatility.
    assert abs(annualized_volatility([100, 110, 121, 133.1])) < 1e-9


def test_volatility_too_few_points_returns_none():
    assert annualized_volatility([100.0]) is None
    assert annualized_volatility([]) is None


def test_volatility_positive_for_varying_returns():
    v = annualized_volatility([100, 110, 100, 115, 95])
    assert v is not None and v > 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest tests/test_calc_performance.py -q`
Expected: FAIL — `ImportError: cannot import name 'xirr' from 'app.calc'`。

- [ ] **Step 3: 在 `app/calc.py` 顶部补 imports**

把第 12–15 行的 import 区改成(加 `datetime`/`math`/`statistics`、`Tuple`):

```python
from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple
```

- [ ] **Step 4: 在 `app/calc.py` 末尾追加四个函数**

```python
# --------------------------------------------------------------------------- #
# Performance analytics (history+performance spec §5). Pure: dates + floats in,
# float/None out. Sign conventions live in the caller (snapshot_service);
# these only crunch numbers so they stay trivially testable.
# --------------------------------------------------------------------------- #
def _npv(rate: float, flows: Sequence[Tuple[dt.date, float]], d0: dt.date) -> float:
    """Net present value of signed dated flows at annual `rate` (ACT/365)."""
    total = 0.0
    for d, amt in flows:
        years = (d - d0).days / 365.0
        total += amt / ((1.0 + rate) ** years)
    return total


def xirr(flows: Sequence[Tuple[dt.date, float]]) -> Optional[float]:
    """Money-weighted IRR of signed dated cash flows (investor view: money in
    negative, money/value out positive). Solves Σ amt/(1+r)^(days/365)=0 by
    bisection on [-0.9999, 10]. Returns None when undefined: <2 flows, all
    zero, or no sign change in NPV across the search interval."""
    flows = [(d, float(a)) for d, a in flows]
    if len(flows) < 2:
        return None
    if all(a == 0.0 for _, a in flows):
        return None
    d0 = min(d for d, _ in flows)
    lo, hi = -0.9999, 10.0
    f_lo, f_hi = _npv(lo, flows, d0), _npv(hi, flows, d0)
    if f_lo == 0.0:
        return lo
    if f_hi == 0.0:
        return hi
    if (f_lo > 0) == (f_hi > 0):  # same sign → no bracketed root
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = _npv(mid, flows, d0)
        if abs(f_mid) < 1e-9 or (hi - lo) < 1e-12:
            return mid
        if (f_mid > 0) == (f_lo > 0):
            lo, f_lo = mid, f_mid
        else:
            hi, f_hi = mid, f_mid
    return (lo + hi) / 2.0


def twr(values: Sequence[Tuple[dt.date, float]],
        flows: Sequence[Tuple[dt.date, float]]) -> Optional[float]:
    """Time-weighted return, annualized. `values`=(date, portfolio value) per
    snapshot (ascending); `flows`=(date, external net inflow: deposits +,
    withdrawals −). Segment return r_i=(V_end−V_begin−netflow_in_(begin,end])
    /V_begin, chained then annualized over the span. Daily snapshots can only
    pin flows to segment endpoints — an approximation (spec §5). Returns None
    if <2 valid values or zero span."""
    vals = sorted(values, key=lambda x: x[0])
    if len(vals) < 2:
        return None
    growth = 1.0
    for (d_begin, v_begin), (d_end, v_end) in zip(vals, vals[1:]):
        if v_begin <= 0:
            continue
        net_flow = sum(a for fd, a in flows if d_begin < fd <= d_end)
        r = (v_end - v_begin - net_flow) / v_begin
        growth *= (1.0 + r)
    span_days = (vals[-1][0] - vals[0][0]).days
    if span_days <= 0 or growth <= 0:
        return None
    years = span_days / 365.0
    return growth ** (1.0 / years) - 1.0


def max_drawdown(nav: Sequence[float]) -> float:
    """Largest peak-to-trough drop of a NAV series, in [0, 1]. 0.0 for empty,
    single-point, or monotonically non-decreasing series."""
    peak = None
    worst = 0.0
    for v in nav:
        if peak is None or v > peak:
            peak = v
        if peak and peak > 0:
            worst = max(worst, (peak - v) / peak)
    return worst


def annualized_volatility(nav: Sequence[float]) -> Optional[float]:
    """Sample std-dev of daily simple returns × √252. None if <2 points or any
    non-positive NAV (return undefined). 0.0 for constant or constant-ratio
    series. NOTE: √252 assumes regular trading-day sampling; sparse/irregular
    snapshots inflate noise — treat as indicative only (spec §5)."""
    nav = list(nav)
    if len(nav) < 2 or any(v <= 0 for v in nav):
        return None
    rets = [nav[i] / nav[i - 1] - 1.0 for i in range(1, len(nav))]
    if len(rets) < 2:
        return abs(rets[0]) * math.sqrt(252.0)  # single return: |r|·√252
    return statistics.stdev(rets) * math.sqrt(252.0)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/bin/pytest tests/test_calc_performance.py -q`
Expected: PASS(全部用例)。

- [ ] **Step 6: 跑全套确认没破坏既有**

Run: `.venv/bin/pytest -q`
Expected: 全绿。

- [ ] **Step 7: Commit(控制方执行)**

```bash
git add app/calc.py tests/test_calc_performance.py
git commit -m "feat(calc): pure performance functions (xirr/twr/max_drawdown/volatility)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: calc — 绩效函数的 Hypothesis 不变量

**Files:**
- Test: `tests/test_calc_properties.py`(在文件末尾追加)

> **修正说明(控制方留意):** spec §5 把"净值单调升 → 波动=0"写松了 —— 单调升只蕴含**回撤=0**,不蕴含波动=0(波动衡量收益离散度,与方向无关)。本计划据此修正为正确不变量:单调升 ⇒ 回撤=0;**常数/等比**序列 ⇒ 波动=0。落地 architecture 时按修正版写。

- [ ] **Step 1: 在 `tests/test_calc_properties.py` 顶部 import 区补两行**

文件顶部已有 `import math`、`from hypothesis import ...`、`from app.calc import (...)`。在文件顶部 import 区加 `import datetime as dt`,并在 `from app.calc import (...)` 块里追加 `max_drawdown, annualized_volatility, twr, xirr`(与现有 `average_cost, compute_dashboard, ...` 并列)。

- [ ] **Step 2: 在 `tests/test_calc_properties.py` 末尾追加不变量**

```python
# =========================================================================== #
# 绩效函数不变量(history+performance spec §5)。每条经变异检查(见 Step 3)。
# =========================================================================== #
navs = st.lists(st.floats(min_value=0.01, max_value=1e7, allow_nan=False,
                          allow_infinity=False), min_size=0, max_size=50)


# I-DD1: 回撤恒在 [0,1](变异:把 max(...) 写成绝对差不除 peak → 抓到 >1)。
@given(navs)
def test_inv_drawdown_in_unit_interval(nav):
    dd = max_drawdown(nav)
    assert 0.0 <= dd <= 1.0


# I-DD2: 单调非降 → 回撤=0(变异:peak 不更新 → 抓到 >0)。
@given(navs)
def test_inv_monotonic_up_zero_drawdown(nav):
    mono = sorted(nav)
    assert max_drawdown(mono) == 0.0


# I-DD3: 空/单点 → 回撤=0。
def test_inv_drawdown_empty_single():
    assert max_drawdown([]) == 0.0
    assert max_drawdown([3.14]) == 0.0


# I-VOL1: 常数序列 → 波动=0(变异:stdev 用了均值偏移以外的东西 → 抓到 !=0)。
@given(st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
       st.integers(min_value=2, max_value=30))
def test_inv_constant_zero_volatility(v, n):
    assert annualized_volatility([v] * n) == 0.0


# I-VOL2: 等比数列(等收益率)→ 波动≈0(变异:用总收益代替逐期收益 → 抓到 >0)。
@given(st.floats(min_value=0.01, max_value=10.0, allow_nan=False, allow_infinity=False),
       st.integers(min_value=2, max_value=20))
def test_inv_constant_ratio_zero_volatility(ratio, n):
    assume(ratio > 0)
    nav = [100.0]
    for _ in range(n):
        nav.append(nav[-1] * (1.0 + ratio))
    v = annualized_volatility(nav)
    assert v is not None and v < 1e-6


# I-VOL3: <2 点 → None;任意合法序列不崩、非负(变异:漏 None 守卫 → 抓到崩)。
@given(navs)
def test_inv_volatility_never_negative_or_nan(nav):
    v = annualized_volatility(nav)
    assert v is None or (v >= 0 and not math.isnan(v))


# I-XIRR1: 翻倍且无中途流入 → ≈ 期间复合年化(变异:days/365 漏掉 → 抓到偏差)。
@given(st.floats(min_value=1.1, max_value=10.0, allow_nan=False, allow_infinity=False),
       st.integers(min_value=180, max_value=1500))
def test_inv_xirr_matches_compound(multiple, days):
    d0 = dt.date(2025, 1, 1)
    r = xirr([(d0, -100.0), (d0 + dt.timedelta(days=days), 100.0 * multiple)])
    expected = multiple ** (365.0 / days) - 1.0
    assert r is not None and abs(r - expected) < 1e-3


# I-XIRR2: 任意流序列不崩、要么 None 要么有限(变异:无 None 守卫 → 抓到崩/NaN)。
flow_lists = st.lists(
    st.tuples(st.integers(min_value=0, max_value=2000),
              st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False)),
    min_size=0, max_size=20)


@given(flow_lists)
def test_inv_xirr_no_crash(raw):
    d0 = dt.date(2025, 1, 1)
    flows = [(d0 + dt.timedelta(days=k), a) for k, a in raw]
    r = xirr(flows)
    assert r is None or (isinstance(r, float) and not math.isnan(r))


# I-TWR1: 无外部流时 TWR 与"末/首−1"同号(变异:net_flow 符号反 → 抓到异号)。
@given(st.lists(st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
                min_size=2, max_size=30))
def test_inv_twr_sign_matches_simple(vals):
    assume(vals[0] > 0)
    d0 = dt.date(2025, 1, 1)
    series = [(d0 + dt.timedelta(days=i), v) for i, v in enumerate(vals)]
    r = twr(series, [])
    simple = vals[-1] / vals[0] - 1.0
    if r is not None and abs(simple) > 1e-6:
        assert (r > 0) == (simple > 0)
```

- [ ] **Step 3: 跑不变量**

Run: `.venv/bin/pytest tests/test_calc_properties.py -q`
Expected: PASS。

- [ ] **Step 4: 变异检查(逐条确认"有牙")**

对每条不变量,临时改坏 `calc.py` 对应实现、跑该测试**确认 FAIL**、再改回。最少做这几处:
- `max_drawdown`:把 `worst = max(...)` 改成 `worst = (peak - v)`(不除 peak)→ `test_inv_drawdown_in_unit_interval` 应 FAIL。
- `annualized_volatility`:把 `statistics.stdev` 换成 `statistics.mean` → `test_inv_constant_ratio_zero_volatility` 仍可能过,但 `test_inv_volatility_never_negative_or_nan` 配 `test_volatility_positive_for_varying_returns` 应能抓;主要确认 `test_inv_constant_zero_volatility` 在去掉 `len(rets)<2` 分支或改 mean 时 FAIL。
- `xirr`:把 `years = (d - d0).days / 365.0` 改成 `= 1.0` → `test_inv_xirr_matches_compound` 应 FAIL。
- `twr`:把 `net_flow` 前加负号 → `test_inv_twr_sign_matches_simple` 在有流场景应 FAIL(此条无流,改测 `test_twr_strips_external_flow_from_segment` 也行)。
改回后 `.venv/bin/pytest tests/test_calc_properties.py tests/test_calc_performance.py -q` 全绿。

- [ ] **Step 5: Commit(控制方)**

```bash
git add tests/test_calc_properties.py
git commit -m "test(calc): Hypothesis invariants for performance functions (mutation-checked)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Snapshot 模型 + reset 清表

**Files:**
- Modify: `app/models.py`(末尾加表)
- Modify: `app/seed.py`(`reset_to_default` 加 `Snapshot`)
- Test: `tests/test_snapshot_service.py`(创建——本 Task 只放建表/清表用例,后续 Task 追加)

**设计要点:** 新**表**靠 `create_all`,无需 `_migrate` 加列。`class_values` 用 `Text` 存 JSON(`{asset_class_id: market_value}`)。`date` 唯一(每天一条)。

- [ ] **Step 1: 写失败测试 `tests/test_snapshot_service.py`**

```python
"""Tests for snapshot capture + history assembly (history+performance spec)."""
import datetime as dt
import json

from app import database, models, seed


def test_snapshot_table_exists_and_roundtrips(client):
    # client fixture已建表并把 database.SessionLocal 绑到临时库。MUST access via
    # `database.SessionLocal` (attribute lookup at call time) — a top-level
    # `from app.database import SessionLocal` would capture the pre-rebind object.
    db = database.SessionLocal()
    try:
        snap = models.Snapshot(
            date=dt.date(2025, 6, 1), total_assets=123.0, net_invested=100.0,
            benchmark=4000.0, class_values=json.dumps({"1": 50.0, "2": 73.0}),
        )
        db.add(snap)
        db.commit()
        got = db.query(models.Snapshot).one()
        assert got.total_assets == 123.0
        assert json.loads(got.class_values) == {"1": 50.0, "2": 73.0}
    finally:
        db.close()


def test_reset_clears_snapshots(client):
    db = database.SessionLocal()
    try:
        db.add(models.Snapshot(date=dt.date(2025, 6, 1), total_assets=1.0,
                               net_invested=1.0, benchmark=None, class_values="{}"))
        db.commit()
        seed.reset_to_default(db)
        assert db.query(models.Snapshot).count() == 0
    finally:
        db.close()
```

- [ ] **Step 2: 跑确认失败**

Run: `.venv/bin/pytest tests/test_snapshot_service.py -q`
Expected: FAIL — `AttributeError: module 'app.models' has no attribute 'Snapshot'`。

- [ ] **Step 3: 在 `app/models.py` 末尾加 `Snapshot`**

```python
class Snapshot(Base):
    """Daily net-asset-value snapshot — a deliberate exception to the project's
    "derive, don't store" rule: a past day's market value can't be recomputed
    (quotes have moved on), so the time series must be persisted. One row per
    date (upserted). class_values is a JSON map {asset_class_id: market_value}
    for the stacked-area chart."""
    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False, unique=True)
    total_assets: Mapped[float] = mapped_column(Float, nullable=False)
    net_invested: Mapped[float] = mapped_column(Float, nullable=False)
    benchmark: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    class_values: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=func.now()
    )
```

- [ ] **Step 4: 在 `app/seed.py` 让 reset 清 `Snapshot`**

把 import(第 14–15 行)加上 `Snapshot`:

```python
from .models import (AssetClass, CashFlow, KnowledgeChunk, NotionSource,
                     PriceQuote, Security, Snapshot, Strategy, Transaction)
```

把 `reset_to_default` 的 model 元组加上 `Snapshot`:

```python
    for model in (Transaction, PriceQuote, Security, CashFlow, AssetClass, Strategy,
                  KnowledgeChunk, NotionSource, Snapshot):
        db.query(model).delete()
```

- [ ] **Step 5: 跑确认通过**

Run: `.venv/bin/pytest tests/test_snapshot_service.py -q`
Expected: PASS(两条)。

- [ ] **Step 6: Commit(控制方)**

```bash
git add app/models.py app/seed.py tests/test_snapshot_service.py
git commit -m "feat(model): Snapshot table for daily NAV time series; reset clears it

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 配置项 `BENCHMARK_CODE` / `SNAPSHOT_INTERVAL_HOURS`

**Files:**
- Modify: `app/config.py`(末尾追加)
- Modify: `tests/conftest.py`(autouse 关自动快照)

- [ ] **Step 1: 在 `app/config.py` 末尾追加**

```python
# History + performance (daily NAV snapshots). BENCHMARK_CODE carries an explicit
# market prefix (sh000300 = 沪深300) because indices are not stocks and the
# per-stock code→market heuristic would mis-route them; empty = skip benchmark.
# INTERVAL 0 disables the in-process daily-snapshot scheduler.
BENCHMARK_CODE = os.getenv("STOCKBOOK_BENCHMARK_CODE", "sh000300")
SNAPSHOT_INTERVAL_HOURS = int(os.getenv("STOCKBOOK_SNAPSHOT_INTERVAL_HOURS", "24"))
```

- [ ] **Step 2: 在 `tests/conftest.py` 的 `_clean_rag_flags` 里加一行**

在 `monkeypatch.setattr(config, "BACKUP_DIR", "")` 之后加:

```python
    monkeypatch.setattr(config, "SNAPSHOT_INTERVAL_HOURS", 0)  # no auto-snapshot in tests
```

- [ ] **Step 3: 跑全套确认没破坏**

Run: `.venv/bin/pytest -q`
Expected: 全绿(此步纯配置,不应改变行为)。

- [ ] **Step 4: Commit(控制方)**

```bash
git add app/config.py tests/conftest.py
git commit -m "feat(config): BENCHMARK_CODE (sh000300) + SNAPSHOT_INTERVAL_HOURS; tests disable auto-snapshot

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: snapshot_service — `run_snapshot`(刷价 → 算总额 → 抓基准 → upsert)

**Files:**
- Create: `app/snapshot_service.py`
- Test: `tests/test_snapshot_service.py`(追加)

**设计要点:**
- **先刷持仓行情**(best-effort,`httpx.HTTPError` 吞掉),否则 `build_ledger` 读到陈价。
- `total_assets`/`net_invested` 取自 `build_ledger(db)["summary"]`。
- `class_values` 取自 `build_dashboard(db, readonly=False, hide_amounts=False)["asset_classes"]`,`{str(ac["id"]): ac["market_value"]}`;dashboard 为 None(空库无策略)→ `{}`。
- 基准:`BENCHMARK_CODE="sh000300"` → 拆成 `(code="000300", market="SH")`(带 `sh/sz` 前缀则用前缀,否则 market=`"CN"`),`fetch_quotes([(code, market)])` 取 `fetched.get(code)["price"]`;抓不到/空 code → `None`。
- **upsert by `date.today()`**:存在则更新该行,不存在则插入。

- [ ] **Step 1: 在 `tests/test_snapshot_service.py` 追加 run_snapshot 用例**

```python
import pytest

from app import snapshot_service


@pytest.fixture()
def fake_quotes(monkeypatch):
    """Stub fetch_quotes so run_snapshot never hits the network. Returns a price
    for every requested code (seeded securities + the benchmark)."""
    calls = {"codes": []}

    def _fake(items, sources=None):
        out = {}
        for code, market in items:
            calls["codes"].append(code)
            out[code] = {"price": 9.99, "name": f"FAKE{code}"}
        return out

    monkeypatch.setattr(snapshot_service.quotes, "fetch_quotes", _fake)
    return calls


def test_run_snapshot_writes_one_row(client, fake_quotes):
    db = database.SessionLocal()
    try:
        snap = snapshot_service.run_snapshot(db)
        assert snap.date == dt.date.today()
        assert snap.total_assets is not None
        # benchmark code 000300 was requested and resolved → not None
        assert snap.benchmark == 9.99
        assert json.loads(snap.class_values)  # non-empty for the seeded strategy
    finally:
        db.close()


def test_run_snapshot_upserts_same_day(client, fake_quotes):
    db = database.SessionLocal()
    try:
        snapshot_service.run_snapshot(db)
        snapshot_service.run_snapshot(db)
        assert db.query(models.Snapshot).count() == 1  # one row per date
    finally:
        db.close()


def test_run_snapshot_benchmark_null_when_unfetchable(client, monkeypatch):
    # fetch_quotes returns nothing for the benchmark → benchmark stored as None,
    # snapshot still succeeds.
    def _empty(items, sources=None):
        return {}
    monkeypatch.setattr(snapshot_service.quotes, "fetch_quotes", _empty)
    db = database.SessionLocal()
    try:
        snap = snapshot_service.run_snapshot(db)
        assert snap.benchmark is None
    finally:
        db.close()
```

- [ ] **Step 2: 跑确认失败**

Run: `.venv/bin/pytest tests/test_snapshot_service.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.snapshot_service'`。

- [ ] **Step 3: 创建 `app/snapshot_service.py`(本 Task 只写到 `run_snapshot`)**

```python
"""Glue between the ORM and the performance calc engine, plus daily-snapshot
capture and scheduling (history+performance spec §4/§6).

Mirrors the calc/services split: pure analytics live in calc.py; here we read
live rows, capture a daily NAV Snapshot, and assemble the /api/history payload.
The scheduler mirrors app.backup's in-process loop as a *second independent*
asyncio task (different cadence) — see main.py lifespan wiring.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Dict, List, Optional, Tuple

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import calc, config, quotes
from .models import CashFlow, PriceQuote, Security, Snapshot
from .services import build_dashboard, build_ledger

_log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Benchmark code parsing: indices carry an explicit sh/sz prefix (spec §6).
# --------------------------------------------------------------------------- #
def _parse_benchmark(code: str) -> Optional[Tuple[str, str]]:
    """('sh000300') -> ('000300', 'SH'); bare digits -> (code, 'CN'); ''->None."""
    c = (code or "").strip()
    if not c:
        return None
    low = c.lower()
    if low.startswith(("sh", "sz")):
        return c[2:], low[:2].upper()
    return c, "CN"


def _fetch_benchmark() -> Optional[float]:
    """Fetch the benchmark index point via the multi-source quote chain.
    Returns None on empty config / fetch failure / no data (never raises)."""
    parsed = _parse_benchmark(config.BENCHMARK_CODE)
    if parsed is None:
        return None
    code, market = parsed
    try:
        fetched = quotes.fetch_quotes([(code, market)])
    except httpx.HTTPError as exc:
        _log.warning("benchmark fetch failed: %s", exc)
        return None
    q = fetched.get(code)
    return q["price"] if q else None


def _refresh_holding_quotes(db: Session) -> None:
    """Best-effort refresh of holding prices before snapshotting, so the daily
    NAV isn't recorded from stale PriceQuote rows. Swallows transport errors —
    a snapshot from last-known prices beats no snapshot."""
    securities = db.scalars(select(Security)).all()
    if not securities:
        return
    try:
        fetched = quotes.fetch_quotes([(s.code, s.market) for s in securities])
    except httpx.HTTPError as exc:
        _log.warning("snapshot price refresh failed, using last-known: %s", exc)
        return
    now = dt.datetime.now()
    for sec in securities:
        q = fetched.get(sec.code)
        if not q:
            continue
        if sec.quote is None:
            sec.quote = PriceQuote(security_id=sec.id)
        sec.quote.price = q["price"]
        sec.quote.source = "auto"
        sec.quote.updated_at = now
        if q.get("name") and sec.name == sec.code:
            sec.name = q["name"]
    db.commit()


def _class_values(db: Session) -> Dict[str, float]:
    dash = build_dashboard(db, readonly=False, hide_amounts=False)
    if dash is None:
        return {}
    return {str(ac["id"]): ac["market_value"] for ac in dash["asset_classes"]}


def run_snapshot(db: Session) -> Snapshot:
    """Capture (or refresh) today's NAV snapshot. Refreshes holding quotes,
    derives total_assets/net_invested from the ledger, per-class market values
    from the dashboard, fetches the benchmark, then upserts by today's date.
    Reads live rows + writes one Snapshot row (and refreshed quotes); never
    touches transactions."""
    _refresh_holding_quotes(db)
    summary = build_ledger(db)["summary"]
    class_values = _class_values(db)
    benchmark = _fetch_benchmark()

    today = dt.date.today()
    snap = db.scalars(select(Snapshot).where(Snapshot.date == today)).first()
    if snap is None:
        snap = Snapshot(date=today)
        db.add(snap)
    snap.total_assets = summary["total_assets"]
    snap.net_invested = summary["net_invested"]
    snap.benchmark = benchmark
    snap.class_values = json.dumps(class_values)
    db.commit()
    db.refresh(snap)
    return snap
```

- [ ] **Step 4: 跑确认通过**

Run: `.venv/bin/pytest tests/test_snapshot_service.py -q`
Expected: PASS(含前序 Task 3 的两条 + 本 Task 三条)。

- [ ] **Step 5: 跑全套**

Run: `.venv/bin/pytest -q`
Expected: 全绿。

- [ ] **Step 6: Commit(控制方)**

```bash
git add app/snapshot_service.py tests/test_snapshot_service.py
git commit -m "feat(snapshot): run_snapshot — refresh quotes, capture daily NAV, upsert by date

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: snapshot_service — `build_history`(读序列 + 区间过滤 + 指标)

**Files:**
- Modify: `app/snapshot_service.py`(追加 `build_history`)
- Test: `tests/test_snapshot_service.py`(追加)

**设计要点:**
- 读全部 `Snapshot` 按 `date` 升序。
- **区间相对最后一条快照日**(非系统 today,便于确定性测试):`3m`=末日−90 天、`1y`=末日−365 天、`all`=不过滤。
- 指标在**过滤后窗口**上算(与图一致):
  - `nav = [s.total_assets ...]` → `max_drawdown(nav)`、`annualized_volatility(nav)`。
  - `twr(values, flows)`:`values=[(date,total_assets)]`;`flows` = 窗口内 `CashFlow`(`in`→+amount,`out`→−amount)。
  - `xirr`:窗口起点净值作期初一笔流出 `[(start_date, -V_start)]` + 窗口内现金流(投资人视角:`in`→−amount、`out`→+amount)+ 期末 `[(end_date, +V_end)]`。
  - 基准:`bench_nav=[s.benchmark ...]`(滤掉 None),`growth`=末/首−1、`cagr`=点对点年化、`max_drawdown(bench_nav)`;基准点 <2 → 各为 None。
- `class_names`:当前 `AssetClass` 的 `{str(id): {"name","color"}}`(已删除大类不在内;前端兜底)。
- 空表:`series=[]`、`metrics` 各 None、`class_names={}`。

- [ ] **Step 1: 在 `tests/test_snapshot_service.py` 追加**

```python
def _add_snap(db, day, total, net, bench, cvals):
    db.add(models.Snapshot(date=day, total_assets=total, net_invested=net,
                           benchmark=bench, class_values=json.dumps(cvals)))


def test_build_history_structure_and_range(client):
    db = database.SessionLocal()
    try:
        base = dt.date(2025, 1, 1)
        # 400 days of synthetic snapshots, value rising.
        for i in range(0, 400, 10):
            _add_snap(db, base + dt.timedelta(days=i), 100.0 + i, 100.0,
                      4000.0 + i, {"1": 50.0 + i, "2": 50.0})
        db.commit()

        all_h = snapshot_service.build_history(db, range_="all")
        assert len(all_h["series"]) == 40
        for key in ("xirr", "twr", "max_drawdown", "volatility", "benchmark"):
            assert key in all_h["metrics"]
        assert isinstance(all_h["class_names"], dict)

        # 3m window is relative to the LAST snapshot date → fewer rows.
        m3 = snapshot_service.build_history(db, range_="3m")
        assert len(m3["series"]) < len(all_h["series"])
        # all rows within the window
        last = all_h["series"][-1]["date"]
        assert all(s["date"] >= (dt.date.fromisoformat(last) - dt.timedelta(days=90)).isoformat()
                   for s in m3["series"])
    finally:
        db.close()


def test_build_history_empty(client):
    db = database.SessionLocal()
    try:
        db.query(models.Snapshot).delete()
        db.commit()
        h = snapshot_service.build_history(db, range_="all")
        assert h["series"] == []
        assert h["metrics"]["xirr"] is None
        assert h["metrics"]["max_drawdown"] is None
    finally:
        db.close()


def test_build_history_class_names_from_current_classes(client):
    db = database.SessionLocal()
    try:
        _add_snap(db, dt.date(2025, 6, 1), 100.0, 100.0, None, {"1": 100.0})
        db.commit()
        h = snapshot_service.build_history(db, range_="all")
        # seeded strategy has asset classes → class_names non-empty, each a dict
        assert all(set(v.keys()) == {"name", "color"} for v in h["class_names"].values())
    finally:
        db.close()
```

- [ ] **Step 2: 跑确认失败**

Run: `.venv/bin/pytest tests/test_snapshot_service.py -q`
Expected: FAIL — `AttributeError: module 'app.snapshot_service' has no attribute 'build_history'`。

- [ ] **Step 3: 在 `app/snapshot_service.py` 末尾追加**

```python
# --------------------------------------------------------------------------- #
# History assembly for GET /api/history.
# --------------------------------------------------------------------------- #
_RANGE_DAYS = {"3m": 90, "1y": 365}  # "all" => no cutoff


def _cagr(nav: List[float], days: int) -> Optional[float]:
    if len(nav) < 2 or nav[0] <= 0 or days <= 0:
        return None
    return (nav[-1] / nav[0]) ** (365.0 / days) - 1.0


def build_history(db: Session, range_: str = "all") -> dict:
    """Assemble the /api/history payload: filtered series + window metrics +
    current class names. Metrics follow the selected range so the cards stay
    consistent with the chart (spec §8)."""
    rows = db.scalars(select(Snapshot).order_by(Snapshot.date)).all()
    if rows:
        cutoff_days = _RANGE_DAYS.get(range_)
        if cutoff_days is not None:
            cutoff = rows[-1].date - dt.timedelta(days=cutoff_days)
            rows = [r for r in rows if r.date >= cutoff]

    series = [
        {
            "date": r.date.isoformat(),
            "total_assets": r.total_assets,
            "net_invested": r.net_invested,
            "benchmark": r.benchmark,
            "class_values": json.loads(r.class_values or "{}"),
        }
        for r in rows
    ]

    metrics = _window_metrics(db, rows)

    strategy = build_dashboard(db, readonly=False, hide_amounts=False)
    class_names: Dict[str, dict] = {}
    if strategy is not None:
        for ac in strategy["asset_classes"]:
            class_names[str(ac["id"])] = {"name": ac["name"], "color": ac["color"]}

    return {"series": series, "metrics": metrics, "class_names": class_names}


def _window_metrics(db: Session, rows: List[Snapshot]) -> dict:
    none_bench = {"growth": None, "cagr": None, "max_drawdown": None}
    if not rows:
        return {"xirr": None, "twr": None, "max_drawdown": None,
                "volatility": None, "benchmark": none_bench}
    if len(rows) < 2:
        # A single point: drawdown is well-defined (0), but XIRR/TWR/vol are not
        # (need ≥2 points). Avoid the degenerate same-date XIRR (NPV≡0).
        return {"xirr": None, "twr": None,
                "max_drawdown": calc.max_drawdown([rows[0].total_assets]),
                "volatility": None, "benchmark": none_bench}
    nav = [r.total_assets for r in rows]
    start_date, end_date = rows[0].date, rows[-1].date

    # External cash flows within the window (inclusive of endpoints).
    cfs = db.scalars(
        select(CashFlow).where(CashFlow.date >= start_date, CashFlow.date <= end_date)
    ).all()
    twr_flows = [(cf.date, cf.amount if cf.direction == "in" else -cf.amount) for cf in cfs]

    values = [(r.date, r.total_assets) for r in rows]
    # XIRR (investor view): window-start value out (−), deposits −, withdrawals +,
    # window-end value in (+).
    xirr_flows = [(start_date, -rows[0].total_assets)]
    for cf in cfs:
        xirr_flows.append((cf.date, -cf.amount if cf.direction == "in" else cf.amount))
    xirr_flows.append((end_date, rows[-1].total_assets))

    bench_nav = [r.benchmark for r in rows if r.benchmark is not None]
    bench_days = (end_date - start_date).days
    bench = {
        "growth": (bench_nav[-1] / bench_nav[0] - 1.0) if len(bench_nav) >= 2 and bench_nav[0] else None,
        "cagr": _cagr(bench_nav, bench_days),
        "max_drawdown": calc.max_drawdown(bench_nav) if len(bench_nav) >= 2 else None,
    }
    return {
        "xirr": calc.xirr(xirr_flows),
        "twr": calc.twr(values, twr_flows),
        "max_drawdown": calc.max_drawdown(nav),
        "volatility": calc.annualized_volatility(nav),
        "benchmark": bench,
    }
```

- [ ] **Step 4: 跑确认通过**

Run: `.venv/bin/pytest tests/test_snapshot_service.py -q`
Expected: PASS。

- [ ] **Step 5: Commit(控制方)**

```bash
git add app/snapshot_service.py tests/test_snapshot_service.py
git commit -m "feat(snapshot): build_history — range-filtered series + windowed metrics

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: 调度器(仿 backup)+ main.py lifespan 接线

**Files:**
- Modify: `app/snapshot_service.py`(追加 `start_scheduler`/`stop_scheduler`/`_scheduler_loop`)
- Modify: `main.py`(lifespan 新增 snapshot 任务)
- Test: `tests/test_snapshot_service.py`(追加:间隔=0 → 不起;客户端起停不崩)

**设计要点:** 完全仿 `app/backup.py` 的 `_scheduler_loop`/`start_scheduler`/`stop_scheduler`,但认 `SNAPSHOT_INTERVAL_HOURS`、调 `run_snapshot`、用独立 `SessionLocal`。`READONLY` 下不自动。**不测定时器本身**(只测 `start_scheduler` 在间隔=0 返回 None、`run_snapshot` 已在 Task 5 覆盖)。

- [ ] **Step 1: 在 `tests/test_snapshot_service.py` 追加**

```python
def test_start_scheduler_disabled_when_interval_zero(monkeypatch):
    monkeypatch.setattr(config, "SNAPSHOT_INTERVAL_HOURS", 0)
    assert snapshot_service.start_scheduler() is None


# 顶部 import 区补:from app import config
```

(在该测试文件已有的 import 区加 `from app import config`。)

- [ ] **Step 2: 跑确认失败**

Run: `.venv/bin/pytest tests/test_snapshot_service.py::test_start_scheduler_disabled_when_interval_zero -q`
Expected: FAIL — `AttributeError: ... has no attribute 'start_scheduler'`。

- [ ] **Step 3: 在 `app/snapshot_service.py` 末尾追加调度器**

```python
# --------------------------------------------------------------------------- #
# In-process scheduler — a SECOND independent asyncio task (mirrors app.backup;
# different cadence: SNAPSHOT_INTERVAL_HOURS, default 24h). Started/stopped by
# main lifespan. Catches up today's snapshot on startup, then every interval.
# --------------------------------------------------------------------------- #
_STARTUP_DELAY_SECS = 6.0  # let the app settle; offset from backup's 5s


def _run_once() -> None:
    from . import database
    db = database.SessionLocal()
    try:
        run_snapshot(db)
    finally:
        db.close()


async def _scheduler_loop() -> None:
    import asyncio
    from starlette.concurrency import run_in_threadpool
    await asyncio.sleep(_STARTUP_DELAY_SECS)
    while True:
        try:
            if not config.READONLY:
                await run_in_threadpool(_run_once)
        except Exception as exc:  # never let the scheduler die silently
            _log.warning("snapshot scheduler error: %s", exc)
        hours = config.SNAPSHOT_INTERVAL_HOURS
        if hours <= 0:
            return
        await asyncio.sleep(hours * 3600)


def start_scheduler():
    """Return an asyncio.Task running the loop, or None if auto-snapshot off."""
    import asyncio
    if config.SNAPSHOT_INTERVAL_HOURS <= 0:
        return None
    return asyncio.create_task(_scheduler_loop())


async def stop_scheduler(task) -> None:
    """Cancel the snapshot scheduler (no final snapshot on shutdown — startup
    catch-up already covers the day)."""
    import asyncio
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
```

- [ ] **Step 4: 在 `main.py` lifespan 接线**

把 import(第 12 行)和 lifespan 改成:

```python
from app import backup, config, snapshot_service
from app.routers import api, pages, rag
from app.seed import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    backup_task = backup.start_scheduler()
    snapshot_task = snapshot_service.start_scheduler()
    try:
        yield
    finally:
        await backup.stop_scheduler(backup_task)
        await snapshot_service.stop_scheduler(snapshot_task)
```

- [ ] **Step 5: 跑确认通过 + 全套**

Run: `.venv/bin/pytest tests/test_snapshot_service.py -q && .venv/bin/pytest -q`
Expected: 全绿。(conftest 已关 `SNAPSHOT_INTERVAL_HOURS=0`,client 起停 lifespan 不会真跑快照。)

- [ ] **Step 6: Commit(控制方)**

```bash
git add app/snapshot_service.py main.py tests/test_snapshot_service.py
git commit -m "feat(snapshot): in-process daily scheduler (mirrors backup) + lifespan wiring

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: API — `POST /api/snapshot` + `GET /api/history`

**Files:**
- Modify: `app/routers/api.py`(末尾追加两个路由 + import)
- Test: `tests/test_history_api.py`(创建)

**设计要点:** `POST /api/snapshot` 走 `require_writable`,调 `run_snapshot`,返回该行 JSON。`GET /api/history?range=` 调 `build_history`,`range` 默认 `all`,只接受 `3m|1y|all`(非法值回退 `all`)。两路由都用现有 `get_db` 依赖。

- [ ] **Step 1: 写失败测试 `tests/test_history_api.py`**

```python
"""API tests for snapshot + history (history+performance spec §8)."""
import datetime as dt
import json

from app import database, models


def _fake_quotes(monkeypatch):
    from app import snapshot_service

    def _fake(items, sources=None):
        return {code: {"price": 9.99, "name": code} for code, market in items}
    monkeypatch.setattr(snapshot_service.quotes, "fetch_quotes", _fake)


def test_post_snapshot_creates_row(client, monkeypatch):
    _fake_quotes(monkeypatch)
    r = client.post("/api/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == dt.date.today().isoformat()
    assert "total_assets" in body
    assert client.post("/api/snapshot").status_code == 200  # upsert, no error
    db = database.SessionLocal()
    try:
        assert db.query(models.Snapshot).count() == 1
    finally:
        db.close()


def test_get_history_shape(client):
    db = database.SessionLocal()
    try:
        base = dt.date(2025, 1, 1)
        for i in range(0, 30, 10):
            db.add(models.Snapshot(date=base + dt.timedelta(days=i),
                                   total_assets=100.0 + i, net_invested=100.0,
                                   benchmark=4000.0 + i, class_values=json.dumps({"1": 100.0})))
        db.commit()
    finally:
        db.close()
    r = client.get("/api/history?range=all")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["series"], list) and len(body["series"]) == 3
    assert set(body["metrics"].keys()) >= {"xirr", "twr", "max_drawdown", "volatility", "benchmark"}
    assert isinstance(body["class_names"], dict)


def test_get_history_empty(client):
    r = client.get("/api/history")
    assert r.status_code == 200
    assert r.json()["series"] == []
```

- [ ] **Step 2: 跑确认失败**

Run: `.venv/bin/pytest tests/test_history_api.py -q`
Expected: FAIL — `404`(路由不存在)。

- [ ] **Step 3: 在 `app/routers/api.py` 追加路由**

import 区(第 14 行附近)加 `snapshot_service`:

```python
from .. import backup, calc, config, quotes, schemas, snapshot_service
```

文件末尾追加:

```python
# --------------------------------------------------------------------------- #
# History + performance: daily NAV snapshot + time series / metrics.
# --------------------------------------------------------------------------- #
@router.post("/snapshot", dependencies=[Depends(require_writable)])
def take_snapshot(db: Session = Depends(get_db)):
    snap = snapshot_service.run_snapshot(db)
    return {
        "date": snap.date.isoformat(),
        "total_assets": snap.total_assets,
        "net_invested": snap.net_invested,
        "benchmark": snap.benchmark,
    }


@router.get("/history")
def history(range: str = Query("all"), db: Session = Depends(get_db)):
    range_ = range if range in ("3m", "1y", "all") else "all"
    return snapshot_service.build_history(db, range_=range_)
```

- [ ] **Step 4: 跑确认通过 + 全套 + 覆盖率 gate**

Run: `.venv/bin/pytest tests/test_history_api.py -q`
Expected: PASS。

Run: `.venv/bin/pytest -q`
Expected: 全绿。

Run: `.venv/bin/coverage run -m pytest -q && .venv/bin/coverage report --include=app/calc.py,app/services.py --fail-under=95`
Expected: PASS(`calc.py` 新增函数已被 Task 1/2 测试覆盖;若 `calc.py` 掉线,补 example 用例直到过线)。

- [ ] **Step 5: Commit(控制方)**

```bash
git add app/routers/api.py tests/test_history_api.py
git commit -m "feat(api): POST /api/snapshot + GET /api/history

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: 前端 —「走势」tab(指标卡 + SVG 折线 + 堆叠面积)

**Files:**
- Modify: `templates/index.html`(tab 按钮 + `panel-trends`)
- Modify: `static/js/app.js`(tab 接线 + `renderTrends` + SVG 渲染)
- Modify: `static/css/style.css`(走势页样式)

> **说明:** 本仓库零 Node、无 JS 测试运行器,前端不做单元 TDD;以**起服务实际点击**验证(CLAUDE.md「涉及 UI 改动起服务点一下」)。下面给完整可粘贴代码。

- [ ] **Step 1: `templates/index.html` 加 tab 按钮**

把 `.tabs`(第 5–9 行)改成(加第 4 个按钮):

```html
<div class="tabs">
  <button class="tab active" data-tab="dashboard">仪表盘</button>
  <button class="tab" data-tab="holdings">持仓</button>
  <button class="tab" data-tab="records">记录</button>
  <button class="tab" data-tab="trends">走势</button>
</div>
```

- [ ] **Step 2: `templates/index.html` 加 `panel-trends` 面板**

在 `panel-records` 面板之后(找到最后一个 `<div ... id="panel-records">…</div>` 的闭合处)追加:

```html
<div id="panel-trends" hidden>
  <div class="card">
    <div class="trends-head">
      <h2>走势 · 绩效</h2>
      <div class="range-switch" id="trend-range">
        <button data-range="3m">3月</button>
        <button data-range="1y">1年</button>
        <button data-range="all" class="active">全部</button>
      </div>
    </div>
    <div class="metric-cards" id="trend-metrics"></div>
  </div>
  <div class="card">
    <div class="trends-head">
      <h3>净值曲线</h3>
      <div class="series-toggle" id="trend-series-toggle"></div>
    </div>
    <div id="trend-chart"></div>
  </div>
  <div class="card">
    <h3>各大类市值(堆叠)</h3>
    <div id="trend-stack"></div>
    <div class="legend" id="trend-stack-legend"></div>
  </div>
</div>
```

- [ ] **Step 3: `static/js/app.js` 把 trends 接入 tab 机制**

把第 44 行 `const TABS` 与 `initTabs` 的 hash 白名单、`switchTab` 改为含 trends:

```javascript
const TABS = ["dashboard", "holdings", "records", "trends"];
```

`initTabs` 里第 42 行:

```javascript
  switchTab(["holdings", "records", "trends"].includes(hash) ? hash : "dashboard");
```

`switchTab` 末尾(第 49 行 `if (name === "records") renderRecords();` 之后)加:

```javascript
  if (name === "trends") renderTrends();
```

- [ ] **Step 4: `static/js/app.js` 末尾追加走势渲染逻辑**

```javascript
/* ===================== 走势 / 绩效 ===================== */
let TREND_RANGE = "all";
let TREND_SHOW = { total_assets: true, net_invested: true, benchmark: true };

async function renderTrends() {
  byId("trend-range").querySelectorAll("button").forEach(b => {
    b.classList.toggle("active", b.dataset.range === TREND_RANGE);
    b.onclick = () => { TREND_RANGE = b.dataset.range; renderTrends(); };
  });
  let h;
  try { h = await api("GET", `/api/history?range=${TREND_RANGE}`); }
  catch (e) { toast(e.message, true); return; }

  renderMetricCards(h.metrics);
  renderSeriesToggle();
  if (!h.series.length) {
    byId("trend-chart").innerHTML = `<p class="muted">攒几天就有曲线了。</p>`;
    byId("trend-stack").innerHTML = "";
    byId("trend-stack-legend").innerHTML = "";
    return;
  }
  byId("trend-chart").innerHTML = navChartSvg(h.series);
  byId("trend-stack").innerHTML = stackChartSvg(h.series, h.class_names);
  renderStackLegend(h.series, h.class_names);
}

function fmtPct(v) { return v == null ? "—" : (v * 100).toFixed(1) + "%"; }

function renderMetricCards(m) {
  const b = m.benchmark || {};
  const cards = [
    ["年化 (XIRR)", fmtPct(m.xirr), "资金加权,口径见说明"],
    ["TWR", fmtPct(m.twr), "时间加权(日快照近似)"],
    ["最大回撤", fmtPct(m.max_drawdown), `基准 ${fmtPct(b.max_drawdown)}`],
    ["年化波动", fmtPct(m.volatility), "采样稀疏仅供参考"],
    ["基准年化 (CAGR)", fmtPct(b.cagr), "沪深300,非 XIRR"],
  ];
  byId("trend-metrics").innerHTML = cards.map(([t, v, sub]) =>
    `<div class="metric"><div class="m-label">${t}</div>
       <div class="m-value">${v}</div><div class="m-sub">${sub}</div></div>`).join("");
}

function renderSeriesToggle() {
  const opts = [
    ["total_assets", "总资产"], ["net_invested", "净投入"], ["benchmark", "基准"],
  ];
  byId("trend-series-toggle").innerHTML = opts.map(([k, label]) =>
    `<label class="chk"><input type="checkbox" data-k="${k}" ${TREND_SHOW[k] ? "checked" : ""}>${label}</label>`
  ).join("");
  byId("trend-series-toggle").querySelectorAll("input").forEach(inp =>
    inp.onchange = () => { TREND_SHOW[inp.dataset.k] = inp.checked; renderTrends(); });
}

/* ---- SVG helpers (zero-Node, 纸质感自绘) ---- */
const CHART_W = 720, CHART_H = 260, PAD = 36;

function _scaleX(i, n) {
  if (n <= 1) return PAD;
  return PAD + (CHART_W - 2 * PAD) * i / (n - 1);
}
function _scaleY(v, lo, hi) {
  if (hi === lo) return CHART_H - PAD;
  return CHART_H - PAD - (CHART_H - 2 * PAD) * (v - lo) / (hi - lo);
}
function _polyline(pts, stroke, dash) {
  const d = pts.map(p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  return `<polyline points="${d}" fill="none" stroke="${stroke}" stroke-width="2"
            ${dash ? `stroke-dasharray="5 4"` : ""}/>`;
}

function navChartSvg(series) {
  // Normalize benchmark to start = first total_assets for visual comparison.
  const ta = series.map(s => s.total_assets);
  const ni = series.map(s => s.net_invested);
  const benchRaw = series.map(s => s.benchmark);
  const firstBench = benchRaw.find(v => v != null);
  const bench = firstBench
    ? benchRaw.map(v => v == null ? null : v / firstBench * ta[0]) : benchRaw;

  const lines = [];
  if (TREND_SHOW.total_assets) lines.push(["#7a5c3e", false, ta]);
  if (TREND_SHOW.net_invested) lines.push(["#9b8b76", true, ni]);
  if (TREND_SHOW.benchmark && firstBench) lines.push(["#5c7a6e", false, bench]);

  const flat = lines.flatMap(([, , arr]) => arr.filter(v => v != null));
  if (!flat.length) return `<p class="muted">没有可显示的曲线。</p>`;
  const lo = Math.min(...flat), hi = Math.max(...flat);
  const n = series.length;

  const polys = lines.map(([color, dash, arr]) => {
    const pts = arr.map((v, i) => v == null ? null : [_scaleX(i, n), _scaleY(v, lo, hi)])
                   .filter(Boolean);
    return _polyline(pts, color, dash);
  }).join("");

  const yLabels = HIDE_AMT
    ? `<text x="2" y="${PAD}" class="axis">•••</text>`
    : `<text x="2" y="${PAD}" class="axis">${money(hi)}</text>
       <text x="2" y="${CHART_H - PAD}" class="axis">${money(lo)}</text>`;
  const xFirst = series[0].date, xLast = series[n - 1].date;
  return `<svg viewBox="0 0 ${CHART_W} ${CHART_H}" class="trend-svg">
    ${polys}
    ${yLabels}
    <text x="${PAD}" y="${CHART_H - 8}" class="axis">${xFirst}</text>
    <text x="${CHART_W - PAD}" y="${CHART_H - 8}" class="axis" text-anchor="end">${xLast}</text>
  </svg>`;
}

function stackChartSvg(series, classNames) {
  // Union of all class ids across the window, in a stable order.
  const ids = [];
  series.forEach(s => Object.keys(s.class_values).forEach(id => {
    if (!ids.includes(id)) ids.push(id);
  }));
  if (!ids.length) return `<p class="muted">暂无大类市值。</p>`;
  const n = series.length;
  const totals = series.map(s => ids.reduce((a, id) => a + (s.class_values[id] || 0), 0));
  const hi = Math.max(...totals, 1);

  // Build stacked areas bottom-up.
  let bands = ids.map(() => []);
  series.forEach((s, i) => {
    let acc = 0;
    ids.forEach((id, k) => {
      const v = s.class_values[id] || 0;
      const y0 = _scaleY(acc, 0, hi);
      const y1 = _scaleY(acc + v, 0, hi);
      bands[k].push([_scaleX(i, n), y0, y1]);
      acc += v;
    });
  });

  const areas = ids.map((id, k) => {
    const top = bands[k].map(p => `${p[0].toFixed(1)},${p[2].toFixed(1)}`);
    const bot = bands[k].map(p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).reverse();
    const fill = stackColor(id, classNames);
    return `<polygon points="${top.concat(bot).join(" ")}" fill="${fill}" opacity="0.85"/>`;
  }).join("");

  return `<svg viewBox="0 0 ${CHART_W} ${CHART_H}" class="trend-svg">${areas}</svg>`;
}

function stackColor(id, classNames) {
  const c = classNames[id];
  return c ? colorVar(c.color) : "#bdb3a6";  // deleted class → neutral grey
}
function stackLabel(id, classNames) {
  const c = classNames[id];
  return c ? c.name : "已删除大类";
}

function renderStackLegend(series, classNames) {
  const ids = [];
  series.forEach(s => Object.keys(s.class_values).forEach(id => {
    if (!ids.includes(id)) ids.push(id);
  }));
  byId("trend-stack-legend").innerHTML = ids.map(id =>
    `<span class="leg"><i style="background:${stackColor(id, classNames)}"></i>${stackLabel(id, classNames)}</span>`
  ).join("");
}
```

- [ ] **Step 5: `static/css/style.css` 末尾追加样式**

```css
/* ---------- 走势 / 绩效 ---------- */
.trends-head { display: flex; align-items: center; justify-content: space-between; }
.range-switch button, .series-toggle .chk { font: inherit; }
.range-switch button {
  border: 1px solid var(--line, #d8cfc0); background: transparent;
  padding: 2px 10px; margin-left: 4px; border-radius: 4px; cursor: pointer;
}
.range-switch button.active { background: #7a5c3e; color: #fff; border-color: #7a5c3e; }
.metric-cards { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 12px; }
.metric {
  flex: 1 1 120px; border: 1px solid var(--line, #e3dccf); border-radius: 6px;
  padding: 10px 12px; background: #fbf8f2;
}
.metric .m-label { font-size: 12px; color: #8a7d6a; }
.metric .m-value { font-size: 22px; font-weight: 600; color: #4a3f30; }
.metric .m-sub { font-size: 11px; color: #a99c87; margin-top: 2px; }
.series-toggle .chk { margin-left: 10px; font-size: 13px; cursor: pointer; }
.trend-svg { width: 100%; height: auto; display: block; margin-top: 8px; }
.trend-svg .axis { font-size: 11px; fill: #a99c87; }
.legend .leg { display: inline-flex; align-items: center; margin: 6px 10px 0 0; font-size: 12px; }
.legend .leg i { width: 12px; height: 12px; border-radius: 2px; display: inline-block; margin-right: 4px; }
.muted { color: #a99c87; font-size: 13px; }
```

- [ ] **Step 6: 起服务、临时库、手动验证**

跑(临时库,绝不碰真实 DB):

```bash
STOCKBOOK_DATABASE_URL=sqlite:////tmp/sb_trends.db .venv/bin/uvicorn main:app --port 8011
```

浏览器开 `http://127.0.0.1:8011`:
1. 点「走势」tab — 首次应显示空态「攒几天就有曲线了」(新库还没快照)。
2. 用 `curl -X POST http://127.0.0.1:8011/api/snapshot` 造一条(走临时库的实例),刷新走势 — 指标卡出现、曲线/堆叠渲染(单点可能只见点/无线,正常)。
3. 多造几条(可手动 `INSERT` 或改日期不便,至少确认无报错、SVG 出图)。
4. `?hideAmounts=1` 验证金额轴掩码、形状仍显示。
确认无 console 报错后停服务。

- [ ] **Step 7: Commit(控制方)**

```bash
git add templates/index.html static/js/app.js static/css/style.css
git commit -m "feat(ui): 走势 tab — metric cards + self-drawn SVG NAV/stacked charts

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: 文档 — `docs/architecture.md`(关键决策 / API / 功能日志)

**Files:**
- Modify: `docs/architecture.md`

- [ ] **Step 1: 「3. 架构分层」表加 `snapshot_service.py` 行**

在 `backup.py` 行之后加:

```markdown
| `snapshot_service.py` | 每日净值快照:`run_snapshot`(刷价→`build_ledger`/`build_dashboard` 取总额/各大类市值→抓基准→按 date upsert)、`build_history`(区间过滤序列 + 调 calc 组装绩效)、进程内调度(仿 backup,独立 task) |
```

并在 `calc.py` 行职责末尾补:`+ 绩效纯函数(xirr/twr/max_drawdown/annualized_volatility)`。

- [ ] **Step 2: 「4. 关键决策」追加条目 21**

```markdown
21. **历史净值 + 绩效(走势板块)**:新增 `Snapshot` 表(每日总资产/净投入/基准点位/各大类市值 JSON),是对「**推导而非存储**」的**有意例外** —— 过去某天的市值无法用现价重算,时间序列必须落盘。`snapshot_service.run_snapshot` 捕获前**先刷持仓行情**避免记陈价,按 `date` upsert(每天一条/手动可刷当天)。调度**仿备份调度器另起一个独立 asyncio task**(两条 cadence 不同:备份 12h、快照 `SNAPSHOT_INTERVAL_HOURS` 默认 24h;启动补当天 + 每日,`READONLY`/间隔=0 不自动)。基准(沪深300)**正向累积**:每日顺带快照指数点位(走多源 `fetch_quotes`,抓不到存 null);`BENCHMARK_CODE` 默认 `sh000300` **带市场前缀**——指数非个股,个股 code→市场启发式会误判,故显式前缀。绩效指标是 `calc` 纯函数:**XIRR**(资金加权,二分求根)/**TWR**(时间加权,日快照只能把现金流归到区间端点 → 近似)/**最大回撤**/**年化波动**(√252 假设规则采样,稀疏时仅供参考);基准用 **CAGR**(纯价格序列无现金流,XIRR 无定义)。`GET /api/history?range=` 的**指标随选中区间计算**(与图一致,XIRR 以窗口起点净值为期初流出)。`reset_to_default` 一并清 `Snapshot`。走势页自绘 SVG(净值折线三条可切 + 大类堆叠面积,已删除大类兜底中性灰),零 Node。
```

- [ ] **Step 3: 「5. JSON API 一览」追加**

```markdown
- 历史/绩效:`POST /api/snapshot`(写,upsert 今日快照)、`GET /api/history?range=3m|1y|all`(序列 + 窗口指标 xirr/twr/max_drawdown/volatility + 基准 growth/cagr/max_drawdown + 当前大类名色)。
```

- [ ] **Step 4: 「7. 功能日志」追加一行**

```markdown
- **2026-06-02** 历史净值 + 绩效分析(走势板块):新 `Snapshot` 表(推导而非存储的有意例外)+ `app/snapshot_service.py`(run_snapshot 刷价→捕获→按 date upsert;build_history 区间+指标)+ `calc` 四个绩效纯函数(XIRR/TWR/最大回撤/年化波动,配 Hypothesis 不变量、每条变异检查)+ 进程内每日调度(仿备份、独立 task)+ 基准沪深300 正向累积(`sh000300` 带前缀绕开个股映射)+ 新 tab「走势」(指标卡 + 可切 SVG 净值线 + 大类堆叠,零 Node)+ 配置 `BENCHMARK_CODE`/`SNAPSHOT_INTERVAL_HOURS`。设计见 `docs/superpowers/specs/2026-06-02-stockbook-history-performance-design.md`,计划见 `docs/superpowers/plans/2026-06-02-history-performance.md`。
```

- [ ] **Step 5: 跑全套最后确认**

Run: `.venv/bin/pytest -q`
Expected: 全绿。

- [ ] **Step 6: Commit(控制方)**

```bash
git add docs/architecture.md
git commit -m "docs: record history+performance (Snapshot table, perf calc, 走势 tab)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 收尾自检(控制方,全部 Task 后)

- [ ] `.venv/bin/pytest -q` 全绿。
- [ ] `.venv/bin/coverage run -m pytest -q && .venv/bin/coverage report --include=app/calc.py,app/services.py --fail-under=95` 通过。
- [ ] 起服务(临时库)走一遍「走势」tab:空态 → 造快照 → 指标/曲线/堆叠 → `?hideAmounts=1` 掩码,均正常。
- [ ] `docs/architecture.md` 三节都更新。
- [ ] 提交历史:每个 Task 一个独立 commit,信息含 Co-Authored-By;**无** amend/squash/rebase。
