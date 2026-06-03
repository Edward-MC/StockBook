"""Tests for benchmark history: kline parsing, BenchmarkPoint, backfill."""
import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError

from app import database, models, seed


def test_benchmark_point_roundtrip(client):
    db = database.SessionLocal()
    try:
        db.add(models.BenchmarkPoint(date=dt.date(2025, 6, 1), close=3900.5))
        db.commit()
        got = db.query(models.BenchmarkPoint).one()
        assert got.date == dt.date(2025, 6, 1)
        assert got.close == 3900.5
    finally:
        db.close()


def test_benchmark_point_date_is_unique(client):
    # The UNIQUE(date) constraint backs the one-row-per-day upsert (BH3 relies
    # on it), so exercise it directly.
    db = database.SessionLocal()
    try:
        db.add(models.BenchmarkPoint(date=dt.date(2025, 6, 1), close=3900.5))
        db.commit()
        db.add(models.BenchmarkPoint(date=dt.date(2025, 6, 1), close=4000.0))
        with pytest.raises(IntegrityError):
            db.commit()
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
