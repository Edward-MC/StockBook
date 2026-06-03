"""Tests for benchmark history: kline parsing, BenchmarkPoint, backfill."""
import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError

from app import database, models, seed
from app import quotes


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


def test_parse_em_kline_basic():
    text = '{"data":{"code":"000300","klines":["2025-06-01,3900.5","2025-06-02,3950.0"]}}'
    out = quotes.parse_em_kline(text)
    assert out == [(dt.date(2025, 6, 1), 3900.5), (dt.date(2025, 6, 2), 3950.0)]


def test_parse_em_kline_empty_or_bad():
    assert quotes.parse_em_kline('{"data":null}') == []
    assert quotes.parse_em_kline('not json') == []
    assert quotes.parse_em_kline('{"data":{"klines":42}}') == []  # non-list klines
    # missing comma / bad date / bad float rows are skipped; the good row survives.
    assert quotes.parse_em_kline(
        '{"data":{"klines":["bad", "bad-date,3950.0", "2025-06-03,notanum", "2025-06-02,3950.0"]}}'
    ) == [(dt.date(2025, 6, 2), 3950.0)]


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
