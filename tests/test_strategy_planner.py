"""strategy_planner JSON 파싱 / 복구 회귀 테스트.

운영 로그(moppu-scheduler)에서 관측된 실제 파싱 실패를 재현한다:
- 2026-05-21: price 값에 자릿수 구분자(`61_000.0`) → "Expecting ',' delimiter"
- 2026-05-15: line 25 구조 손상 → "Expecting property name ..."
실패 시 그날 전략 수립 전체가 빈 계획으로 끝나므로(dry_run=false 운영),
복구 + 재시도 경로를 검증한다.
"""

from moppu.agent.strategy_planner import (
    StrategyPlannerAgent,
    _parse_plan,
    _repair_json,
)
from moppu.llm.base import LLMResponse

# ── _repair_json ─────────────────────────────────────────────────────────────

def test_repair_json_digit_separator():
    assert _repair_json('{"p": 1_000_000}') == '{"p": 1000000}'
    assert _repair_json('{"price": 61_000.0}') == '{"price": 61000.0}'


def test_repair_json_trailing_comma():
    assert _repair_json("[1, 2, ]") == "[1, 2]"
    assert _repair_json('{"a": 1, }') == '{"a": 1}'


def test_repair_json_leaves_valid_untouched():
    valid = '{"buys": [1, 2], "n": 100}'
    assert _repair_json(valid) == valid


# ── _parse_plan ──────────────────────────────────────────────────────────────

def test_parse_plan_normal():
    text = (
        '{"sells": [], '
        '"buys": [{"ticker": "005930", "quantity": 5, "price": 61000.0, "reason": "ok"}], '
        '"summary": "s"}'
    )
    plan = _parse_plan(text)
    assert plan is not None
    assert plan.buys[0].ticker == "005930"
    assert plan.buys[0].price == 61000.0


def test_parse_plan_recovers_digit_separator():
    """2026-05-21 실패 재현: LLM이 price 에 자릿수 구분자(_)를 넣음."""
    text = (
        '{"sells": [], '
        '"buys": [{"ticker": "086790", "quantity": 2, "price": 61_000.0, "reason": "r"}], '
        '"summary": "s"}'
    )
    plan = _parse_plan(text)
    assert plan is not None
    assert plan.buys[0].price == 61000.0


def test_parse_plan_recovers_trailing_comma():
    text = (
        '{"sells": [], '
        '"buys": [{"ticker": "005930", "quantity": 1, "price": 100.0, "reason": "r"},], '
        '"summary": "s",}'
    )
    plan = _parse_plan(text)
    assert plan is not None
    assert len(plan.buys) == 1


def test_parse_plan_strips_code_fence():
    plan = _parse_plan('```json\n{"sells": [], "buys": [], "summary": "s"}\n```')
    assert plan is not None
    assert plan.summary == "s"


def test_parse_plan_extracts_embedded_object():
    plan = _parse_plan('다음은 계획입니다:\n{"sells": [], "buys": [], "summary": "s"}\n이상.')
    assert plan is not None


def test_parse_plan_returns_none_on_unrecoverable():
    """복구 불가능한 손상은 None — 호출부가 재시도하도록."""
    assert _parse_plan('{"sells": [{"ticker": "005930" "quantity": 1}], "buys": []}') is None


def test_parse_plan_returns_none_on_schema_mismatch():
    """JSON 으로는 파싱되나 스키마 불일치(필수 필드 누락)도 None."""
    assert _parse_plan('{"sells": [{"ticker": "005930"}], "buys": []}') is None


# ── _build_plan 재시도 경로 ──────────────────────────────────────────────────

class _SeqLLM:
    """호출 순서대로 미리 정해둔 응답 텍스트를 돌려주는 페이크 LLM."""

    name = "fake"
    model = "fake"

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.calls = 0

    def chat(self, messages, *, system=None, temperature=None,
             max_tokens=None, tools=None, **kwargs) -> LLMResponse:
        text = self._responses[self.calls]
        self.calls += 1
        return LLMResponse(
            text=text, model="fake", provider="fake",
            usage={"input_tokens": 10, "output_tokens": 20},
        )


def _bare_agent(llm: _SeqLLM) -> StrategyPlannerAgent:
    # _build_plan 은 cfg/settings/broker 를 쓰지 않으므로 최소 인스턴스만 구성
    agent = object.__new__(StrategyPlannerAgent)
    agent._llm = llm
    agent._log_lines = []
    return agent


def test_build_plan_retries_on_parse_failure():
    """1차 응답이 깨지면 재요청하고, 2차 정상 응답으로 복구한다."""
    broken = '{"sells": [], "buys": [{"ticker": "005930", "quantity": 1 "price": 100.0}]}'
    good = '{"sells": [], "buys": [], "summary": "재시도 성공"}'
    llm = _SeqLLM([broken, good])
    agent = _bare_agent(llm)

    plan, usage = agent._build_plan(
        portfolio_text="", sector_analysis="", candidate_text="",
        sell_tickers_hint=[], quotes={}, cash=0.0, positions=[],
    )

    assert llm.calls == 2
    assert plan.summary == "재시도 성공"
    # usage 는 두 호출이 누적되어야 한다
    assert usage["output_tokens"] == 40


def test_build_plan_gives_up_after_retry():
    """재시도 후에도 실패하면 빈 계획으로 안전하게 종료한다."""
    broken = '{"buys": [{"ticker": "x" "quantity": 1}]}'
    llm = _SeqLLM([broken, broken])
    agent = _bare_agent(llm)

    plan, _ = agent._build_plan(
        portfolio_text="", sector_analysis="", candidate_text="",
        sell_tickers_hint=[], quotes={}, cash=0.0, positions=[],
    )

    assert llm.calls == 2
    assert plan.buys == []
    assert "파싱 실패" in plan.summary


# ── _verify_and_retry — 미체결 재시도 경로 ──────────────────────────────────

from datetime import datetime  # noqa: E402

from moppu.agent.strategy_planner import KST  # noqa: E402
from moppu.broker.base import Order, OrderAck, TradeFill  # noqa: E402
from moppu.config import StrategyPlannerConfig  # noqa: E402


class _FakeBroker:
    """ODNO 별 체결 진행을 시뮬레이션하는 가짜 broker.

    각 호출에서 ODNO 의 filled_qty 가 호출 횟수에 따라 점진적으로 채워지도록 설정.
    """

    def __init__(self, *, fill_plan: dict[str, list[int]], next_odno_start: int = 100) -> None:
        # fill_plan[odno] = [filled_qty_at_call_1, filled_qty_at_call_2, ...]
        self._fill_plan = fill_plan
        self._orders: dict[str, dict] = {}     # ODNO → 주문 메타
        self._daily_calls = 0
        self._next_odno = next_odno_start
        self.placed: list[Order] = []

    # broker protocol
    def place_order(self, order: Order) -> OrderAck:
        self.placed.append(order)
        odno = f"ODNO{self._next_odno:04d}"
        self._next_odno += 1
        # 새 주문 — 재시도 ODNO 는 기본적으로 전량 체결 가정 (테스트에서 override 가능)
        if odno not in self._fill_plan:
            self._fill_plan[odno] = [order.quantity]
        self._orders[odno] = {
            "ticker": order.ticker,
            "side": "SELL" if order.side.value == "SELL" else "BUY",
            "quantity": order.quantity,
        }
        return OrderAck(order_id="krx-route", status="0", raw={}, kis_odno=odno)

    def get_daily_trades(self, *, ticker=None, days=30):
        self._daily_calls += 1
        idx = self._daily_calls - 1
        today = datetime.now(KST).strftime("%Y%m%d")
        fills: list[TradeFill] = []
        for odno, meta in self._orders.items():
            plan = self._fill_plan.get(odno, [])
            filled = plan[idx] if idx < len(plan) else (plan[-1] if plan else 0)
            ord_qty = meta["quantity"]
            if filled <= 0:
                status = "pending"
            elif filled >= ord_qty:
                status = "filled"
            else:
                status = "partial"
            fills.append(TradeFill(
                order_date=today, order_time="093001",
                ticker=meta["ticker"], name=None, side=meta["side"],
                quantity=ord_qty, filled_qty=filled,
                price=0.0, avg_fill_price=0.0, total_amount=0.0,
                status=status, order_id=odno,
            ))
        return fills

    def get_max_buy_qty(self, ticker, *, price=0, market=True):
        return 10_000


def _verify_agent(broker: _FakeBroker, *, wait_min: int = 0, max_retries: int = 2) -> StrategyPlannerAgent:
    agent = object.__new__(StrategyPlannerAgent)
    agent._broker = broker
    agent._log_lines = []
    agent._cfg = StrategyPlannerConfig(
        verify_wait_min=wait_min, verify_max_retries=max_retries, dry_run=False
    )
    return agent


def test_verify_filled_on_first_check():
    """첫 검증에서 전량 체결되면 재시도 없이 종료."""
    broker = _FakeBroker(fill_plan={"ODNO0100": [10]}, next_odno_start=100)
    # 외부에서 만든 ODNO 와 매칭되도록 주문을 먼저 등록
    broker._orders["ODNO0100"] = {"ticker": "005930", "side": "BUY", "quantity": 10}
    agent = _verify_agent(broker)
    results = [{"status": "ok", "action": "BUY", "ticker": "005930", "qty": 10, "odno": "ODNO0100"}]
    out = agent._verify_and_retry(results, datetime.now(KST))
    assert len(out) == 1
    assert out[0]["final_status"] == "filled"
    assert out[0]["filled"] == 10
    assert out[0]["retries"] == 0
    assert broker.placed == []   # 재주문 없음


def test_verify_partial_then_retry_fills():
    """1차 부분체결 → 재시도 1회로 완전 체결."""
    # ODNO0100: 1차 7주, 2차에도 7주 (그 이상 안 채워짐)
    # 재시도 주문(ODNO0101) 은 자동으로 전량 체결되도록 default 처리.
    broker = _FakeBroker(fill_plan={"ODNO0100": [7, 7]}, next_odno_start=101)
    broker._orders["ODNO0100"] = {"ticker": "005930", "side": "BUY", "quantity": 10}
    agent = _verify_agent(broker)
    results = [{"status": "ok", "action": "BUY", "ticker": "005930", "qty": 10, "odno": "ODNO0100"}]
    out = agent._verify_and_retry(results, datetime.now(KST))
    assert out[0]["final_status"] == "filled"
    assert out[0]["filled"] == 10            # 7(원) + 3(재시도)
    assert out[0]["retries"] == 1
    assert len(broker.placed) == 1
    assert broker.placed[0].quantity == 3    # 부족분만큼만 재주문


def test_verify_unfilled_after_max_retries():
    """재시도 한도 끝까지 체결 안 되면 unfilled 로 종료."""
    # 원본 + 모든 재시도 ODNO 가 0으로 고정
    broker = _FakeBroker(
        fill_plan={
            "ODNO0100": [0, 0, 0],
            # 재시도 주문도 0 체결 — 미리 등록해두면 _FakeBroker 가 신규 등록을 건너뜀
        },
        next_odno_start=101,
    )
    broker._orders["ODNO0100"] = {"ticker": "005930", "side": "BUY", "quantity": 10}
    # 재시도로 생성될 ODNO 들도 모두 0 체결로 강제
    broker._fill_plan["ODNO0101"] = [0, 0, 0]
    broker._fill_plan["ODNO0102"] = [0, 0, 0]
    agent = _verify_agent(broker, max_retries=2)
    results = [{"status": "ok", "action": "BUY", "ticker": "005930", "qty": 10, "odno": "ODNO0100"}]
    out = agent._verify_and_retry(results, datetime.now(KST))
    assert out[0]["final_status"] == "unfilled"
    assert out[0]["filled"] == 0
    assert out[0]["retries"] == 2
    assert len(broker.placed) == 2           # 2회 재주문


def test_verify_skips_when_no_ok_orders():
    """ok 가 하나도 없으면 빈 리스트 반환, sleep 도 호출 안 됨."""
    broker = _FakeBroker(fill_plan={})
    agent = _verify_agent(broker)
    results = [
        {"status": "rejected", "action": "BUY", "ticker": "005930", "qty": 5},
        {"status": "error", "action": "SELL", "ticker": "000660", "qty": 3},
    ]
    out = agent._verify_and_retry(results, datetime.now(KST))
    assert out == []
    assert broker._daily_calls == 0          # 체결 조회 호출 없음
