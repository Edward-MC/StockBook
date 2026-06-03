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
from .models import CashFlow, Security, Snapshot
from .services import apply_fetched_quotes, build_dashboard, build_ledger

_log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Benchmark code parsing: indices carry an explicit sh/sz prefix (spec §6).
# --------------------------------------------------------------------------- #
def _parse_benchmark(code: str) -> Optional[Tuple[str, str]]:
    """"sh000300\" → (\"000300\", \"SH\"); bare digits → (code, \"CN\"); empty → None."""
    c = (code or "").strip()
    if not c:
        return None
    low = c.lower()
    if low.startswith(("sh", "sz")):
        return c[2:], low[:2].upper()
    return c, "CN"


def _fetch_benchmark() -> Optional[float]:
    """Fetch the benchmark index point via the multi-source quote chain.
    Returns None on empty config, transport failure, or when no source resolved the code (never raises)."""
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
    apply_fetched_quotes(db, fetched)
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
    # Commit fresh quotes now so the live dashboard benefits too; the snapshot
    # is committed below. A crash between leaves quotes updated but no Snapshot
    # row — the next run simply re-snapshots with fresh prices.
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
    consistent with the chart (spec §8). An unrecognized range_ falls back to
    the full series (the API route validates the value)."""
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

    # CFs in [start_date, end_date]. calc.twr re-applies exclusive-start; XIRR
    # skips start-date CFs (already inside −V_start), counts the rest.
    cfs = db.scalars(
        select(CashFlow).where(CashFlow.date >= start_date, CashFlow.date <= end_date)
    ).all()
    twr_flows = [(cf.date, cf.amount if cf.direction == "in" else -cf.amount) for cf in cfs]

    nav_series = [(r.date, r.total_assets) for r in rows]
    # XIRR (investor view): window-start value out (−), deposits −, withdrawals +,
    # window-end value in (+).
    xirr_flows = [(start_date, -rows[0].total_assets)]
    for cf in cfs:
        if cf.date > start_date:  # start-date CFs are already inside −V_start
            xirr_flows.append((cf.date, -cf.amount if cf.direction == "in" else cf.amount))
    xirr_flows.append((end_date, rows[-1].total_assets))

    bench_rows = [r for r in rows if r.benchmark is not None]
    bench_nav = [r.benchmark for r in bench_rows]
    bench_days = (bench_rows[-1].date - bench_rows[0].date).days if len(bench_rows) >= 2 else 0
    bench = {
        "growth": (bench_nav[-1] / bench_nav[0] - 1.0) if len(bench_nav) >= 2 and bench_nav[0] else None,
        "cagr": _cagr(bench_nav, bench_days),
        "max_drawdown": calc.max_drawdown(bench_nav) if len(bench_nav) >= 2 else None,
    }
    return {
        "xirr": calc.xirr(xirr_flows),
        "twr": calc.twr(nav_series, twr_flows),
        "max_drawdown": calc.max_drawdown(nav),
        "volatility": calc.annualized_volatility(nav),
        "benchmark": bench,
    }
