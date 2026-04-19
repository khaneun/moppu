"""매매 실행기 — TradePlan을 받아 매도 → 매수 순서로 실제 주문을 냅니다."""

from __future__ import annotations

import time
from typing import Any

from moppu.agent.strategy_planner import SellInstruction, TradePlan
from moppu.broker.base import Broker, Order, OrderSide, Position
from moppu.logging_setup import get_logger

log = get_logger(__name__)


class TradeExecutor:
    def __init__(self, *, broker: Broker | None, dry_run: bool = True) -> None:
        self._broker = broker
        self._dry_run = dry_run

    def execute(self, plan: TradePlan, positions: list[Position]) -> list[dict[str, Any]]:
        """TradePlan을 실행하고 결과 목록을 반환합니다."""
        if self._dry_run or not self._broker:
            log.info("executor.dry_run", n_sells=len(plan.sells), n_buys=len(plan.buys))
            return [{"status": "dry_run", "plan": plan.model_dump()}]

        pos_map = {p.ticker: p for p in positions}
        results: list[dict[str, Any]] = []

        # 매도 먼저 (자금 확보)
        for sell in plan.sells:
            result = self._execute_sell(sell, pos_map)
            results.append(result)

        # 매도 주문 처리 여유
        if plan.sells:
            time.sleep(2)

        # 매수
        for buy in plan.buys:
            order = Order(
                ticker=buy.ticker,
                side=OrderSide.BUY,
                quantity=buy.quantity,
                order_type="market",
            )
            result = _place(self._broker, order)
            results.append({"action": "BUY", "ticker": buy.ticker, "qty": buy.quantity, **result})

        return results

    def _execute_sell(
        self,
        sell: SellInstruction,
        pos_map: dict[str, Position],
    ) -> dict[str, Any]:
        qty = sell.quantity
        if sell.is_full:
            pos = pos_map.get(sell.ticker)
            qty = pos.quantity if pos else 0

        if qty <= 0:
            log.warning("executor.sell_skip_zero", ticker=sell.ticker)
            return {"action": "SELL", "ticker": sell.ticker, "qty": 0, "status": "skip", "reason": "zero qty"}

        order = Order(
            ticker=sell.ticker,
            side=OrderSide.SELL,
            quantity=qty,
            order_type="market",
        )
        result = _place(self._broker, order)
        return {"action": "SELL", "ticker": sell.ticker, "qty": qty, **result}


def _place(broker: Broker, order: Order) -> dict[str, Any]:
    try:
        ack = broker.place_order(order)
        log.info(
            "executor.order_placed",
            side=order.side.value,
            ticker=order.ticker,
            qty=order.quantity,
            order_id=ack.order_id,
        )
        return {"status": "ok", "order_id": ack.order_id, "ack_status": ack.status}
    except Exception as e:
        log.error("executor.order_failed", side=order.side.value, ticker=order.ticker, err=str(e))
        return {"status": "error", "error": str(e)}
