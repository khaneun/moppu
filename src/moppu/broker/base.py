"""Broker protocol — keeps the agent decoupled from a specific broker."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(slots=True)
class Order:
    ticker: str
    side: OrderSide
    quantity: int
    price: float | None = None       # None → market order
    order_type: str = "market"       # "market" | "limit"


@dataclass(slots=True)
class OrderAck:
    order_id: str
    status: str
    raw: Any = None


@dataclass(slots=True)
class Position:
    ticker: str
    quantity: int
    avg_price: float
    unrealized_pl: float | None = None


@dataclass(slots=True)
class Quote:
    ticker: str
    price: float
    timestamp_iso: str


class Broker(Protocol):
    def place_order(self, order: Order) -> OrderAck: ...
    def get_positions(self) -> list[Position]: ...
    def get_cash_balance_krw(self) -> float: ...
    def get_quote(self, ticker: str) -> Quote: ...
