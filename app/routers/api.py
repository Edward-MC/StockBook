"""JSON API (spec §6). All write endpoints are blocked when the instance is
globally read-only (config.READONLY)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

import httpx

from .. import backup, calc, config, quotes, schemas, snapshot_service
from ..database import get_db
from ..models import AssetClass, CashFlow, PriceQuote, Security, Transaction
from ..seed import reset_to_default
from ..services import apply_fetched_quotes, build_dashboard, build_ledger, get_primary_strategy, security_payload

router = APIRouter(prefix="/api", tags=["api"])

TARGET_EPSILON = 1e-6


def require_writable():
    """Dependency: reject writes when the instance is globally read-only."""
    if config.READONLY:
        raise HTTPException(status_code=403, detail="实例为只读模式,禁止修改")


def _get_asset_class(db: Session, ac_id: int) -> AssetClass:
    ac = db.get(AssetClass, ac_id)
    if ac is None:
        raise HTTPException(status_code=404, detail="大类不存在")
    return ac


def _get_security(db: Session, sec_id: int) -> Security:
    sec = db.get(Security, sec_id)
    if sec is None:
        raise HTTPException(status_code=404, detail="标的不存在")
    return sec


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
@router.get("/dashboard", response_model=schemas.DashboardOut)
def get_dashboard(
    readonly: bool = Query(False),
    hideAmounts: bool = Query(False),
    db: Session = Depends(get_db),
):
    payload = build_dashboard(
        db,
        readonly=readonly or config.READONLY,
        hide_amounts=hideAmounts or config.HIDE_AMOUNTS,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="尚无策略,请先初始化数据")
    return payload


# --------------------------------------------------------------------------- #
# Asset class CRUD
# --------------------------------------------------------------------------- #
@router.post("/asset-classes", response_model=schemas.AssetClassOut, dependencies=[Depends(require_writable)])
def create_asset_class(payload: schemas.AssetClassCreate, db: Session = Depends(get_db)):
    strategy = get_primary_strategy(db)
    if strategy is None:
        raise HTTPException(status_code=404, detail="尚无策略")
    data = payload.model_dump()
    color = data.pop("color", None)
    ac = AssetClass(strategy_id=strategy.id, color=color or _auto_color(strategy), **data)
    db.add(ac)
    if ac.is_cash:
        _make_sole_cash_class(db, strategy, ac)
    db.commit()
    db.refresh(ac)
    return _asset_class_out(ac)


@router.put("/asset-classes/{ac_id}", response_model=schemas.AssetClassOut, dependencies=[Depends(require_writable)])
def update_asset_class(ac_id: int, payload: schemas.AssetClassUpdate, db: Session = Depends(get_db)):
    ac = _get_asset_class(db, ac_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(ac, field, value)
    if ac.is_cash:
        _make_sole_cash_class(db, ac.strategy, ac)
    db.commit()
    db.refresh(ac)
    return _asset_class_out(ac)


# Hex of the original --c1..--c5 CSS vars, so distance math works for legacy
# classes that still reference them.
_LEGACY_HEX = {"--c1": "#A8431F", "--c2": "#CC8636", "--c3": "#B9A23E",
               "--c4": "#6E8E55", "--c5": "#5A6E86"}

# Curated palette: every pair is comfortably distinguishable (hue AND lightness
# vary, so we never sit two purples next to each other). Auto-assignment picks
# the one most different from the classes that already exist.
_PALETTE = [
    "#A8431F",  # terracotta
    "#5A6E86",  # slate blue
    "#B9A23E",  # olive gold
    "#6E8E55",  # olive green
    "#8E5BA6",  # purple
    "#CC8636",  # ochre
    "#3E8C8C",  # teal
    "#B0506A",  # wine rose
    "#4661A0",  # royal blue
    "#5E9E5A",  # green
    "#9C6B3C",  # brown
    "#C77FA8",  # light pink
    "#3FA0B8",  # cyan
    "#7B5EA8",  # indigo
    "#D0A85C",  # light gold
    "#7A8C3A",  # khaki green
]


def _rgb(color: str):
    hexv = _LEGACY_HEX.get(color, color or "")
    if not (hexv.startswith("#") and len(hexv) == 7):
        return None
    try:
        return tuple(int(hexv[i:i + 2], 16) for i in (1, 3, 5))
    except ValueError:
        return None


def _farthest_color(existing_colors) -> str:
    """Palette color with the greatest minimum RGB distance to the existing set."""
    existing = [c for c in (_rgb(c) for c in existing_colors) if c is not None]
    best, best_dist = _PALETTE[0], -1.0
    for hexv in _PALETTE:
        r, g, b = _rgb(hexv)
        dist = min((((r - er) ** 2 + (g - eg) ** 2 + (b - eb) ** 2) ** 0.5
                    for er, eg, eb in existing), default=1e9)
        if dist > best_dist:
            best, best_dist = hexv, dist
    return best


def _auto_color(strategy) -> str:
    return _farthest_color([ac.color for ac in strategy.asset_classes])


def _make_sole_cash_class(db: Session, strategy, ac: AssetClass) -> None:
    """At most one cash class per strategy — clear the flag on every other."""
    db.flush()  # ensure ac has an id before comparing
    for other in strategy.asset_classes:
        if other.id != ac.id and other.is_cash:
            other.is_cash = False


@router.post("/asset-classes/recolor", dependencies=[Depends(require_writable)])
def recolor_asset_classes(db: Session = Depends(get_db)):
    """Re-assign every class a maximally-distinct palette color (greedy)."""
    strategy = get_primary_strategy(db)
    if strategy is None:
        raise HTTPException(status_code=404, detail="尚无策略")
    assigned = []
    for ac in sorted(strategy.asset_classes, key=lambda a: (a.sort_order, a.id)):
        ac.color = _farthest_color(assigned)
        assigned.append(ac.color)
    db.commit()
    return {"ok": True}


@router.delete("/asset-classes/{ac_id}", dependencies=[Depends(require_writable)])
def delete_asset_class(ac_id: int, db: Session = Depends(get_db)):
    ac = _get_asset_class(db, ac_id)
    db.delete(ac)
    db.commit()
    return {"ok": True}


def _asset_class_out(ac: AssetClass) -> dict:
    """Minimal asset-class shape (no derived values) for CRUD responses."""
    return {
        "id": ac.id, "name": ac.name, "target_weight": ac.target_weight,
        "band_low": ac.band_low, "band_high": ac.band_high, "color": ac.color,
        "sort_order": ac.sort_order, "is_cash": ac.is_cash, "market_value": 0.0,
        "current_weight": None, "deviation": None, "status": calc.STATUS_NA,
        "rebalance_amount": None, "securities": [],
    }


# --------------------------------------------------------------------------- #
# Security CRUD
# --------------------------------------------------------------------------- #
@router.post("/securities", response_model=schemas.SecurityOut, dependencies=[Depends(require_writable)])
def create_security(payload: schemas.SecurityCreate, db: Session = Depends(get_db)):
    _get_asset_class(db, payload.asset_class_id)  # validate FK
    sec = Security(**payload.model_dump())
    db.add(sec)
    db.commit()
    db.refresh(sec)
    return _security_out(sec)


@router.put("/securities/{sec_id}", response_model=schemas.SecurityOut, dependencies=[Depends(require_writable)])
def update_security(sec_id: int, payload: schemas.SecurityUpdate, db: Session = Depends(get_db)):
    sec = _get_security(db, sec_id)
    data = payload.model_dump(exclude_unset=True)
    if "asset_class_id" in data:
        _get_asset_class(db, data["asset_class_id"])
    for field, value in data.items():
        setattr(sec, field, value)
    db.commit()
    db.refresh(sec)
    return _security_out(sec)


@router.delete("/securities/{sec_id}", dependencies=[Depends(require_writable)])
def delete_security(sec_id: int, db: Session = Depends(get_db)):
    sec = _get_security(db, sec_id)
    db.delete(sec)
    db.commit()
    return {"ok": True}


def _security_out(sec: Security) -> dict:
    shares = calc.net_shares(sec.transactions)
    price = sec.quote.price if sec.quote else None
    avg_cost = calc.average_cost(sec.transactions)
    cost_value, mv, pnl, pnl_pct = calc.derive_holding(shares, price, avg_cost)
    return security_payload(
        id=sec.id, code=sec.code, name=sec.name, market=sec.market,
        shares=shares, price=price, avg_cost=avg_cost, cost_value=cost_value,
        market_value=mv, unrealized_pnl=pnl, pnl_pct=pnl_pct,
    )


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #
def _resolve_security(db: Session, payload: schemas.TransactionCreate) -> Security:
    """Find the target security by id or code; auto-create it (by code, under the
    given asset class) the first time a code is seen."""
    if payload.security_id is not None:
        return _get_security(db, payload.security_id)
    sec = db.scalars(select(Security).where(Security.code == payload.code)).first()
    if sec is not None:
        return sec
    if payload.asset_class_id is None:
        raise HTTPException(status_code=400, detail=f"标的 {payload.code} 是新代码,需指定所属大类")
    _get_asset_class(db, payload.asset_class_id)  # validate FK
    sec = Security(asset_class_id=payload.asset_class_id, code=payload.code,
                   name=payload.name or payload.code, market="CN")
    db.add(sec)
    db.flush()
    return sec


def _tx_out(tx: Transaction) -> dict:
    return {"id": tx.id, "security_id": tx.security_id, "date": tx.date.isoformat(),
            "action": tx.action, "shares": tx.shares, "price": tx.price,
            "target_sell_price": tx.target_sell_price, "matched_buy_id": tx.matched_buy_id}


def _lot_remaining(buy: Transaction, *, exclude_sell_id: int = None) -> float:
    """Shares still open in a buy lot = lot shares − Σ sells matched to it
    (optionally excluding one sell, for edit recomputation)."""
    sold = sum(t.shares for t in buy.security.transactions
               if t.action == "sell" and t.matched_buy_id == buy.id and t.id != exclude_sell_id)
    return buy.shares - sold


@router.post("/transactions", dependencies=[Depends(require_writable)])
def create_transaction(payload: schemas.TransactionCreate, db: Session = Depends(get_db)):
    if payload.action == "sell":
        return _create_sell(db, payload)
    # --- buy ---
    sec = _resolve_security(db, payload)
    tx = Transaction(security_id=sec.id, date=payload.date, action="buy",
                     shares=payload.shares, price=payload.price,
                     target_sell_price=payload.target_sell_price)
    db.add(tx)
    # Seed a provisional price if the security has none yet (refinable later).
    if sec.quote is None:
        sec.quote = PriceQuote(security_id=sec.id, price=payload.price,
                               source="manual", updated_at=datetime.now())
    db.commit()
    db.refresh(tx)
    return _tx_out(tx)


def _create_sell(db: Session, payload: schemas.TransactionCreate) -> dict:
    """A sell closes a specific buy lot (specific-lot matching)."""
    if payload.matched_buy_id is None:
        raise HTTPException(status_code=400, detail="卖出需指定对应的买入批次")
    lot = db.get(Transaction, payload.matched_buy_id)
    if lot is None or lot.action != "buy":
        raise HTTPException(status_code=404, detail="对应的买入批次不存在")
    remaining = _lot_remaining(lot)
    if payload.shares > remaining + 1e-9:
        raise HTTPException(status_code=400,
                            detail=f"卖出 {payload.shares} 股超过该批次剩余 {remaining} 股")
    tx = Transaction(security_id=lot.security_id, date=payload.date, action="sell",
                     shares=payload.shares, price=payload.price,
                     matched_buy_id=lot.id, target_sell_price=None)
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return _tx_out(tx)


@router.get("/securities/{sec_id}/transactions", response_model=list[schemas.TransactionOut])
def list_transactions(sec_id: int, db: Session = Depends(get_db)):
    sec = _get_security(db, sec_id)
    return [_tx_out(tx) for tx in sorted(sec.transactions, key=lambda t: (t.date, t.id))]


@router.put("/transactions/{tx_id}", dependencies=[Depends(require_writable)])
def update_transaction(tx_id: int, payload: schemas.TransactionUpdate, db: Session = Depends(get_db)):
    tx = db.get(Transaction, tx_id)
    if tx is None:
        raise HTTPException(status_code=404, detail="交易不存在")
    data = payload.model_dump(exclude_unset=True)
    for field in ("date", "shares", "price", "target_sell_price"):
        if field in data:
            setattr(tx, field, data[field])  # target_sell_price may be null to clear
    if tx.action != "buy":
        tx.target_sell_price = None
    # A buy lot can't shrink below the shares already sold out of it; a sell can't
    # exceed its lot's remaining (spec §7 invariant, now per-lot).
    if tx.action == "buy":
        sold = _lot_remaining_sold(tx)
        if tx.shares < sold - 1e-9:
            db.rollback()
            raise HTTPException(status_code=400,
                                detail=f"该批次已卖出 {sold} 股,股数不能改到其下")
    else:
        lot = db.get(Transaction, tx.matched_buy_id) if tx.matched_buy_id else None
        if lot is not None and tx.shares > _lot_remaining(lot, exclude_sell_id=tx.id) + 1e-9:
            db.rollback()
            raise HTTPException(status_code=400, detail="卖出股数超过该批次剩余")
    db.commit()
    db.refresh(tx)
    return _tx_out(tx)


def _lot_remaining_sold(buy: Transaction) -> float:
    return sum(t.shares for t in buy.security.transactions
               if t.action == "sell" and t.matched_buy_id == buy.id)


@router.delete("/transactions/{tx_id}", dependencies=[Depends(require_writable)])
def delete_transaction(tx_id: int, db: Session = Depends(get_db)):
    tx = db.get(Transaction, tx_id)
    if tx is None:
        raise HTTPException(status_code=404, detail="交易不存在")
    # A buy lot with sells matched to it can't be deleted (the sells would lose
    # their cost basis) — delete those sells first.
    if tx.action == "buy" and _lot_remaining_sold(tx) > 1e-9:
        raise HTTPException(status_code=400, detail="该买入批次已有对应卖出,请先删除相关卖出")
    db.delete(tx)
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Price
# --------------------------------------------------------------------------- #
@router.put("/securities/{sec_id}/price", dependencies=[Depends(require_writable)])
def update_price(sec_id: int, payload: schemas.PriceUpdate, db: Session = Depends(get_db)):
    sec = _get_security(db, sec_id)
    now = datetime.now()
    if sec.quote is None:
        sec.quote = PriceQuote(security_id=sec.id)
    sec.quote.price = payload.price
    sec.quote.source = payload.source
    sec.quote.updated_at = now
    db.commit()
    db.refresh(sec)
    return {"security_id": sec.id, "price": sec.quote.price,
            "source": sec.quote.source, "updated_at": sec.quote.updated_at.isoformat()}


# --------------------------------------------------------------------------- #
# Strategy targets (unallocated-pool save)
# --------------------------------------------------------------------------- #
@router.put("/strategy/targets", dependencies=[Depends(require_writable)])
def update_targets(payload: schemas.TargetsUpdate, db: Session = Depends(get_db)):
    strategy = get_primary_strategy(db)
    if strategy is None:
        raise HTTPException(status_code=404, detail="尚无策略")

    by_id = {ac.id: ac for ac in strategy.asset_classes}

    # The submission must cover exactly the strategy's classes — otherwise a
    # partial subset summing to 100 would silently leave others' targets,
    # pushing the true total past 100 (spec §7).
    submitted_ids = {t.asset_class_id for t in payload.targets}
    if submitted_ids != set(by_id):
        raise HTTPException(
            status_code=400,
            detail="目标提交必须覆盖且仅覆盖当前所有大类",
        )

    # Enforce: unallocated must be 0, i.e. Σ target == 100 (spec §7).
    total = sum(t.target_weight for t in payload.targets)
    if abs(total - 100.0) > TARGET_EPSILON:
        raise HTTPException(
            status_code=400,
            detail=f"目标权重合计为 {total:.1f}%,必须为 100%(未分配 = 0)才能保存",
        )

    for t in payload.targets:
        ac = by_id.get(t.asset_class_id)
        if ac is None:
            raise HTTPException(status_code=404, detail=f"大类 {t.asset_class_id} 不存在")
        ac.target_weight = t.target_weight
        if t.band_low is not None:
            ac.band_low = t.band_low
        if t.band_high is not None:
            ac.band_high = t.band_high
    db.commit()
    return {"ok": True, "total": total}


# --------------------------------------------------------------------------- #
# Records ledger + cash flows (资金注入 / 移出)
# --------------------------------------------------------------------------- #
@router.get("/ledger")
def get_ledger(db: Session = Depends(get_db)):
    return build_ledger(db)


@router.post("/cashflows", dependencies=[Depends(require_writable)])
def create_cashflow(payload: schemas.CashFlowCreate, db: Session = Depends(get_db)):
    cf = CashFlow(**payload.model_dump())
    db.add(cf)
    db.commit()
    db.refresh(cf)
    return {"id": cf.id, "date": cf.date.isoformat(), "direction": cf.direction,
            "amount": cf.amount, "note": cf.note}


@router.delete("/cashflows/{cf_id}", dependencies=[Depends(require_writable)])
def delete_cashflow(cf_id: int, db: Session = Depends(get_db)):
    cf = db.get(CashFlow, cf_id)
    if cf is None:
        raise HTTPException(status_code=404, detail="资金流水不存在")
    db.delete(cf)
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Live quote refresh (by code; updates prices to source="auto")
# --------------------------------------------------------------------------- #
@router.post("/prices/refresh", dependencies=[Depends(require_writable)])
def refresh_prices(db: Session = Depends(get_db)):
    securities = db.scalars(select(Security)).all()
    try:
        fetched = quotes.fetch_quotes([(s.code, s.market) for s in securities])
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="行情源连接失败,已保留现有价格")

    updated = apply_fetched_quotes(db, fetched)
    db.commit()
    # Codes the source returned nothing for (unclassifiable market, delisted,
    # suspended…) — surfaced so they don't fail silently.
    unresolved = [sec.code for sec in securities if sec.code not in fetched]
    return {"updated": updated, "total": len(securities),
            "unresolved": unresolved, "source": quotes.LAST_SOURCE or "auto"}


# --------------------------------------------------------------------------- #
# Mark a rebalance as done (timestamps the strategy for the reminder)
# --------------------------------------------------------------------------- #
@router.post("/strategy/rebalanced", dependencies=[Depends(require_writable)])
def mark_rebalanced(db: Session = Depends(get_db)):
    strategy = get_primary_strategy(db)
    if strategy is None:
        raise HTTPException(status_code=404, detail="尚无策略")
    strategy.last_rebalanced_at = datetime.now()
    db.commit()
    return {"ok": True, "last_rebalanced_at": strategy.last_rebalanced_at.isoformat()}


# --------------------------------------------------------------------------- #
# Backup (snapshot the SQLite file into <db-dir>/backups/)
# --------------------------------------------------------------------------- #
@router.post("/backup", dependencies=[Depends(require_writable)])
def backup_now():
    return backup.make_backup(force=True)


@router.get("/backups")
def list_backups():
    out = []
    seen = {}
    for d in backup.get_destinations():
        for m in d.list():
            row = seen.get(m.name)
            if row is None:
                row = {"file": m.name, "size": m.size, "modified": m.created_at,
                       "integrity_ok": m.integrity_ok, "destinations": [],
                       "encrypted": False}
                seen[m.name] = row
                out.append(row)
            row["destinations"].append(d.name)
            if getattr(m, "encrypted", False):
                row["encrypted"] = True
    return out


@router.post("/backup/verify", dependencies=[Depends(require_writable)])
def verify_backups(file: Optional[str] = Query(None), destination: Optional[str] = Query(None)):
    return backup.verify(name=file, destination=destination, allow_pull=True)


@router.post("/restore", dependencies=[Depends(require_writable)])
def restore(payload: schemas.RestoreRequest):
    try:
        return backup.restore_backup(payload.file, getattr(payload, "destination", None))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="备份文件不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --------------------------------------------------------------------------- #
# Reset to default seed data (auto-backs up first, so a misclick is recoverable)
# --------------------------------------------------------------------------- #
@router.post("/reset", dependencies=[Depends(require_writable)])
def reset(db: Session = Depends(get_db)):
    try:
        backup.make_backup(force=True)
    except Exception:
        pass
    reset_to_default(db)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# History + performance: daily NAV snapshot + time series / metrics.
# --------------------------------------------------------------------------- #
@router.post("/snapshot", dependencies=[Depends(require_writable)])
def take_snapshot(db: Session = Depends(get_db)):
    snap = snapshot_service.run_snapshot(db)
    return {
        "date": snap.date.isoformat(),
        "total_assets": snap.total_assets,
        "net_invested": snap.net_invested,
        "benchmark": snap.benchmark,
    }


@router.get("/history")
def history(range_: str = Query("all", alias="range"), db: Session = Depends(get_db)):
    range_ = range_ if range_ in ("3m", "1y", "all") else "all"
    return snapshot_service.build_history(db, range_=range_)
