"""Glue between the ORM and the pure calc engine.

Maps SQLAlchemy rows onto calc inputs, runs the engine, and shapes the
result into the dashboard response payload.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import calc, quotes
from .models import AssetClass, CashFlow, PriceQuote, Security, Strategy, Transaction


def security_payload(*, id, code, name, market, shares, price, market_value,
                     avg_cost=None, cost_value=None, unrealized_pnl=None, pnl_pct=None,
                     weight_in_class=None, weight_in_total=None, pending=None) -> dict:
    """Canonical JSON shape for a security, shared by the dashboard and CRUD
    responses so the field set never drifts between them."""
    return {
        "id": id, "code": code, "name": name, "market": market,
        "shares": shares, "price": price, "avg_cost": avg_cost, "cost_value": cost_value,
        "market_value": market_value, "unrealized_pnl": unrealized_pnl, "pnl_pct": pnl_pct,
        "weight_in_class": weight_in_class, "weight_in_total": weight_in_total,
        "pending": price is None if pending is None else pending,
    }


def get_primary_strategy(db: Session) -> Strategy:
    """v1 has a single strategy; return the first (lowest id)."""
    return db.scalars(select(Strategy).order_by(Strategy.id)).first()


def cash_components(db: Session) -> dict:
    """Cash accounting derived from all transactions + cash flows.

    cash_balance = deposits − withdrawals + sell proceeds − buy costs.
    """
    buy_amt = sell_amt = 0.0
    for tx in db.scalars(select(Transaction)).all():
        amt = tx.shares * tx.price
        if tx.action == "buy":
            buy_amt += amt
        else:
            sell_amt += amt
    deposits = withdrawals = 0.0
    for cf in db.scalars(select(CashFlow)).all():
        if cf.direction == "in":
            deposits += cf.amount
        else:
            withdrawals += cf.amount
    return {
        "deposits": deposits, "withdrawals": withdrawals,
        "buy_amount": buy_amt, "sell_amount": sell_amt,
        "cash_balance": deposits - withdrawals + sell_amt - buy_amt,
    }


def apply_fetched_quotes(db: Session, fetched: dict) -> int:
    """Write fetched {code: {"price","name"}} into PriceQuote rows as source
    'auto', backfilling placeholder names (name == code). Returns the number of
    securities updated. Does NOT commit — the caller commits. Shared by the
    /prices/refresh endpoint and the daily snapshot capture (DRY)."""
    securities = db.scalars(select(Security)).all()
    now = datetime.now()
    updated = 0
    for sec in securities:
        q = fetched.get(sec.code)
        if not q:
            continue
        if sec.quote is None:
            sec.quote = PriceQuote(security_id=sec.id)
        sec.quote.price = q["price"]
        sec.quote.source = "auto"
        sec.quote.updated_at = now
        if q.get("name") and sec.name == sec.code:
            sec.name = q["name"]
        updated += 1
    return updated


def _to_calc_inputs(strategy: Strategy):
    """Build calc inputs and, in the same pass, the latest valuation timestamp."""
    inputs = []
    latest_valuation = None
    for ac in strategy.asset_classes:  # ordered by sort_order via relationship
        secs = []
        for sec in ac.securities:
            shares = calc.net_shares(sec.transactions)
            price = sec.quote.price if sec.quote else None
            if sec.quote and sec.quote.updated_at:
                if latest_valuation is None or sec.quote.updated_at > latest_valuation:
                    latest_valuation = sec.quote.updated_at
            secs.append(calc.SecurityInput(
                id=sec.id, code=sec.code, name=sec.name, market=sec.market,
                shares=shares, price=price, avg_cost=calc.average_cost(sec.transactions),
            ))
        inputs.append(calc.AssetClassInput(
            id=ac.id, name=ac.name, target_weight=ac.target_weight,
            band_low=ac.band_low, band_high=ac.band_high, color=ac.color,
            sort_order=ac.sort_order, is_cash=ac.is_cash, securities=secs,
        ))
    return inputs, latest_valuation


def _sec_dict(s: calc.SecurityView) -> dict:
    return security_payload(
        id=s.id, code=s.code, name=s.name, market=s.market, shares=s.shares,
        price=s.price, avg_cost=s.avg_cost, cost_value=s.cost_value,
        market_value=s.market_value, unrealized_pnl=s.unrealized_pnl, pnl_pct=s.pnl_pct,
        weight_in_class=s.weight_in_class, weight_in_total=s.weight_in_total,
        pending=s.pending,
    )


def build_dashboard(db: Session, *, readonly: bool, hide_amounts: bool) -> Optional[dict]:
    """Return the full dashboard payload dict, or None if no strategy exists."""
    strategy = get_primary_strategy(db)
    if strategy is None:
        return None

    inputs, valuation_date = _to_calc_inputs(strategy)
    cash_balance = cash_components(db)["cash_balance"]
    dash = calc.compute_dashboard(inputs, cash_balance=cash_balance)

    return {
        "strategy_id": strategy.id,
        "strategy_name": strategy.name,
        "total_assets": dash.total_assets,
        "cash_balance": cash_balance,
        "unallocated": dash.unallocated,
        "is_balanced": dash.is_balanced,
        "deviating_count": dash.deviating_count,
        "valuation_date": valuation_date,
        "price_state": (None if valuation_date is None
                        else ("live" if quotes.is_trading_session(valuation_date) else "close")),
        "last_rebalanced_at": strategy.last_rebalanced_at,
        "readonly": readonly,
        "hide_amounts": hide_amounts,
        "asset_classes": [
            {
                "id": ac.id, "name": ac.name, "target_weight": ac.target_weight,
                "band_low": ac.band_low, "band_high": ac.band_high, "color": ac.color,
                "sort_order": ac.sort_order, "is_cash": ac.is_cash, "market_value": ac.market_value,
                "current_weight": ac.current_weight, "deviation": ac.deviation,
                "status": ac.status, "rebalance_amount": ac.rebalance_amount,
                "securities": [_sec_dict(s) for s in ac.securities],
            }
            for ac in dash.asset_classes
        ],
        "rebalance": [
            {
                "asset_class_id": r.asset_class_id, "name": r.name, "color": r.color,
                "status": r.status, "amount": r.amount, "edge_amount": r.edge_amount,
                "current_weight": r.current_weight, "target_weight": r.target_weight,
            }
            for r in dash.rebalance
        ],
        "pending_securities": [_sec_dict(s) for s in dash.pending_securities],
    }


def build_ledger(db: Session) -> dict:
    """Unified records ledger: buys / sells / deposits / withdrawals, newest
    first, plus a cash / asset / P&L summary. Realized P&L is per matched buy lot
    (specific-lot)."""
    strategy = get_primary_strategy(db)
    entries = []
    total_realized = 0.0
    holdings_value = 0.0
    if strategy is not None:
        for ac in strategy.asset_classes:
            for sec in ac.securities:
                price = sec.quote.price if sec.quote else None
                if price is not None:
                    holdings_value += calc.net_shares(sec.transactions) * price
                by_id = {tx.id: tx for tx in sec.transactions}
                for tx in sec.transactions:
                    amount = tx.shares * tx.price
                    row = {
                        "kind": tx.action,  # "buy" | "sell"
                        "id": tx.id, "date": tx.date.isoformat(),
                        "security_id": sec.id, "code": sec.code, "name": sec.name,
                        "shares": tx.shares, "price": tx.price, "amount": amount,
                        "buy_price": None, "realized_pnl": None,
                        "matched_buy_id": tx.matched_buy_id if tx.action == "sell" else None,
                        "matched_buy_date": None, "group": None,
                    }
                    if tx.action == "sell":
                        # Realized P&L from the specifically matched buy lot.
                        lot = by_id.get(tx.matched_buy_id)
                        if lot is not None:
                            row["buy_price"] = lot.price
                            row["matched_buy_date"] = lot.date.isoformat()
                            row["realized_pnl"] = (tx.price - lot.price) * tx.shares
                            total_realized += row["realized_pnl"]
                    entries.append(row)

    for cf in db.scalars(select(CashFlow)).all():
        entries.append({
            "kind": "deposit" if cf.direction == "in" else "withdraw",
            "id": cf.id, "date": cf.date.isoformat(), "amount": cf.amount,
            "note": cf.note, "security_id": None, "code": None, "name": None,
        })

    # Group each buy lot with the sells that closed it, so the records UI can
    # show how a sell corresponds to its buy. Only paired lots get a group no.
    matched_ids = {e["matched_buy_id"] for e in entries if e["kind"] == "sell" and e["matched_buy_id"]}
    paired_buys = sorted((e for e in entries if e["kind"] == "buy" and e["id"] in matched_ids),
                         key=lambda e: (e["date"], e["id"]))
    gmap = {b["id"]: i + 1 for i, b in enumerate(paired_buys)}
    for e in entries:
        if e["kind"] == "buy":
            e["group"] = gmap.get(e["id"])
        elif e["kind"] == "sell":
            e["group"] = gmap.get(e["matched_buy_id"])

    entries.sort(key=lambda e: (e["date"], e.get("id", 0)), reverse=True)
    cash = cash_components(db)
    net_invested = cash["deposits"] - cash["withdrawals"]
    total_assets = holdings_value + cash["cash_balance"]
    summary = {
        **cash,
        "holdings_value": holdings_value,          # 持仓市值(今日行情)
        "total_assets": total_assets,              # 总资产 = 持仓市值 + 现金余额
        "net_invested": net_invested,              # 净投入 = 注入 − 移出(本金)
        "total_return": total_assets - net_invested,  # 总收益(浮动+已实现)
        "realized_pnl": total_realized,            # 已实现盈亏(批次配对)
    }
    return {"entries": entries, "summary": summary}
