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
    name: str | None = None


@dataclass(slots=True)
class Quote:
    ticker: str
    price: float
    timestamp_iso: str


@dataclass(slots=True)
class AccountSummary:
    """KIS inquire-balance output2 요약.

    금액은 모두 KRW.
    """
    cash: float               # 예수금 (dnca_tot_amt)
    d2_cash: float            # D+2 예수금 (prvs_rcdl_excc_amt)
    stock_eval: float         # 유가증권 평가금액 (scts_evlu_amt)
    total_eval: float         # 총평가금액 (tot_evlu_amt)
    total_purchase: float     # 매입금액 합계 (pchs_amt_smtl_amt)
    eval_pl: float            # 평가손익 합계 (evlu_pfls_smtl_amt)
    net_asset: float          # 순자산금액 (nass_amt)
    asset_change: float       # 자산증감액 (asst_icdc_amt)
    asset_change_rate: float  # 자산증감수익율 (asst_icdc_erng_rt)


@dataclass(slots=True)
class TradeFill:
    """주문/체결 이력 한 건."""
    order_date: str           # YYYYMMDD
    order_time: str           # HHMMSS
    ticker: str
    name: str | None
    side: str                 # "BUY" | "SELL"
    quantity: int             # 주문수량
    filled_qty: int           # 체결수량
    price: float              # 주문단가
    avg_fill_price: float     # 평균체결단가
    total_amount: float       # 체결금액
    status: str               # "filled" | "partial" | "cancelled" | "rejected" | "pending"


class Broker(Protocol):
    def place_order(self, order: Order) -> OrderAck: ...
    def get_positions(self) -> list[Position]: ...
    def get_cash_balance_krw(self) -> float: ...
    def get_quote(self, ticker: str) -> Quote: ...
    def get_account_summary(self) -> AccountSummary: ...
    def get_daily_trades(
        self, *, ticker: str | None = None, days: int = 30
    ) -> list[TradeFill]: ...
