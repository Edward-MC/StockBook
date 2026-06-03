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
