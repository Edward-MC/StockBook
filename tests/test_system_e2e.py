"""End-to-end system tests: complete user journeys through the public HTTP API.

Unlike test_api.py (single-endpoint integration), these walk multi-step user
journeys and assert *cross-step consistency of real derived values* and *state
transitions* — exercising router → services → calc → SQLite as one system.

Determinism: every holding is given a manual price (PUT …/price) so nothing
depends on live quotes; all network / RAG / LLM boundaries are monkeypatched.
The `client` fixture (tests/conftest.py) isolates each test in a temp SQLite DB
with seeded example data, RAG off, and auto backup/snapshot schedulers off.
"""
from __future__ import annotations

import datetime as dt
import sys
import types

import httpx
import pytest

from app import database
from app.models import PriceQuote, Security


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _dash(client):
    return client.get("/api/dashboard").json()


def _secs(client):
    """{code: security-dict} across all asset classes on the dashboard."""
    return {s["code"]: s
            for ac in _dash(client)["asset_classes"]
            for s in ac["securities"]}


# --------------------------------------------------------------------------- #
# Journey 1 — a holding's full lifecycle: create → buy → price → edit → sell →
# delete-guard, asserting hand-computed derived values at every step.
# --------------------------------------------------------------------------- #
def test_journey_holding_lifecycle(client):
    # Create a fresh asset class to hold the new position.
    ac = client.post("/api/asset-classes",
                     json={"name": "成长", "band_low": 0, "band_high": 100}).json()

    # Buy 1000 @ 2.0 by code → auto-creates the security under that class.
    buy = client.post("/api/transactions", json={
        "code": "159915", "asset_class_id": ac["id"], "date": "2025-05-01",
        "action": "buy", "shares": 1000, "price": 2.0}).json()
    sec_id, buy_id = buy["security_id"], buy["id"]

    # Set a manual current price 2.5 (overrides the provisional price seeded at buy).
    r = client.put(f"/api/securities/{sec_id}/price", json={"price": 2.5})
    assert r.status_code == 200 and r.json()["price"] == 2.5

    # Derived values: shares 1000, avg_cost 2.0, mv 2500, cost 2000, pnl +500 (25%).
    s = _secs(client)["159915"]
    assert s["shares"] == pytest.approx(1000)
    assert s["avg_cost"] == pytest.approx(2.0)
    assert s["market_value"] == pytest.approx(2500.0)
    assert s["cost_value"] == pytest.approx(2000.0)
    assert s["unrealized_pnl"] == pytest.approx(500.0)
    assert s["pnl_pct"] == pytest.approx(25.0)
    # Sole priced security in its class → 100% of class market value.
    assert s["weight_in_class"] == pytest.approx(100.0)

    # Correct the lot price 2.0 → 3.0. avg_cost follows; the manual price (2.5)
    # is unchanged, so mv stays 2500 and the position is now at a loss.
    client.put(f"/api/transactions/{buy_id}", json={"price": 3.0})
    s = _secs(client)["159915"]
    assert s["avg_cost"] == pytest.approx(3.0)
    assert s["cost_value"] == pytest.approx(3000.0)
    assert s["market_value"] == pytest.approx(2500.0)
    assert s["unrealized_pnl"] == pytest.approx(-500.0)

    # Sell 400 @ 4.0 out of that lot (specific-lot matching).
    sell = client.post("/api/transactions", json={
        "matched_buy_id": buy_id, "date": "2025-05-10",
        "action": "sell", "shares": 400, "price": 4.0})
    assert sell.status_code == 200

    # Net position drops to 600; ledger shows realized P&L = (4.0 − 3.0) × 400.
    assert _secs(client)["159915"]["shares"] == pytest.approx(600)
    ledger = client.get("/api/ledger").json()
    srow = next(e for e in ledger["entries"]
                if e["kind"] == "sell" and e["code"] == "159915")
    assert srow["buy_price"] == pytest.approx(3.0)          # post-edit lot price
    assert srow["realized_pnl"] == pytest.approx(400.0)
    assert srow["matched_buy_date"] == "2025-05-01"

    # Delete-guard: the buy lot now has a matched sell → deletion is rejected.
    r = client.delete(f"/api/transactions/{buy_id}")
    assert r.status_code == 400 and "卖出" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Journey 2 — targets validation + rebalance consistency + mark-rebalanced.
# --------------------------------------------------------------------------- #
def test_journey_targets_and_rebalance(client):
    acs = _dash(client)["asset_classes"]

    # A submission covering all classes but summing to 90 (≠100) is rejected.
    bad = [{"asset_class_id": a["id"], "target_weight": 18.0} for a in acs]  # 5×18 = 90
    r = client.put("/api/strategy/targets", json={"targets": bad})
    assert r.status_code == 400 and "100%" in r.json()["detail"]

    # Cover all classes summing to exactly 100 → accepted, strategy balanced.
    good = [{"asset_class_id": a["id"], "target_weight": 20.0} for a in acs]  # 5×20 = 100
    assert client.put("/api/strategy/targets", json={"targets": good}).status_code == 200
    d = _dash(client)
    assert d["is_balanced"] is True
    assert d["unallocated"] == pytest.approx(0.0)

    # Rebalance suggestions must be internally consistent with each class's state:
    # seed has no cash class, so every class value ≥ 0 and deviating_count == len(rebalance).
    assert d["deviating_count"] == len(d["rebalance"])
    by_id = {a["id"]: a for a in d["asset_classes"]}
    for reb in d["rebalance"]:
        ac = by_id[reb["asset_class_id"]]
        cw = ac["current_weight"]
        if reb["status"] == "over":
            assert cw > ac["band_high"]          # above band → needs trimming
            assert reb["amount"] < 0             # sell to reach target
            assert reb["edge_amount"] < 0        # sell to re-enter the band
        else:
            assert reb["status"] == "under"
            assert cw < ac["band_low"]           # below band → needs adding
            assert reb["amount"] > 0             # buy to reach target
            assert reb["edge_amount"] > 0
        # status/deviation agree with the band classification reported per class.
        assert ac["status"] == reb["status"]

    # Marking a rebalance done timestamps the strategy (None → set).
    assert d["last_rebalanced_at"] is None
    assert client.post("/api/strategy/rebalanced").status_code == 200
    assert _dash(client)["last_rebalanced_at"] is not None


# --------------------------------------------------------------------------- #
# Journey 3 — cash class + bookkeeping: cash balance / net-invested / total-
# assets / total-return / realized are all derived consistently across the API.
# --------------------------------------------------------------------------- #
def test_journey_cash_and_ledger(client):
    # A dedicated cash class (no securities) whose market value == cash balance.
    cash = client.post("/api/asset-classes", json={"name": "现金"}).json()
    client.put(f"/api/asset-classes/{cash['id']}", json={"is_cash": True})

    # Seed has deposits 200000 / withdrawals 20000. Add 50000 in, 10000 out.
    client.post("/api/cashflows", json={"date": "2026-01-01", "direction": "in", "amount": 50000})
    client.post("/api/cashflows", json={"date": "2026-01-05", "direction": "out", "amount": 10000})

    # Sell 1000 of the seeded 沪深300 lot @ 4.5 to exercise sell-proceeds in cash.
    s510300 = _secs(client)["510300"]
    txs = client.get(f"/api/securities/{s510300['id']}/transactions").json()
    buy_lot = next(t for t in txs if t["action"] == "buy")
    client.post("/api/transactions", json={
        "matched_buy_id": buy_lot["id"], "date": "2026-01-06",
        "action": "sell", "shares": 1000, "price": 4.5})

    # cash = deposits − withdrawals + sells − buys
    #      = 250000 − 30000 + 4500 − 105200 = 119300
    L = client.get("/api/ledger").json()
    sm = L["summary"]
    assert sm["deposits"] == pytest.approx(250000.0)
    assert sm["withdrawals"] == pytest.approx(30000.0)
    assert sm["cash_balance"] == pytest.approx(119300.0)
    assert sm["net_invested"] == pytest.approx(220000.0)   # 250000 − 30000
    # holdings: 510300 now 9000×4.0=36000, +510500 32500 +515080 12800 +512170 2250
    #           +511260 21000  (516160 pending → excluded) = 104550
    assert sm["holdings_value"] == pytest.approx(104550.0)
    assert sm["total_assets"] == pytest.approx(104550.0 + 119300.0)
    assert sm["total_return"] == pytest.approx(sm["total_assets"] - sm["net_invested"])
    assert sm["realized_pnl"] == pytest.approx((4.5 - 3.80) * 1000)  # +700

    # Dashboard cash class value mirrors the derived cash balance.
    cash_view = next(a for a in _dash(client)["asset_classes"] if a["id"] == cash["id"])
    assert cash_view["is_cash"] is True
    assert cash_view["market_value"] == pytest.approx(119300.0)


# --------------------------------------------------------------------------- #
# Journey 4 — quote multi-source failover through POST /api/prices/refresh.
# --------------------------------------------------------------------------- #
class _FakeSource:
    """A QuoteSource stub — returns a canned result or raises a transport error."""
    def __init__(self, name, result=None, error=None):
        self.name = name
        self._result = result or {}
        self._error = error

    def fetch(self, items):
        if self._error is not None:
            raise self._error
        return self._result


def test_journey_quote_failover(client, monkeypatch):
    from app import quotes

    # First source (tencent) fails at transport level; the next (sina) serves data.
    monkeypatch.setitem(quotes.QUOTE_SOURCES, "tencent",
                        _FakeSource("tencent", error=httpx.ConnectError("down")))
    monkeypatch.setitem(quotes.QUOTE_SOURCES, "sina",
                        _FakeSource("sina", result={"510300": {"price": 7.77, "name": "沪深300ETF"}}))

    r = client.post("/api/prices/refresh")
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == 1                 # only 510300 came back
    assert body["source"] == "sina"             # the source that took over
    assert "510300" not in body["unresolved"]
    assert "510500" in body["unresolved"]       # other seeded codes unresolved

    # The price was written and tagged source="auto".
    assert _secs(client)["510300"]["price"] == pytest.approx(7.77)
    db = database.SessionLocal()
    try:
        sec = db.query(Security).filter_by(code="510300").first()
        assert sec.quote.source == "auto"
    finally:
        db.close()

    # Every source down → 502 (the whole chain failed at transport level).
    for name in ("tencent", "sina", "eastmoney"):
        monkeypatch.setitem(quotes.QUOTE_SOURCES, name,
                            _FakeSource(name, error=httpx.ConnectError("down")))
    assert client.post("/api/prices/refresh").status_code == 502


# --------------------------------------------------------------------------- #
# Journey 5 — data-safety closed loop: backup → mutate → restore → verify,
# plus reset's automatic pre-backup.
# --------------------------------------------------------------------------- #
def test_journey_backup_restore_verify_reset(client):
    # Snapshot the seeded 5-class state.
    assert client.post("/api/backup").status_code == 200
    file = client.get("/api/backups").json()[0]["file"]

    # Mutate: drop a class → 4 remain.
    victim = _dash(client)["asset_classes"][0]["id"]
    client.delete(f"/api/asset-classes/{victim}")
    assert len(_dash(client)["asset_classes"]) == 4

    # Restore rolls the live DB back to the 5-class snapshot.
    assert client.post("/api/restore", json={"file": file}).status_code == 200
    assert len(_dash(client)["asset_classes"]) == 5

    # Verify reports tri-state "ok" for the intact backup(s).
    res = client.post("/api/backup/verify").json()
    assert res and all(r["status"] == "ok" for r in res)

    # Reset auto-backs up first (a misclick is recoverable), then re-seeds.
    before = len(client.get("/api/backups").json())
    assert client.post("/api/reset").status_code == 200
    assert len(client.get("/api/backups").json()) > before
    assert len(_dash(client)["asset_classes"]) == 5


def test_journey_encrypted_offsite_restore(client, tmp_path, monkeypatch):
    from app import config

    offsite = tmp_path / "offsite"
    monkeypatch.setattr(config, "BACKUP_DIR", str(offsite))
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "correct-horse")

    # Backup writes a local copy + an encrypted offsite copy.
    client.post("/api/backup")
    file = client.get("/api/backups").json()[0]["file"]

    # Restore from the encrypted offsite with the correct passphrase → ok.
    r = client.post("/api/restore", json={"file": file, "destination": "offsite"})
    assert r.status_code == 200 and r.json()["ok"] is True

    # Wrong passphrase → 400, and the live DB is left untouched (still 5 classes).
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "wrong")
    r = client.post("/api/restore", json={"file": file, "destination": "offsite"})
    assert r.status_code == 400
    assert len(_dash(client)["asset_classes"]) == 5


# --------------------------------------------------------------------------- #
# Journey 6 — read-only sharing guard: writes 403, reads still pass.
# --------------------------------------------------------------------------- #
def test_journey_readonly_guard(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "READONLY", True)

    ac_id = _dash(client)["asset_classes"][0]["id"]

    # Every write path is blocked with 403.
    assert client.post("/api/asset-classes", json={"name": "X"}).status_code == 403
    assert client.post("/api/transactions", json={
        "code": "159915", "asset_class_id": ac_id, "date": "2025-05-01",
        "action": "buy", "shares": 100, "price": 1.0}).status_code == 403
    assert client.put("/api/strategy/targets", json={
        "targets": [{"asset_class_id": ac_id, "target_weight": 100}]}).status_code == 403
    assert client.post("/api/cashflows", json={
        "date": "2026-01-01", "direction": "in", "amount": 100}).status_code == 403
    assert client.post("/api/backup").status_code == 403
    assert client.post("/api/restore", json={"file": "x.db"}).status_code == 403
    assert client.post("/api/snapshot").status_code == 403
    assert client.post("/api/reset").status_code == 403

    # Read paths remain available.
    assert client.get("/api/dashboard").status_code == 200
    assert client.get("/api/ledger").status_code == 200
    assert client.get("/api/history").status_code == 200


# --------------------------------------------------------------------------- #
# Journey 7 — RAG Q&A journey (stubbed at the network/LLM boundary) + guards.
# --------------------------------------------------------------------------- #
def _enable_rag(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "RAG_ENABLED", True)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")


def test_journey_rag_sync_and_ask(client, monkeypatch):
    _enable_rag(monkeypatch)
    from app.rag import embed, notion, store

    # Embedding cache is a module-global keyed on (count, max_id); reset it so a
    # prior test's temp-DB cache can't shadow this fresh DB.
    store._embed_cache.update(key=None, ids=[], matrix=None)

    # Stub the Notion crawl and the local embedder (both deterministic, no network).
    PAGE = {"page_id": "p1", "url": "https://notion.so/p1",
            "title": "策略/红利", "text": "红利策略偏好高股息蓝筹。"}
    monkeypatch.setattr(notion, "crawl_source",
                        lambda nid, kind, on_progress=None: [PAGE])
    monkeypatch.setattr(embed, "embed_texts", lambda texts: [[1.0, 0.0] for _ in texts])
    monkeypatch.setattr(embed, "embed_one", lambda q: [1.0, 0.0])

    # Register a source and sync → chunks land in the knowledge base.
    client.post("/api/rag/sources", json={"notion_id": "p1", "title": "策略", "kind": "page"})
    sync = client.post("/api/rag/sync").json()
    assert sync["chunk_count"] >= 1
    assert sync["errors"] == 0

    # Stub only the Claude call inside ask.answer (real retrieval + prompt build).
    captured = {}

    class _FakeBlock:
        type = "text"
        text = "摘要:红利偏好高股息。[1]"

    class _FakeResp:
        content = [_FakeBlock()]

    class _FakeMessages:
        def create(self, **kw):
            captured["kw"] = kw
            return _FakeResp()

    class _FakeAnthropic:
        def __init__(self, api_key=None):
            self.messages = _FakeMessages()

    fake_mod = types.ModuleType("anthropic")
    fake_mod.Anthropic = _FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

    r = client.post("/api/rag/ask", json={"question": "红利怎么看?"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "摘要:红利偏好高股息。[1]"
    # Retrieval hit the stored chunk → its Notion link is returned as a citation.
    assert any(c["notion_url"] == "https://notion.so/p1" for c in body["citations"])

    # The prompt actually sent to Claude embedded the retrieved excerpt, its
    # source link, and the live holdings snapshot.
    prompt = captured["kw"]["messages"][0]["content"]
    assert "红利策略偏好高股息蓝筹。" in prompt
    assert "https://notion.so/p1" in prompt
    assert "【当前持仓】" in prompt


def test_journey_rag_guards(client, monkeypatch):
    # Disabled (default): status reports off and /ask is forbidden.
    assert client.get("/api/rag/status").json()["enabled"] is False
    assert client.post("/api/rag/ask", json={"question": "hi"}).status_code == 403

    # Enabled but read-only → still forbidden.
    _enable_rag(monkeypatch)
    from app import config
    monkeypatch.setattr(config, "READONLY", True)
    assert client.post("/api/rag/ask", json={"question": "hi"}).status_code == 403


def test_journey_rag_rate_limit(client, monkeypatch):
    _enable_rag(monkeypatch)
    from app import config
    from app.rag import ask
    from app.routers import rag as rag_router

    # Limit of 1: a fresh limiter, a stubbed (successful) answer.
    monkeypatch.setattr(config, "RAG_DAILY_LIMIT", 1)
    monkeypatch.setattr(rag_router, "_limiter",
                        rag_router.limiter.DailyLimiter(config.RAG_DAILY_LIMIT))
    monkeypatch.setattr(ask, "answer", lambda db, q: {"answer": "ok", "citations": []})

    assert client.post("/api/rag/ask", json={"question": "1"}).status_code == 200
    assert client.post("/api/rag/ask", json={"question": "2"}).status_code == 429  # over limit


# --------------------------------------------------------------------------- #
# Journey 8 — history / performance: snapshot write, history shape + metrics,
# benchmark-null tolerance, empty-state.
# --------------------------------------------------------------------------- #
def _add_snap(db, day, total, net, bench):
    import json as _json
    from app.models import Snapshot
    db.add(Snapshot(date=day, total_assets=total, net_invested=net,
                    benchmark=bench, class_values=_json.dumps({"1": total})))


def test_journey_history_and_performance(client, monkeypatch):
    # Empty state: no snapshots yet → empty series + None metrics, no crash.
    h0 = client.get("/api/history?range=all").json()
    assert h0["series"] == []
    assert h0["metrics"]["xirr"] is None
    assert h0["metrics"]["max_drawdown"] is None

    # POST /api/snapshot captures today's NAV (quotes stubbed → benchmark resolves).
    def _fake(items, sources=None):
        return {code: {"price": 9.99, "name": code} for code, market in items}
    monkeypatch.setattr("app.snapshot_service.quotes.fetch_quotes", _fake)
    snap = client.post("/api/snapshot").json()
    assert snap["date"] == dt.date.today().isoformat()
    assert snap["benchmark"] == pytest.approx(9.99)
    db = database.SessionLocal()
    try:
        from app.models import Snapshot
        assert db.query(Snapshot).count() == 1   # one row for today
        # Replace with a controlled, flow-free series: 100 → 200 over exactly 365
        # days (benchmark null both days → must not crash) to pin the metrics.
        db.query(Snapshot).delete()
        from app.models import CashFlow
        db.query(CashFlow).delete()
        d0 = dt.date(2025, 1, 1)
        _add_snap(db, d0, 100.0, 100.0, None)
        _add_snap(db, d0 + dt.timedelta(days=365), 200.0, 100.0, None)
        db.commit()
    finally:
        db.close()

    h = client.get("/api/history?range=all").json()
    assert len(h["series"]) == 2
    assert set(h["series"][0].keys()) == {
        "date", "total_assets", "net_invested", "benchmark", "class_values"}
    m = h["metrics"]
    assert set(m.keys()) >= {"xirr", "twr", "max_drawdown", "volatility", "benchmark"}
    assert m["xirr"] == pytest.approx(1.0, abs=1e-3)          # doubled in 1y, no flows
    assert m["twr"] == pytest.approx(1.0, abs=1e-3)
    assert m["max_drawdown"] == 0.0                            # monotonically up
    assert set(m["benchmark"].keys()) == {"growth", "cagr", "max_drawdown"}
