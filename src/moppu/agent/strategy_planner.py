"""전략 수립가 에이전트.

LSY Agent(TraderAgent)와 다중 대화를 통해 포트폴리오 섹터 분석 → 종목 후보 확인 →
최종 매도/매수 계획을 수립하고, TradeExecutor에 실행을 위임합니다.

일일 1회(장 시작 후) 자동 실행 또는 CLI로 수동 트리거.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field

from moppu.broker.base import Broker, Order, OrderSide, Position
from moppu.config import Settings, StrategyPlannerConfig
from moppu.llm.base import ChatMessage, LLMProvider
from moppu.logging_setup import get_logger

log = get_logger(__name__)

KST = timezone(timedelta(hours=9))


# ── Trade Plan 모델 ───────────────────────────────────────────────────────────

class SellInstruction(BaseModel):
    ticker: str
    quantity: int           # -1 = 전량 매도
    reason: str

    @property
    def is_full(self) -> bool:
        return self.quantity < 0


class BuyInstruction(BaseModel):
    ticker: str
    quantity: int
    price: float
    reason: str

    @property
    def total_krw(self) -> float:
        return self.price * self.quantity


class TradePlan(BaseModel):
    sells: list[SellInstruction] = Field(default_factory=list)
    buys: list[BuyInstruction] = Field(default_factory=list)
    summary: str = ""
    sectors_to_add: list[str] = Field(default_factory=list)
    sectors_to_reduce: list[str] = Field(default_factory=list)
    total_sell_krw: float = 0.0
    total_buy_krw: float = 0.0
    needs_additional_krw: float = 0.0


# ── 전략 수립가 LLM 시스템 프롬프트 ──────────────────────────────────────────

_STRATEGY_SYSTEM = """\
당신은 한국 주식 포트폴리오 전략 수립가입니다.

## 역할
LSY 애널리스트의 섹터 분석과 종목 추천을 바탕으로, 현실적인 포트폴리오 업데이트 계획을 JSON으로 작성합니다.

## 원칙
- 매도로 확보되는 자금도 매수 예산에 포함합니다.
- 가용 예산 내에서만 매수 계획을 수립합니다.
- 분할 매도(전량이 아닌 일부)도 허용합니다.
- 종목코드는 한국 주식 6자리 숫자입니다.
- quantity=-1 은 전량 매도를 의미합니다.

## LSY 강경도(1-10)에 따른 계획 수립 가이드
- 8-10 (강력 매수 신호): 기회 선점을 위해 예산을 최대한 활용. 가용 자금 대비 90%+ 매수 집행.
  부족하면 `needs_additional_krw` 에 기존 대비 *큰 금액*을 반영하고 매수 reason 에
  강하게 졸라 (긴급·적극적 어조). 기존 보유 중 부진 종목 매도도 공격적으로.
- 5-7 (중립-약강세): 균형 있는 배분. 가용 자금의 60-80% 사용. 매수 이유는 차분한 톤.
- 1-4 (신중): 자금 투입 최소화. 관망 권고. `summary` 에 "신중 유지" 명시.

## 출력 형식
반드시 아래 JSON 스키마에 맞는 단일 JSON 객체만 출력하세요. 코드 블록 없이 순수 JSON만 출력합니다.

{
  "sells": [{"ticker": "005930", "quantity": 10, "reason": "..."}],
  "buys":  [{"ticker": "000660", "quantity": 5, "price": 120000.0, "reason": "..."}],
  "summary": "전략 요약 (2-3문장)",
  "sectors_to_add":    ["반도체", "이차전지"],
  "sectors_to_reduce": ["은행", "철강"]
}

## JSON 작성 규칙 (반드시 준수 — 위반 시 파싱 실패)
- 숫자에 자릿수 구분자를 넣지 마세요: `61000.0` (O) / `61_000.0`·`61,000.0` (X)
- price 는 따옴표 없는 숫자입니다: `"price": 61000.0` (O) / `"price": "61000"` (X)
- reason 등 문자열 안에서는 큰따옴표(")를 쓰지 말고 작은따옴표(')를 사용하세요.
- 배열·객체의 마지막 항목 뒤에 쉼표를 남기지 마세요(trailing comma 금지).
"""

# ── LSY 대화 Turn 3 — JSON 추출 프롬프트 ────────────────────────────────────

_TICKER_EXTRACT_PROMPT = """\
위 대화에서 언급된 매수/매도 종목코드를 아래 JSON 형식으로 정리해주세요.
코드 블록 없이 순수 JSON만 출력하세요.

{"buy": ["005930", "000660"], "sell": ["035420"]}

매수: 강화·신규 편입 추천 종목 코드
매도: 정리 검토 종목 코드
코드가 없으면 빈 배열로 남겨두세요.
"""


# ── 메인 에이전트 ─────────────────────────────────────────────────────────────

class StrategyPlannerAgent:
    def __init__(
        self,
        *,
        cfg: StrategyPlannerConfig,
        settings: Settings,
        llm: LLMProvider,
        trader_agent: Any,       # TraderAgent — 순환 참조 회피용 Any
        broker: Broker | None = None,
        data_dir: Any = None,    # Path — 이력 저장 경로
    ) -> None:
        self._cfg = cfg
        self._settings = settings
        self._llm = llm
        self._trader = trader_agent
        self._broker = broker
        self._data_dir = data_dir

    # ── 공개 진입점 ───────────────────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        """전략 수립 전체 파이프라인. 실패 시에도 로그를 남기고 결과 dict 를 반환."""
        now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
        self._log_lines: list[str] = []
        self._append_log(f"=== 전략 수립 시작 ({now_str}) ===")
        self._append_log(f"dry_run={self._cfg.dry_run}, max_order_krw={self._cfg.max_order_krw:,}")

        log.info("strategy_planner.start", ts=now_str)

        if not self._broker:
            self._append_log("[ERROR] broker not configured")
            log.warning("strategy_planner.no_broker")
            return {"error": "broker not configured", "log": "\n".join(self._log_lines)}

        try:
            result = self._pipeline()
        except Exception as e:
            self._append_log(f"[ERROR] 전략 수립 실패: {e}")
            result = {
                "error": str(e),
                "plan": {"sells": [], "buys": [], "summary": f"실행 실패: {e}"},
                "results": [],
            }
        result["log"] = "\n".join(self._log_lines)
        self._save_history(result)
        return result

    def _append_log(self, msg: str) -> None:
        ts = datetime.now(KST).strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self._log_lines.append(line)
        log.info("strategy_planner.step", msg=msg)

    # ── 내부 파이프라인 ───────────────────────────────────────────────────

    def _pipeline(self) -> dict[str, Any]:
        from moppu.agent.executor import TradeExecutor

        # 1. 현재 포트폴리오 조회
        self._append_log("[1/6] 포트폴리오 조회 중...")
        positions = self._broker.get_positions()
        cash = self._broker.get_cash_balance_krw()
        portfolio_text = _format_portfolio(positions, cash)
        self._append_log(f"  보유 종목 {len(positions)}개, 예수금 {cash:,.0f}원")
        log.info("strategy_planner.portfolio_loaded", n_positions=len(positions), cash_krw=cash)

        # 2. LSY Turn 1 — 섹터 분석
        self._append_log("[2/6] LSY Turn 1 — 섹터 분석 요청...")
        history: list[dict[str, str]] = []
        sector_prompt = (
            f"[현재 포트폴리오]\n{portfolio_text}\n\n"
            "위 포트폴리오를 분석해주세요:\n"
            "1. 현재 보유 섹터별 긍정/부정 요인\n"
            "2. 비중을 늘려야 할 섹터 (이유 포함)\n"
            "3. 정리 또는 축소해야 할 섹터/종목 (이유 포함)\n"
            "4. 신규 편입을 고려할 섹터\n"
            "5. 전체 시장 전망의 강경도 (1-10, 10이 가장 강경한 매수 신호)\n"
        )
        sector_result = self._trader.chat(sector_prompt, history=history)
        sector_analysis = sector_result["text"]
        history.append({"role": "user", "content": sector_prompt})
        history.append({"role": "assistant", "content": sector_analysis})
        # LSY 강경도 추출 (1-10)
        self._lsy_conviction = _extract_conviction(sector_analysis)
        self._append_log(f"  섹터 분석 완료 (강경도={self._lsy_conviction}/10)")
        log.info("strategy_planner.sector_analysis_done", conviction=self._lsy_conviction)

        # usage 누적 (비용 집계용)
        _acc_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

        def _add_usage(u: dict) -> None:
            _acc_usage["input_tokens"]  += u.get("input_tokens",  0)
            _acc_usage["output_tokens"] += u.get("output_tokens", 0)

        _add_usage(sector_result.get("usage") or {})

        # 3. LSY Turn 2 — 구체적 종목 후보
        self._append_log("[3/6] LSY Turn 2 — 종목 후보 요청...")
        candidate_prompt = (
            "위 분석에서 추천한 섹터의 구체적인 매수 종목 후보를 5개 이내로 추천해주세요.\n"
            "각 종목: 종목명 (종목코드: 6자리숫자) 형식으로 작성하고, 추천 이유를 함께 써주세요.\n"
            "매도 검토 종목도 같은 형식으로 명시해주세요.\n"
            "각 종목별 확신도(conviction)를 1-10으로 명시해주세요."
        )
        candidate_result = self._trader.chat(candidate_prompt, history=history)
        candidate_text = candidate_result["text"]
        history.append({"role": "user", "content": candidate_prompt})
        history.append({"role": "assistant", "content": candidate_text})
        _add_usage(candidate_result.get("usage") or {})
        self._append_log("  종목 후보 수신")

        # 4. LSY Turn 3 — 종목 코드 JSON 추출
        self._append_log("[4/6] LSY Turn 3 — 종목 코드 추출...")
        ticker_result = self._trader.chat(_TICKER_EXTRACT_PROMPT, history=history)
        tickers = _parse_ticker_json(ticker_result["text"])
        _add_usage(ticker_result.get("usage") or {})
        buy_tickers: list[str] = tickers.get("buy", [])
        sell_tickers_from_lsy: list[str] = tickers.get("sell", [])
        self._append_log(f"  매수 후보 {len(buy_tickers)}개, 매도 검토 {len(sell_tickers_from_lsy)}개")

        # 5. 시세 조회 (현재 보유 + 신규 후보)
        all_tickers = list(set(buy_tickers + [p.ticker for p in positions]))
        self._append_log(f"[5/6] 시세 조회 ({len(all_tickers)}종목)...")
        quotes = self._fetch_quotes(all_tickers)

        # 6. 전략 수립가 LLM — 최종 계획 수립
        self._append_log("[6/6] 최종 계획 수립 (LLM)...")
        plan, plan_usage = self._build_plan(
            portfolio_text=portfolio_text,
            sector_analysis=sector_analysis,
            candidate_text=candidate_text,
            sell_tickers_hint=sell_tickers_from_lsy,
            quotes=quotes,
            cash=cash,
            positions=positions,
        )
        _add_usage(plan_usage)
        self._append_log(f"  매도 {len(plan.sells)}건 / 매수 {len(plan.buys)}건 / 강경도 {self._lsy_conviction}/10")

        # 7. 자금 요청 (부족 시)
        if plan.needs_additional_krw > 0:
            self._append_log(f"  자금 부족 {plan.needs_additional_krw:,.0f}원 — 텔레그램 요청")
            cash = self._handle_fund_request(plan, cash)
            self._append_log(f"  재확인 예수금: {cash:,.0f}원")
            plan = self._adjust_plan_to_budget(plan, cash, quotes, positions)
            self._append_log(f"  예산 내 조정됨 — 매수 {len(plan.buys)}건")

        # 8. 실행 위임
        self._append_log(f"  실행 중 (dry_run={self._cfg.dry_run})...")
        exec_started_at = datetime.now(KST)
        executor = TradeExecutor(broker=self._broker, dry_run=self._cfg.dry_run)
        results = executor.execute(plan, positions)
        ok = sum(1 for r in results if r.get("status") == "ok")
        rej = sum(1 for r in results if r.get("status") == "rejected")
        skp = sum(1 for r in results if r.get("status") == "skip")
        err = sum(1 for r in results if r.get("status") == "error")
        parts = [f"성공 {ok}건"]
        if rej: parts.append(f"거부 {rej}건")
        if skp: parts.append(f"스킵 {skp}건")
        if err: parts.append(f"실패 {err}건")
        self._append_log("  실행 완료 — " + ", ".join(parts))
        for r in results:
            if r.get("status") in ("rejected", "skip", "error"):
                msg = r.get("message") or r.get("reason") or r.get("error") or ""
                self._append_log(f"    · {r.get('action')} {r.get('ticker')}: {msg}")

        # 9. 체결 검증 + 미체결 재시도 (dry_run=False, ok 주문이 있을 때만)
        verification: list[dict[str, Any]] = []
        if not self._cfg.dry_run and any(r.get("status") == "ok" for r in results):
            verification = self._verify_and_retry(results, exec_started_at)

        # 10. Telegram 완료 알림
        self._notify_completion(plan, results, verification)
        self._append_log("=== 전략 수립 완료 ===")

        return {
            "plan": plan.model_dump(),
            "results": results,
            "verification": verification,
            "usage": _acc_usage,
            "provider": self._llm.name,
            "model": self._llm.model,
            "conviction": self._lsy_conviction,
        }

    # ── 계획 수립 ─────────────────────────────────────────────────────────

    def _fetch_quotes(self, tickers: list[str]) -> dict[str, float]:
        quotes: dict[str, float] = {}
        for ticker in tickers:
            try:
                q = self._broker.get_quote(ticker)
                quotes[ticker] = q.price
            except Exception as e:
                log.warning("strategy_planner.quote_failed", ticker=ticker, err=str(e))
        return quotes

    def _build_plan(
        self,
        *,
        portfolio_text: str,
        sector_analysis: str,
        candidate_text: str,
        sell_tickers_hint: list[str],
        quotes: dict[str, float],
        cash: float,
        positions: list[Position],
    ) -> tuple[TradePlan, dict[str, int]]:
        quotes_text = (
            "\n".join(f"- {t}: {p:,.0f}원" for t, p in quotes.items())
            or "(시세 없음)"
        )
        hint_text = (
            f"\nLSY가 매도 검토로 언급한 종목코드: {', '.join(sell_tickers_hint)}"
            if sell_tickers_hint else ""
        )

        schema_example = json.dumps(
            {
                "sells": [{"ticker": "005930", "quantity": -1, "reason": "..."}],
                "buys": [{"ticker": "000660", "quantity": 5, "price": 120000.0, "reason": "..."}],
                "summary": "...",
                "sectors_to_add": ["반도체"],
                "sectors_to_reduce": ["은행"],
            },
            ensure_ascii=False,
            indent=2,
        )

        conviction = getattr(self, "_lsy_conviction", 5)
        user_prompt = (
            f"## LSY 강경도\n{conviction}/10\n\n"
            f"## 현재 포트폴리오\n{portfolio_text}\n\n"
            f"## 가용 현금\n{cash:,.0f}원\n\n"
            f"## LSY 섹터 분석\n{sector_analysis[:2000]}\n\n"
            f"## LSY 종목 추천\n{candidate_text[:1500]}{hint_text}\n\n"
            f"## 종목별 현재 시세\n{quotes_text}\n\n"
            f"LSY 강경도 {conviction}/10 에 따른 계획 수립 가이드를 따라주세요.\n"
            "매도 후 확보 자금도 매수에 활용할 수 있습니다.\n"
            f"JSON 예시:\n{schema_example}"
        )

        resp = self._llm.chat(
            messages=[ChatMessage(role="user", content=user_prompt)],
            system=_STRATEGY_SYSTEM,
            temperature=0.1,
            max_tokens=3000,
        )
        log.info("strategy_planner.llm_plan_done", provider=resp.provider, usage=resp.usage)

        usage: dict[str, int] = dict(resp.usage or {})
        plan = _parse_plan(resp.text)

        # 1차 파싱 실패 시 — 깨진 응답을 보여주며 1회 재요청
        if plan is None:
            self._append_log("  [재시도] JSON 파싱 실패 — LLM에 재요청")
            log.warning("strategy_planner.plan_retry")
            retry_prompt = (
                "직전에 출력한 아래 JSON 이 파싱에 실패했습니다. "
                "동일한 내용을 유효한 JSON 객체 하나로만 다시 출력하세요.\n"
                "- 코드 블록·주석·설명 없이 순수 JSON 만\n"
                "- 숫자에 자릿수 구분자(_)나 콤마 금지 (예: 61000.0)\n"
                "- 문자열 안에서는 큰따옴표(\") 대신 작은따옴표(') 사용\n"
                "- 배열·객체 마지막 항목 뒤 쉼표 금지\n\n"
                f"[직전 응답]\n{resp.text}"
            )
            resp2 = self._llm.chat(
                messages=[ChatMessage(role="user", content=retry_prompt)],
                system=_STRATEGY_SYSTEM,
                temperature=0.0,
                max_tokens=3000,
            )
            for k, v in (resp2.usage or {}).items():
                usage[k] = usage.get(k, 0) + v
            plan = _parse_plan(resp2.text)

        if plan is None:
            log.error("strategy_planner.plan_retry_failed")
            self._append_log("  [실패] 재시도 후에도 JSON 파싱 실패 — 빈 계획으로 종료")
            plan = TradePlan(summary="계획 파싱 실패 (재시도 후에도 실패)")

        # needs_additional_krw 계산
        sell_proceeds = _estimate_sell_proceeds(plan.sells, quotes, positions)
        plan.total_sell_krw = sell_proceeds
        plan.total_buy_krw = sum(b.total_krw for b in plan.buys)
        shortfall = plan.total_buy_krw - cash - sell_proceeds
        plan.needs_additional_krw = max(0.0, shortfall)

        return plan, usage

    # ── 자금 요청 ─────────────────────────────────────────────────────────

    def _handle_fund_request(self, plan: TradePlan, current_cash: float) -> float:
        """추가 자금 요청. LSY 강경도에 따라 메시지 톤을 조절.

        - 강경도 8+ : 강하게 졸라 (즉시 이체 요청)
        - 강경도 5-7: 권유 (이체 고려 부탁)
        - 강경도 1-4: 소극 (예산 내 조정 제안)
        """
        from moppu.bot.telegram_bot import send_telegram_message

        conviction = getattr(self, "_lsy_conviction", 5)

        # 포트폴리오 현황
        try:
            positions = self._broker.get_positions()
        except Exception:
            positions = []
        pf_lines = []
        if positions:
            pf_lines.append("*현재 포트폴리오*")
            for p in positions[:10]:
                label = f"{p.name}({p.ticker})" if p.name else p.ticker
                pl_pct = ((p.unrealized_pl or 0) / (p.avg_price * p.quantity) * 100) if p.avg_price * p.quantity > 0 else 0
                pf_lines.append(f"  • {label}: {p.quantity}주, 손익 {pl_pct:+.1f}%")

        buy_lines = []
        for b in plan.buys[:8]:
            buy_lines.append(f"  • {b.ticker}: {b.quantity}주 × {b.price:,.0f}원 — {b.reason[:50]}")

        # 톤 결정
        if conviction >= 8:
            tone_head = "🔥 *긴급 — LSY 강력 매수 신호 (강경도 {conv}/10)*"
            tone_ask = (
                "LSY 애널리스트가 *매우 강하게* 매수를 권고하고 있습니다.\n"
                "*지금 이체해서라도 편입하는 것이 합리적*이라는 판단입니다.\n"
                "가능하시면 *{shortfall:,.0f}원* 이체 부탁드립니다."
            )
        elif conviction >= 5:
            tone_head = "💰 *자금 요청 (LSY 강경도 {conv}/10)*"
            tone_ask = (
                "LSY 추천에 따라 매수를 검토합니다.\n"
                "추가 이체 *{shortfall:,.0f}원* 이 가능하시면 계획대로 집행하고,\n"
                "어려우시면 보유 자금 내에서 축소 집행합니다."
            )
        else:
            tone_head = "📝 *자금 부족 알림 (LSY 강경도 {conv}/10)*"
            tone_ask = (
                "LSY 의견이 강하지 않아 무리한 이체는 권장하지 않습니다.\n"
                "보유 자금 내에서 축소 집행하거나, *{shortfall:,.0f}원* 이체도 가능합니다."
            )

        header = tone_head.format(conv=conviction)
        ask = tone_ask.format(shortfall=plan.needs_additional_krw)

        parts = [
            header,
            "",
            f"가용 현금: {current_cash:,.0f}원",
            f"예상 매도 확보: {plan.total_sell_krw:,.0f}원",
            f"매수 예정 총액: {plan.total_buy_krw:,.0f}원",
            f"*부족 금액: {plan.needs_additional_krw:,.0f}원*",
            "",
        ]
        if pf_lines:
            parts.extend(pf_lines)
            parts.append("")
        if buy_lines:
            parts.append("*매수 계획*")
            parts.extend(buy_lines)
            parts.append("")
        parts.append(ask)
        parts.append("")
        parts.append(f"⏱ {self._cfg.fund_request_wait_min}분 후 잔고 재확인합니다.")

        send_telegram_message(self._settings, "\n".join(parts))
        log.info(
            "strategy_planner.fund_request_sent",
            shortfall=plan.needs_additional_krw,
            conviction=conviction,
        )

        time.sleep(self._cfg.fund_request_wait_min * 60)

        new_cash = self._broker.get_cash_balance_krw()
        log.info("strategy_planner.fund_recheck", before=current_cash, after=new_cash)

        # 강경도 8+이고 이체가 안됐으면 보유 자금 정리 제안을 추가로 보냄
        if conviction >= 8 and new_cash <= current_cash + 1000:
            follow_up = [
                "⚠️ *이체 미확인 — 자산 정리 제안*",
                "",
                f"LSY 강경도 {conviction}/10 기준, 편입 기회를 놓치는 것보다는",
                "*기존 보유 중 비중이 낮거나 손익이 둔한 종목을 정리*해서라도",
                "신규 편입 자금을 확보하는 편이 합리적입니다.",
                "",
                "자동 조정 로직이 가용 자금 내에서 최선을 다합니다.",
            ]
            send_telegram_message(self._settings, "\n".join(follow_up))
            log.info("strategy_planner.conviction_follow_up_sent", conviction=conviction)

        return new_cash

    def _adjust_plan_to_budget(
        self,
        plan: TradePlan,
        available_cash: float,
        quotes: dict[str, float],
        positions: list[Position],
    ) -> TradePlan:
        """가용 자금 내로 매수 계획을 조정합니다."""
        sell_proceeds = _estimate_sell_proceeds(plan.sells, quotes, positions)
        budget = available_cash + sell_proceeds

        adjusted_buys: list[BuyInstruction] = []
        remaining = budget
        for buy in plan.buys:
            if remaining <= 0:
                break
            cost = buy.total_krw
            if remaining >= cost:
                adjusted_buys.append(buy)
                remaining -= cost
            elif remaining >= buy.price:
                max_qty = int(remaining // buy.price)
                adjusted_buys.append(BuyInstruction(
                    ticker=buy.ticker,
                    quantity=max_qty,
                    price=buy.price,
                    reason=buy.reason + f" (예산 내 조정 {max_qty}주)",
                ))
                remaining -= buy.price * max_qty

        total_buy = sum(b.total_krw for b in adjusted_buys)
        return TradePlan(
            sells=plan.sells,
            buys=adjusted_buys,
            summary=plan.summary + f"\n※ 가용 예산({budget:,.0f}원) 내로 조정됨",
            sectors_to_add=plan.sectors_to_add,
            sectors_to_reduce=plan.sectors_to_reduce,
            total_sell_krw=plan.total_sell_krw,
            total_buy_krw=total_buy,
            needs_additional_krw=0.0,
        )

    # ── 체결 검증 + 미체결 재시도 ────────────────────────────────────────
    #
    # `place_order(rt_cd=0)` 는 주문 *접수* 일 뿐 체결이 아니다. 시가 매수가
    # LP 부재/거래정지/동시호가 적체 등으로 체결되지 않는 경우가 실제로
    # 관찰됐다 → 일정 시간 대기 후 KIS 일별 주문/체결 조회로 ODNO 단위
    # 체결량을 확인하고, 부족분만 시장가로 재주문한다.

    def _verify_and_retry(
        self,
        exec_results: list[dict[str, Any]],
        exec_started_at: datetime,
    ) -> list[dict[str, Any]]:
        """매매 실행 결과를 검증하고 미체결 분량을 재시도한다.

        Returns: 각 원 주문에 대한 최종 검증 결과 리스트.
        """
        # 추적 대상: status='ok' 였던 주문들. ODNO 기준 매칭.
        tracked: list[dict[str, Any]] = []
        for r in exec_results:
            if r.get("status") != "ok":
                continue
            odno = str(r.get("odno") or "")
            if not odno:
                # ODNO 없으면 매칭 불가 — 추적 제외하고 그냥 ok 로 기록만
                self._append_log(
                    f"  [검증] ODNO 누락으로 추적 불가: {r.get('action')} {r.get('ticker')}"
                )
                continue
            tracked.append({
                "action": r.get("action"),
                "ticker": r.get("ticker"),
                "ordered": int(r.get("qty") or 0),
                "odnos": [odno],          # 재시도 시 누적
                "filled": 0,
                "retries": 0,
                "final_status": "pending",
            })

        if not tracked:
            self._append_log("  [검증] 추적할 ok 주문 없음 — 검증 단계 스킵")
            return []

        wait_min = max(0, int(self._cfg.verify_wait_min))
        max_retries = max(0, int(self._cfg.verify_max_retries))
        attempt = 0

        while attempt <= max_retries:
            self._append_log(
                f"  [검증 {attempt}/{max_retries}] {wait_min}분 대기 후 체결 조회..."
            )
            if wait_min > 0:
                time.sleep(wait_min * 60)

            fill_by_odno = self._collect_fills_by_odno()
            today_date = datetime.now(KST).strftime("%Y%m%d")

            # 각 추적 항목의 누적 체결량 갱신
            pending_after: list[dict[str, Any]] = []
            for item in tracked:
                if item["final_status"] == "filled":
                    continue
                filled_total = 0
                any_cancelled = False
                for od in item["odnos"]:
                    f = fill_by_odno.get(od)
                    if f is None:
                        continue
                    # 오늘자 주문만 — 휴장/날짜 경계 보호
                    if f.order_date and f.order_date != today_date:
                        continue
                    filled_total += int(f.filled_qty or 0)
                    if f.status == "cancelled":
                        any_cancelled = True
                item["filled"] = filled_total
                missing = max(0, int(item["ordered"]) - filled_total)
                if missing == 0:
                    item["final_status"] = "filled"
                else:
                    item["_missing"] = missing
                    item["_cancelled_seen"] = any_cancelled
                    pending_after.append(item)

            if not pending_after:
                self._append_log("  [검증] 모든 주문 체결 완료")
                break

            # 재시도 한도 도달했으면 미체결 그대로 종료
            if attempt == max_retries:
                for item in pending_after:
                    item["final_status"] = (
                        "partial" if item["filled"] > 0 else "unfilled"
                    )
                self._append_log(
                    f"  [검증] 재시도 한도 도달 — 미체결 {len(pending_after)}건"
                )
                break

            # 미체결 분량 재주문
            self._append_log(
                f"  [재시도 {attempt + 1}/{max_retries}] 미체결 {len(pending_after)}건 재주문..."
            )
            for item in pending_after:
                missing = int(item.get("_missing") or 0)
                if missing <= 0:
                    continue
                side = item["action"]
                ticker = item["ticker"]
                qty = missing

                # 매수는 예수금 캡 다시 확인 — 1차 주문이 일부 체결됐다면
                # 예수금이 부족할 수 있음.
                if side == "BUY":
                    try:
                        max_qty = self._broker.get_max_buy_qty(ticker, market=True)
                    except Exception as e:
                        log.warning(
                            "strategy_planner.retry_psbl_failed", ticker=ticker, err=str(e)
                        )
                        max_qty = qty
                    if max_qty <= 0:
                        self._append_log(
                            f"    · BUY {ticker} {missing}주 재시도 스킵 — 예수금 부족"
                        )
                        item["final_status"] = "partial" if item["filled"] > 0 else "unfilled"
                        item["retries"] += 1
                        continue
                    if qty > max_qty:
                        qty = max_qty

                order = Order(
                    ticker=ticker,
                    side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                    quantity=qty,
                    order_type="market",
                )
                ack_result = self._place_retry(order)
                item["retries"] += 1
                if ack_result.get("status") == "ok":
                    new_odno = str(ack_result.get("odno") or "")
                    if new_odno:
                        item["odnos"].append(new_odno)
                    self._append_log(
                        f"    · {side} {ticker} {qty}주 재주문 접수 (ODNO={new_odno})"
                    )
                else:
                    why = (
                        ack_result.get("message")
                        or ack_result.get("error")
                        or ack_result.get("reason")
                        or "사유 없음"
                    )
                    self._append_log(
                        f"    · {side} {ticker} {qty}주 재주문 실패: {why}"
                    )

            attempt += 1

        # 정리 — 내부 키 제거
        for item in tracked:
            item.pop("_missing", None)
            item.pop("_cancelled_seen", None)

        return tracked

    def _collect_fills_by_odno(self) -> dict[str, Any]:
        """오늘자 일별 체결 조회 → {ODNO: TradeFill} 맵."""
        try:
            fills = self._broker.get_daily_trades(days=1)
        except Exception as e:
            self._append_log(f"  [검증] 체결 조회 실패: {e}")
            log.warning("strategy_planner.verify_daily_ccld_failed", err=str(e))
            return {}
        out: dict[str, Any] = {}
        for f in fills:
            odno = getattr(f, "order_id", "") or ""
            if not odno:
                continue
            # 동일 ODNO 가 여러 행으로 분산되는 경우(부분체결 누적)는 KIS 응답상
            # 합산되어 있으므로 단일 행 매칭으로 충분. 만약 들어오면 최신을
            # 그대로 사용 (역순 정렬이므로 첫 항목).
            if odno not in out:
                out[odno] = f
        return out

    def _place_retry(self, order: Order) -> dict[str, Any]:
        """재주문 — executor._place 와 동일 로직(broker 직접 호출)."""
        try:
            ack = self._broker.place_order(order)
            if ack.status != "0":
                raw = ack.raw or {}
                msg = str(raw.get("msg1") or "").strip() or "주문 거부"
                log.warning(
                    "strategy_planner.retry_rejected",
                    side=order.side.value,
                    ticker=order.ticker,
                    qty=order.quantity,
                    rt_cd=ack.status,
                    msg=msg,
                )
                return {"status": "rejected", "message": msg}
            log.info(
                "strategy_planner.retry_placed",
                side=order.side.value,
                ticker=order.ticker,
                qty=order.quantity,
                odno=ack.kis_odno,
            )
            return {"status": "ok", "odno": ack.kis_odno, "order_id": ack.order_id}
        except Exception as e:
            log.error(
                "strategy_planner.retry_failed",
                side=order.side.value, ticker=order.ticker, err=str(e),
            )
            return {"status": "error", "error": str(e)}

    # ── 이력 저장 ─────────────────────────────────────────────────────────

    def _save_history(self, result: dict[str, Any]) -> None:
        if not self._data_dir:
            return
        from pathlib import Path
        hist_dir = Path(self._data_dir) / "strategy_history"
        hist_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(KST).strftime("%Y-%m-%d_%H-%M-%S")
        path = hist_dir / f"{ts}.json"
        try:
            payload = {
                "run_at": datetime.now(KST).isoformat(),
                "dry_run": self._cfg.dry_run,
                **result,
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            log.info("strategy_planner.history_saved", path=str(path))
            # 로그 파일 동시 저장 (실행 로그 상세 팝업용)
            log_text = result.get("log") or ""
            if log_text:
                log_path = hist_dir / f"{ts}.log"
                log_path.write_text(log_text, encoding="utf-8")
        except Exception as e:
            log.warning("strategy_planner.history_save_failed", err=str(e))

    # ── 완료 알림 ─────────────────────────────────────────────────────────

    def _notify_completion(
        self,
        plan: TradePlan,
        results: list[dict],
        verification: list[dict] | None = None,
    ) -> None:
        from moppu.bot.telegram_bot import send_telegram_message

        def _fmt(ticker: str) -> str:
            name = _ticker_name_from_broker(self._broker, ticker)
            return f"{name}({ticker})" if name else ticker

        sell_lines = (
            "\n".join(
                f"  - {_fmt(s.ticker)} {'전량' if s.is_full else str(s.quantity) + '주'} 매도"
                for s in plan.sells
            ) or "  없음"
        )
        buy_lines = (
            "\n".join(
                f"  - {_fmt(b.ticker)} {b.quantity}주 × {b.price:,.0f}원"
                for b in plan.buys
            ) or "  없음"
        )

        executed = sum(1 for r in results if r.get("status") == "ok")
        rejected = sum(1 for r in results if r.get("status") == "rejected")
        skipped = sum(1 for r in results if r.get("status") == "skip")
        failed = sum(1 for r in results if r.get("status") == "error")
        dry = self._cfg.dry_run

        mode_str = "🔵 DRY RUN" if dry else "🟢 실행완료"
        if not dry:
            cnt_parts = [f"실행 {executed}건"]
            if rejected: cnt_parts.append(f"거부 {rejected}건")
            if skipped:  cnt_parts.append(f"스킵 {skipped}건")
            if failed:   cnt_parts.append(f"실패 {failed}건")
            tail = "\n" + " / ".join(cnt_parts)
        else:
            tail = ""

        # 거부/스킵/실패 사유 상세
        problem_lines: list[str] = []
        for r in results:
            st = r.get("status")
            if st not in ("rejected", "skip", "error"):
                continue
            tag = {"rejected": "❌ 거부", "skip": "⏭️ 스킵", "error": "⚠️ 실패"}[st]
            why = r.get("message") or r.get("reason") or r.get("error") or "사유 없음"
            problem_lines.append(f"  {tag} {r.get('action')} {_fmt(r.get('ticker',''))}: {why}")
        problem_block = ("\n\n*거부·스킵 사유*\n" + "\n".join(problem_lines)) if problem_lines else ""

        # 체결 검증 결과 섹션
        verify_block = ""
        v_filled = v_partial = v_unfilled = 0
        if verification:
            v_lines: list[str] = []
            for v in verification:
                fs = v.get("final_status")
                ticker = v.get("ticker") or ""
                ordered = int(v.get("ordered") or 0)
                filled = int(v.get("filled") or 0)
                retries = int(v.get("retries") or 0)
                action = v.get("action") or ""
                retry_tag = f" (재시도 {retries}회)" if retries else ""
                if fs == "filled":
                    v_filled += 1
                    v_lines.append(
                        f"  ✅ {action} {_fmt(ticker)} {filled}주 체결{retry_tag}"
                    )
                elif fs == "partial":
                    v_partial += 1
                    v_lines.append(
                        f"  ⚠️ {action} {_fmt(ticker)} 부분체결 {filled}/{ordered}주{retry_tag}"
                    )
                else:  # unfilled, pending
                    v_unfilled += 1
                    v_lines.append(
                        f"  ❌ {action} {_fmt(ticker)} 미체결 0/{ordered}주{retry_tag}"
                    )
            head_parts = [f"체결 {v_filled}건"]
            if v_partial:
                head_parts.append(f"부분 {v_partial}건")
            if v_unfilled:
                head_parts.append(f"미체결 {v_unfilled}건")
            verify_block = "\n\n*체결 검증* — " + " / ".join(head_parts) + "\n" + "\n".join(v_lines)

        msg = (
            f"*[전략 수립가] {mode_str}*\n\n"
            f"📋 {plan.summary[:400]}\n\n"
            f"📉 *매도*\n{sell_lines}\n\n"
            f"📈 *매수*\n{buy_lines}\n\n"
            f"예상 매도: {plan.total_sell_krw:,.0f}원\n"
            f"예상 매수: {plan.total_buy_krw:,.0f}원"
            f"{tail}"
            f"{problem_block}"
            f"{verify_block}"
        )
        send_telegram_message(self._settings, msg)
        log.info(
            "strategy_planner.notify_sent",
            dry_run=dry, executed=executed, rejected=rejected, skipped=skipped, failed=failed,
            verify_filled=v_filled, verify_partial=v_partial, verify_unfilled=v_unfilled,
        )


# ── 유틸 함수 ─────────────────────────────────────────────────────────────────

def _ticker_name_from_broker(broker: Any, ticker: str) -> str | None:
    """KISBroker에 get_stock_name이 있으면 호출, 없으면 None."""
    fn = getattr(broker, "get_stock_name", None)
    if fn is None:
        return None
    try:
        return fn(ticker)
    except Exception:
        return None


def _format_portfolio(positions: list[Position], cash: float) -> str:
    lines = [f"가용 현금: {cash:,.0f}원", ""]
    if not positions:
        lines.append("보유 종목 없음")
        return "\n".join(lines)
    lines.append("보유 종목:")
    for p in positions:
        pl = f"{p.unrealized_pl:+,.0f}원" if p.unrealized_pl is not None else "N/A"
        label = f"{p.name}({p.ticker})" if p.name else p.ticker
        lines.append(
            f"  {label}: {p.quantity}주 × {p.avg_price:,.0f}원 (평균단가) | 평가손익 {pl}"
        )
    return "\n".join(lines)


def _parse_ticker_json(text: str) -> dict[str, list[str]]:
    """LSY의 응답에서 {"buy": [...], "sell": [...]} JSON을 파싱합니다."""
    candidate = _strip_code_fences(text).strip()
    try:
        data = json.loads(candidate)
        return {
            "buy": [str(t) for t in data.get("buy", [])],
            "sell": [str(t) for t in data.get("sell", [])],
        }
    except Exception:
        # JSON 파싱 실패 시 6자리 숫자 패턴으로 fallback 추출
        tickers = re.findall(r'\b(\d{6})\b', text)
        return {"buy": list(dict.fromkeys(tickers)), "sell": []}


def _repair_json(s: str) -> str:
    """LLM이 흔히 내는 JSON 표준 위반을 보수적으로 복구합니다.

    - 숫자 자릿수 구분자 제거: 61_000.0 → 61000.0 (Python 리터럴이라 LLM이 종종 출력)
    - trailing comma 제거: [.., ] / {.., } → [..] / {..}

    문자열 리터럴 내부에도 적용될 수 있으나, 위 두 패턴은 한국어 reason
    텍스트에 사실상 나타나지 않으므로 부작용은 무시할 수준입니다. 1차
    파싱이 실패했을 때에만 호출됩니다.
    """
    s = re.sub(r"(?<=\d)_(?=\d)", "", s)        # 61_000 → 61000
    s = re.sub(r",\s*([}\]])", r"\1", s)         # trailing comma 제거
    return s


def _loads_with_repair(candidate: str, *, raw: str) -> dict[str, Any] | None:
    """json.loads — 1차 실패 시 흔한 LLM JSON 오류를 복구하고 재시도합니다."""
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as first_err:
        repaired = _repair_json(candidate)
        if repaired != candidate:
            try:
                data = json.loads(repaired)
                log.info("strategy_planner.plan_json_repaired", err=str(first_err))
                return data
            except json.JSONDecodeError:
                pass
        log.error("strategy_planner.plan_parse_failed", err=str(first_err), raw=raw[:2000])
        return None


def _parse_plan(text: str) -> TradePlan | None:
    """전략 수립가 LLM 응답에서 TradePlan을 파싱합니다. 실패 시 None 을 반환합니다."""
    candidate = _strip_code_fences(text).strip()

    # JSON 블록 추출 시도
    if not candidate.startswith("{"):
        match = re.search(r'\{[\s\S]*\}', candidate)
        candidate = match.group() if match else "{}"

    data = _loads_with_repair(candidate, raw=text)
    if data is None:
        return None

    try:
        sells = [SellInstruction(**s) for s in data.get("sells", [])]
        buys = [BuyInstruction(**b) for b in data.get("buys", [])]
    except Exception as e:
        log.error("strategy_planner.plan_model_failed", err=str(e), raw=text[:2000])
        return None

    return TradePlan(
        sells=sells,
        buys=buys,
        summary=data.get("summary", ""),
        sectors_to_add=data.get("sectors_to_add", []),
        sectors_to_reduce=data.get("sectors_to_reduce", []),
    )


def _estimate_sell_proceeds(
    sells: list[SellInstruction],
    quotes: dict[str, float],
    positions: list[Position],
) -> float:
    pos_map = {p.ticker: p for p in positions}
    total = 0.0
    for sell in sells:
        price = quotes.get(sell.ticker, 0.0)
        if sell.is_full:
            pos = pos_map.get(sell.ticker)
            qty = pos.quantity if pos else 0
        else:
            qty = sell.quantity
        total += price * qty
    return total


def _extract_conviction(text: str) -> int:
    """LSY 응답에서 강경도(1-10)를 추출. 실패 시 5(중립) 반환."""
    patterns = [
        r"강경도\s*[=:]\s*(\d+)",
        r"확신도\s*[=:]\s*(\d+)",
        r"conviction\s*[=:]\s*(\d+)",
        r"강경도는?\s*(\d+)",
        r"(\d+)\s*/\s*10",
        r"(\d+)\s*점",
    ]
    for p in patterns:
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            try:
                v = int(m.group(1))
                return max(1, min(10, v))
            except (TypeError, ValueError):
                continue
    # 키워드 기반 fallback
    strong_signals = sum(text.count(k) for k in ["강력", "강세", "적극", "확신", "매수 추천"])
    weak_signals = sum(text.count(k) for k in ["신중", "관망", "조심", "하락", "약세"])
    if strong_signals > weak_signals + 2:
        return 8
    if weak_signals > strong_signals + 2:
        return 3
    return 5


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
