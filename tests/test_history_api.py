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


def test_get_history_invalid_range_falls_back(client):
    # An unrecognized range_ is accepted and treated as "all" (no 4xx).
    r = client.get("/api/history?range=garbage")
    assert r.status_code == 200
    assert r.json()["series"] == []


def test_get_history_6m_window_through_api(client):
    # Seed ~3y of dense benchmark points; 6m must return a genuinely narrower
    # window than 3y/all — proving the route passes 6m through (not coerced to all).
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
    for b in client.get("/api/history?range=6m").json()["benchmark_series"]:
        assert b["date"] >= cutoff
