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

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from moppu.broker.base import Order, OrderAck, OrderSide, Position, Quote
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

    TR_INQUIRE_PRICE = "FHKST01010100"

    def __init__(self, cfg: KISBrokerConfig, settings: Settings | None = None) -> None:
        settings = settings or Settings()
        self._settings = settings
        self._cfg = cfg

        self._base_url = cfg.base_url_real if settings.kis_env == "real" else cfg.base_url_paper
        self._is_paper = settings.kis_env != "real"
        self._client = httpx.Client(base_url=self._base_url, timeout=20.0)
        self._token: _Token | None = None

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

    def _auth_header(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._get_token()}",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
            "content-type": "application/json; charset=utf-8",
        }

    def _get_token(self) -> str:
        now = time.time()
        if self._token and self._token.expires_at > now + 60:
            return self._token.value

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
        self._token = _Token(
            value=data["access_token"],
            expires_at=now + int(data.get("expires_in", 60 * 60 * 23)),
        )
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
        return OrderAck(
            order_id=str(data.get("output", {}).get("KRX_FWDG_ORD_ORGNO", "")),
            status=str(data.get("rt_cd", "")),
            raw=data,
        )

    def get_cash_balance_krw(self) -> float:
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
        data = self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id=tr_id,
            params=params,
        )
        for row in data.get("output2", []) or []:
            if "dnca_tot_amt" in row:
                return float(row["dnca_tot_amt"])
        return 0.0

    def get_positions(self) -> list[Position]:
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
        data = self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id=tr_id,
            params=params,
        )
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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10), reraise=True)
    def _request(
        self,
        method: str,
        path: str,
        *,
        tr_id: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {**self._auth_header(), "tr_id": tr_id, "custtype": "P"}
        resp = self._client.request(method, path, headers=headers, params=params, json=json)
        resp.raise_for_status()
        data = resp.json()
        if data.get("rt_cd") not in (None, "0"):
            log.warning("kis.non_success", path=path, tr_id=tr_id, msg=data.get("msg1"))
        return data
