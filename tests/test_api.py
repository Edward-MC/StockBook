"""API integration tests (spec §8)."""
import pytest


def test_dashboard_seeded(client):
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    data = r.json()
    assert data["strategy_name"] == "我的核心配置"
    assert len(data["asset_classes"]) == 5
    assert data["total_assets"] > 0
    # one seeded security (新能源ETF) has no price → pending
    codes = [s["code"] for s in data["pending_securities"]]
    assert "516160" in codes


def test_dashboard_targets_sum_to_100_balanced():
    pass  # covered indirectly; seed targets sum to 100


def test_auto_color_assigned_and_distinct(client):
    a = client.post("/api/asset-classes", json={"name": "A", "band_low": 0, "band_high": 10}).json()
    b = client.post("/api/asset-classes", json={"name": "B", "band_low": 0, "band_high": 10}).json()
    for c in (a, b):
        assert c["color"].startswith("#") and len(c["color"]) == 7  # auto hex
    assert a["color"] != b["color"]


def test_recolor_makes_all_distinct(client):
    for n in ["海外债券", "海外新兴", "海外成熟", "现金", "黄金2"]:
        client.post("/api/asset-classes", json={"name": n, "band_low": 0, "band_high": 10})
    client.post("/api/asset-classes/recolor")
    cols = [a["color"] for a in client.get("/api/dashboard").json()["asset_classes"]]
    assert len(cols) == len(set(cols))  # every class a different color


def test_create_update_delete_asset_class(client):
    r = client.post("/api/asset-classes", json={"name": "黄金", "target_weight": 0,
                                                 "band_low": 0, "band_high": 10, "color": "--c1"})
    assert r.status_code == 200
    ac_id = r.json()["id"]

    r = client.put(f"/api/asset-classes/{ac_id}", json={"name": "贵金属"})
    assert r.status_code == 200 and r.json()["name"] == "贵金属"

    r = client.delete(f"/api/asset-classes/{ac_id}")
    assert r.status_code == 200


def test_create_security_and_transaction_and_price(client):
    # add to first asset class
    dash = client.get("/api/dashboard").json()
    ac_id = dash["asset_classes"][0]["id"]

    r = client.post("/api/securities", json={"asset_class_id": ac_id, "code": "159915",
                                             "name": "创业板ETF"})
    assert r.status_code == 200
    sec_id = r.json()["id"]

    r = client.post("/api/transactions", json={"security_id": sec_id, "date": "2025-05-01",
                                               "action": "buy", "shares": 1000, "price": 2.0})
    assert r.status_code == 200

    r = client.put(f"/api/securities/{sec_id}/price", json={"price": 2.5})
    assert r.status_code == 200 and r.json()["price"] == 2.5


def test_oversell_rejected(client):
    dash = client.get("/api/dashboard").json()
    ac_id = dash["asset_classes"][0]["id"]
    sec_id = client.post("/api/securities", json={"asset_class_id": ac_id, "code": "X",
                                                  "name": "x"}).json()["id"]
    buy = client.post("/api/transactions", json={"security_id": sec_id, "date": "2025-05-01",
                                                 "action": "buy", "shares": 100, "price": 1.0}).json()
    r = client.post("/api/transactions", json={"matched_buy_id": buy["id"], "date": "2025-05-02",
                                               "action": "sell", "shares": 200, "price": 1.0})
    assert r.status_code == 400
    assert "超过该批次剩余" in r.json()["detail"]


def test_sell_requires_matched_buy(client):
    dash = client.get("/api/dashboard").json()
    ac_id = dash["asset_classes"][0]["id"]
    sec_id = client.post("/api/securities", json={"asset_class_id": ac_id, "code": "M",
                                                  "name": "m"}).json()["id"]
    client.post("/api/transactions", json={"security_id": sec_id, "date": "2025-05-01",
                                           "action": "buy", "shares": 100, "price": 1.0})
    r = client.post("/api/transactions", json={"security_id": sec_id, "date": "2025-05-02",
                                               "action": "sell", "shares": 10, "price": 1.0})
    assert r.status_code == 400 and "买入批次" in r.json()["detail"]


def test_negative_shares_rejected_by_schema(client):
    dash = client.get("/api/dashboard").json()
    ac_id = dash["asset_classes"][0]["id"]
    sec_id = client.post("/api/securities", json={"asset_class_id": ac_id, "code": "Y",
                                                  "name": "y"}).json()["id"]
    r = client.post("/api/transactions", json={"security_id": sec_id, "date": "2025-05-01",
                                               "action": "buy", "shares": -5, "price": 1.0})
    assert r.status_code == 422  # pydantic gt=0


def test_targets_must_sum_to_100(client):
    dash = client.get("/api/dashboard").json()
    acs = dash["asset_classes"]
    # cover all classes but with a wrong total (sum != 100)
    bad = [{"asset_class_id": ac["id"], "target_weight": 50} for ac in acs]
    r = client.put("/api/strategy/targets", json={"targets": bad})
    assert r.status_code == 400
    assert "100%" in r.json()["detail"]


def test_targets_partial_subset_rejected(client):
    # A subset that sums to 100 must still be rejected (would leave others' targets).
    dash = client.get("/api/dashboard").json()
    one = dash["asset_classes"][0]["id"]
    r = client.put("/api/strategy/targets",
                   json={"targets": [{"asset_class_id": one, "target_weight": 100}]})
    assert r.status_code == 400
    assert "覆盖" in r.json()["detail"]


def test_delete_buy_with_matched_sell_blocked(client):
    dash = client.get("/api/dashboard").json()
    ac_id = dash["asset_classes"][0]["id"]
    sec_id = client.post("/api/securities",
                         json={"asset_class_id": ac_id, "code": "Z", "name": "z"}).json()["id"]
    buy = client.post("/api/transactions", json={"security_id": sec_id, "date": "2025-05-01",
                                                 "action": "buy", "shares": 100, "price": 1.0}).json()
    client.post("/api/transactions", json={"matched_buy_id": buy["id"], "date": "2025-05-02",
                                           "action": "sell", "shares": 60, "price": 1.0})
    # The buy lot has a matched sell → can't delete it.
    r = client.delete(f"/api/transactions/{buy['id']}")
    assert r.status_code == 400 and "对应卖出" in r.json()["detail"]


def test_unallocated_returns_to_pool_on_delete_class(client):
    # Seed targets sum to 100 (balanced). Deleting a class returns its target
    # to the unallocated pool.
    dash = client.get("/api/dashboard").json()
    assert dash["is_balanced"] is True
    victim = next(ac for ac in dash["asset_classes"] if ac["name"] == "沪深300")
    client.delete(f"/api/asset-classes/{victim['id']}")
    after = client.get("/api/dashboard").json()
    assert after["is_balanced"] is False
    assert after["unallocated"] == 30.0  # 沪深300's target returns to the pool


def test_new_class_keeps_pool_when_target_zero(client):
    dash = client.get("/api/dashboard").json()
    assert dash["unallocated"] == 0.0
    client.post("/api/asset-classes", json={"name": "黄金", "target_weight": 0,
                                            "band_low": 0, "band_high": 10, "color": "--c1"})
    after = client.get("/api/dashboard").json()
    # New class defaults to 0% target → pool unchanged, still balanced.
    assert after["unallocated"] == 0.0
    assert after["is_balanced"] is True


def test_targets_valid_save(client):
    dash = client.get("/api/dashboard").json()
    acs = dash["asset_classes"]
    n = len(acs)
    each = 100.0 / n
    targets = [{"asset_class_id": ac["id"], "target_weight": each} for ac in acs]
    # fix rounding on last so the sum is exactly 100
    targets[-1]["target_weight"] = 100.0 - each * (n - 1)
    r = client.put("/api/strategy/targets", json={"targets": targets})
    assert r.status_code == 200

    dash2 = client.get("/api/dashboard").json()
    assert dash2["is_balanced"] is True


def test_reset_restores_seed(client):
    # delete a class, then reset
    dash = client.get("/api/dashboard").json()
    client.delete(f"/api/asset-classes/{dash['asset_classes'][0]['id']}")
    assert len(client.get("/api/dashboard").json()["asset_classes"]) == 4
    client.post("/api/reset")
    assert len(client.get("/api/dashboard").json()["asset_classes"]) == 5


def test_readonly_param_reflected(client):
    data = client.get("/api/dashboard?readonly=1&hideAmounts=1").json()
    assert data["readonly"] is True
    assert data["hide_amounts"] is True


def test_transaction_seeds_provisional_price(client):
    # New security has no price; the first trade should seed a provisional price
    # so its market value is immediately meaningful.
    dash = client.get("/api/dashboard").json()
    ac_id = dash["asset_classes"][0]["id"]
    sec_id = client.post("/api/securities",
                         json={"asset_class_id": ac_id, "code": "PV", "name": "pv"}).json()["id"]
    client.post("/api/transactions", json={"security_id": sec_id, "date": "2025-06-01",
                                           "action": "buy", "shares": 1000, "price": 5.0})
    secs = {s["code"]: s for ac in client.get("/api/dashboard").json()["asset_classes"]
            for s in ac["securities"]}
    pv = secs["PV"]
    assert pv["price"] == 5.0
    assert pv["market_value"] == 5000.0
    assert pv["pending"] is False


def test_explicit_price_not_overwritten_by_trade(client):
    dash = client.get("/api/dashboard").json()
    ac_id = dash["asset_classes"][0]["id"]
    sec_id = client.post("/api/securities",
                         json={"asset_class_id": ac_id, "code": "PX", "name": "px"}).json()["id"]
    client.put(f"/api/securities/{sec_id}/price", json={"price": 9.0})
    client.post("/api/transactions", json={"security_id": sec_id, "date": "2025-06-01",
                                           "action": "buy", "shares": 100, "price": 5.0})
    secs = {s["code"]: s for ac in client.get("/api/dashboard").json()["asset_classes"]
            for s in ac["securities"]}
    assert secs["PX"]["price"] == 9.0  # user-set price preserved


def test_pnl_and_avg_cost_in_dashboard(client):
    # Seeded 沪深300/510300: buy 10000 @ 3.80, price 4.00 → avg 3.8, pnl +2000
    secs = {s["code"]: s for ac in client.get("/api/dashboard").json()["asset_classes"]
            for s in ac["securities"]}
    s = secs["510300"]
    assert s["avg_cost"] == pytest.approx(3.80)
    assert s["unrealized_pnl"] == pytest.approx((4.00 - 3.80) * 10000)


def test_dashboard_cash_balance(client):
    # seed: deposits 200000 − withdrawals 20000 − buys 105200 + sells 0 = 74800
    assert client.get("/api/dashboard").json()["cash_balance"] == pytest.approx(74800.0)


def test_ledger_entries_and_summary(client):
    L = client.get("/api/ledger").json()
    kinds = {e["kind"] for e in L["entries"]}
    assert {"buy", "deposit", "withdraw"} <= kinds
    s = L["summary"]
    assert s["deposits"] == 200000 and s["withdrawals"] == 20000
    assert s["cash_balance"] == pytest.approx(74800.0)
    assert s["net_invested"] == pytest.approx(180000.0)
    # holdings_value = Σ priced security MV (516160 pending → excluded)
    assert s["holdings_value"] == pytest.approx(108550.0)
    assert s["total_assets"] == pytest.approx(108550.0 + 74800.0)
    assert s["total_return"] == pytest.approx(s["total_assets"] - s["net_invested"])


def test_cashflow_create_and_delete_affect_balance(client):
    before = client.get("/api/dashboard").json()["cash_balance"]
    cf = client.post("/api/cashflows", json={"date": "2026-01-01", "direction": "in",
                                             "amount": 5000, "note": "加仓资金"}).json()
    assert client.get("/api/dashboard").json()["cash_balance"] == pytest.approx(before + 5000)
    client.delete(f"/api/cashflows/{cf['id']}")
    assert client.get("/api/dashboard").json()["cash_balance"] == pytest.approx(before)


def test_mark_cash_class_value_is_balance(client):
    d = client.get("/api/dashboard").json()
    ac_id = d["asset_classes"][-1]["id"]
    client.put(f"/api/asset-classes/{ac_id}", json={"is_cash": True})
    d2 = client.get("/api/dashboard").json()
    cash = next(a for a in d2["asset_classes"] if a["id"] == ac_id)
    assert cash["is_cash"] is True
    assert cash["market_value"] == pytest.approx(d2["cash_balance"])


def test_only_one_cash_class(client):
    acs = client.get("/api/dashboard").json()["asset_classes"]
    client.put(f"/api/asset-classes/{acs[0]['id']}", json={"is_cash": True})
    client.put(f"/api/asset-classes/{acs[1]['id']}", json={"is_cash": True})
    cash_ids = [a["id"] for a in client.get("/api/dashboard").json()["asset_classes"] if a["is_cash"]]
    assert cash_ids == [acs[1]["id"]]


def test_dashboard_has_price_state(client):
    data = client.get("/api/dashboard").json()
    assert data["price_state"] in ("live", "close")  # seed has priced securities


def test_backup_creates_and_lists_file(client):
    assert client.get("/api/backups").json() == []
    r = client.post("/api/backup")
    assert r.status_code == 200 and r.json()["file"].endswith(".db")
    backups = client.get("/api/backups").json()
    assert len(backups) == 1 and backups[0]["size"] > 0


def test_reset_auto_backups(client):
    client.post("/api/reset")
    # reset should have snapshotted the DB first
    assert len(client.get("/api/backups").json()) >= 1


def test_restore_reverts_to_backup(client):
    client.post("/api/backup")  # snapshot with all 5 classes
    file = client.get("/api/backups").json()[0]["file"]
    # mutate: delete a class
    dash = client.get("/api/dashboard").json()
    client.delete(f"/api/asset-classes/{dash['asset_classes'][0]['id']}")
    assert len(client.get("/api/dashboard").json()["asset_classes"]) == 4
    # restore → back to 5
    r = client.post("/api/restore", json={"file": file})
    assert r.status_code == 200
    assert len(client.get("/api/dashboard").json()["asset_classes"]) == 5


def test_restore_missing_file_404(client):
    assert client.post("/api/restore", json={"file": "nope.db"}).status_code == 404
    # path traversal is reduced to a basename → still 404, never escapes backups/
    assert client.post("/api/restore", json={"file": "../../etc/passwd"}).status_code == 404


def test_mark_rebalanced(client):
    assert client.get("/api/dashboard").json()["last_rebalanced_at"] is None
    r = client.post("/api/strategy/rebalanced")
    assert r.status_code == 200
    assert client.get("/api/dashboard").json()["last_rebalanced_at"] is not None


def test_rebalance_has_edge_amount(client):
    reb = client.get("/api/dashboard").json()["rebalance"]
    assert reb and all("edge_amount" in r for r in reb)


def test_transaction_by_code_autocreates_security(client):
    ac_id = client.get("/api/dashboard").json()["asset_classes"][0]["id"]
    r = client.post("/api/transactions", json={"code": "159920", "asset_class_id": ac_id,
                                               "date": "2025-06-01", "action": "buy",
                                               "shares": 500, "price": 1.2, "target_sell_price": 1.5})
    assert r.status_code == 200
    sid = r.json()["security_id"]
    assert r.json()["target_sell_price"] == 1.5
    # security now exists with name defaulting to the code
    secs = {s["code"]: s for ac in client.get("/api/dashboard").json()["asset_classes"]
            for s in ac["securities"]}
    assert "159920" in secs and secs["159920"]["name"] == "159920"
    # a second trade with the same code reuses it (no asset_class_id needed)
    r2 = client.post("/api/transactions", json={"code": "159920", "date": "2025-06-02",
                                                "action": "buy", "shares": 100, "price": 1.3})
    assert r2.json()["security_id"] == sid


def test_transaction_new_code_requires_asset_class(client):
    r = client.post("/api/transactions", json={"code": "999999", "date": "2025-06-01",
                                               "action": "buy", "shares": 100, "price": 1.0})
    assert r.status_code == 400
    assert "大类" in r.json()["detail"]


def test_sell_does_not_store_target_sell_price(client):
    ac_id = client.get("/api/dashboard").json()["asset_classes"][0]["id"]
    buy = client.post("/api/transactions", json={"code": "AAA", "asset_class_id": ac_id,
                                                 "date": "2025-06-01", "action": "buy",
                                                 "shares": 100, "price": 1.0}).json()
    r = client.post("/api/transactions", json={"matched_buy_id": buy["id"], "date": "2025-06-02",
                                               "action": "sell", "shares": 50, "price": 1.1,
                                               "target_sell_price": 9.0})
    assert r.json()["target_sell_price"] is None


def test_refresh_prices_updates_to_auto(client, monkeypatch):
    from app import quotes
    monkeypatch.setattr(quotes, "fetch_quotes", lambda items: {"510300": {"price": 9.99, "name": "沪深300ETF"}})
    r = client.post("/api/prices/refresh")
    assert r.status_code == 200 and r.json()["updated"] == 1
    secs = {s["code"]: s for ac in client.get("/api/dashboard").json()["asset_classes"]
            for s in ac["securities"]}
    assert secs["510300"]["price"] == 9.99


def test_refresh_backfills_placeholder_name(client, monkeypatch):
    from app import quotes
    ac_id = client.get("/api/dashboard").json()["asset_classes"][0]["id"]
    # auto-created by code → name defaults to the code
    client.post("/api/transactions", json={"code": "159920", "asset_class_id": ac_id,
                                           "date": "2026-05-30", "action": "buy", "shares": 100, "price": 2.0})
    monkeypatch.setattr(quotes, "fetch_quotes", lambda items: {"159920": {"price": 2.2, "name": "恒生ETF"}})
    client.post("/api/prices/refresh")
    secs = {s["code"]: s for ac in client.get("/api/dashboard").json()["asset_classes"]
            for s in ac["securities"]}
    assert secs["159920"]["name"] == "恒生ETF"  # placeholder name filled from quote


def test_refresh_reports_unresolved(client, monkeypatch):
    from app import quotes
    # Source returns only 510300; everything else is "unresolved".
    monkeypatch.setattr(quotes, "fetch_quotes", lambda items: {"510300": {"price": 4.0, "name": "x"}})
    r = client.post("/api/prices/refresh").json()
    assert r["updated"] == 1
    assert "516160" in r["unresolved"] and "510300" not in r["unresolved"]


def test_refresh_network_failure_returns_502(client, monkeypatch):
    import httpx
    from app import quotes
    def boom(items):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(quotes, "fetch_quotes", boom)
    r = client.post("/api/prices/refresh")
    assert r.status_code == 502


def test_list_and_update_transaction_target(client):
    ac_id = client.get("/api/dashboard").json()["asset_classes"][0]["id"]
    tx = client.post("/api/transactions", json={"code": "BBB", "asset_class_id": ac_id,
                                                "date": "2025-06-01", "action": "buy",
                                                "shares": 100, "price": 2.0}).json()
    sid, tid = tx["security_id"], tx["id"]
    lst = client.get(f"/api/securities/{sid}/transactions").json()
    assert len(lst) == 1 and lst[0]["target_sell_price"] is None
    # set then clear the target
    assert client.put(f"/api/transactions/{tid}", json={"target_sell_price": 2.8}).json()["target_sell_price"] == 2.8
    assert client.put(f"/api/transactions/{tid}", json={"target_sell_price": None}).json()["target_sell_price"] is None


def test_edit_lot_price_recomputes_avg_cost(client):
    ac_id = client.get("/api/dashboard").json()["asset_classes"][0]["id"]
    tx = client.post("/api/transactions", json={"code": "CCC", "asset_class_id": ac_id,
                                                "date": "2025-06-01", "action": "buy",
                                                "shares": 100, "price": 2.0}).json()
    # fix a mis-entered price 2.0 → 3.0; avg cost should follow
    client.put(f"/api/transactions/{tx['id']}", json={"price": 3.0})
    secs = {s["code"]: s for ac in client.get("/api/dashboard").json()["asset_classes"]
            for s in ac["securities"]}
    assert secs["CCC"]["avg_cost"] == pytest.approx(3.0)


def test_edit_lot_shares_below_sold_rejected(client):
    ac_id = client.get("/api/dashboard").json()["asset_classes"][0]["id"]
    buy = client.post("/api/transactions", json={"code": "DDD", "asset_class_id": ac_id,
                                                 "date": "2025-06-01", "action": "buy",
                                                 "shares": 100, "price": 1.0}).json()
    client.post("/api/transactions", json={"matched_buy_id": buy["id"], "date": "2025-06-02",
                                           "action": "sell", "shares": 60, "price": 1.1})
    # can't shrink the lot below the 60 already sold out of it
    r = client.put(f"/api/transactions/{buy['id']}", json={"shares": 50})
    assert r.status_code == 400 and "已卖出" in r.json()["detail"]


def test_sell_realized_pnl_in_ledger(client):
    ac_id = client.get("/api/dashboard").json()["asset_classes"][0]["id"]
    buy = client.post("/api/transactions", json={"code": "EEE", "asset_class_id": ac_id,
                                                 "date": "2025-06-01", "action": "buy",
                                                 "shares": 100, "price": 2.0}).json()
    client.post("/api/transactions", json={"matched_buy_id": buy["id"], "date": "2025-06-05",
                                           "action": "sell", "shares": 40, "price": 3.0})
    entries = client.get("/api/ledger").json()["entries"]
    s = next(e for e in entries if e["kind"] == "sell" and e["code"] == "EEE")
    assert s["buy_price"] == 2.0
    assert s["realized_pnl"] == pytest.approx((3.0 - 2.0) * 40)  # +40
    assert s["matched_buy_date"] == "2025-06-01"
    # the buy and its sell share a pairing group number
    b = next(e for e in entries if e["kind"] == "buy" and e["id"] == buy["id"])
    assert s["group"] is not None and s["group"] == b["group"]
