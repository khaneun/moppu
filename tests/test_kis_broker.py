"""KISBroker 토큰 라이프사이클 / 인증 실패 복구 테스트.

네트워크를 타지 않도록 httpx.MockTransport 로 KIS 응답을 흉내낸다.
"""

from __future__ import annotations

import json

import httpx

from moppu.broker.kis import KISBroker
from moppu.config import KISBrokerConfig, Settings


def _make_broker(handler, tmp_path=None) -> KISBroker:
    cfg = KISBrokerConfig()
    settings = Settings(
        kis_env="real",
        kis_app_key="ak",
        kis_app_secret="as",
        kis_account_no="6793195101",
    )
    token_cache = (tmp_path / ".kis_token.json") if tmp_path else None
    broker = KISBroker(cfg, settings, token_cache_path=token_cache)
    broker._client = httpx.Client(
        base_url=broker._base_url, transport=httpx.MockTransport(handler)
    )
    return broker


def _balance_ok_body() -> dict:
    return {"rt_cd": "0", "output1": [], "output2": [{"dnca_tot_amt": "12345"}]}


def test_rate_limit_500_retries_same_token(tmp_path, monkeypatch):
    """초당 거래건수 초과(EGW00201, HTTP 500) → 토큰 재발급 없이 백오프 재시도."""
    monkeypatch.setattr("moppu.broker.kis.time.sleep", lambda *_: None)
    state = {"token_seq": 0, "balance_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            state["token_seq"] += 1
            return httpx.Response(
                200,
                json={"access_token": f"tok{state['token_seq']}", "expires_in": 86400},
            )
        if request.url.path.endswith("/inquire-balance"):
            state["balance_calls"] += 1
            # 첫 호출만 레이트리밋(500), 이후 정상.
            if state["balance_calls"] == 1:
                return httpx.Response(
                    500,
                    json={
                        "rt_cd": "1",
                        "msg_cd": "EGW00201",
                        "msg1": "원장에서 허용 가능한 초당 거래건수를 초과하였습니다.",
                    },
                )
            return httpx.Response(200, json=_balance_ok_body())
        return httpx.Response(404)

    broker = _make_broker(handler, tmp_path)
    summary = broker.get_account_summary()

    assert summary.cash == 12345.0
    assert state["balance_calls"] == 2, "백오프 후 같은 토큰으로 1회 재시도"
    assert state["token_seq"] == 1, "레이트리밋에는 토큰을 재발급하면 안 된다"


def test_expired_token_401_triggers_refresh(tmp_path, monkeypatch):
    """토큰 만료(EGW00123/401) → 토큰 재발급 후 재시도해 성공."""
    monkeypatch.setattr("moppu.broker.kis.time.sleep", lambda *_: None)
    state = {"token_seq": 0, "balance_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            state["token_seq"] += 1
            return httpx.Response(
                200,
                json={"access_token": f"tok{state['token_seq']}", "expires_in": 86400},
            )
        if request.url.path.endswith("/inquire-balance"):
            state["balance_calls"] += 1
            if "tok1" in request.headers.get("authorization", ""):
                return httpx.Response(
                    401, json={"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token"}
                )
            return httpx.Response(200, json=_balance_ok_body())
        return httpx.Response(404)

    broker = _make_broker(handler, tmp_path)
    summary = broker.get_account_summary()

    assert summary.cash == 12345.0
    assert state["token_seq"] == 2, "만료 토큰은 재발급되어야 한다"
    assert state["balance_calls"] == 2


def test_shared_token_cache_reused_across_instances(tmp_path):
    """공유 파일 캐시가 있으면 두 번째 인스턴스는 토큰을 재발급하지 않는다."""
    state = {"token_seq": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            state["token_seq"] += 1
            return httpx.Response(
                200,
                json={"access_token": f"tok{state['token_seq']}", "expires_in": 86400},
            )
        if request.url.path.endswith("/inquire-balance"):
            return httpx.Response(200, json=_balance_ok_body())
        return httpx.Response(404)

    b1 = _make_broker(handler, tmp_path)
    b1.get_account_summary()
    assert state["token_seq"] == 1

    # 새 프로세스를 흉내낸 두 번째 인스턴스 — 같은 파일 캐시를 공유.
    b2 = _make_broker(handler, tmp_path)
    b2.get_account_summary()
    assert state["token_seq"] == 1, "공유 캐시의 유효 토큰을 재발급 없이 채택해야 한다"

    cached = json.loads((tmp_path / ".kis_token.json").read_text())
    assert cached["value"] == "tok1"


def test_no_cache_path_uses_in_memory_only(tmp_path):
    """token_cache_path 미지정 시 파일을 만들지 않고 메모리 캐시만 쓴다."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 86400})
        return httpx.Response(200, json=_balance_ok_body())

    broker = _make_broker(handler, tmp_path=None)
    broker.get_account_summary()
    assert not (tmp_path / ".kis_token.json").exists()
