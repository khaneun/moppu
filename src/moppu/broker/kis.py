"""한국투자증권 (KIS) OpenAPI client.

Scaffold only — the KIS API has 50+ endpoints, TR codes per endpoint, and a
token lifecycle. This file sets up:

- Base-URL selection for 실전/모의투자 (real vs paper)
- OAuth token fetch + caching
- A couple of representative endpoints (place order, cash balance, quote)
  that the agent actually calls today

Extending to additional endpoints (연결 잔고, 기간별 손익, 실시간 체결통보
via WebSocket) is mostly boilerplate on top of ``_request``.

Docs: https://apiportal.koreainvestment.com/apiservice
"""

from __future__ import annotations

import json as _jsonlib
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from moppu.broker.base import AccountSummary, Order, OrderAck, OrderSide, Position, Quote, TradeFill
from moppu.config import KISBrokerConfig, Settings
from moppu.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class _Token:
    value: str
    expires_at: float


class KISBroker:
    """Minimal KIS OpenAPI client.

    All real TR codes live as class constants so they're easy to audit and
    swap between paper/real environments.
    """

    # TR codes for 주식 현금 주문. The paper-trade suffix is "_D1" in some
    # endpoints — verify against the portal when wiring new calls.
    TR_ORDER_CASH_BUY_REAL = "TTTC0802U"
    TR_ORDER_CASH_SELL_REAL = "TTTC0801U"
    TR_ORDER_CASH_BUY_PAPER = "VTTC0802U"
    TR_ORDER_CASH_SELL_PAPER = "VTTC0801U"

    TR_INQUIRE_BALANCE_REAL = "TTTC8434R"
    TR_INQUIRE_BALANCE_PAPER = "VTTC8434R"

    # 매수가능조회 (주문가능현금 / 미수없는매수수량 / 최대매수수량)
    TR_INQUIRE_PSBL_ORDER_REAL = "TTTC8908R"
    TR_INQUIRE_PSBL_ORDER_PAPER = "VTTC8908R"

    # 주식일별주문체결조회 — 최대 3개월. TR은 실전/모의 공용이 아님.
    TR_DAILY_CCLD_REAL_3M = "TTTC8001R"
    TR_DAILY_CCLD_PAPER_3M = "VTTC8001R"

    TR_INQUIRE_PRICE = "FHKST01010100"

    def __init__(
        self,
        cfg: KISBrokerConfig,
        settings: Settings | None = None,
        *,
        token_cache_path: Path | None = None,
    ) -> None:
        settings = settings or Settings()
        self._settings = settings
        self._cfg = cfg

        self._base_url = cfg.base_url_real if settings.kis_env == "real" else cfg.base_url_paper
        self._is_paper = settings.kis_env != "real"
        self._client = httpx.Client(base_url=self._base_url, timeout=20.0)
        self._token: _Token | None = None
        # 대시보드/스케줄러/봇 3개 프로세스가 같은 app key 로 각자 토큰을 들고 있으면
        # KIS 가 재발급 시 이전 토큰을 무효화 → 다른 프로세스가 모르고 쓰다 HTTP 500.
        # 파일 캐시를 공유해 프로세스 간 토큰을 하나로 수렴시킨다.
        self._token_cache_path = token_cache_path

        missing = [k for k in ("kis_app_key", "kis_app_secret", "kis_account_no") if not getattr(settings, k)]
        if missing:
            log.warning("kis.missing_credentials", missing=missing)

    # ------------------------------------------------------------------ #
    # Auth                                                                #
    # ------------------------------------------------------------------ #

    @property
    def _app_key(self) -> str:
        if self._is_paper and self._settings.kis_paper_app_key:
            return self._settings.kis_paper_app_key
        return self._settings.kis_app_key or ""

    @property
    def _app_secret(self) -> str:
        if self._is_paper and self._settings.kis_paper_app_secret:
            return self._settings.kis_paper_app_secret
        return self._settings.kis_app_secret or ""

    def _auth_header(self, token: str | None = None) -> dict[str, str]:
        return {
            "authorization": f"Bearer {token or self._get_token()}",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
            "content-type": "application/json; charset=utf-8",
        }

    def _read_shared_token(self) -> _Token | None:
        if not self._token_cache_path:
            return None
        try:
            raw = _jsonlib.loads(self._token_cache_path.read_text())
            return _Token(value=str(raw["value"]), expires_at=float(raw["expires_at"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_shared_token(self, token: _Token) -> None:
        if not self._token_cache_path:
            return
        try:
            self._token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._token_cache_path.with_suffix(self._token_cache_path.suffix + ".tmp")
            tmp.write_text(_jsonlib.dumps({"value": token.value, "expires_at": token.expires_at}))
            tmp.replace(self._token_cache_path)
        except OSError as e:
            log.warning("kis.token_cache_write_failed", err=str(e))

    def _issue_token(self) -> _Token:
        resp = self._client.post(
            "/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self._app_key,
                "appsecret": self._app_secret,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        token = _Token(
            value=data["access_token"],
            expires_at=time.time() + int(data.get("expires_in", 60 * 60 * 23)),
        )
        self._write_shared_token(token)
        log.info("kis.token_issued", env=self._settings.kis_env)
        return token

    def _get_token(self) -> str:
        now = time.time()
        if self._token and self._token.expires_at > now + 60:
            return self._token.value
        # 다른 프로세스가 이미 발급한 공유 토큰이 있으면 재발급 없이 채택한다.
        shared = self._read_shared_token()
        if shared and shared.expires_at > now + 60:
            self._token = shared
            return shared.value
        self._token = self._issue_token()
        return self._token.value

    def _refresh_after_auth_failure(self, failed_value: str) -> str:
        """인증 실패 후 토큰 복구.

        다른 프로세스가 이미 새 토큰을 발급했다면(파일의 값이 방금 실패한
        값과 다르면) 그것을 채택해 불필요한 재발급/무효화 churn 을 막는다.
        아니면 직접 새로 발급한다.
        """
        now = time.time()
        shared = self._read_shared_token()
        if shared and shared.value != failed_value and shared.expires_at > now + 60:
            self._token = shared
            log.info("kis.token_adopted_shared", env=self._settings.kis_env)
            return shared.value
        self._token = self._issue_token()
        return self._token.value

    # ------------------------------------------------------------------ #
    # Broker API                                                          #
    # ------------------------------------------------------------------ #

    def place_order(self, order: Order) -> OrderAck:
        tr_id = self._order_tr_id(order.side)
        body = {
            "CANO": self._account_cano(),
            "ACNT_PRDT_CD": self._settings.kis_account_product_code,
            "PDNO": order.ticker,
            "ORD_DVSN": "01" if order.order_type == "market" else "00",
            "ORD_QTY": str(order.quantity),
            "ORD_UNPR": str(int(order.price)) if order.price else "0",
        }
        data = self._request(
            "POST",
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id=tr_id,
            json=body,
        )
        output = data.get("output") or {}
        return OrderAck(
            order_id=str(output.get("KRX_FWDG_ORD_ORGNO", "")),
            status=str(data.get("rt_cd", "")),
            raw=data,
            kis_odno=str(output.get("ODNO", "") or ""),
        )

    def get_max_buy_qty(self, ticker: str, *, price: int = 0, market: bool = True) -> int:
        """KIS 매수가능조회 — 시장가/지정가에 따라 미수없는 최대 매수수량을 반환.

        시장가(market=True) 주문은 KIS가 상한가 기준으로 주문가능금액을 검증하므로,
        plan 의 price × qty 가 예수금 이내라도 실제로는 거부될 수 있다. 본 메서드는
        KIS 가 직접 계산해주는 nrcvb_buy_qty(미수없는매수수량) 를 우선 사용한다.
        실패 시 0 반환 — 호출 측에서 fallback 결정.
        """
        tr_id = self.TR_INQUIRE_PSBL_ORDER_PAPER if self._is_paper else self.TR_INQUIRE_PSBL_ORDER_REAL
        params = {
            "CANO": self._account_cano(),
            "ACNT_PRDT_CD": self._settings.kis_account_product_code,
            "PDNO": ticker,
            "ORD_UNPR": "0" if market else str(int(price or 0)),
            "ORD_DVSN": "01" if market else "00",
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN": "N",
        }
        try:
            data = self._request(
                "GET",
                "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
                tr_id=tr_id,
                params=params,
            )
        except Exception as e:
            log.warning("kis.psbl_order_failed", ticker=ticker, err=str(e))
            return 0
        out = data.get("output") or {}
        # 미수없는매수수량을 우선 사용 (안전). 비어 있으면 최대매수수량으로 폴백.
        qty_str = out.get("nrcvb_buy_qty") or out.get("max_buy_qty") or "0"
        try:
            return int(float(qty_str))
        except (TypeError, ValueError):
            return 0

    def _inquire_balance_raw(self) -> dict[str, Any]:
        tr_id = self.TR_INQUIRE_BALANCE_PAPER if self._is_paper else self.TR_INQUIRE_BALANCE_REAL
        params = {
            "CANO": self._account_cano(),
            "ACNT_PRDT_CD": self._settings.kis_account_product_code,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        return self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id=tr_id,
            params=params,
        )

    def get_cash_balance_krw(self) -> float:
        """예수금 총금액 (주문 가능 현금이 아닌 실예수금)."""
        data = self._inquire_balance_raw()
        for row in data.get("output2", []) or []:
            if "dnca_tot_amt" in row:
                return float(row["dnca_tot_amt"] or 0)
        return 0.0

    def get_account_summary(self) -> AccountSummary:
        """계좌 요약 — inquire-balance output2 필드를 파싱해서 반환."""
        data = self._inquire_balance_raw()
        row: dict[str, Any] = {}
        for r in data.get("output2", []) or []:
            if "tot_evlu_amt" in r or "dnca_tot_amt" in r:
                row = r
                break

        def _f(k: str) -> float:
            try:
                return float(row.get(k, 0) or 0)
            except (TypeError, ValueError):
                return 0.0

        return AccountSummary(
            cash=_f("dnca_tot_amt"),
            d2_cash=_f("prvs_rcdl_excc_amt"),
            stock_eval=_f("scts_evlu_amt"),
            total_eval=_f("tot_evlu_amt"),
            total_purchase=_f("pchs_amt_smtl_amt"),
            eval_pl=_f("evlu_pfls_smtl_amt"),
            net_asset=_f("nass_amt"),
            asset_change=_f("asst_icdc_amt"),
            asset_change_rate=_f("asst_icdc_erng_rt"),
        )

    def get_positions(self) -> list[Position]:
        data = self._inquire_balance_raw()
        positions: list[Position] = []
        for row in data.get("output1", []) or []:
            qty = int(row.get("hldg_qty", 0) or 0)
            if qty <= 0:
                continue
            positions.append(
                Position(
                    ticker=row.get("pdno", ""),
                    quantity=qty,
                    avg_price=float(row.get("pchs_avg_pric", 0) or 0),
                    unrealized_pl=float(row.get("evlu_pfls_amt", 0) or 0),
                    name=row.get("prdt_name") or None,
                )
            )
        return positions

    def get_daily_trades(
        self, *, ticker: str | None = None, days: int = 30
    ) -> list[TradeFill]:
        """주식일별주문체결조회 — 최대 3개월. 모의투자도 동일 API 지원 (VTTC8001R)."""
        from datetime import datetime as _dt, timedelta as _td

        end = _dt.now()
        start = end - _td(days=days)
        tr_id = self.TR_DAILY_CCLD_PAPER_3M if self._is_paper else self.TR_DAILY_CCLD_REAL_3M

        fills: list[TradeFill] = []
        fk100, nk100 = "", ""
        while True:
            params = {
                "CANO": self._account_cano(),
                "ACNT_PRDT_CD": self._settings.kis_account_product_code,
                "INQR_STRT_DT": start.strftime("%Y%m%d"),
                "INQR_END_DT": end.strftime("%Y%m%d"),
                "SLL_BUY_DVSN_CD": "00",          # 00=전체, 01=매도, 02=매수
                "INQR_DVSN": "00",                # 00=역순 (최근부터)
                "PDNO": ticker or "",
                "CCLD_DVSN": "00",                # 00=전체, 01=체결, 02=미체결
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": fk100,
                "CTX_AREA_NK100": nk100,
            }
            try:
                data = self._request(
                    "GET",
                    "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                    tr_id=tr_id,
                    params=params,
                )
            except Exception as e:
                log.warning("kis.daily_ccld_failed", err=str(e))
                break

            for row in data.get("output1", []) or []:
                try:
                    ord_qty = int(row.get("ord_qty", 0) or 0)
                    tot_ccld_qty = int(row.get("tot_ccld_qty", 0) or 0)
                    side_code = (row.get("sll_buy_dvsn_cd", "") or "").strip()
                    side = "SELL" if side_code == "01" else "BUY"
                    cancel = (row.get("cncl_yn", "N") or "N") == "Y"
                    status = (
                        "cancelled" if cancel
                        else "filled" if tot_ccld_qty >= ord_qty > 0
                        else "partial" if tot_ccld_qty > 0
                        else "pending"
                    )
                    fills.append(TradeFill(
                        order_date=row.get("ord_dt", "") or "",
                        order_time=row.get("ord_tmd", "") or "",
                        ticker=row.get("pdno", "") or "",
                        name=row.get("prdt_name") or None,
                        side=side,
                        quantity=ord_qty,
                        filled_qty=tot_ccld_qty,
                        price=float(row.get("ord_unpr", 0) or 0),
                        avg_fill_price=float(row.get("avg_prvs", 0) or 0),
                        total_amount=float(row.get("tot_ccld_amt", 0) or 0),
                        status=status,
                        order_id=str(row.get("odno", "") or ""),
                    ))
                except Exception as e:
                    log.warning("kis.daily_ccld_row_parse_failed", err=str(e))

            # 페이징: tr_cont == 'M' 이면 다음 페이지 존재
            fk100 = data.get("ctx_area_fk100", "") or ""
            nk100 = data.get("ctx_area_nk100", "") or ""
            if not nk100.strip():
                break

        return fills

    def get_stock_name(self, ticker: str) -> str | None:
        try:
            data = self._request(
                "GET",
                "/uapi/domestic-stock/v1/quotations/inquire-price",
                tr_id=self.TR_INQUIRE_PRICE,
                params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
            )
            return data.get("output", {}).get("hts_kor_isnm") or None
        except Exception:
            return None

    def get_quote(self, ticker: str) -> Quote:
        data = self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id=self.TR_INQUIRE_PRICE,
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        )
        out = data.get("output", {})
        return Quote(
            ticker=ticker,
            price=float(out.get("stck_prpr", 0) or 0),
            timestamp_iso=datetime.now(tz=timezone.utc).isoformat(),
        )

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    def _order_tr_id(self, side: OrderSide) -> str:
        if self._is_paper:
            return self.TR_ORDER_CASH_BUY_PAPER if side == OrderSide.BUY else self.TR_ORDER_CASH_SELL_PAPER
        return self.TR_ORDER_CASH_BUY_REAL if side == OrderSide.BUY else self.TR_ORDER_CASH_SELL_REAL

    def _account_cano(self) -> str:
        # KIS accounts are 10-digit CANO + 2-digit product code.
        if self._is_paper and self._settings.kis_paper_account_no:
            return self._settings.kis_paper_account_no[:8]
        return (self._settings.kis_account_no or "")[:8]

    # 토큰 만료/무효 시 KIS 가 내려주는 msg_cd. 이때만 토큰을 재발급한다.
    _TOKEN_ERROR_MSG_CDS = frozenset({"EGW00121", "EGW00123", "EGW00106"})

    # 초당 거래건수 초과(EGW00201) 등 일시적 오류. KIS 는 이 경우 rt_cd=1,
    # msg_cd=EGW00201 과 함께 HTTP 500 을 내려준다. 토큰과 무관하므로
    # 짧게 백오프한 뒤 같은 토큰으로 재시도해야 한다(토큰 재발급은 오히려
    # 발급 throttle 403(EGW00133) 을 유발).
    _MAX_ATTEMPTS = 6

    def _request(
        self,
        method: str,
        path: str,
        *,
        tr_id: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        last_resp: httpx.Response | None = None
        for attempt in range(self._MAX_ATTEMPTS):
            token = self._get_token()
            headers = {**self._auth_header(token), "tr_id": tr_id, "custtype": "P"}
            try:
                resp = self._client.request(method, path, headers=headers, params=params, json=json)
            except httpx.TransportError as e:
                if attempt + 1 >= self._MAX_ATTEMPTS:
                    raise
                log.warning("kis.transport_retry", path=path, tr_id=tr_id, err=str(e))
                time.sleep(0.5 * (attempt + 1))
                continue

            if resp.is_success:
                data = resp.json()
                if data.get("rt_cd") not in (None, "0"):
                    log.warning("kis.non_success", path=path, tr_id=tr_id, msg=data.get("msg1"))
                return data

            last_resp = resp
            msg_cd, msg1 = "", ""
            try:
                body = resp.json()
                msg_cd, msg1 = body.get("msg_cd", ""), body.get("msg1", "")
            except (ValueError, AttributeError):
                pass
            is_last = attempt + 1 >= self._MAX_ATTEMPTS
            # 진짜 토큰 만료/무효는 401 로 온다. 레이트리밋 폭주 중 KIS 가 500 과
            # 함께 EGW0012x 를 흘리는 경우가 있는데(2026-06-11 09:30 장애), 이를
            # 토큰에러로 오인해 재발급하면 재시도 1회를 낭비하고 발급 트랜잭션이
            # TPS 압박을 가중시킨다 → 500 이면 토큰에러로 보지 않는다.
            is_token_error = resp.status_code == 401 or (
                msg_cd in self._TOKEN_ERROR_MSG_CDS and resp.status_code < 500
            )
            # 초당 거래건수 초과(EGW00201)는 HTTP 500 으로 내려온다.
            is_transient = resp.status_code >= 500 or msg_cd == "EGW00201"

            # (1) 토큰 만료/무효 → 토큰 갱신 후 재시도 (재발급 churn 방지를 위해
            #     다른 프로세스가 이미 발급한 공유 토큰을 우선 채택).
            if is_token_error and not is_last:
                log.warning(
                    "kis.token_refresh", path=path, tr_id=tr_id,
                    status=resp.status_code, msg_cd=msg_cd,
                )
                self._refresh_after_auth_failure(token)
                continue
            # (2) 초당 거래건수 초과 / 5xx 일시 오류 → 백오프 후 같은 토큰으로 재시도.
            if is_transient and not is_last:
                log.warning(
                    "kis.rate_limited_retry", path=path, tr_id=tr_id,
                    status=resp.status_code, msg_cd=msg_cd, msg=msg1, attempt=attempt + 1,
                )
                # 정각 충돌 시 여러 프로세스가 같은 백오프로 동시 재시도하면 또
                # 부딪힌다 → 지터를 섞어 재시도 타이밍을 분산한다.
                time.sleep(0.4 + 0.5 * attempt + random.uniform(0.0, 0.3))
                continue

            # 재시도 대상이 아니거나 마지막 시도 → 에러 본문을 남기고 raise.
            log.warning(
                "kis.request_failed", path=path, tr_id=tr_id,
                status=resp.status_code, msg_cd=msg_cd, msg=msg1, body=resp.text[:500],
            )
            resp.raise_for_status()
            return resp.json()

        assert last_resp is not None  # pragma: no cover
        last_resp.raise_for_status()
        return last_resp.json()  # pragma: no cover
