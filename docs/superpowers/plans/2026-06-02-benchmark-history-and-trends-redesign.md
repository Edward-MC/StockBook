# 沪深300 历史基准线 + 走势页视觉重做 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让走势页的 沪深300 基准线从第一天就有(抓历史日线落本地表),并把走势页换成 Robinhood 极简风(大数字抬头 + 渐变填充曲线 + 区间药丸)。

**Architecture:** 新表 `BenchmarkPoint`(date→close)持久化东财抓来的指数日线,与 `Snapshot` 解耦;`/api/history` 只读本地表(页面零网络),后台调度器启动时 backfill ~3 年 + 每日补当天。基准线与基准指标按这张表算、独立于持仓快照数量;**不与持仓对齐**(各自缩放)。前端走势页重做。

**Tech Stack:** Python 3.9(`typing.Optional/List/Dict`,**不用** `X|None`)、FastAPI、SQLAlchemy 2.0、SQLite、pytest、原生 JS + 自绘 SVG(渐变 `<linearGradient>`)。

---

## 关键约束(每个 Task 都要守)

- **绝不碰真实 `stockbook.db`**:测试一律走 conftest 临时库;起服务验证用 `STOCKBOOK_DATABASE_URL=sqlite:////tmp/xxx.db`。绝不打真实外部网络(测试用 fake/monkeypatch)。
- **review 前置于 commit**(subagent-driven):实现子代理只 `git add` 暂存、**绝不 commit**;控制方核验 + 两段 review 过了才 commit。跟进修复=独立新 commit。
- **Python 3.9**:`Optional[X]`/`List[X]`,不写 `X | None`。
- 跑测试:`.venv/bin/pytest -q`(不 `source`)。
- 提交信息末尾附:`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- 测试访问 session 一律 `from app import database` 后 `database.SessionLocal()`(**不要**顶层 `from app.database import SessionLocal` —— 会捕获重绑前的对象)。

## 文件结构(本计划要动的文件)

| 文件 | 创建/修改 | 职责 |
|---|---|---|
| `app/models.py` | 修改(追加) | 加 `BenchmarkPoint` 表(date PK/unique, close) |
| `app/seed.py` | 修改 | `reset_to_default` 清 `BenchmarkPoint` |
| `app/quotes.py` | 修改(追加) | `parse_em_kline`(纯解析)+ `fetch_index_history`(东财日线,网络) |
| `app/snapshot_service.py` | 修改 | `backfill_benchmark` + `_upsert_benchmark_points`;`run_snapshot` 末尾写当天 close;`_run_once` 先 backfill;`build_history` 读 `BenchmarkPoint` → `benchmark_series` + 基准指标按表算;range → 今天相对日期窗口(加 6m/3y) |
| `app/routers/api.py` | 修改 | `/api/history` 接受 `6m`/`3y` range 值 |
| `static/js/app.js` | 修改 | 走势页重做:大数字抬头、日期轴、渐变填充主线、沪深300 自适应叠线、药丸区间、末点圆点 |
| `templates/index.html` | 修改 | 走势面板抬头区 + 5 个药丸按钮 |
| `static/css/style.css` | 修改(追加) | Robinhood 极简风样式 |
| `tests/test_benchmark.py` | **创建** | kline 解析 + `BenchmarkPoint` 模型/reset + backfill |
| `tests/test_snapshot_service.py` | 修改 | `build_history` 基准来自 `BenchmarkPoint`、range 窗口(更新既有 range 测试为今天相对) |
| `tests/test_history_api.py` | 修改(追加) | `range=6m/3y` + `benchmark_series` 字段 |

---

## Task 1: `BenchmarkPoint` 模型 + reset 清表

**Files:**
- Modify: `app/models.py`(末尾加表)
- Modify: `app/seed.py`(import + reset 元组)
- Test: `tests/test_benchmark.py`(创建)

- [ ] **Step 1: 写失败测试 `tests/test_benchmark.py`**

```python
"""Tests for benchmark history: kline parsing, BenchmarkPoint, backfill."""
import datetime as dt

from app import database, models, seed


def test_benchmark_point_roundtrip_and_unique(client):
    db = database.SessionLocal()
    try:
        db.add(models.BenchmarkPoint(date=dt.date(2025, 6, 1), close=3900.5))
        db.commit()
        got = db.query(models.BenchmarkPoint).one()
        assert got.date == dt.date(2025, 6, 1)
        assert got.close == 3900.5
    finally:
        db.close()


def test_reset_clears_benchmark_points(client):
    db = database.SessionLocal()
    try:
        db.add(models.BenchmarkPoint(date=dt.date(2025, 6, 1), close=1.0))
        db.commit()
        seed.reset_to_default(db)
        assert db.query(models.BenchmarkPoint).count() == 0
    finally:
        db.close()
```

- [ ] **Step 2: 跑确认失败**

Run: `.venv/bin/pytest tests/test_benchmark.py -q`
Expected: FAIL — `AttributeError: module 'app.models' has no attribute 'BenchmarkPoint'`。

- [ ] **Step 3: 在 `app/models.py` 末尾加 `BenchmarkPoint`**

```python
class BenchmarkPoint(Base):
    """Daily close of the benchmark index (沪深300), persisted locally so the
    trends page reads from the DB (no network on page load) and a benchmark line
    is available from day one. Decoupled from Snapshot — the index has trading
    days the user never snapshotted. One row per date (upserted)."""
    __tablename__ = "benchmark_points"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False, unique=True)
    close: Mapped[float] = mapped_column(Float, nullable=False)
```
(`dt`, `Date`, `Float`, `Mapped`, `mapped_column` 均已在 models.py 顶部导入。)

- [ ] **Step 4: `app/seed.py` 让 reset 清表**

把 import 改为(加 `BenchmarkPoint`):
```python
from .models import (AssetClass, BenchmarkPoint, CashFlow, KnowledgeChunk,
                     NotionSource, PriceQuote, Security, Snapshot, Strategy,
                     Transaction)
```
把 `reset_to_default` 的 model 元组加上 `BenchmarkPoint`:
```python
    for model in (Transaction, PriceQuote, Security, CashFlow, AssetClass, Strategy,
                  KnowledgeChunk, NotionSource, Snapshot, BenchmarkPoint):
        db.query(model).delete()
```

- [ ] **Step 5: 跑确认通过 + 全套**

Run: `.venv/bin/pytest tests/test_benchmark.py -q` → PASS(2)。
Run: `.venv/bin/pytest -q` → 全绿。

- [ ] **Step 6: Commit(控制方)**

```bash
git add app/models.py app/seed.py tests/test_benchmark.py
git commit -m "feat(model): BenchmarkPoint table for local 沪深300 daily history; reset clears it

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `quotes` — 东财指数日线解析 + 抓取

**Files:**
- Modify: `app/quotes.py`(URL 常量 + `parse_em_kline` + `fetch_index_history`)
- Test: `tests/test_benchmark.py`(追加)

**设计要点:** 东财 K 线接口 `push2his.eastmoney.com/api/qt/stock/kline/get`,`fields2=f51,f53` → 每条 `"YYYY-MM-DD,close"`。复用 `to_em_secid`(`("000300","SH")` → `1.000300`)。解析(纯)与网络分离。

- [ ] **Step 1: 在 `tests/test_benchmark.py` 追加**

```python
from app import quotes


def test_parse_em_kline_basic():
    text = '{"data":{"code":"000300","klines":["2025-06-01,3900.5","2025-06-02,3950.0"]}}'
    out = quotes.parse_em_kline(text)
    assert out == [(dt.date(2025, 6, 1), 3900.5), (dt.date(2025, 6, 2), 3950.0)]


def test_parse_em_kline_empty_or_bad():
    assert quotes.parse_em_kline('{"data":null}') == []
    assert quotes.parse_em_kline('not json') == []
    assert quotes.parse_em_kline('{"data":{"klines":["bad", "2025-06-02,3950.0"]}}') \
        == [(dt.date(2025, 6, 2), 3950.0)]


def test_fetch_index_history_maps_and_parses(monkeypatch):
    # Stub the HTTP GET so no real network; assert secid mapping + parse.
    class _Resp:
        text = '{"data":{"klines":["2025-06-02,3950.0"]}}'
    captured = {}

    def _fake_get(url, headers=None):
        captured["url"] = url
        return _Resp()
    monkeypatch.setattr(quotes, "_get", _fake_get)
    out = quotes.fetch_index_history("000300", "SH", 750)
    assert out == [(dt.date(2025, 6, 2), 3950.0)]
    assert "secid=1.000300" in captured["url"] and "lmt=750" in captured["url"]


def test_fetch_index_history_unmappable_code_returns_empty(monkeypatch):
    monkeypatch.setattr(quotes, "_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no net")))
    assert quotes.fetch_index_history("abc", "CN", 750) == []
```

- [ ] **Step 2: 跑确认失败**

Run: `.venv/bin/pytest tests/test_benchmark.py -q`
Expected: FAIL — `AttributeError: module 'app.quotes' has no attribute 'parse_em_kline'`。

- [ ] **Step 3: 在 `app/quotes.py` 加常量 + 两个函数**

在文件的 URL 常量区(`_EASTMONEY_URL = ...` 附近)加:
```python
# Eastmoney daily kline (history). fields2=f51,f53 → each row "YYYY-MM-DD,close".
_EM_KLINE_URL = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
                 "?fields1=f1&fields2=f51,f53&klt=101&fqt=0&end=20500101")
```
在 `parse_eastmoney` 之后(parsers 区)加纯解析:
```python
def parse_em_kline(text: str) -> List[Tuple[dt.date, float]]:
    """Parse Eastmoney kline JSON {data:{klines:["YYYY-MM-DD,close", ...]}} into
    [(date, close)] ascending. Malformed rows are skipped; bad/empty → []."""
    out: List[Tuple[dt.date, float]] = []
    try:
        klines = (json.loads(text).get("data") or {}).get("klines") or []
    except (ValueError, AttributeError):
        return out
    for row in klines:
        parts = str(row).split(",")
        if len(parts) < 2:
            continue
        try:
            d = dt.date.fromisoformat(parts[0])
            close = float(parts[1])
        except ValueError:
            continue
        out.append((d, close))
    return out
```
在 `_get` 定义之后(网络区)加抓取:
```python
def fetch_index_history(code: str, market: str, days: int) -> List[Tuple[dt.date, float]]:
    """Daily closes for an index over ~`days` rows via Eastmoney kline. Returns
    [(date, close)] ascending; raises httpx.HTTPError on transport failure.
    Empty list if the code can't be mapped to a secid."""
    secid = to_em_secid(code, market)
    if not secid:
        return []
    r = _get(f"{_EM_KLINE_URL}&secid={secid}&lmt={int(days)}")
    return parse_em_kline(r.text)
```

- [ ] **Step 4: 跑确认通过**

Run: `.venv/bin/pytest tests/test_benchmark.py -q` → PASS(6)。

- [ ] **Step 5: Commit(控制方)**

```bash
git add app/quotes.py tests/test_benchmark.py
git commit -m "feat(quotes): Eastmoney index daily kline — parse_em_kline + fetch_index_history

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `snapshot_service` — backfill + 每日补 + 调度

**Files:**
- Modify: `app/snapshot_service.py`(import + `_upsert_benchmark_points` + `backfill_benchmark` + `run_snapshot` 写当天 + `_run_once` 先 backfill)
- Test: `tests/test_benchmark.py`(追加)

**设计要点:** `backfill_benchmark` 表空或最新点早于昨天时抓 ~3 年 upsert;`run_snapshot` 末尾把当天 live 基准点写 `BenchmarkPoint`(线含今天);`_run_once` 每轮先 backfill(freshness guard 后续轮变 no-op)再 snapshot。best-effort 吞 httpx 错。

- [ ] **Step 1: 在 `tests/test_benchmark.py` 追加**

```python
import pytest

from app import snapshot_service


@pytest.fixture()
def fake_index(monkeypatch):
    """Stub fetch_index_history so backfill never hits the network."""
    pts = [(dt.date(2025, 6, 1), 3900.0), (dt.date(2025, 6, 2), 3950.0),
           (dt.date(2025, 6, 3), 3975.0)]
    monkeypatch.setattr(snapshot_service.quotes, "fetch_index_history",
                        lambda code, market, days: list(pts))
    return pts


def test_backfill_benchmark_writes_and_is_idempotent(client, fake_index):
    db = database.SessionLocal()
    try:
        n = snapshot_service.backfill_benchmark(db)
        assert n == 3
        assert db.query(models.BenchmarkPoint).count() == 3
        # second call upserts the same dates → still 3 rows, no duplicates
        snapshot_service.backfill_benchmark(db)
        assert db.query(models.BenchmarkPoint).count() == 3
    finally:
        db.close()


def test_backfill_benchmark_swallows_network_error(client, monkeypatch):
    import httpx

    def _boom(code, market, days):
        raise httpx.HTTPError("down")
    monkeypatch.setattr(snapshot_service.quotes, "fetch_index_history", _boom)
    db = database.SessionLocal()
    try:
        assert snapshot_service.backfill_benchmark(db) == 0  # no raise
    finally:
        db.close()
```

- [ ] **Step 2: 跑确认失败**

Run: `.venv/bin/pytest tests/test_benchmark.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'backfill_benchmark'`。

- [ ] **Step 3: `app/snapshot_service.py` — import + 写函数**

把 models import 改为(加 `BenchmarkPoint`):
```python
from .models import BenchmarkPoint, CashFlow, Security, Snapshot
```
在 `_fetch_benchmark` 附近加常量 + 两个函数:
```python
_BENCHMARK_BACKFILL_DAYS = 1100  # ~3 trading years (over-ask; the API trims)


def _upsert_benchmark_points(db: Session, points: List[Tuple[dt.date, float]]) -> int:
    """Upsert [(date, close)] into BenchmarkPoint (one row per date). Returns the
    number of points written. Commits."""
    if not points:
        return 0
    existing = {d for d in db.scalars(select(BenchmarkPoint.date))}
    written = 0
    for d, close in points:
        if d in existing:
            row = db.scalars(select(BenchmarkPoint).where(BenchmarkPoint.date == d)).first()
            row.close = close
        else:
            db.add(BenchmarkPoint(date=d, close=close))
            existing.add(d)
        written += 1
    db.commit()
    return written


def backfill_benchmark(db: Session) -> int:
    """Fetch ~3y of benchmark daily closes and upsert when the table is empty or
    stale (latest point older than yesterday). Best-effort: returns 0 and logs on
    empty config / transport failure (never raises). Reads the live DB + writes
    BenchmarkPoint only."""
    parsed = _parse_benchmark(config.BENCHMARK_CODE)
    if parsed is None:
        return 0
    latest = db.scalars(
        select(BenchmarkPoint.date).order_by(BenchmarkPoint.date.desc())
    ).first()
    if latest is not None and latest >= dt.date.today() - dt.timedelta(days=1):
        return 0  # fresh enough — skip the network
    code, market = parsed
    try:
        points = quotes.fetch_index_history(code, market, _BENCHMARK_BACKFILL_DAYS)
    except httpx.HTTPError as exc:
        _log.warning("benchmark backfill failed: %s", exc)
        return 0
    return _upsert_benchmark_points(db, points)
```

- [ ] **Step 4: `run_snapshot` 末尾写当天 close**

在 `run_snapshot` 里 `benchmark = _fetch_benchmark()` 之后、`upsert Snapshot` 之前(或之后均可,同一事务),加:
```python
    if benchmark is not None:
        _upsert_benchmark_points(db, [(today, benchmark)])
```
(注意 `today = dt.date.today()` 已在函数内定义;若顺序上 `today` 在后面才定义,把这段移到 `today` 定义之后、`db.commit()` 之前。)

- [ ] **Step 5: `_run_once` 每轮先 backfill**

把 `_run_once` 改为:
```python
def _run_once() -> None:
    from . import database
    db = database.SessionLocal()
    try:
        backfill_benchmark(db)  # ensures ~3y history exists / stays fresh (no-op once current)
        run_snapshot(db)
    finally:
        db.close()
```

- [ ] **Step 6: 跑确认通过 + 全套**

Run: `.venv/bin/pytest tests/test_benchmark.py tests/test_snapshot_service.py -q` → PASS。
Run: `.venv/bin/pytest -q` → 全绿。

- [ ] **Step 7: Commit(控制方)**

```bash
git add app/snapshot_service.py tests/test_benchmark.py
git commit -m "feat(snapshot): backfill 沪深300 daily history + daily append + scheduler wiring

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `build_history` — benchmark_series + 基准指标按表算 + 区间窗口

**Files:**
- Modify: `app/snapshot_service.py`(`build_history` / `_window_metrics` / `_RANGE_DAYS`)
- Test: `tests/test_snapshot_service.py`(更新既有 range 测试 + 加基准来自表的测试)

**设计要点:**
- `_RANGE_DAYS = {"3m":90,"6m":180,"1y":365,"3y":1095}`;`all`=不设下界。**窗口相对今天**(`dt.date.today()`),因为基准是日历日、要「最近 N 天」。
- `benchmark_series` 来自 `BenchmarkPoint`(按窗口裁剪)。
- 基准指标(growth/cagr/max_drawdown)按 `BenchmarkPoint` 窗口算 → ≥2 个基准点即有数,**哪怕 0 持仓快照**。
- 持仓 `series` 与持仓指标不变(仍来自 `Snapshot`,需 ≥2 条)。

- [ ] **Step 1: 更新/追加测试 `tests/test_snapshot_service.py`**

把既有 `test_build_history_structure_and_range` 整体替换为**今天相对**版本(原版按「最后快照日」相对、基准取自 snapshot.benchmark,语义已变):
```python
def test_build_history_structure_and_range(client):
    db = database.SessionLocal()
    try:
        today = dt.date.today()
        # snapshots across the last ~200 days (every 10 days)
        for i in range(0, 200, 10):
            _add_snap(db, today - dt.timedelta(days=i), 100.0 + i, 100.0,
                      4000.0 + i, {"1": 50.0 + i, "2": 50.0})
        db.commit()

        all_h = snapshot_service.build_history(db, range_="all")
        assert len(all_h["series"]) == 20
        for key in ("xirr", "twr", "max_drawdown", "volatility", "benchmark"):
            assert key in all_h["metrics"]
        assert "benchmark_series" in all_h
        assert isinstance(all_h["class_names"], dict)

        # 3m window (relative to today) → only snapshots within 90 days
        m3 = snapshot_service.build_history(db, range_="3m")
        cutoff = (today - dt.timedelta(days=90)).isoformat()
        assert all(s["date"] >= cutoff for s in m3["series"])
        assert len(m3["series"]) < len(all_h["series"])
    finally:
        db.close()
```
追加基准来自表的测试:
```python
def test_build_history_benchmark_from_table_no_snapshots(client):
    db = database.SessionLocal()
    try:
        db.query(models.Snapshot).delete()
        today = dt.date.today()
        for i in range(0, 30, 3):  # benchmark points within window
            db.add(models.BenchmarkPoint(date=today - dt.timedelta(days=i),
                                         close=4000.0 + i))
        db.commit()
        h = snapshot_service.build_history(db, range_="3m")
        assert h["series"] == []                       # no portfolio yet
        assert len(h["benchmark_series"]) >= 2         # but benchmark line exists
        assert h["metrics"]["benchmark"]["cagr"] is not None     # and its metrics
        assert h["metrics"]["benchmark"]["max_drawdown"] is not None
        assert h["metrics"]["xirr"] is None            # portfolio metrics still None
    finally:
        db.close()


def test_build_history_range_6m_3y_accepted(client):
    db = database.SessionLocal()
    try:
        today = dt.date.today()
        for i in range(0, 1000, 50):
            db.add(models.BenchmarkPoint(date=today - dt.timedelta(days=i), close=4000.0 + i))
        db.commit()
        h6 = snapshot_service.build_history(db, range_="6m")
        h3y = snapshot_service.build_history(db, range_="3y")
        assert len(h6["benchmark_series"]) < len(h3y["benchmark_series"])
        c6 = (today - dt.timedelta(days=180)).isoformat()
        assert all(b["date"] >= c6 for b in h6["benchmark_series"])
    finally:
        db.close()
```

- [ ] **Step 2: 跑确认失败**

Run: `.venv/bin/pytest tests/test_snapshot_service.py -k history -q`
Expected: FAIL(`benchmark_series` 缺失 / range 语义不符)。

- [ ] **Step 3: 重写 `build_history` + `_window_metrics`(替换现有实现)**

把现有 `_RANGE_DAYS`/`build_history`/`_window_metrics` 段替换为:
```python
_RANGE_DAYS = {"3m": 90, "6m": 180, "1y": 365, "3y": 1095}  # "all" => no cutoff


def _cagr(nav: List[float], days: int) -> Optional[float]:
    if len(nav) < 2 or nav[0] <= 0 or days <= 0:
        return None
    return (nav[-1] / nav[0]) ** (365.0 / days) - 1.0


def build_history(db: Session, range_: str = "all") -> dict:
    """Assemble the /api/history payload: portfolio series (sparse, from Snapshot)
    + dense benchmark_series (from BenchmarkPoint) + window metrics + class names.
    The range is a date window relative to today; an unrecognized range_ falls
    back to the full data (the API route validates the value)."""
    cutoff = None
    days = _RANGE_DAYS.get(range_)
    if days is not None:
        cutoff = dt.date.today() - dt.timedelta(days=days)

    snaps = db.scalars(select(Snapshot).order_by(Snapshot.date)).all()
    bpts = db.scalars(select(BenchmarkPoint).order_by(BenchmarkPoint.date)).all()
    if cutoff is not None:
        snaps = [r for r in snaps if r.date >= cutoff]
        bpts = [b for b in bpts if b.date >= cutoff]

    series = [
        {
            "date": r.date.isoformat(),
            "total_assets": r.total_assets,
            "net_invested": r.net_invested,
            "benchmark": r.benchmark,
            "class_values": json.loads(r.class_values or "{}"),
        }
        for r in snaps
    ]
    benchmark_series = [{"date": b.date.isoformat(), "close": b.close} for b in bpts]
    metrics = _window_metrics(db, snaps, bpts)

    strategy = build_dashboard(db, readonly=False, hide_amounts=False)
    class_names: Dict[str, dict] = {}
    if strategy is not None:
        for ac in strategy["asset_classes"]:
            class_names[str(ac["id"])] = {"name": ac["name"], "color": ac["color"]}

    return {"series": series, "benchmark_series": benchmark_series,
            "metrics": metrics, "class_names": class_names}


def _benchmark_metrics(bpts: List[BenchmarkPoint]) -> dict:
    if len(bpts) < 2:
        return {"growth": None, "cagr": None, "max_drawdown": None}
    nav = [b.close for b in bpts]
    days = (bpts[-1].date - bpts[0].date).days
    return {
        "growth": (nav[-1] / nav[0] - 1.0) if nav[0] else None,
        "cagr": _cagr(nav, days),
        "max_drawdown": calc.max_drawdown(nav),
    }


def _window_metrics(db: Session, snaps: List[Snapshot],
                    bpts: List[BenchmarkPoint]) -> dict:
    bench = _benchmark_metrics(bpts)
    if not snaps:
        return {"xirr": None, "twr": None, "max_drawdown": None,
                "volatility": None, "benchmark": bench}
    if len(snaps) < 2:
        return {"xirr": None, "twr": None,
                "max_drawdown": calc.max_drawdown([snaps[0].total_assets]),
                "volatility": None, "benchmark": bench}
    nav = [r.total_assets for r in snaps]
    start_date, end_date = snaps[0].date, snaps[-1].date

    cfs = db.scalars(
        select(CashFlow).where(CashFlow.date >= start_date, CashFlow.date <= end_date)
    ).all()
    twr_flows = [(cf.date, cf.amount if cf.direction == "in" else -cf.amount) for cf in cfs]

    nav_series = [(r.date, r.total_assets) for r in snaps]
    # XIRR (investor view): window-start value out (−), deposits −, withdrawals +,
    # window-end value in (+). Start-date cashflows are already inside −V_start.
    xirr_flows = [(start_date, -snaps[0].total_assets)]
    for cf in cfs:
        if cf.date > start_date:
            xirr_flows.append((cf.date, -cf.amount if cf.direction == "in" else cf.amount))
    xirr_flows.append((end_date, snaps[-1].total_assets))

    return {
        "xirr": calc.xirr(xirr_flows),
        "twr": calc.twr(nav_series, twr_flows),
        "max_drawdown": calc.max_drawdown(nav),
        "volatility": calc.annualized_volatility(nav),
        "benchmark": bench,
    }
```

- [ ] **Step 4: 跑确认通过 + 全套**

Run: `.venv/bin/pytest tests/test_snapshot_service.py -q` → PASS。
Run: `.venv/bin/pytest -q` → 全绿。

- [ ] **Step 5: Commit(控制方)**

```bash
git add app/snapshot_service.py tests/test_snapshot_service.py
git commit -m "feat(snapshot): build_history serves dense benchmark_series + table-based benchmark metrics; today-relative range windows (6m/3y)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: API — `/api/history` 接受 `6m`/`3y`

**Files:**
- Modify: `app/routers/api.py`(history 路由 range 白名单)
- Test: `tests/test_history_api.py`(追加)

- [ ] **Step 1: 追加测试 `tests/test_history_api.py`**

(文件顶部已 `import datetime as dt, json` 和 `from app import database, models`。)
```python
def test_get_history_6m_window_through_api(client):
    # Seed ~3y of dense benchmark points; 6m must return a genuinely narrower
    # window than all — proving the route passes 6m through (not coerced to all).
    db = database.SessionLocal()
    try:
        today = dt.date.today()
        for i in range(0, 1000, 20):
            db.add(models.BenchmarkPoint(date=today - dt.timedelta(days=i), close=4000.0 + i))
        db.commit()
    finally:
        db.close()
    n6 = len(client.get("/api/history?range=6m").json()["benchmark_series"])
    n3y = len(client.get("/api/history?range=3y").json()["benchmark_series"])
    n_all = len(client.get("/api/history?range=all").json()["benchmark_series"])
    assert 0 < n6 < n3y <= n_all
    cutoff = (today - dt.timedelta(days=180)).isoformat()
    assert all(b["date"] >= cutoff for b in client.get("/api/history?range=6m").json()["benchmark_series"])
    assert client.get("/api/history?range=garbage").status_code == 200  # invalid → all
```

- [ ] **Step 2: 跑确认失败**

Run: `.venv/bin/pytest tests/test_history_api.py::test_get_history_6m_window_through_api -q`
Expected: FAIL — `6m` 未在白名单 → 回退 `all` → `n6 == n_all`,`0 < n6 < n3y` 不成立。

- [ ] **Step 3: 改 `app/routers/api.py` history 路由 range 白名单**

把:
```python
@router.get("/history")
def history(range_: str = Query("all", alias="range"), db: Session = Depends(get_db)):
    range_ = range_ if range_ in ("3m", "1y", "all") else "all"
    return snapshot_service.build_history(db, range_=range_)
```
改为:
```python
@router.get("/history")
def history(range_: str = Query("all", alias="range"), db: Session = Depends(get_db)):
    range_ = range_ if range_ in ("3m", "6m", "1y", "3y", "all") else "all"
    return snapshot_service.build_history(db, range_=range_)
```

- [ ] **Step 4: 跑确认通过 + 全套 + 覆盖率 gate**

Run: `.venv/bin/pytest tests/test_history_api.py -q` → PASS。
Run: `.venv/bin/pytest -q` → 全绿。
Run: `.venv/bin/coverage run -m pytest -q && .venv/bin/coverage report --include=app/calc.py,app/services.py --fail-under=95` → PASS。

- [ ] **Step 5: Commit(控制方)**

```bash
git add app/routers/api.py tests/test_history_api.py
git commit -m "feat(api): /api/history accepts 6m/3y ranges (benchmark history windows)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: 前端 — 走势页 Robinhood 极简风重做

**Files:**
- Modify: `templates/index.html`(抬头区 + 5 个药丸按钮)
- Modify: `static/js/app.js`(`TREND_RANGE` 默认、`renderTrends`、抬头、`navChartSvg` 日期轴+渐变+基准自适应)
- Modify: `static/css/style.css`(极简风样式)

> 零 Node、无 JS 测试器;以 `node --check` + headless-Chrome 渲染 + 起服务点验。下面给完整可粘贴代码。

- [ ] **Step 1: `templates/index.html` — 第一张卡换成抬头 + 5 药丸**

把走势面板第一张 `<div class="card">…metric-cards…</div>`(含 `trends-head`/`range-switch`)替换为:
```html
    <div class="card trend-top">
      <div class="trend-headline">
        <div class="th-label">总资产</div>
        <div class="th-value" id="trend-total">—</div>
        <div class="th-change" id="trend-change"></div>
      </div>
      <div class="range-switch" id="trend-range">
        <button data-range="3m">3月</button>
        <button data-range="6m">6月</button>
        <button data-range="1y" class="active">1年</button>
        <button data-range="3y">3年</button>
        <button data-range="all">全部</button>
      </div>
      <div class="metric-cards" id="trend-metrics"></div>
    </div>
```

- [ ] **Step 2: `static/js/app.js` — 默认区间改 1 年**

把 `let TREND_RANGE = "all";` 改为 `let TREND_RANGE = "1y";`。

- [ ] **Step 3: `static/js/app.js` — 重写 `renderTrends`(抬头 + 传 benchmark_series)**

把现有 `renderTrends` 整体替换为:
```javascript
const RANGE_LABEL = { "3m": "近3月", "6m": "近6月", "1y": "近1年", "3y": "近3年", "all": "全部" };

async function renderTrends() {
  byId("trend-range").querySelectorAll("button").forEach(b => {
    b.classList.toggle("active", b.dataset.range === TREND_RANGE);
    b.onclick = () => { TREND_RANGE = b.dataset.range; renderTrends(); };
  });
  let h;
  try { h = await api("GET", `/api/history?range=${TREND_RANGE}`); }
  catch (e) { toast(e.message, true); return; }

  renderTrendHeadline(h.series);
  renderMetricCards(h.metrics);
  renderSeriesToggle();
  byId("trend-chart").innerHTML = navChartSvg(h.series, h.benchmark_series || []);
  byId("trend-stack").innerHTML = stackChartSvg(h.series, h.class_names);
  renderStackLegend(h.series, h.class_names);
}

function renderTrendHeadline(series) {
  const last = series.length ? series[series.length - 1].total_assets : null;
  byId("trend-total").textContent = last == null ? "—" : money(last);
  const chg = byId("trend-change");
  if (series.length >= 2 && series[0].total_assets) {
    const r = series[series.length - 1].total_assets / series[0].total_assets - 1;
    const up = r >= 0;
    chg.className = "th-change " + (up ? "up" : "down");
    chg.textContent = `${up ? "▲" : "▼"} ${pctSigned(r * 100)} · ${RANGE_LABEL[TREND_RANGE]}`;
  } else {
    chg.className = "th-change";
    chg.textContent = RANGE_LABEL[TREND_RANGE];
  }
}
```
(`pctSigned` 来自 common.js。)

- [ ] **Step 4: `static/js/app.js` — 重写 `navChartSvg`(日期轴 + 渐变填充 + 基准自适应)**

把现有 `navChartSvg` 整体替换为:
```javascript
function _dnum(s) { return Date.parse(s); }  // ISO date string → ms (browser)

function _xScaler(dmin, dmax) {
  if (dmin === dmax) return () => CHART_W / 2;
  return d => PAD + (CHART_W - 2 * PAD) * (_dnum(d) - dmin) / (dmax - dmin);
}

// Map a value series to plot points on its OWN min/max (auto-scale, used for the
// non-aligned benchmark line) or a given [lo,hi] (used for the ¥ portfolio lines).
function _linePts(dated, xOf, lo, hi) {
  return dated.filter(p => p.v != null).map(p => [xOf(p.d), _scaleY(p.v, lo, hi)]);
}

function navChartSvg(series, bench) {
  const allDates = [...series.map(s => s.date), ...bench.map(b => b.date)];
  if (!allDates.length) return _chartSvg(_centerHint("攒几天就有曲线了"));
  const ds = allDates.map(_dnum);
  const dmin = Math.min(...ds), dmax = Math.max(...ds);
  const xOf = _xScaler(dmin, dmax);

  const hasPort = series.length >= 2;
  const benchDated = bench.map(b => ({ d: b.date, v: b.close }));
  const showBench = TREND_SHOW.benchmark && bench.length >= 2;

  let body = "", yLabels = "";

  if (hasPort) {
    const ta = series.map(s => ({ d: s.date, v: s.total_assets }));
    const ni = series.map(s => ({ d: s.date, v: s.net_invested }));
    const vals = [];
    if (TREND_SHOW.total_assets) vals.push(...ta.map(p => p.v));
    if (TREND_SHOW.net_invested) vals.push(...ni.map(p => p.v));
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (!vals.length) { lo = 0; hi = 1; }
    if (lo === hi) { lo -= 1; hi += 1; }

    if (TREND_SHOW.total_assets) {
      const pts = _linePts(ta, xOf, lo, hi);
      body += _areaFill(pts) + _polyline(pts, "#7a5c3e", false) + _endDot(pts, "#7a5c3e");
    }
    if (TREND_SHOW.net_invested) {
      body += _polyline(_linePts(ni, xOf, lo, hi), "#9b8b76", true);
    }
    if (showBench) {  // overlay, auto-scaled to its own range (not aligned)
      const bv = benchDated.map(p => p.v);
      const blo = Math.min(...bv), bhi = Math.max(...bv);
      body += _polyline(_linePts(benchDated, xOf, blo === bhi ? blo - 1 : blo, blo === bhi ? bhi + 1 : bhi),
                        "#5c7a6e", false);
    }
    yLabels = HIDE_AMT
      ? `<text x="2" y="${PAD}" class="axis">•••</text>`
      : `<text x="2" y="${PAD}" class="axis">${money(hi)}</text>
         <text x="2" y="${CHART_H - PAD}" class="axis">${money(lo)}</text>`;
  } else if (showBench) {
    // No portfolio yet — the 沪深300 line is the main subject (its own scale).
    const bv = benchDated.map(p => p.v);
    let blo = Math.min(...bv), bhi = Math.max(...bv);
    if (blo === bhi) { blo -= 1; bhi += 1; }
    const pts = _linePts(benchDated, xOf, blo, bhi);
    body += _areaFill(pts, "#5c7a6e") + _polyline(pts, "#5c7a6e", false) + _endDot(pts, "#5c7a6e");
    yLabels = `<text x="2" y="${PAD}" class="axis">${Math.round(bhi)}</text>
               <text x="2" y="${CHART_H - PAD}" class="axis">${Math.round(blo)}</text>
               <text x="${CHART_W - PAD}" y="${PAD - 6}" class="axis hint" text-anchor="end">沪深300 点位</text>`;
  } else {
    return _chartSvg(_centerHint("攒几天就有曲线了"));
  }

  const fmt = ms => new Date(ms).toISOString().slice(0, 10);
  return _chartSvg(`${body}${yLabels}
    <text x="${PAD}" y="${CHART_H - 8}" class="axis">${fmt(dmin)}</text>
    <text x="${CHART_W - PAD}" y="${CHART_H - 8}" class="axis" text-anchor="end">${fmt(dmax)}</text>`);
}

function _areaFill(pts, color) {
  if (pts.length < 2) return "";
  const c = color || "#7a5c3e";
  const id = "grad" + c.replace("#", "");
  const base = CHART_H - PAD;
  const poly = pts.map(p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ")
    + ` ${pts[pts.length - 1][0].toFixed(1)},${base} ${pts[0][0].toFixed(1)},${base}`;
  return `<defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${c}" stop-opacity="0.26"/>
      <stop offset="100%" stop-color="${c}" stop-opacity="0"/>
    </linearGradient></defs>
    <polygon points="${poly}" fill="url(#${id})"/>`;
}

function _endDot(pts, color) {
  if (!pts.length) return "";
  const p = pts[pts.length - 1];
  return `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="3.5" fill="${color}"/>`;
}
```
(注:`_polyline` 已对 1 点序列返回单坐标无可见线;`_linePts` 用日期定位 x,稀疏快照按真实日期排布。)

- [ ] **Step 5: `static/css/style.css` — 追加极简风样式**

在现有走势样式块后追加:
```css
/* Robinhood-minimal headline + pills */
.trend-top { padding-bottom: 14px; }
.trend-headline { margin-bottom: 10px; }
.th-label { font-size: 12px; color: #8a7d6a; }
.th-value { font-size: 34px; font-weight: 700; color: #4a3f30; line-height: 1.1; letter-spacing: .5px; }
.th-change { font-size: 13px; color: #a99c87; margin-top: 2px; }
.th-change.up { color: #b4452f; }      /* A股惯例:红涨 */
.th-change.down { color: #3f7a55; }    /* 绿跌 */
.range-switch { display: inline-flex; gap: 4px; margin: 6px 0 4px; }
.range-switch button {
  border: 1px solid var(--line, #d8cfc0); background: transparent;
  padding: 3px 12px; border-radius: 999px; cursor: pointer; font: inherit; color: #6b5d49;
}
.range-switch button.active { background: #7a5c3e; color: #fff; border-color: #7a5c3e; }
```
(若旧 `.range-switch button` 规则与此重复,以本块为准——删除 Task 9 旧版那条非药丸的 `.range-switch button` 样式,避免冲突。)

- [ ] **Step 6: 校验 — `node --check` + headless 渲染(0 快照也有基准线 + 渐变 + 药丸)**

```bash
cd /Users/chenmeng/PycharmProjects/StockBook
node --check static/js/app.js && echo "JS OK"
```
起服务(临时库)+ 造数据 + headless dump:
```bash
DB=/tmp/sb_redesign.db; rm -f "$DB"
STOCKBOOK_DATABASE_URL=sqlite:///$DB .venv/bin/python -c "
import datetime as dt, json
from app import database, models, seed
seed.init_db()
db=database.SessionLocal(); t=dt.date.today()
# benchmark history only (no extra portfolio snapshots) → 基准线应作为主线出现
for i in range(0,120,3):
    db.add(models.BenchmarkPoint(date=t-dt.timedelta(days=i), close=3800.0+i*2))
db.commit(); db.close()
"
STOCKBOOK_DATABASE_URL=sqlite:///$DB STOCKBOOK_SNAPSHOT_INTERVAL_HOURS=0 STOCKBOOK_BACKUP_INTERVAL_HOURS=0 STOCKBOOK_AUTO_REFRESH=0 .venv/bin/uvicorn main:app --port 8014 --log-level error &
sleep 3
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$CHROME" --headless --disable-gpu --no-sandbox --virtual-time-budget=6000 --dump-dom 'http://127.0.0.1:8014/#trends' 2>/dev/null > /tmp/rd.html
echo "pills:        $(grep -o 'data-range=' /tmp/rd.html | wc -l|tr -d ' ')  (expect 5)"
echo "gradient:     $(grep -o '<linearGradient' /tmp/rd.html | wc -l|tr -d ' ')  (expect >=1 — benchmark主线渐变)"
echo "bench polyline:$(grep -o '<polyline' /tmp/rd.html | wc -l|tr -d ' ')  (expect >=1, 0持仓也有沪深300线)"
echo "headline:     $(grep -o 'id=\"trend-total\"[^<]*' /tmp/rd.html | head -1)"
echo "NaN:          $(grep -o 'NaN' /tmp/rd.html | wc -l|tr -d ' ')  (expect 0)"
pkill -f "uvicorn main:app --port 8014"; sleep 1; rm -f "$DB" /tmp/rd.html
```
Expected: pills=5;gradient≥1;bench polyline≥1(0 持仓快照时基准线即主线);NaN=0。

- [ ] **Step 7: 起服务人工点验(可选,推荐)**

```bash
STOCKBOOK_DATABASE_URL=sqlite:////tmp/sb_view.db STOCKBOOK_SNAPSHOT_INTERVAL_HOURS=0 STOCKBOOK_BACKUP_INTERVAL_HOURS=0 .venv/bin/uvicorn main:app --port 8015
```
浏览器点「走势」:抬头大数字、暖渐变主线、药丸切区间、沪深300 细线;`?hideAmounts=1` 金额掩码。确认无 console 报错后停服务(`pkill -f "uvicorn main:app --port 8015"`)。

- [ ] **Step 8: Commit(控制方)**

```bash
git add templates/index.html static/js/app.js static/css/style.css
git commit -m "feat(ui): 走势 redesign — big-number headline, gradient-fill line, date axis, 沪深300 line by default, pill ranges

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: 文档 — `docs/architecture.md`

**Files:**
- Modify: `docs/architecture.md`

- [ ] **Step 1: 「3. 架构分层」`snapshot_service.py` 行职责补**

在该行末尾追加:`;沪深300 历史 backfill(`backfill_benchmark`)落 `BenchmarkPoint`,基准线/指标按表算`。

- [ ] **Step 2: 「4. 关键决策」追加条目 22**

```markdown
22. **沪深300 历史基准线 + 走势页重做**:新增 `BenchmarkPoint` 表(date→close)持久化东财抓来的指数日线(与 `Snapshot` 解耦——指数有用户没快照的交易日;历史收盘同属「过去不可重算」该落库)。**本地存 + 后台刷**:调度器启动 backfill ~3 年(`fetch_index_history` 东财日线,解析/网络分离)、每日 `run_snapshot` 补当天 close;**`/api/history` 只读本地表、页面零网络**。基准线与基准指标(growth/CAGR/最大回撤)按 `BenchmarkPoint` 在所选窗口算 → ≥2 个基准点即有数,**独立于持仓快照数量**;**不与持仓对齐**(各自缩放,仅形状参照)。区间 `3月/6月/1年/3年/全部` 驱动**今天相对**日期窗口。走势页换 **Robinhood 极简风**:大数字抬头(总资产 + 区间涨跌)+ 主曲线暖色渐变填充 + 沪深300 细线叠加 + 药丸区间 + 暖纸底 + 末点圆点;空/稀疏态网格框 + 基准线兜底。`reset_to_default` 清 `BenchmarkPoint`。
```

- [ ] **Step 3: 「5. JSON API 一览」更新 history 行**

把历史/绩效那行的 `GET /api/history?range=` 改为:`range=3m|6m|1y|3y|all`,并补:`返回新增 `benchmark_series`(密集,来自本地 `BenchmarkPoint`);基准指标按该表窗口算`。

- [ ] **Step 4: 「7. 功能日志」追加一行**

```markdown
- **2026-06-02** 沪深300 历史基准线 + 走势页重做:`BenchmarkPoint` 表本地存指数日线(东财 `fetch_index_history`/`parse_em_kline`,解析网络分离)+ `backfill_benchmark`(调度器启动补 ~3 年 + 每日补当天,页面只读表)+ `build_history` 返回 `benchmark_series`、基准指标按表算、区间今天相对(加 6m/3y)+ 走势页 Robinhood 极简风(大数字抬头 + 渐变填充主线 + 沪深300 默认线 + 药丸区间,headless 验证)。基准独立于持仓、不对齐。设计见 `docs/superpowers/specs/2026-06-02-stockbook-benchmark-history-and-trends-redesign-design.md`,计划见 `docs/superpowers/plans/2026-06-02-benchmark-history-and-trends-redesign.md`。
```

- [ ] **Step 5: 跑全套最后确认**

Run: `.venv/bin/pytest -q` → 全绿。

- [ ] **Step 6: Commit(控制方)**

```bash
git add docs/architecture.md
git commit -m "docs: record 沪深300 history line + 走势 redesign (BenchmarkPoint, build_history, Robinhood UI)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 收尾自检(控制方,全部 Task 后)

- [ ] `.venv/bin/pytest -q` 全绿;`coverage … --fail-under=95` 通过。
- [ ] headless 渲染:0 持仓快照时基准线即出现(渐变+药丸+大数字);有持仓时总资产渐变主线 + 沪深300 叠线;`hideAmounts` 掩码。
- [ ] 起服务点验走势页 Robinhood 风,无 console 报错。
- [ ] `docs/architecture.md` 三节更新。
- [ ] 每个 Task 独立 commit、含 Co-Authored-By;无 amend/squash/rebase。
