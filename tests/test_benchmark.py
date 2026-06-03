"""Tests for benchmark history: kline parsing, BenchmarkPoint, backfill."""
import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError

from app import database, models, seed
from app import quotes
from app import snapshot_service


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


def test_backfill_benchmark_skips_when_fresh(client, monkeypatch):
    # Latest point is today → freshness guard returns 0 WITHOUT touching the net.
    def _no_net(code, market, days):
        raise AssertionError("fetch_index_history must not be called when fresh")
    monkeypatch.setattr(snapshot_service.quotes, "fetch_index_history", _no_net)
    db = database.SessionLocal()
    try:
        db.add(models.BenchmarkPoint(date=dt.date.today(), close=4000.0))
        db.commit()
        assert snapshot_service.backfill_benchmark(db) == 0
    finally:
        db.close()
