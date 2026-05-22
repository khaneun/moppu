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
