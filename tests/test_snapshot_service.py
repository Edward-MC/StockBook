"""Tests for snapshot capture + history assembly (history+performance spec)."""
import datetime as dt
import json

import pytest

from app import database, models, seed
from app import snapshot_service


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
