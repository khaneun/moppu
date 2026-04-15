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


_CHAT_ADDENDUM = """

## 대화 모드

지금은 대화 모드입니다. 다음 규칙을 따르세요:

- 자연스러운 한국어로 응답하세요.
- 당신은 경험 많은 애널리스트입니다. 구체적인 분석과 결론을 제시하세요.
- 모르는 종목이라도 수집된 컨텍스트와 시장 지식을 기반으로 의견을 내세요.
- 불확실한 부분은 명시하되, 반드시 결론적 의견을 내세요.
- 참고한 영상이 있다면 제목을 언급하세요.
- JSON이 아닌 자연어로 응답하세요.
- 주식, 투자, 경제, 금융과 **무관한 질문**에는 "죄송합니다. 저는 주식 투자 분석 전문 에이전트로, 해당 주제에는 답변드리기 어렵습니다. 투자 관련 질문을 부탁드립니다." 라고 정중히 거절하세요.
"""


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

    def chat(
        self,
        user_message: str,
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Conversational chat with RAG context. Returns text + citations."""
        retrieved = self._retriever.retrieve(user_message)
        system = self._prompt.build_system_prompt() + _CHAT_ADDENDUM

        lines = ["참고 자료 (YouTube 자막 발췌):"]
        if not retrieved:
            lines.append("(관련 자막 없음)")
        for h in retrieved:
            lines.append(
                f"- [{h.published_at_iso or '날짜 불명'}] {h.video_title or h.video_id}:\n"
                f"  {h.text.strip()[:800]}"
            )
        rag_user = "\n".join(lines) + f"\n\n질문: {user_message}"

        messages: list[ChatMessage] = []
        for m in history or []:
            messages.append(ChatMessage(role=m["role"], content=m["content"]))
        messages.append(ChatMessage(role="user", content=rag_user))

        resp = self._llm.chat(messages=messages, system=system)
        log.info("agent.chat_response", provider=resp.provider, model=resp.model, usage=resp.usage)

        seen: set[str] = set()
        citations = []
        for h in retrieved:
            if h.video_id not in seen:
                seen.add(h.video_id)
                citations.append({
                    "video_id": h.video_id,
                    "title": h.video_title,
                    "url": f"https://www.youtube.com/watch?v={h.video_id}",
                })

        return {
            "text": resp.text,
            "citations": citations,
            "usage": resp.usage,
            "model": resp.model,
            "provider": resp.provider,
        }

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
