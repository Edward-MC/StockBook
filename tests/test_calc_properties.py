"""Property-based 不变量测试(Hypothesis)for the pure calc engine.

calc.py 已正确;这些测试断言「对任意合法输入,不变量恒成立」,
覆盖 example-based 测试漏掉的边界(零总资产、负现金、空仓、单类等)。
变异检查见 plan:每条不变量都验证过「改坏 calc 能被抓到」。
"""
import datetime as dt
import math
from dataclasses import dataclass
from typing import Optional

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.calc import (
    AssetClassInput,
    SecurityInput,
    annualized_volatility,
    average_cost,
    compute_dashboard,
    max_drawdown,
    net_shares,
    open_lots,
    rebalance_amount,
    twr,
    xirr,
)


# --------------------------------------------------------------------------- #
# 共享策略:生成有限、合理量级的浮点(避免极值让浮点比较失去意义)。
# --------------------------------------------------------------------------- #
finite = dict(allow_nan=False, allow_infinity=False)
prices = st.floats(min_value=0.01, max_value=1e6, **finite)
# 股数:或为精确 0(空仓),或为不低于 1e-3 的正数——避免 5e-324 这类
# 次正规数,它与价格相乘会下溢到 0,使加权均价在数值上失去意义(非 calc 错)。
shares = st.one_of(st.just(0.0), st.floats(min_value=1e-3, max_value=1e6, **finite))
weights = st.floats(min_value=0.0, max_value=100.0, **finite)


@dataclass
class Tx:
    """最小交易对象,够 net_shares / open_lots / average_cost 用。"""
    id: int
    action: str           # "buy" | "sell"
    shares: float
    price: float = 0.0
    matched_buy_id: Optional[int] = None


@st.composite
def matched_txs(draw):
    """一组买入 + 全部已配对到某买入批次的卖出。"""
    n_buys = draw(st.integers(min_value=1, max_value=5))
    buys = [
        Tx(id=i, action="buy", shares=draw(shares), price=draw(prices))
        for i in range(1, n_buys + 1)
    ]
    n_sells = draw(st.integers(min_value=0, max_value=5))
    sells = []
    for k in range(n_sells):
        target = draw(st.sampled_from(buys))
        sells.append(Tx(id=1000 + k, action="sell",
                        shares=draw(st.floats(min_value=0.0, max_value=target.shares, **finite)),
                        matched_buy_id=target.id))
    return buys + sells


# --------------------------------------------------------------------------- #
# I4: net_shares == Σ 未平仓批次剩余(所有卖出都已配对时)
# --------------------------------------------------------------------------- #
@given(txs=matched_txs())
def test_net_shares_equals_sum_of_open_lot_remaining(txs):
    net = net_shares(txs)
    remaining = sum(rem for _buy, rem in open_lots(txs))
    assert math.isclose(net, remaining, abs_tol=1e-6)


# --------------------------------------------------------------------------- #
# I5: average_cost 落在 [最低买价, 最高买价](有未平仓批次时)
# --------------------------------------------------------------------------- #
@given(txs=matched_txs())
def test_average_cost_within_buy_price_range(txs):
    avg = average_cost(txs)
    open_prices = [buy.price for buy, rem in open_lots(txs) if rem > 0]
    assume(open_prices)
    assert avg is not None
    assert min(open_prices) - 1e-6 <= avg <= max(open_prices) + 1e-6


# --------------------------------------------------------------------------- #
# I6: rebalance_amount 符号 —— 欠配(current<target)为正、超配为负、相等为 0
# --------------------------------------------------------------------------- #
@given(
    target=weights,
    current=weights,
    total=st.floats(min_value=0.0, max_value=1e9, **finite),
)
def test_rebalance_amount_sign(target, current, total):
    amount = rebalance_amount(target, current, total)
    assume(total > 0)
    if target > current:
        assert amount > -1e-6
    elif target < current:
        assert amount < 1e-6
    else:
        assert math.isclose(amount, 0.0, abs_tol=1e-6)


# --------------------------------------------------------------------------- #
# I3: 对非负大类施加其 rebalance_amount 后,新占比落到 target_weight
# --------------------------------------------------------------------------- #
@st.composite
def nonneg_dashboards(draw):
    """1–5 个非负大类(证券价/股数皆 ≥0 → class_mv ≥0),无现金类。"""
    n = draw(st.integers(min_value=1, max_value=5))
    classes = []
    for i in range(n):
        secs = draw(st.lists(
            st.builds(SecurityInput,
                      id=st.integers(min_value=1, max_value=10_000),
                      code=st.just("X"), name=st.just("X"), market=st.just("CN"),
                      shares=shares, price=prices,
                      avg_cost=st.one_of(st.none(), prices)),
            max_size=4))
        classes.append(AssetClassInput(
            id=i + 1, name="C%d" % i, target_weight=draw(weights),
            band_low=draw(weights), band_high=draw(weights),
            color="#000000", sort_order=i, is_cash=False, securities=secs))
    return classes


@settings(max_examples=200, deadline=None)
@given(classes=nonneg_dashboards())
def test_rebalance_lands_on_target(classes):
    dash = compute_dashboard(classes)
    denom = sum(max(0.0, ac.market_value) for ac in dash.asset_classes)
    assume(denom > 0)
    for ac in dash.asset_classes:
        assert ac.rebalance_amount is not None
        new_value = ac.market_value + ac.rebalance_amount
        new_weight = new_value / denom * 100.0
        assert math.isclose(new_weight, ac.target_weight, abs_tol=1e-4)


# --------------------------------------------------------------------------- #
# 含现金类与任意现金余额(可负)的完整仪表盘
# --------------------------------------------------------------------------- #
@st.composite
def full_dashboards(draw):
    classes = draw(nonneg_dashboards())
    cash_balance = 0.0
    if draw(st.booleans()):
        classes.append(AssetClassInput(
            id=len(classes) + 1, name="Cash", target_weight=draw(weights),
            band_low=draw(weights), band_high=draw(weights),
            color="#000000", sort_order=len(classes), is_cash=True, securities=[]))
        cash_balance = draw(st.floats(min_value=-1e6, max_value=1e6, **finite))
    return classes, cash_balance


# I1: 各大类 current_weight 之和 ≈ 100%(weight_denom>0 时)。实际占比,与 I8 不同量。
@settings(max_examples=200, deadline=None)
@given(data=full_dashboards())
def test_current_weights_sum_to_100(data):
    classes, cash = data
    dash = compute_dashboard(classes, cash_balance=cash)
    denom = sum(max(0.0, ac.market_value) for ac in dash.asset_classes)
    assume(denom > 0)
    total = sum(ac.current_weight for ac in dash.asset_classes
                if ac.current_weight is not None)
    assert math.isclose(total, 100.0, abs_tol=1e-4)


# I2: 每个 current_weight 恒在 [0, 100](含负现金 floor 到 0)。
@settings(max_examples=200, deadline=None)
@given(data=full_dashboards())
def test_current_weight_bounded_0_100(data):
    classes, cash = data
    dash = compute_dashboard(classes, cash_balance=cash)
    for ac in dash.asset_classes:
        if ac.current_weight is not None:
            assert -1e-6 <= ac.current_weight <= 100.0 + 1e-6


# I7: 对任意合法输入不抛异常,且输出无 NaN/inf。
@settings(max_examples=300, deadline=None)
@given(data=full_dashboards())
def test_compute_dashboard_robust(data):
    classes, cash = data
    dash = compute_dashboard(classes, cash_balance=cash)
    for ac in dash.asset_classes:
        for v in (ac.market_value, ac.current_weight, ac.deviation,
                  ac.rebalance_amount):
            assert v is None or math.isfinite(v)
        for sv in ac.securities:
            for v in (sv.market_value, sv.cost_value, sv.unrealized_pnl,
                      sv.pnl_pct, sv.weight_in_class, sv.weight_in_total):
                assert v is None or math.isfinite(v)


# --------------------------------------------------------------------------- #
# I8: 目标占比规则 —— unallocated == 100 − Σtarget;is_balanced ⟺ |unallocated|<eps。
#     管「目标占比」(拨杆,带未分配池),与 I1 是不同的量。
# --------------------------------------------------------------------------- #
@given(targets=st.lists(weights, min_size=1, max_size=6))
def test_unallocated_and_is_balanced(targets):
    classes = [
        AssetClassInput(id=i + 1, name="C%d" % i, target_weight=t,
                        band_low=0.0, band_high=100.0, color="#000000",
                        sort_order=i, is_cash=False, securities=[])
        for i, t in enumerate(targets)
    ]
    eps = 1e-6
    dash = compute_dashboard(classes, epsilon=eps)
    assert math.isclose(dash.unallocated, 100.0 - sum(targets), abs_tol=1e-9)
    assert dash.is_balanced == (abs(dash.unallocated) < eps)


@given(
    head=st.lists(st.floats(min_value=0.0, max_value=100.0, **finite),
                  min_size=0, max_size=4),
)
def test_targets_summing_to_100_are_balanced(head):
    assume(sum(head) <= 100.0)
    targets = head + [100.0 - sum(head)]
    classes = [
        AssetClassInput(id=i + 1, name="C%d" % i, target_weight=t,
                        band_low=0.0, band_high=100.0, color="#000000",
                        sort_order=i, is_cash=False, securities=[])
        for i, t in enumerate(targets)
    ]
    dash = compute_dashboard(classes, epsilon=1e-6)
    assert dash.is_balanced


# =========================================================================== #
# 绩效函数不变量(history+performance spec §5)。每条经变异检查(见 Step 3)。
# =========================================================================== #
navs = st.lists(st.floats(min_value=0.01, max_value=1e7, allow_nan=False,
                          allow_infinity=False), min_size=0, max_size=50)


# I-DD1: 回撤恒在 [0,1](变异:把 max(...) 写成绝对差不除 peak → 抓到 >1)。
@given(navs)
def test_inv_drawdown_in_unit_interval(nav):
    dd = max_drawdown(nav)
    assert 0.0 <= dd <= 1.0


# I-DD2: 单调非降 → 回撤=0(变异:peak 不更新 → 抓到 >0)。
@given(navs)
def test_inv_monotonic_up_zero_drawdown(nav):
    mono = sorted(nav)
    assert max_drawdown(mono) == 0.0


# I-DD3: 空/单点 → 回撤=0。
def test_inv_drawdown_empty_single():
    assert max_drawdown([]) == 0.0
    assert max_drawdown([3.14]) == 0.0


# I-VOL1: 常数序列 → 波动=0(变异:stdev 用了均值偏移以外的东西 → 抓到 !=0)。
@given(st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
       st.integers(min_value=2, max_value=30))
def test_inv_constant_zero_volatility(v, n):
    assert annualized_volatility([v] * n) == 0.0


# I-VOL2: 等比数列(等收益率)→ 波动≈0(变异:用总收益代替逐期收益 → 抓到 >0)。
@given(st.floats(min_value=0.01, max_value=10.0, allow_nan=False, allow_infinity=False),
       st.integers(min_value=2, max_value=20))
def test_inv_constant_ratio_zero_volatility(ratio, n):
    nav = [100.0]
    for _ in range(n):
        nav.append(nav[-1] * (1.0 + ratio))
    v = annualized_volatility(nav)
    assert v is not None and v < 1e-6


# I-VOL3: <2 点 → None;任意合法序列不崩、非负(变异:漏 None 守卫 → 抓到崩)。
@given(navs)
def test_inv_volatility_never_negative_or_nan(nav):
    v = annualized_volatility(nav)
    assert v is None or (v >= 0 and not math.isnan(v))


# I-XIRR1: 翻倍且无中途流入 → ≈ 期间复合年化(变异:days/365 漏掉 → 抓到偏差)。
# multiple 上限取 3.0:xirr 搜索区间上界 hi=10(年化);worst case days=180 →
# 3^(365/180)≈8.1<10,确保 expected 在搜索域内不返回 None。
@given(st.floats(min_value=1.1, max_value=3.0, allow_nan=False, allow_infinity=False),
       st.integers(min_value=180, max_value=1500))
def test_inv_xirr_matches_compound(multiple, days):
    d0 = dt.date(2025, 1, 1)
    r = xirr([(d0, -100.0), (d0 + dt.timedelta(days=days), 100.0 * multiple)])
    expected = multiple ** (365.0 / days) - 1.0
    assert r is not None and abs(r - expected) < 1e-3


# I-XIRR2: 任意流序列不崩、要么 None 要么有限(变异:无 None 守卫 → 抓到崩/NaN)。
flow_lists = st.lists(
    st.tuples(st.integers(min_value=0, max_value=2000),
              st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False)),
    min_size=0, max_size=20)


@given(flow_lists)
def test_inv_xirr_no_crash(raw):
    d0 = dt.date(2025, 1, 1)
    flows = [(d0 + dt.timedelta(days=k), a) for k, a in raw]
    r = xirr(flows)
    assert r is None or (isinstance(r, float) and not math.isnan(r))


# I-TWR1: 无外部流时 TWR 与"末/首−1"同号(变异:分段用 v_begin/v_end 代替
# v_end/v_begin,或用差代替比 → 抓到异号)。flows 恒空,故不验 net_flow 符号。
# twr 现已 overflow-safe(极端输入返回 None),故无需在测试里重算 growth 或 guard 溢出。
@given(st.lists(st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
                min_size=2, max_size=30))
def test_inv_twr_sign_matches_simple(vals):
    assume(vals[0] > 0)
    d0 = dt.date(2025, 1, 1)
    series = [(d0 + dt.timedelta(days=i), v) for i, v in enumerate(vals)]
    r = twr(series, [])
    simple = vals[-1] / vals[0] - 1.0
    if r is not None and abs(simple) > 1e-6:
        assert (r > 0) == (simple > 0)
