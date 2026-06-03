"""Tests for snapshot capture + history assembly (history+performance spec)."""
import datetime as dt
import json

import pytest

from app import database, models, seed
from app import snapshot_service
from app import config


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
        assert got.id is not None
        assert got.date == dt.date(2025, 6, 1)
        assert got.total_assets == 123.0
        assert got.net_invested == 100.0
        assert got.benchmark == 4000.0
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
        # benchmark code 000300 was requested and resolved -> not None
        assert snap.benchmark == 9.99
        assert json.loads(snap.class_values)  # non-empty for the seeded strategy
        assert "000300" in fake_quotes["codes"]  # benchmark code was fetched
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
    # fetch_quotes returns nothing for the benchmark -> benchmark stored as None,
    # snapshot still succeeds.
    def _empty(items, sources=None):
        # _empty patches both the holding-quote refresh and the benchmark fetch →
        # verifies run_snapshot tolerates total quote unavailability.
        return {}
    monkeypatch.setattr(snapshot_service.quotes, "fetch_quotes", _empty)
    db = database.SessionLocal()
    try:
        snap = snapshot_service.run_snapshot(db)
        assert snap.benchmark is None
    finally:
        db.close()


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
        # rising NAV → zero drawdown; benchmark grows 4000 → 4390 over the window.
        assert all_h["metrics"]["max_drawdown"] == 0.0
        assert all_h["metrics"]["benchmark"]["growth"] == pytest.approx(390.0 / 4000.0, rel=1e-6)
        assert all_h["metrics"]["benchmark"]["cagr"] is not None

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


def test_build_history_metrics_numeric(client):
    # Controlled data with NO external flows: 100 → 200 over exactly 365 days.
    db = database.SessionLocal()
    try:
        db.query(models.CashFlow).delete()
        db.query(models.Snapshot).delete()
        db.commit()
        d0 = dt.date(2025, 1, 1)
        _add_snap(db, d0, 100.0, 100.0, None, {"1": 100.0})
        _add_snap(db, d0 + dt.timedelta(days=365), 200.0, 100.0, None, {"1": 200.0})
        db.commit()
        m = snapshot_service.build_history(db, range_="all")["metrics"]
        assert m["xirr"] == pytest.approx(1.0, abs=1e-3)   # doubled in 1yr, no flows
        assert m["twr"] == pytest.approx(1.0, abs=1e-3)
        assert m["max_drawdown"] == 0.0
    finally:
        db.close()


def test_build_history_xirr_excludes_start_date_cashflow(client):
    # A deposit ON the window-start date is already embedded in the start
    # snapshot's total_assets → it must NOT be double-counted in XIRR (C1).
    db = database.SessionLocal()
    try:
        db.query(models.CashFlow).delete()
        db.query(models.Snapshot).delete()
        db.commit()
        d0 = dt.date(2025, 1, 1)
        _add_snap(db, d0, 100.0, 100.0, None, {"1": 100.0})
        _add_snap(db, d0 + dt.timedelta(days=365), 200.0, 100.0, None, {"1": 200.0})
        db.add(models.CashFlow(date=d0, direction="in", amount=50.0, note="t"))
        db.commit()
        m = snapshot_service.build_history(db, range_="all")["metrics"]
        assert m["xirr"] == pytest.approx(1.0, abs=1e-3)  # unchanged by start-date flow
    finally:
        db.close()


def test_build_history_single_row_metrics(client):
    db = database.SessionLocal()
    try:
        db.query(models.Snapshot).delete()
        db.commit()
        _add_snap(db, dt.date(2025, 6, 1), 100.0, 100.0, 4000.0, {"1": 100.0})
        db.commit()
        m = snapshot_service.build_history(db, range_="all")["metrics"]
        assert m["xirr"] is None
        assert m["twr"] is None
        assert m["volatility"] is None
        assert m["max_drawdown"] == 0.0
    finally:
        db.close()


def test_start_scheduler_disabled_when_interval_zero(monkeypatch):
    monkeypatch.setattr(config, "SNAPSHOT_INTERVAL_HOURS", 0)
    assert snapshot_service.start_scheduler() is None
