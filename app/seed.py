"""Database initialization and example seed data.

On first run we create the schema and seed one example strategy so the
dashboard has content immediately. `reset_to_default` wipes and re-seeds.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from .database import Base, SessionLocal, engine
from .models import (AssetClass, CashFlow, KnowledgeChunk, NotionSource,
                     PriceQuote, Security, Strategy, Transaction)

# Example asset classes: name, target, band_low, band_high, color, sort_order.
_EXAMPLE_CLASSES = [
    ("沪深300", 30.0, 25.0, 35.0, "--c1", 0),
    ("中证500", 20.0, 15.0, 25.0, "--c2", 1),
    ("红利", 20.0, 15.0, 25.0, "--c3", 2),
    ("行业", 10.0, 5.0, 15.0, "--c4", 3),
    ("债券", 20.0, 15.0, 25.0, "--c5", 4),
]

# class_name -> list of (code, name, [(date, action, shares, price, target_sell?), ...], current_price)
# current_price=None means "pending valuation" (待估值); target_sell optional (buys only).
_EXAMPLE_SECURITIES = {
    "沪深300": [("510300", "沪深300ETF", [("2025-01-10", "buy", 10000, 3.80, 4.60)], 4.00)],
    "中证500": [("510500", "中证500ETF", [("2025-02-15", "buy", 5000, 6.00, 7.50)], 6.50)],
    "红利":    [("515080", "中证红利ETF", [("2025-03-01", "buy", 8000, 1.50, 1.85)], 1.60)],
    "行业":    [
        ("512170", "医疗ETF", [("2025-03-20", "buy", 5000, 0.50)], 0.45),
        ("516160", "新能源ETF", [("2025-04-01", "buy", 3000, 0.90)], None),  # pending price
    ],
    "债券":    [("511260", "十年国债ETF", [("2025-01-05", "buy", 200, 100.0)], 105.0)],
}


def create_schema():
    Base.metadata.create_all(bind=engine)
    _migrate()


def _migrate():
    """Tiny additive migrations for DBs created by an older schema version
    (SQLite create_all never alters existing tables)."""
    insp = inspect(engine)
    tables = insp.get_table_names()
    if "strategies" in tables:
        cols = {c["name"] for c in insp.get_columns("strategies")}
        if "last_rebalanced_at" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE strategies ADD COLUMN last_rebalanced_at DATETIME"))
    if "transactions" in tables:
        cols = {c["name"] for c in insp.get_columns("transactions")}
        if "target_sell_price" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE transactions ADD COLUMN target_sell_price FLOAT"))
        if "matched_buy_id" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE transactions ADD COLUMN matched_buy_id INTEGER"))
    if "asset_classes" in tables:
        cols = {c["name"] for c in insp.get_columns("asset_classes")}
        if "is_cash" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE asset_classes ADD COLUMN is_cash BOOLEAN DEFAULT 0"))


def _seed(db: Session):
    strategy = Strategy(name="我的核心配置")
    db.add(strategy)
    db.flush()

    for name, target, low, high, color, order in _EXAMPLE_CLASSES:
        ac = AssetClass(strategy_id=strategy.id, name=name, target_weight=target,
                        band_low=low, band_high=high, color=color, sort_order=order)
        db.add(ac)
        db.flush()

        for code, sec_name, txs, price in _EXAMPLE_SECURITIES.get(name, []):
            sec = Security(asset_class_id=ac.id, code=code, name=sec_name, market="CN")
            db.add(sec)
            db.flush()
            for tx in txs:
                d, action, shares, px = tx[0], tx[1], tx[2], tx[3]
                target = tx[4] if len(tx) > 4 else None
                db.add(Transaction(security_id=sec.id, date=date.fromisoformat(d),
                                   action=action, shares=shares, price=px, target_sell_price=target))
            if price is not None:
                db.add(PriceQuote(security_id=sec.id, price=price, source="manual",
                                  updated_at=datetime.now()))

    # Example cash flows so the records ledger has content out of the box.
    db.add(CashFlow(date=date.fromisoformat("2025-01-02"), direction="in",
                    amount=200000.0, note="初始入金"))
    db.add(CashFlow(date=date.fromisoformat("2025-04-10"), direction="out",
                    amount=20000.0, note="取出应急"))
    db.commit()


def init_db():
    """Create the schema and seed example data if the DB is empty."""
    create_schema()
    db = SessionLocal()
    try:
        if db.scalars(select(Strategy)).first() is None:
            _seed(db)
    finally:
        db.close()


def reset_to_default(db: Session):
    """Wipe all data and re-seed the example strategy. Also clears the RAG
    knowledge base (sources + chunks) so a reset is a true clean slate; re-add
    Notion sources and re-sync afterward."""
    for model in (Transaction, PriceQuote, Security, CashFlow, AssetClass, Strategy,
                  KnowledgeChunk, NotionSource):
        db.query(model).delete()
    db.commit()
    _seed(db)
