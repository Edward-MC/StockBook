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
