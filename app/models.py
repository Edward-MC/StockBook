"""Domain model — see spec §3.1.

Two-layer structure: a Security belongs to an AssetClass, which belongs to a
Strategy. Positions and cost are *derived* from Transactions (not stored).
PriceQuote holds the latest manual price per security.

The model is deliberately "strategy-aware" and "market-agnostic" so future
versions add rows/columns rather than restructuring tables.
"""
import datetime as dt
from typing import List, Optional

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # When the user last marked a rebalance done (for the 距上次再平衡 reminder).
    last_rebalanced_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    asset_classes: Mapped[List["AssetClass"]] = relationship(
        back_populates="strategy",
        cascade="all, delete-orphan",
        order_by="AssetClass.sort_order",
    )


class AssetClass(Base):
    __tablename__ = "asset_classes"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Target weight (%) set top-down via slider.
    target_weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # Tolerance band (%) lower/upper bounds.
    band_low: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    band_high: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    color: Mapped[str] = mapped_column(String, nullable=False, default="--c1")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The cash class (现金): holds no securities; its value is the cash balance
    # (deposits − withdrawals + sell proceeds − buy costs). At most one per strategy.
    is_cash: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    strategy: Mapped["Strategy"] = relationship(back_populates="asset_classes")
    securities: Mapped[List["Security"]] = relationship(
        back_populates="asset_class",
        cascade="all, delete-orphan",
        order_by="Security.id",
    )


class Security(Base):
    __tablename__ = "securities"

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_class_id: Mapped[int] = mapped_column(ForeignKey("asset_classes.id"), nullable=False)
    code: Mapped[str] = mapped_column(String, nullable=False)  # e.g. 510300
    name: Mapped[str] = mapped_column(String, nullable=False)
    market: Mapped[str] = mapped_column(String, nullable=False, default="CN")  # reserved for US/HK

    asset_class: Mapped["AssetClass"] = relationship(back_populates="securities")
    transactions: Mapped[List["Transaction"]] = relationship(
        back_populates="security",
        cascade="all, delete-orphan",
        order_by="Transaction.date",
    )
    # Latest manual price; v1 keeps a single quote per security.
    quote: Mapped[Optional["PriceQuote"]] = relationship(
        back_populates="security",
        cascade="all, delete-orphan",
        uselist=False,
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id"), nullable=False)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)  # "buy" | "sell"
    shares: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    # Per-buy-lot expected sell price (only meaningful for buys; null = not set).
    target_sell_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # For a SELL: the buy lot (transaction) it closes — specific-lot matching.
    # Realized P&L = (sell price − matched buy price) × shares. Null for buys.
    matched_buy_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("transactions.id"), nullable=True
    )

    security: Mapped["Security"] = relationship(back_populates="transactions")


class PriceQuote(Base):
    __tablename__ = "price_quotes"

    id: Mapped[int] = mapped_column(primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id"), nullable=False, unique=True
    )
    price: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False, default="manual")  # "manual" | "auto"
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=func.now()
    )

    security: Mapped["Security"] = relationship(back_populates="quote")


class CashFlow(Base):
    """Cash injection / withdrawal (资金注入 / 移出). Independent of securities;
    feeds the cash balance and the records ledger."""
    __tablename__ = "cash_flows"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)  # "in" | "out"
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class NotionSource(Base):
    """A Notion page/database the user authorized for the knowledge base.
    Only these are crawled (spec §2:指定几个页面/库, not whole workspace)."""
    __tablename__ = "notion_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    notion_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String, nullable=False, default="")
    kind: Mapped[str] = mapped_column(String, nullable=False, default="page")  # "page" | "database"
    last_synced_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    chunks: Mapped[List["KnowledgeChunk"]] = relationship(
        back_populates="source", cascade="all, delete-orphan",
    )


class KnowledgeChunk(Base):
    """One embedded text fragment from a Notion page. Embedding is a JSON list
    in a Text column — portable across SQLite builds (no vector extension).
    Retrieval is brute-force numpy cosine over all chunks (spec §5)."""
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("notion_sources.id"), nullable=False)
    notion_page_id: Mapped[str] = mapped_column(String, nullable=False)
    notion_url: Mapped[str] = mapped_column(String, nullable=False, default="")
    title_path: Mapped[str] = mapped_column(String, nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[str] = mapped_column(Text, nullable=False)  # JSON-encoded list[float]
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    source: Mapped["NotionSource"] = relationship(back_populates="chunks")
