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
