"""Core calculation engine — pure functions, no DB, no framework.

Everything the dashboard shows is *derived* here from positions + prices +
targets. Kept framework-free so it is trivially unit-testable (spec §8).

Conventions:
- Weights and bands are percentages in [0, 100].
- A security with no price is "pending valuation": it is EXCLUDED from market
  value aggregation so it does not pollute weights (spec §7).
- All monetary/weight outputs are raw floats; rounding is a presentation concern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

# Status codes for an asset class relative to its tolerance band.
STATUS_UNDER = "under"  # current < band_low  → 需加仓
STATUS_OVER = "over"    # current > band_high → 需减仓
STATUS_OK = "ok"        # within band         → 合规
STATUS_NA = "na"        # cannot be evaluated (no total assets yet)


# --------------------------------------------------------------------------- #
# Inputs (plain data — the API layer maps ORM rows onto these).
# --------------------------------------------------------------------------- #
@dataclass
class SecurityInput:
    id: int
    code: str
    name: str
    market: str
    shares: float                # net shares (buys − sells), already summed
    price: Optional[float]       # latest price, or None if not yet entered
    avg_cost: Optional[float] = None  # average buy cost per share (None if no buys)


@dataclass
class AssetClassInput:
    id: int
    name: str
    target_weight: float
    band_low: float
    band_high: float
    color: str
    sort_order: int
    is_cash: bool = False
    securities: List[SecurityInput] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Outputs (derived views).
# --------------------------------------------------------------------------- #
@dataclass
class SecurityView:
    id: int
    code: str
    name: str
    market: str
    shares: float
    price: Optional[float]
    avg_cost: Optional[float]          # average buy cost per share
    cost_value: Optional[float]        # avg_cost × net shares (cost of holding)
    market_value: Optional[float]      # None when price missing → 待估值
    unrealized_pnl: Optional[float]    # market_value − cost_value
    pnl_pct: Optional[float]           # unrealized_pnl / cost_value × 100
    weight_in_class: Optional[float]   # % of its asset class market value
    weight_in_total: Optional[float]   # % of total assets
    pending: bool                      # True when awaiting valuation


@dataclass
class AssetClassView:
    id: int
    name: str
    target_weight: float
    band_low: float
    band_high: float
    color: str
    sort_order: int
    is_cash: bool
    market_value: float
    current_weight: Optional[float]    # % of total assets; None when total == 0
    deviation: Optional[float]         # current − target (percentage points)
    status: str
    rebalance_amount: Optional[float]  # +buy / −sell to hit target; None when n/a
    securities: List[SecurityView] = field(default_factory=list)


@dataclass
class RebalanceSuggestion:
    asset_class_id: int
    name: str
    color: str
    status: str           # "under" or "over"
    amount: float         # +buy / −sell, to reach the exact target
    edge_amount: float    # +buy / −sell, to just re-enter the tolerance band
    current_weight: float
    target_weight: float


@dataclass
class Dashboard:
    total_assets: float
    unallocated: float                 # 100 − Σ target weights
    is_balanced: bool                  # unallocated == 0 (allowing for float epsilon)
    deviating_count: int               # asset classes outside their band
    asset_classes: List[AssetClassView]
    rebalance: List[RebalanceSuggestion]
    pending_securities: List[SecurityView]  # securities awaiting a price


# --------------------------------------------------------------------------- #
# Primitive derivations.
# --------------------------------------------------------------------------- #
def net_shares(transactions) -> float:
    """Net position = Σ buy shares − Σ sell shares.

    `transactions` is any iterable of objects/tuples exposing `.action` and
    `.shares` (or indices [0]=action, [1]=shares).
    """
    total = 0.0
    for tx in transactions:
        action = getattr(tx, "action", None)
        shares = getattr(tx, "shares", None)
        if action is None:  # tuple-like fallback
            action, shares = tx[0], tx[1]
        total += shares if action == "buy" else -shares
    return total


def open_lots(transactions):
    """Open buy lots with their remaining shares (specific-lot matching).

    A sell with `matched_buy_id` reduces that specific buy lot. Returns a list of
    (buy_tx, remaining_shares) for every buy, including fully-closed ones (rem 0).
    """
    sold = {}  # buy id -> shares sold against it
    for tx in transactions:
        if getattr(tx, "action", None) == "sell":
            mid = getattr(tx, "matched_buy_id", None)
            if mid is not None:
                sold[mid] = sold.get(mid, 0.0) + tx.shares
    lots = []
    for tx in transactions:
        if getattr(tx, "action", None) == "buy":
            lots.append((tx, tx.shares - sold.get(tx.id, 0.0)))
    return lots


def average_cost(transactions) -> Optional[float]:
    """Average cost per share of the OPEN position = Σ(remaining_i × price_i) /
    Σ remaining_i over still-open buy lots (specific-lot). None when flat."""
    num = den = 0.0
    for buy, remaining in open_lots(transactions):
        if remaining > 0:
            num += remaining * buy.price
            den += remaining
    return (num / den) if den > 0 else None


def security_market_value(shares: float, price: Optional[float]) -> Optional[float]:
    """Market value, or None when the price is missing (pending valuation)."""
    if price is None:
        return None
    return shares * price


def derive_holding(shares: float, price: Optional[float], avg_cost: Optional[float]):
    """Single source of truth for a security's cost/value/P&L.

    Returns (cost_value, market_value, unrealized_pnl, pnl_pct); any element is
    None when its inputs are missing (no price → pending; no buys → no cost).
    """
    mv = security_market_value(shares, price)
    cost_value = avg_cost * shares if avg_cost is not None else None
    pnl = (mv - cost_value) if (mv is not None and cost_value is not None) else None
    pnl_pct = (pnl / cost_value * 100.0) if (pnl is not None and cost_value) else None
    return cost_value, mv, pnl, pnl_pct


def _classify(current: Optional[float], band_low: float, band_high: float) -> str:
    if current is None:
        return STATUS_NA
    if current < band_low:
        return STATUS_UNDER
    if current > band_high:
        return STATUS_OVER
    return STATUS_OK


def rebalance_amount(target_weight: float, current_weight: float, total_assets: float) -> float:
    """Amount to move to reach target: +buy / −sell (spec §3.2)."""
    return (target_weight - current_weight) / 100.0 * total_assets


# --------------------------------------------------------------------------- #
# Full dashboard computation.
# --------------------------------------------------------------------------- #
def compute_dashboard(classes: List[AssetClassInput], *, cash_balance: float = 0.0,
                      epsilon: float = 1e-6) -> Dashboard:
    """Roll positions up to asset classes and the total, then derive weights,
    deviations, statuses and rebalance amounts (spec §3.2, §4).

    The cash class (is_cash) holds no securities — its value is `cash_balance`.
    """
    # First pass: per-class value. Cash class = cash_balance; others = Σ security MV.
    class_values = {}
    for ac in classes:
        if ac.is_cash:
            class_values[ac.id] = cash_balance
            continue
        cv = 0.0
        for sec in ac.securities:
            mv = security_market_value(sec.shares, sec.price)
            if mv is not None:
                cv += mv
        class_values[ac.id] = cv

    # `total_assets` is the TRUE signed sum (holdings + cash), shown on the
    # dashboard and consistent with the records ledger's 总资产.
    total_assets = sum(class_values.values())
    # `weight_denom` is what weights/rebalance divide by: a negative cash class
    # (usually deposits not yet recorded) must NOT shrink it, or every other
    # class's weight would blow past 100%. Floor each contribution at 0.
    weight_denom = sum(max(0.0, v) for v in class_values.values())

    asset_class_views: List[AssetClassView] = []
    pending: List[SecurityView] = []
    rebalance: List[RebalanceSuggestion] = []
    deviating_count = 0

    for ac in classes:
        class_mv = class_values[ac.id]

        sec_views: List[SecurityView] = []
        for sec in ac.securities:
            cost_value, mv, pnl, pnl_pct = derive_holding(sec.shares, sec.price, sec.avg_cost)
            is_pending = mv is None
            w_in_class = (mv / class_mv * 100.0) if (mv is not None and class_mv > 0) else None
            w_in_total = (mv / weight_denom * 100.0) if (mv is not None and weight_denom > 0) else None
            sv = SecurityView(
                id=sec.id, code=sec.code, name=sec.name, market=sec.market,
                shares=sec.shares, price=sec.price, avg_cost=sec.avg_cost,
                cost_value=cost_value, market_value=mv, unrealized_pnl=pnl, pnl_pct=pnl_pct,
                weight_in_class=w_in_class, weight_in_total=w_in_total,
                pending=is_pending,
            )
            sec_views.append(sv)
            if is_pending:
                pending.append(sv)

        # Negative class value (negative cash) → 0% rather than a nonsensical
        # negative/over-100 weight.
        current_weight = (max(0.0, class_mv) / weight_denom * 100.0) if weight_denom > 0 else None
        status = _classify(current_weight, ac.band_low, ac.band_high)
        deviation = (current_weight - ac.target_weight) if current_weight is not None else None
        reb_amount = (
            rebalance_amount(ac.target_weight, current_weight, weight_denom)
            if current_weight is not None else None
        )

        # A class whose value is negative (negative cash) is shown at 0% but must
        # NOT generate a rebalance suggestion: the amount would ignore the deficit
        # magnitude and mislead. The negative-cash banner already prompts the fix.
        if status in (STATUS_UNDER, STATUS_OVER) and class_mv >= 0:
            deviating_count += 1
            # Amount to just re-enter the band edge (cheaper than going to target):
            # over → down to band_high; under → up to band_low.
            edge = ac.band_high if status == STATUS_OVER else ac.band_low
            edge_amount = (edge - current_weight) / 100.0 * weight_denom
            rebalance.append(RebalanceSuggestion(
                asset_class_id=ac.id, name=ac.name, color=ac.color, status=status,
                amount=reb_amount, edge_amount=edge_amount,
                current_weight=current_weight, target_weight=ac.target_weight,
            ))

        asset_class_views.append(AssetClassView(
            id=ac.id, name=ac.name, target_weight=ac.target_weight,
            band_low=ac.band_low, band_high=ac.band_high, color=ac.color,
            sort_order=ac.sort_order, is_cash=ac.is_cash, market_value=class_mv,
            current_weight=current_weight, deviation=deviation, status=status,
            rebalance_amount=reb_amount, securities=sec_views,
        ))

    unallocated = 100.0 - sum(ac.target_weight for ac in classes)
    is_balanced = abs(unallocated) < epsilon

    # Largest moves first so the most impactful suggestions surface on top.
    rebalance.sort(key=lambda r: abs(r.amount), reverse=True)

    return Dashboard(
        total_assets=total_assets,
        unallocated=unallocated,
        is_balanced=is_balanced,
        deviating_count=deviating_count,
        asset_classes=asset_class_views,
        rebalance=rebalance,
        pending_securities=pending,
    )
