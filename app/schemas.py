"""Pydantic request/response schemas (spec §6, §7)."""
from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------- #
# Asset class CRUD
# --------------------------------------------------------------------------- #
class AssetClassBase(BaseModel):
    name: str = Field(..., min_length=1)
    target_weight: float = Field(0.0, ge=0, le=100)
    band_low: float = Field(0.0, ge=0, le=100)
    band_high: float = Field(100.0, ge=0, le=100)
    color: Optional[str] = None  # None → auto-assigned a distinct color
    sort_order: int = 0
    is_cash: bool = False


class AssetClassCreate(AssetClassBase):
    pass


class AssetClassUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    target_weight: Optional[float] = Field(None, ge=0, le=100)
    band_low: Optional[float] = Field(None, ge=0, le=100)
    band_high: Optional[float] = Field(None, ge=0, le=100)
    color: Optional[str] = None
    sort_order: Optional[int] = None
    is_cash: Optional[bool] = None


class CashFlowCreate(BaseModel):
    date: date_type
    direction: Literal["in", "out"]
    amount: float = Field(..., gt=0)
    note: Optional[str] = None


# --------------------------------------------------------------------------- #
# Security CRUD
# --------------------------------------------------------------------------- #
class SecurityCreate(BaseModel):
    asset_class_id: int
    code: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    market: str = "CN"


class SecurityUpdate(BaseModel):
    asset_class_id: Optional[int] = None
    code: Optional[str] = Field(None, min_length=1)
    name: Optional[str] = Field(None, min_length=1)
    market: Optional[str] = None


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #
class TransactionCreate(BaseModel):
    # Buy: reference a security by id, OR by code (auto-created on first use, with
    # asset_class_id). Sell: reference the buy lot via matched_buy_id (the security
    # is derived from that lot).
    security_id: Optional[int] = None
    code: Optional[str] = Field(None, min_length=1)
    name: Optional[str] = None
    asset_class_id: Optional[int] = None
    matched_buy_id: Optional[int] = None  # required for sells (specific-lot)
    date: date_type
    action: Literal["buy", "sell"]
    shares: float = Field(..., gt=0)
    price: float = Field(..., gt=0)
    target_sell_price: Optional[float] = Field(None, gt=0)  # buys only


class TransactionUpdate(BaseModel):
    # All optional — only provided fields are applied (correct a mis-entered lot).
    date: Optional[date_type] = None
    shares: Optional[float] = Field(None, gt=0)
    price: Optional[float] = Field(None, gt=0)
    target_sell_price: Optional[float] = Field(None, gt=0)  # buys only; null clears


class TransactionOut(BaseModel):
    id: int
    security_id: int
    date: date_type
    action: str
    shares: float
    price: float
    target_sell_price: Optional[float]
    matched_buy_id: Optional[int]


# --------------------------------------------------------------------------- #
# Price
# --------------------------------------------------------------------------- #
class PriceUpdate(BaseModel):
    price: float = Field(..., gt=0)
    source: str = "manual"


# --------------------------------------------------------------------------- #
# Strategy targets (unallocated-pool save)
# --------------------------------------------------------------------------- #
class TargetItem(BaseModel):
    asset_class_id: int
    target_weight: float = Field(..., ge=0, le=100)
    band_low: Optional[float] = Field(None, ge=0, le=100)
    band_high: Optional[float] = Field(None, ge=0, le=100)


class RestoreRequest(BaseModel):
    file: str = Field(..., min_length=1)
    destination: Optional[str] = None


class TargetsUpdate(BaseModel):
    targets: List[TargetItem]

    @field_validator("targets")
    @classmethod
    def non_empty(cls, v):
        if not v:
            raise ValueError("targets must not be empty")
        return v


# --------------------------------------------------------------------------- #
# Dashboard response (mirrors app.calc dataclasses)
# --------------------------------------------------------------------------- #
class SecurityOut(BaseModel):
    id: int
    code: str
    name: str
    market: str
    shares: float
    price: Optional[float]
    avg_cost: Optional[float]
    cost_value: Optional[float]
    market_value: Optional[float]
    unrealized_pnl: Optional[float]
    pnl_pct: Optional[float]
    weight_in_class: Optional[float]
    weight_in_total: Optional[float]
    pending: bool


class AssetClassOut(BaseModel):
    id: int
    name: str
    target_weight: float
    band_low: float
    band_high: float
    color: str
    sort_order: int
    is_cash: bool
    market_value: float
    current_weight: Optional[float]
    deviation: Optional[float]
    status: str
    rebalance_amount: Optional[float]
    securities: List[SecurityOut]


class RebalanceOut(BaseModel):
    asset_class_id: int
    name: str
    color: str
    status: str
    amount: float
    edge_amount: float
    current_weight: float
    target_weight: float


class DashboardOut(BaseModel):
    strategy_id: int
    strategy_name: str
    total_assets: float
    cash_balance: float
    unallocated: float
    is_balanced: bool
    deviating_count: int
    valuation_date: Optional[datetime]
    price_state: Optional[str]
    last_rebalanced_at: Optional[datetime]
    readonly: bool
    hide_amounts: bool
    asset_classes: List[AssetClassOut]
    rebalance: List[RebalanceOut]
    pending_securities: List[SecurityOut]


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)


class NotionSourceCreate(BaseModel):
    notion_id: str = Field(..., min_length=1)
    title: Optional[str] = ""
    kind: str = "page"  # "page" | "database"
