"""High-level trading agent.

Ties together:

- The (re-building) system prompt from :class:`PromptBuilder`
- RAG retrieval from :class:`RAGRetriever`
- An :class:`LLMProvider` for reasoning
- A :class:`Broker` for execution (optional in dry-run)

The agent is intentionally thin — it shapes inputs, asks the LLM for a
structured decision, validates it, and either returns or executes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from moppu.agent.prompt import PromptBuilder
from moppu.agent.rag import RAGRetriever, RetrievedChunk
from moppu.broker.base import Broker, Order, OrderSide
from moppu.config import AgentConfig
from moppu.llm.base import ChatMessage, LLMProvider
from moppu.logging_setup import get_logger

log = get_logger(__name__)


_DECISION_SCHEMA = {
    "name": "trade_decision",
    "schema": {
        "type": "object",
        "required": ["action", "reason"],
        "properties": {
            "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
            "ticker": {"type": ["string", "null"]},
            "quantity": {"type": ["integer", "null"], "minimum": 0},
            "price": {"type": ["number", "null"]},
            "order_type": {"type": "string", "enum": ["market", "limit"]},
            "reason": {"type": "string"},
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "video_id": {"type": "string"},
                        "video_title": {"type": ["string", "null"]},
                        "quote": {"type": "string"},
                    },
                },
            },
        },
    },
}


class Citation(BaseModel):
    video_id: str
    video_title: str | None = None
    quote: str


class TradeDecision(BaseModel):
    action: Literal["BUY", "SELL", "HOLD"]
    ticker: str | None = None
    quantity: int | None = Field(default=None, ge=0)
    price: float | None = None
    order_type: Literal["market", "limit"] = "market"
    reason: str
    citations: list[Citation] = Field(default_factory=list)


@dataclass(slots=True)
class AgentContext:
    account_snapshot: str = "(not loaded)"


class TraderAgent:
    def __init__(
        self,
        *,
        cfg: AgentConfig,
        llm: LLMProvider,
        prompt_builder: PromptBuilder,
        retriever: RAGRetriever,
        broker: Broker | None = None,
    ) -> None:
        self._cfg = cfg
        self._llm = llm
        self._prompt = prompt_builder
        self._retriever = retriever
        self._broker = broker

    # ------------------------------------------------------------------ #

    def decide(self, user_message: str, *, context: AgentContext | None = None) -> TradeDecision:
        ctx = context or AgentContext()
        retrieved = self._retriever.retrieve(user_message)

        system = self._prompt.build_system_prompt()
        user = self._format_user(user_message, retrieved, ctx)

        resp = self._llm.chat(
            messages=[ChatMessage(role="user", content=user)],
            system=system,
        )
        log.info(
            "agent.llm_response",
            provider=resp.provider,
            model=resp.model,
            usage=resp.usage,
        )
        return self._parse_decision(resp.text)

    def act(self, decision: TradeDecision) -> dict[str, Any]:
        if decision.action == "HOLD":
            return {"executed": False, "reason": "HOLD"}
        if self._cfg.dry_run or self._broker is None:
            return {"executed": False, "dry_run": True, "decision": decision.model_dump()}

        if not decision.ticker or not decision.quantity:
            raise ValueError("BUY/SELL requires ticker and quantity")

        # Respect max_order_krw when we have a price.
        if decision.price and decision.quantity:
            gross = decision.price * decision.quantity
            if gross > self._cfg.max_order_krw:
                raise ValueError(
                    f"Order {gross:.0f} KRW exceeds max_order_krw={self._cfg.max_order_krw}"
                )

        order = Order(
            ticker=decision.ticker,
            side=OrderSide.BUY if decision.action == "BUY" else OrderSide.SELL,
            quantity=decision.quantity,
            price=decision.price,
            order_type=decision.order_type,
        )
        ack = self._broker.place_order(order)
        return {"executed": True, "ack": ack}

    # ------------------------------------------------------------------ #

    def _format_user(self, user_message: str, hits: list[RetrievedChunk], ctx: AgentContext) -> str:
        lines = ["Retrieved transcript excerpts (top-k):"]
        if not hits:
            lines.append("(no excerpts above score threshold)")
        for h in hits:
            lines.append(
                f"- [{h.published_at_iso or '????'}] {h.video_title or h.video_id} "
                f"(score={h.score:.2f}, chunk #{h.chunk_index}):\n  {h.text.strip()[:800]}"
            )

        schema = json.dumps(_DECISION_SCHEMA, indent=2, ensure_ascii=False)
        return (
            "\n".join(lines)
            + f"\n\nAccount snapshot:\n{ctx.account_snapshot}\n\n"
            f"User message:\n{user_message}\n\n"
            f"Respond with a single JSON object matching this schema:\n{schema}"
        )

    def _parse_decision(self, text: str) -> TradeDecision:
        candidate = _strip_code_fences(text).strip()
        try:
            data = json.loads(candidate)
            return TradeDecision.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as e:
            log.warning("agent.parse_failed", err=str(e), raw=text[:400])
            return TradeDecision(action="HOLD", reason=f"parse_failed: {e}")


def _strip_code_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines)
    return s
