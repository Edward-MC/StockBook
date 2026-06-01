"""Unit tests for the pure calculation engine (spec §8)."""
import pytest

from app.calc import (
    STATUS_NA,
    STATUS_OK,
    STATUS_OVER,
    STATUS_UNDER,
    AssetClassInput,
    SecurityInput,
    average_cost,
    compute_dashboard,
    net_shares,
    rebalance_amount,
    security_market_value,
)


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #
def test_net_shares_buys_minus_sells():
    class Tx:
        def __init__(self, action, shares):
            self.action = action
            self.shares = shares

    txs = [Tx("buy", 100), Tx("buy", 50), Tx("sell", 30)]
    assert net_shares(txs) == 120


def test_net_shares_accepts_tuples():
    assert net_shares([("buy", 10), ("sell", 4)]) == 6


def test_security_market_value():
    assert security_market_value(100, 4.0) == 400.0


def test_security_market_value_missing_price_is_none():
    assert security_market_value(100, None) is None


def test_rebalance_amount_sign():
    # target above current → positive (buy)
    assert rebalance_amount(40, 30, 1000) == pytest.approx(100)
    # target below current → negative (sell)
    assert rebalance_amount(20, 30, 1000) == pytest.approx(-100)


class _Tx:
    def __init__(self, id, action, shares, price, matched=None):
        self.id, self.action, self.shares, self.price = id, action, shares, price
        self.matched_buy_id = matched


def test_average_cost_open_lots_specific_match():
    # buy 100@4 (id1), buy 50@6 (id2), sell 30 matched to lot1
    txs = [_Tx(1, "buy", 100, 4.0), _Tx(2, "buy", 50, 6.0), _Tx(3, "sell", 30, 5.0, matched=1)]
    # remaining: lot1 70@4, lot2 50@6 → (70*4 + 50*6) / 120
    assert average_cost(txs) == pytest.approx(580 / 120)


def test_average_cost_none_when_fully_sold():
    txs = [_Tx(1, "buy", 100, 4.0), _Tx(2, "sell", 100, 5.0, matched=1)]
    assert average_cost(txs) is None


def test_average_cost_none_without_buys():
    assert average_cost([("sell", 10, 5.0)]) is None
    assert average_cost([]) is None


def test_pnl_derivation():
    # 100 shares @ avg cost 4, current price 5 → cost 400, mv 500, pnl +100 (+25%)
    classes = [_ac(1, 100, 0, 100, [_sec(1, 100, 5.0, avg_cost=4.0)])]
    s = compute_dashboard(classes).asset_classes[0].securities[0]
    assert s.cost_value == pytest.approx(400)
    assert s.market_value == pytest.approx(500)
    assert s.unrealized_pnl == pytest.approx(100)
    assert s.pnl_pct == pytest.approx(25.0)


def test_rebalance_edge_vs_target_amount():
    # A: 25% actual vs target 40 (band 35-45) → under. total 1000.
    classes = [
        _ac(1, 40, 35, 45, [_sec(1, 250, 1.0)]),   # 250 → 25%
        _ac(2, 60, 55, 65, [_sec(2, 750, 1.0)]),   # 750 → 75%
    ]
    by_id = {r.asset_class_id: r for r in compute_dashboard(classes).rebalance}
    # to target: +150 ; to band edge (35%): +100
    assert by_id[1].amount == pytest.approx(150)
    assert by_id[1].edge_amount == pytest.approx(100)
    # over class to target: -150 ; to edge (65%): -100
    assert by_id[2].amount == pytest.approx(-150)
    assert by_id[2].edge_amount == pytest.approx(-100)


# --------------------------------------------------------------------------- #
# Dashboard aggregation
# --------------------------------------------------------------------------- #
def _sec(id, shares, price, avg_cost=None):
    return SecurityInput(id=id, code=f"S{id}", name=f"sec{id}", market="CN",
                         shares=shares, price=price, avg_cost=avg_cost)


def _ac(id, target, low, high, secs, name=None):
    return AssetClassInput(id=id, name=name or f"ac{id}", target_weight=target,
                           band_low=low, band_high=high, color="--c1",
                           sort_order=id, securities=secs)


def test_total_and_weights_roll_up():
    # Class A: 100 sh * 4 = 400 ; Class B: 100 sh * 6 = 600 ; total = 1000
    classes = [
        _ac(1, 40, 35, 45, [_sec(1, 100, 4.0)]),
        _ac(2, 60, 55, 65, [_sec(2, 100, 6.0)]),
    ]
    dash = compute_dashboard(classes)
    assert dash.total_assets == 1000
    a, b = dash.asset_classes
    assert a.market_value == 400
    assert a.current_weight == pytest.approx(40.0)
    assert b.current_weight == pytest.approx(60.0)
    assert a.status == STATUS_OK and b.status == STATUS_OK
    assert dash.deviating_count == 0


def test_weight_in_class_and_total():
    classes = [_ac(1, 100, 0, 100, [_sec(1, 100, 4.0), _sec(2, 100, 6.0)])]
    dash = compute_dashboard(classes)
    s1, s2 = dash.asset_classes[0].securities
    # class value 1000; s1=400, s2=600
    assert s1.weight_in_class == pytest.approx(40.0)
    assert s2.weight_in_class == pytest.approx(60.0)
    assert s1.weight_in_total == pytest.approx(40.0)


def test_deviation_and_status_under_over():
    # A is 25% actual vs 40% target with band 35–45 → under
    # B is 75% actual vs 60% target with band 55–65 → over
    classes = [
        _ac(1, 40, 35, 45, [_sec(1, 250, 1.0)]),   # 250
        _ac(2, 60, 55, 65, [_sec(2, 750, 1.0)]),   # 750
    ]
    dash = compute_dashboard(classes)
    a, b = dash.asset_classes
    assert a.current_weight == pytest.approx(25.0)
    assert a.deviation == pytest.approx(-15.0)
    assert a.status == STATUS_UNDER
    assert b.status == STATUS_OVER
    assert dash.deviating_count == 2
    # rebalance: A needs +15% of 1000 = +150 ; B needs -150
    by_id = {r.asset_class_id: r for r in dash.rebalance}
    assert by_id[1].amount == pytest.approx(150)
    assert by_id[2].amount == pytest.approx(-150)


def test_missing_price_excluded_from_aggregation():
    # s2 has no price → pending, excluded from totals
    classes = [_ac(1, 100, 0, 100, [_sec(1, 100, 4.0), _sec(2, 100, None)])]
    dash = compute_dashboard(classes)
    assert dash.total_assets == 400  # only s1 counts
    assert len(dash.pending_securities) == 1
    pend = dash.pending_securities[0]
    assert pend.id == 2 and pend.pending is True
    assert pend.market_value is None and pend.weight_in_class is None


def test_zero_total_assets_status_na():
    classes = [_ac(1, 50, 40, 60, [_sec(1, 0, None)])]
    dash = compute_dashboard(classes)
    assert dash.total_assets == 0
    a = dash.asset_classes[0]
    assert a.current_weight is None
    assert a.status == STATUS_NA
    assert a.rebalance_amount is None
    assert dash.rebalance == []


# --------------------------------------------------------------------------- #
# Unallocated pool (spec §4)
# --------------------------------------------------------------------------- #
def test_negative_cash_does_not_inflate_weights():
    cash = AssetClassInput(id=2, name="现金", target_weight=40, band_low=0, band_high=100,
                           color="--c5", sort_order=2, is_cash=True, securities=[])
    classes = [_ac(1, 60, 0, 100, [_sec(1, 100, 4.0)]), cash]  # holdings 400, cash −1000
    dash = compute_dashboard(classes, cash_balance=-1000)
    assert dash.total_assets == pytest.approx(400)          # cash floored at 0 in denom
    assert dash.asset_classes[0].current_weight == pytest.approx(100.0)  # not >100
    assert dash.asset_classes[1].current_weight == pytest.approx(0.0)    # negative cash → 0%
    assert dash.asset_classes[1].market_value == -1000      # true balance still shown


def test_unallocated_pool_balanced():
    classes = [
        _ac(1, 60, 0, 100, []),
        _ac(2, 40, 0, 100, []),
    ]
    dash = compute_dashboard(classes)
    assert dash.unallocated == pytest.approx(0.0)
    assert dash.is_balanced is True


def test_unallocated_pool_underfilled():
    classes = [_ac(1, 60, 0, 100, []), _ac(2, 30, 0, 100, [])]
    dash = compute_dashboard(classes)
    assert dash.unallocated == pytest.approx(10.0)
    assert dash.is_balanced is False
