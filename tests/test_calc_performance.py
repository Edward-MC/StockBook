"""Unit tests for the pure performance functions (history+performance spec §5)."""
import datetime as dt
import math

import pytest

from app.calc import xirr, twr, max_drawdown, annualized_volatility

D0 = dt.date(2025, 1, 1)


def d(days):
    return D0 + dt.timedelta(days=days)


# ----------------------------- xirr ------------------------------------- #
def test_xirr_double_in_one_year_is_100pct():
    # Put in 100 at t0, portfolio worth 200 a year later → IRR ≈ 100%.
    r = xirr([(d(0), -100.0), (d(365), 200.0)])
    assert r is not None
    assert abs(r - 1.0) < 1e-3


def test_xirr_no_sign_change_returns_none():
    # All outflows, never any return → no IRR.
    assert xirr([(d(0), -100.0), (d(365), -50.0)]) is None


def test_xirr_too_few_or_zero_flows_returns_none():
    assert xirr([(d(0), -100.0)]) is None
    assert xirr([]) is None
    assert xirr([(d(0), 0.0), (d(365), 0.0)]) is None


# ----------------------------- twr -------------------------------------- #
def test_twr_no_flows_matches_simple_return_sign():
    # 100 → 110 → 121, no external flows: two +10% segments → annualized 0.21.
    r = twr([(d(0), 100.0), (d(182), 110.0), (d(365), 121.0)], [])
    assert r is not None and abs(r - 0.21) < 1e-6


def test_twr_strips_external_flow_from_segment():
    # Value jumps 100 → 200 but 100 of that was a deposit → segment return 0.
    r = twr([(d(0), 100.0), (d(365), 200.0)], [(d(100), 100.0)])
    assert r is not None and abs(r) < 1e-9


def test_twr_too_few_points_returns_none():
    assert twr([(d(0), 100.0)], []) is None
    assert twr([], []) is None


# ------------------------- max_drawdown --------------------------------- #
def test_max_drawdown_basic():
    # peak 100 → trough 60 → recover: max DD = 40%.
    assert abs(max_drawdown([100, 120, 60, 90]) - (120 - 60) / 120) < 1e-9


def test_max_drawdown_monotonic_up_is_zero():
    assert max_drawdown([1, 2, 3, 4]) == 0.0


def test_max_drawdown_empty_or_single_is_zero():
    assert max_drawdown([]) == 0.0
    assert max_drawdown([42.0]) == 0.0


# --------------------- annualized_volatility ---------------------------- #
def test_volatility_constant_series_is_zero():
    assert annualized_volatility([100, 100, 100, 100]) == 0.0


def test_volatility_constant_ratio_is_zero():
    # Equal periodic returns → zero dispersion → zero volatility.
    assert abs(annualized_volatility([100, 110, 121, 133.1])) < 1e-9


def test_volatility_too_few_points_returns_none():
    assert annualized_volatility([100.0]) is None
    assert annualized_volatility([]) is None


def test_volatility_two_point_nav_returns_float():
    # Exactly 2 NAV points = 1 return → |r|·√252 fallback (documented).
    v = annualized_volatility([100.0, 110.0])
    assert v is not None and abs(v - 0.1 * math.sqrt(252.0)) < 1e-9


def test_volatility_positive_for_varying_returns():
    v = annualized_volatility([100, 110, 100, 115, 95])
    assert v is not None and v > 0
