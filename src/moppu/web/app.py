"""Moppu Monitor — FastAPI dashboard backend."""

from __future__ import annotations

import json
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import desc, func

from moppu.config import Settings
from moppu.llm import build_llm
from moppu.logging_setup import get_logger
from moppu.runtime import Runtime, build_runtime
from moppu.storage.db import Channel, Transcript, TranscriptChunk, Video, VideoListEntry

log = get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

KST = timezone(timedelta(hours=9))

PRICING_PER_1M: dict[tuple[str, str], tuple[float, float]] = {
    ("openai", "gpt-4.1"): (2.0, 8.0),
    ("openai", "gpt-4.1-mini"): (0.4, 1.6),
    ("openai", "gpt-4.1-nano"): (0.1, 0.4),
    ("anthropic", "claude-sonnet-4-6"): (3.0, 15.0),
    ("anthropic", "claude-opus-4-6"): (15.0, 75.0),
    ("anthropic", "claude-haiku-4-5-20251001"): (0.8, 4.0),
    ("google", "gemini-2.5-pro"): (1.25, 10.0),
    ("google", "gemini-2.5-flash"): (0.15, 0.6),
}

_rt: Runtime | None = None
_token_log: list[dict[str, Any]] = []
_ticker_name_cache: dict[str, str] = {}
_token_log_path: Path | None = None
_sessions: set[str] = set()
_pipeline_running: bool = False
_pipeline_run_msg: str = ""
_deleted_embedding_count: int = 0
_local_run_requested: bool = False   # 로컬 수집기 실행 요청 플래그
_local_retry_video_ids: list[str] = []  # 로컬 수집기에 재시도 요청할 video_id 큐
_local_last_heartbeat: datetime | None = None  # 로컬 수집기 마지막 폴링 시각
LOCAL_HEARTBEAT_STALE_SEC: int = 300   # 5분 이상 폴링 없으면 연결 끊김으로 간주


def _emergency_stop_path() -> Path:
    return Path((_rt.cfg.app.data_dir if _rt else Path("data")) / ".emergency_stop")


def _is_emergency_stopped() -> bool:
    return _emergency_stop_path().exists()


def _estimate_cost(provider: str, model: str, inp: int, out: int) -> float:
    rates = PRICING_PER_1M.get((provider, model))
    if not rates:
        for (p, m), r in PRICING_PER_1M.items():
            if p == provider and model.startswith(m):
                rates = r
                break
    if not rates:
        rates = (10.0, 30.0)
    return (inp * rates[0] + out * rates[1]) / 1_000_000


def _send_telegram(text: str) -> None:
    """설정된 Telegram 채팅으로 메시지를 전송합니다."""
    if _rt is None or not _rt.settings.telegram_bot_token:
        return
    from moppu.bot.telegram_bot import send_telegram_message
    try:
        send_telegram_message(_rt.settings, text)
    except Exception as e:
        log.warning("telegram.notify_failed", err=str(e))


def _write_pipeline_log(msg: str) -> None:
    if _rt is None:
        return
    path = _rt.cfg.app.data_dir / "pipeline.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _log_token_usage(provider: str, model: str, usage: dict[str, int]) -> None:
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    _token_log.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "model": model,
        "input_tokens": inp,
        "output_tokens": out,
        "cost_usd": _estimate_cost(provider, model, inp, out),
    })
    if _token_log_path:
        try:
            _token_log_path.write_text(json.dumps(_token_log, ensure_ascii=False, indent=2))
        except Exception:
            pass


def _load_token_log() -> None:
    global _token_log
    if _token_log_path and _token_log_path.exists():
        try:
            _token_log = json.loads(_token_log_path.read_text())
        except Exception:
            _token_log = []


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _rt, _token_log_path
    _rt = build_runtime()
    _token_log_path = _rt.cfg.app.data_dir / "token_usage.json"
    _load_token_log()
    _recover_interrupted_strategy()
    yield
    _rt = None


app = FastAPI(title="Moppu Monitor", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path == "/" or path.startswith("/static/") or path.startswith("/api/auth/"):
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if token not in _sessions:
        return JSONResponse(status_code=401, content={"detail": "인증 필요"})
    return await call_next(request)


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# -------------------------------------------------------------------- #
# Overview                                                              #
# -------------------------------------------------------------------- #


@app.get("/api/overview")
def overview():
    assert _rt is not None
    data: dict[str, Any] = {
        "summary": {
            "cash": 0,
            "d2_cash": 0,
            "stock_eval": 0,
            "total_eval": 0,
            "total_purchase": 0,
            "eval_pl": 0,
            "eval_pl_rate": 0,
            "net_asset": 0,
            "asset_change": 0,
            "asset_change_rate": 0,
        },
        "positions": [],
        "kis_mode": _rt.settings.kis_env,
        "dry_run": _rt.cfg.agent.dry_run,
        "emergency_stopped": _is_emergency_stopped(),
    }
    if _rt.broker is None:
        data["broker_error"] = "브로커 미설정 (API 키 확인 필요)"
        return data
    try:
        summary = _rt.broker.get_account_summary()
        positions = _rt.broker.get_positions()
        pos_list = []
        for p in positions:
            if p.name:
                _ticker_name_cache[p.ticker] = p.name
            cost_basis = p.avg_price * p.quantity
            eval_amt = cost_basis + (p.unrealized_pl or 0)
            pl_rate = ((p.unrealized_pl or 0) / cost_basis * 100) if cost_basis > 0 else 0
            pos_list.append({
                "ticker": p.ticker,
                "name": p.name,
                "quantity": p.quantity,
                "avg_price": p.avg_price,
                "eval_amount": eval_amt,
                "cost_basis": cost_basis,
                "unrealized_pl": p.unrealized_pl or 0,
                "pl_rate": round(pl_rate, 2),
            })
        # 수익률 — 매입 기준
        pl_rate = (summary.eval_pl / summary.total_purchase * 100) if summary.total_purchase > 0 else 0
        data["summary"] = {
            "cash": summary.cash,
            "d2_cash": summary.d2_cash,
            "stock_eval": summary.stock_eval,
            "total_eval": summary.total_eval,
            "total_purchase": summary.total_purchase,
            "eval_pl": summary.eval_pl,
            "eval_pl_rate": round(pl_rate, 2),
            "net_asset": summary.net_asset,
            "asset_change": summary.asset_change,
            "asset_change_rate": summary.asset_change_rate,
        }
        data["positions"] = pos_list
    except Exception as e:
        err = str(e)
        if "500" in err:
            data["broker_error"] = "계좌 조회 중 에러 (KIS 서버 오류)"
        elif "401" in err or "403" in err:
            data["broker_error"] = "계좌 조회 중 에러 (인증 실패 — API 키 확인)"
        elif "timeout" in err.lower():
            data["broker_error"] = "계좌 조회 중 에러 (응답 시간 초과)"
        else:
            data["broker_error"] = "계좌 조회 중 에러 발생"
    return data


@app.get("/api/positions/{ticker}/trades")
def position_trades(ticker: str, days: int = 90):
    """종목별 매매 이력 — 모달 상세 팝업용. 실현손익·수익률도 집계."""
    assert _rt is not None
    if _rt.broker is None:
        raise HTTPException(503, "브로커 미설정")

    # 현재 보유 포지션 정보
    pos_info: dict[str, Any] = {}
    try:
        for p in _rt.broker.get_positions():
            if p.ticker == ticker:
                cost_basis = p.avg_price * p.quantity
                eval_amt = cost_basis + (p.unrealized_pl or 0)
                pos_info = {
                    "ticker": p.ticker,
                    "name": p.name,
                    "quantity": p.quantity,
                    "avg_price": p.avg_price,
                    "cost_basis": cost_basis,
                    "eval_amount": eval_amt,
                    "unrealized_pl": p.unrealized_pl or 0,
                    "pl_rate": round(((p.unrealized_pl or 0) / cost_basis * 100) if cost_basis > 0 else 0, 2),
                }
                break
    except Exception as e:
        log.warning("position_trades.positions_failed", err=str(e))

    # 매매 이력
    try:
        fills = _rt.broker.get_daily_trades(ticker=ticker, days=days)
    except Exception as e:
        raise HTTPException(500, f"매매 이력 조회 실패: {e}")

    # 매도 체결에서 실현손익을 대략 계산 (KIS가 직접 주지 않으므로 평균매입가 기준)
    # 오래된 매매일수록 정확도는 떨어지지만, 최근 매매 요약용으로는 충분.
    trade_list = []
    total_buy_qty = 0
    total_buy_amt = 0.0
    total_sell_qty = 0
    total_sell_amt = 0.0
    realized_pl = 0.0

    # 시간순 정렬 (오래된 것부터) 후 평균가 추적
    fills_sorted = sorted(fills, key=lambda f: (f.order_date, f.order_time))
    running_qty = 0
    running_cost = 0.0

    for f in fills_sorted:
        if f.filled_qty <= 0:
            continue
        if f.side == "BUY":
            running_cost += f.avg_fill_price * f.filled_qty
            running_qty += f.filled_qty
            total_buy_qty += f.filled_qty
            total_buy_amt += f.avg_fill_price * f.filled_qty
            pl = 0.0
            pl_rate = 0.0
        else:  # SELL
            avg_cost = (running_cost / running_qty) if running_qty > 0 else 0.0
            pl = (f.avg_fill_price - avg_cost) * f.filled_qty
            pl_rate = ((f.avg_fill_price - avg_cost) / avg_cost * 100) if avg_cost > 0 else 0.0
            # 매도한 만큼 비용 차감
            if running_qty > 0:
                running_cost -= avg_cost * min(f.filled_qty, running_qty)
                running_qty = max(0, running_qty - f.filled_qty)
            realized_pl += pl
            total_sell_qty += f.filled_qty
            total_sell_amt += f.avg_fill_price * f.filled_qty
        trade_list.append({
            "date": f.order_date,
            "time": f.order_time,
            "side": f.side,
            "quantity": f.quantity,
            "filled_qty": f.filled_qty,
            "avg_fill_price": f.avg_fill_price,
            "total_amount": f.total_amount,
            "status": f.status,
            "pl": round(pl, 0),
            "pl_rate": round(pl_rate, 2),
            "is_win": pl > 0,
        })

    # 표시 시간순 (최신부터)
    trade_list.reverse()

    return {
        "position": pos_info or {"ticker": ticker, "name": _ticker_name_cache.get(ticker)},
        "trades": trade_list,
        "stats": {
            "total_trades": len(trade_list),
            "buy_count": sum(1 for t in trade_list if t["side"] == "BUY"),
            "sell_count": sum(1 for t in trade_list if t["side"] == "SELL"),
            "total_buy_qty": total_buy_qty,
            "total_buy_amt": total_buy_amt,
            "total_sell_qty": total_sell_qty,
            "total_sell_amt": total_sell_amt,
            "realized_pl": round(realized_pl, 0),
            "win_count": sum(1 for t in trade_list if t["side"] == "SELL" and t["is_win"]),
            "loss_count": sum(1 for t in trade_list if t["side"] == "SELL" and not t["is_win"] and t["pl"] < 0),
        },
    }


# -------------------------------------------------------------------- #
# Pipeline                                                              #
# -------------------------------------------------------------------- #


@app.get("/api/pipeline/status")
def pipeline_status():
    assert _rt is not None
    with _rt.session_factory() as s:
        ch_total = s.query(func.count(Channel.id)).scalar() or 0
        ch_enabled = s.query(func.count(Channel.id)).filter_by(enabled=True).scalar() or 0

        vid_total = s.query(func.count(Video.id)).scalar() or 0
        vid_embedded = s.query(func.count(Video.id)).filter(Video.status == "embedded").scalar() or 0
        vid_pending = s.query(func.count(Video.id)).filter(Video.status == "pending").scalar() or 0
        vid_failed = s.query(func.count(Video.id)).filter(Video.status == "failed").scalar() or 0

        vl_total = s.query(func.count(VideoListEntry.id)).scalar() or 0

        recent = (
            s.query(Video)
            .order_by(desc(Video.created_at))
            .limit(10)
            .all()
        )
        recent_list = [
            {
                "video_id": v.video_id,
                "title": v.title,
                "status": v.status,
                "created_at": v.created_at.isoformat() if v.created_at else None,
                "url": v.url,
                "error": v.error,
            }
            for v in recent
        ]

    return {
        "channels": {"total": ch_total, "enabled": ch_enabled},
        "videos": {
            "total": vid_total,
            "embedded": vid_embedded,
            "pending": vid_pending,
            "failed": vid_failed,
        },
        "video_list_entries": vl_total,
        "recent_ingestions": recent_list,
        "emergency_stopped": _is_emergency_stopped(),
        "pipeline_running": _pipeline_running,
        "pipeline_run_msg": _pipeline_run_msg,
        "deleted_embeddings": _deleted_embedding_count,
        "local_collector": _local_connection_status(),
    }


@app.post("/api/pipeline/run")
def run_pipeline():
    """수동으로 파이프라인을 실행합니다 (비동기 백그라운드)."""
    global _pipeline_running
    assert _rt is not None
    if _pipeline_running:
        raise HTTPException(409, "파이프라인이 이미 실행 중입니다.")

    def _run() -> None:
        global _pipeline_running, _pipeline_run_msg, _local_run_requested
        _pipeline_running = True
        try:
            _write_pipeline_log("=== 대시보드 실행 요청 ===")

            # 1) 영상 목록 DB 동기화 (YouTube 호출 없음 — 안전)
            _pipeline_run_msg = "영상 목록 동기화 중..."
            _write_pipeline_log(_pipeline_run_msg)
            _rt.pipeline.sync_video_lists()
            _write_pipeline_log("영상 목록 동기화 완료")

            # 2) 로컬 수집기 연결 확인
            conn = _local_connection_status()
            if not conn["connected"]:
                _pipeline_run_msg = "Local Machine Error — 로컬 수집기 연결 끊김"
                _write_pipeline_log(f"[ERROR] {_pipeline_run_msg}")
                return

            # 3) 로컬 수집기에 실행 신호
            _local_run_requested = True
            _pipeline_run_msg = "로컬 수집기에 실행 신호 전송됨 (watch 모드 대기 중...)"
            _write_pipeline_log("[SIGNAL] 로컬 수집기 실행 요청 → /api/collect/poll 대기")

        except Exception as e:
            # EC2 측 동작은 sync_video_lists 만 수행 — YouTube 접근 없음.
            # 어떤 예외든 Local Machine Error 로 취급 (AWS IP 밴 방지).
            _pipeline_run_msg = f"Local Machine Error — {e}"
            _write_pipeline_log(f"[ERROR] Local Machine Error — {e}")
            log.error("pipeline.manual_run_failed", err=str(e))
        finally:
            _pipeline_running = False

    threading.Thread(target=_run, daemon=True).start()
    return {"started": True, "message": "파이프라인 실행을 시작했습니다."}


@app.get("/api/logs/app")
def app_log():
    """애플리케이션 로그 — journald(EC2) 또는 data/app.log(로컬) 반환."""
    import subprocess

    # 1) journald 시도 (EC2 systemd 환경)
    try:
        result = subprocess.run(
            ["journalctl", "-u", "moppu-dashboard", "--no-pager", "-n", "500",
             "--output=short-iso", "--no-hostname"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().splitlines()
            return {"lines": lines, "source": "journald"}
    except Exception:
        pass

    # 2) 파일 폴백 (로컬 개발)
    if _rt:
        path = _rt.cfg.app.data_dir / "app.log"
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
            return {"lines": lines[-500:], "source": "file"}

    return {"lines": ["로그를 찾을 수 없습니다."], "source": "none"}


@app.get("/api/pipeline/log")
def pipeline_log():
    """파이프라인 실행 로그 파일의 마지막 200줄을 반환합니다."""
    assert _rt is not None
    path = _rt.cfg.app.data_dir / "pipeline.log"
    if not path.exists():
        return {"lines": [], "exists": False}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return {"lines": lines[-500:], "exists": True, "total": len(lines)}
    except Exception as e:
        return {"lines": [f"[ERROR] 로그 파일 읽기 실패: {e}"], "exists": True}


# -------------------------------------------------------------------- #
# Ingestion summary                                                     #
# -------------------------------------------------------------------- #


@app.get("/api/agent/summary")
def ingestion_summary():
    """Return today's summary from saved file (no LLM call).

    Falls back to video list only if no summary has been generated yet.
    """
    assert _rt is not None
    from moppu.agent.daily_summary import load

    today_str = datetime.now(KST).strftime("%Y-%m-%d")

    # Try saved file first
    saved = load(_rt.cfg.app.data_dir, today_str)
    if saved:
        return saved

    # No saved summary yet — return video list only
    midnight_kst = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_utc = midnight_kst.astimezone(timezone.utc).replace(tzinfo=None)
    with _rt.session_factory() as s:
        recent = (
            s.query(Video)
            .filter(Video.created_at >= midnight_utc, Video.status == "embedded")
            .order_by(desc(Video.created_at))
            .all()
        )
        videos = [
            {
                "video_id": v.video_id,
                "title": v.title,
                "url": v.url or f"https://www.youtube.com/watch?v={v.video_id}",
                "published_at": v.published_at.isoformat() if v.published_at else None,
            }
            for v in recent
        ]
    return {"date": today_str, "summary": None, "videos": videos}


@app.get("/api/agent/summary-list")
def summary_list(page: int = 1, per_page: int = 5):
    """Return paginated list of saved daily summaries, newest first."""
    assert _rt is not None
    data_dir = _rt.cfg.app.data_dir
    files = sorted(data_dir.glob("daily_summary_*.json"), reverse=True)
    total = len(files)
    start = (page - 1) * per_page
    page_files = files[start : start + per_page]
    items = []
    for f in page_files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            items.append({
                "date": data.get("date"),
                "summary": data.get("summary"),
                "videos": data.get("videos", []),
                "generated_at": data.get("generated_at"),
            })
        except Exception:
            pass
    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page) if total else 0,
    }


class SummaryRequest(BaseModel):
    force: bool = False


@app.post("/api/agent/generate-summary")
def generate_summary(req: SummaryRequest | None = None):
    """Generate (or return cached) daily summary and persist to disk."""
    assert _rt is not None
    from moppu.agent.daily_summary import generate_and_save

    force = bool(req and req.force)
    try:
        result = generate_and_save(
            _rt.session_factory, _rt.llm, _rt.cfg.app.data_dir, force=force
        )
    except Exception as e:
        raise HTTPException(500, f"요약 생성 실패: {e}")

    if result is None:
        today_str = datetime.now(KST).strftime("%Y-%m-%d")
        return {"date": today_str, "summary": None, "videos": [], "usage": {}}

    usage = result.get("usage") or {}
    if usage.get("input_tokens"):
        provider = result.get("provider", _rt.cfg.llm.provider)
        model = result.get("model", _rt.cfg.llm.model)
        _log_token_usage(provider, model, usage)
    return result


# -------------------------------------------------------------------- #
# Agent                                                                 #
# -------------------------------------------------------------------- #


@app.get("/api/agent/prompt")
def agent_prompt():
    assert _rt is not None
    try:
        prompt = _rt.agent._prompt.build_system_prompt()
    except Exception as e:
        prompt = f"(프롬프트 로드 실패: {e})"
    return {
        "system_prompt": prompt,
        "template_path": str(_rt.cfg.agent.prompt_template),
    }


class ChatRequest(BaseModel):
    message: str
    history: list[dict[str, str]] = []


@app.post("/api/agent/chat")
def agent_chat(req: ChatRequest):
    assert _rt is not None
    if _is_emergency_stopped():
        raise HTTPException(503, "긴급 중단 상태입니다.")
    try:
        result = _rt.agent.chat(req.message, history=req.history)
        _log_token_usage(result["provider"], result["model"], result["usage"])
        return result
    except Exception as e:
        log.error("web.agent_chat_failed", err=str(e))
        raise HTTPException(500, f"Agent 오류: {e}")


# -------------------------------------------------------------------- #
# Settings                                                              #
# -------------------------------------------------------------------- #


@app.get("/api/settings")
def get_settings():
    assert _rt is not None
    provider, kwargs = _rt.cfg.llm.resolved()
    return {
        "llm": {"provider": provider, "model": kwargs["model"]},
        "kis_mode": _rt.settings.kis_env,
        "dry_run": _rt.cfg.agent.dry_run,
        "emergency_stopped": _is_emergency_stopped(),
        "available_providers": ["openai", "anthropic", "google"],
        "max_order_krw": _rt.cfg.agent.max_order_krw,
    }


class LLMSettingsRequest(BaseModel):
    provider: str
    model: str


@app.post("/api/settings/llm")
def update_llm(req: LLMSettingsRequest):
    assert _rt is not None
    _rt.cfg.llm.provider = req.provider  # type: ignore[assignment]
    _rt.cfg.llm.model = req.model
    try:
        new_llm = build_llm(_rt.cfg.llm, _rt.settings)
        _rt.agent._llm = new_llm
        return {"ok": True, "provider": req.provider, "model": req.model}
    except Exception as e:
        raise HTTPException(400, f"LLM 초기화 실패: {e}")


class KISKeysRequest(BaseModel):
    mode: str           # "paper" | "real"
    app_key: str
    app_secret: str
    account_no: str | None = None


@app.post("/api/settings/kis-keys")
def update_kis_keys(req: KISKeysRequest):
    assert _rt is not None
    if req.mode not in ("paper", "real"):
        raise HTTPException(400, "mode는 'paper' 또는 'real'이어야 합니다.")
    if req.mode == "paper":
        _rt.settings.kis_paper_app_key = req.app_key           # type: ignore[assignment]
        _rt.settings.kis_paper_app_secret = req.app_secret      # type: ignore[assignment]
        if req.account_no:
            _rt.settings.kis_paper_account_no = req.account_no  # type: ignore[assignment]
    else:
        _rt.settings.kis_app_key = req.app_key                  # type: ignore[assignment]
        _rt.settings.kis_app_secret = req.app_secret             # type: ignore[assignment]
        if req.account_no:
            _rt.settings.kis_account_no = req.account_no        # type: ignore[assignment]
    from moppu.broker.kis import KISBroker
    try:
        new_broker = KISBroker(_rt.cfg.broker.kis, _rt.settings)
        _rt.agent._broker = new_broker
        object.__setattr__(_rt, "broker", new_broker)
        return {"ok": True, "message": f"{req.mode} 키 적용 및 브로커 재연결 완료"}
    except Exception as e:
        raise HTTPException(400, f"브로커 초기화 실패: {e}")


class KISModeRequest(BaseModel):
    mode: str  # "paper" | "real"


@app.post("/api/settings/kis-mode")
def update_kis_mode(req: KISModeRequest):
    assert _rt is not None
    if req.mode not in ("paper", "real"):
        raise HTTPException(400, "모드는 'paper' 또는 'real'이어야 합니다.")
    _rt.settings.kis_env = req.mode  # type: ignore[assignment]
    from moppu.broker.kis import KISBroker
    try:
        new_broker = KISBroker(_rt.cfg.broker.kis, _rt.settings)
        _rt.agent._broker = new_broker
        # Also replace in runtime
        object.__setattr__(_rt, "broker", new_broker)
        return {"ok": True, "mode": req.mode}
    except Exception as e:
        raise HTTPException(400, f"브로커 재초기화 실패: {e}")


class DryRunRequest(BaseModel):
    enabled: bool


@app.post("/api/settings/dry-run")
def update_dry_run(req: DryRunRequest):
    assert _rt is not None
    _rt.cfg.agent.dry_run = req.enabled
    _rt.agent._cfg.dry_run = req.enabled
    return {"ok": True, "dry_run": req.enabled}


class EmergencyStopRequest(BaseModel):
    active: bool


@app.post("/api/settings/emergency-stop")
def emergency_stop(req: EmergencyStopRequest):
    p = _emergency_stop_path()
    if req.active:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(datetime.now(timezone.utc).isoformat())
        if _rt:
            _rt.cfg.agent.dry_run = True
            _rt.agent._cfg.dry_run = True
        return {"ok": True, "stopped": True, "message": "긴급 중단 활성화됨. dry_run=true 전환됨."}
    else:
        p.unlink(missing_ok=True)
        return {"ok": True, "stopped": False, "message": "긴급 중단 해제됨."}


# -------------------------------------------------------------------- #
# Cost estimation                                                       #
# -------------------------------------------------------------------- #


@app.get("/api/cost")
def cost_info():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_entries = [e for e in _token_log if e["ts"].startswith(today)]

    today_inp = sum(e.get("input_tokens", 0) for e in today_entries)
    today_out = sum(e.get("output_tokens", 0) for e in today_entries)
    today_cost = sum(e.get("cost_usd", 0) for e in today_entries)

    total_inp = sum(e.get("input_tokens", 0) for e in _token_log)
    total_out = sum(e.get("output_tokens", 0) for e in _token_log)
    total_cost = sum(e.get("cost_usd", 0) for e in _token_log)

    return {
        "today": {
            "input_tokens": today_inp,
            "output_tokens": today_out,
            "total_tokens": today_inp + today_out,
            "estimated_cost_usd": round(today_cost, 6),
        },
        "total": {
            "input_tokens": total_inp,
            "output_tokens": total_out,
            "total_tokens": total_inp + total_out,
            "estimated_cost_usd": round(total_cost, 6),
        },
        "recent_entries": _token_log[-10:],
        "pricing_table": {
            f"{p}/{m}": {"input_per_1M": r[0], "output_per_1M": r[1]}
            for (p, m), r in PRICING_PER_1M.items()
        },
    }


# -------------------------------------------------------------------- #
# Auth                                                                  #
# -------------------------------------------------------------------- #


class LoginRequest(BaseModel):
    id: str
    password: str


@app.post("/api/auth/login")
def login(req: LoginRequest):
    assert _rt is not None
    if req.id == _rt.settings.dashboard_id and req.password == _rt.settings.dashboard_password:
        token = secrets.token_hex(32)
        _sessions.add(token)
        return {"token": token}
    raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다.")


# -------------------------------------------------------------------- #
# Suggested questions  (파일에서 읽기 — LLM 호출 없음)                  #
# -------------------------------------------------------------------- #

_DEFAULT_QUESTIONS = [
    "오늘 수집된 영상 기준으로 주목할 섹터나 종목은?",
    "현재 시장에서 가장 큰 리스크 요인은 무엇인가요?",
    "지금 시점에 매수를 고려한다면 어떤 전략이 좋을까요?",
]


@app.get("/api/agent/suggested-questions")
def suggested_questions():
    """수집 시 저장된 파일에서 추천 질문을 반환합니다. LLM 호출 없음."""
    assert _rt is not None
    from moppu.agent.daily_summary import load

    today_str = datetime.now(KST).strftime("%Y-%m-%d")
    saved = load(_rt.cfg.app.data_dir, today_str)
    questions = (saved or {}).get("questions") or _DEFAULT_QUESTIONS
    return {"questions": questions}


# -------------------------------------------------------------------- #
# Channel / video-list edit                                             #
# -------------------------------------------------------------------- #


@app.get("/api/channels")
def list_channels():
    assert _rt is not None
    with _rt.session_factory() as s:
        rows = s.query(Channel).order_by(Channel.name).all()
        return [
            {
                "channel_id": c.channel_id,
                "handle": c.handle,
                "name": c.name,
                "tags": c.tags,
                "enabled": c.enabled,
                "title_contains": c.title_contains,
                "last_polled_at": c.last_polled_at.isoformat() if c.last_polled_at else None,
            }
            for c in rows
        ]


class ChannelCreateRequest(BaseModel):
    channel_id: str | None = None
    handle: str | None = None
    name: str | None = None
    title_contains: str | None = None
    upload_day: int | None = None
    enabled: bool = True


@app.post("/api/channels")
def create_channel(req: ChannelCreateRequest):
    assert _rt is not None
    if not req.channel_id and not req.handle:
        raise HTTPException(400, "channel_id 또는 handle 중 하나는 필수입니다.")
    from moppu.config import ChannelSpec
    spec = ChannelSpec(
        channel_id=req.channel_id,
        handle=req.handle,
        name=req.name,
        enabled=req.enabled,
        title_contains=req.title_contains,
        upload_day=req.upload_day,
    )
    try:
        ch = _rt.pipeline.add_channel(spec)
        return {"ok": True, "channel_id": ch.channel_id, "name": ch.name}
    except Exception as e:
        raise HTTPException(400, str(e))


class ChannelUpdateRequest(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    handle: str | None = None
    title_contains: str | None = None


@app.put("/api/channels/{channel_id}")
def update_channel(channel_id: str, req: ChannelUpdateRequest):
    assert _rt is not None
    with _rt.session_factory() as s:
        ch = s.query(Channel).filter_by(channel_id=channel_id).one_or_none()
        if ch is None:
            raise HTTPException(404, "채널을 찾을 수 없습니다.")
        if req.name is not None:
            ch.name = req.name
        if req.enabled is not None:
            ch.enabled = req.enabled
        if req.handle is not None:
            ch.handle = req.handle
        if req.title_contains is not None:
            ch.title_contains = req.title_contains
        s.commit()
    return {"ok": True}


def _delete_video_embeddings(s, video_ids: list[str]) -> int:
    """Chroma에서 영상들의 임베딩을 삭제하고 삭제 건수를 반환."""
    from moppu.storage.db import Transcript, TranscriptChunk
    eids: list[str] = []
    for vid in video_ids:
        v = s.query(Video).filter_by(video_id=vid).one_or_none()
        if v and v.transcript:
            for chunk in v.transcript.chunks:
                if chunk.embedding_id:
                    eids.append(chunk.embedding_id)
    if eids:
        _rt.vector_store.delete(eids)
    return len(eids)


@app.delete("/api/channels/{channel_id}")
def delete_channel(channel_id: str):
    global _deleted_embedding_count
    assert _rt is not None
    with _rt.session_factory() as s:
        ch = s.query(Channel).filter_by(channel_id=channel_id).one_or_none()
        if ch is None:
            raise HTTPException(404, "채널을 찾을 수 없습니다.")
        video_ids = [v.video_id for v in s.query(Video).filter_by(channel_fk=ch.id).all()]
        n_emb = _delete_video_embeddings(s, video_ids)
        s.delete(ch)
        s.commit()
    _deleted_embedding_count += n_emb
    return {"ok": True, "deleted_embeddings": n_emb}


@app.get("/api/video-lists")
def list_video_lists():
    assert _rt is not None
    with _rt.session_factory() as s:
        entries = s.query(VideoListEntry).order_by(VideoListEntry.list_name, VideoListEntry.added_at).all()
        result: dict[str, list] = {}
        for e in entries:
            result.setdefault(e.list_name, []).append({
                "video_id": e.video_id,
                "source_url": e.source_url,
                "added_at": e.added_at.isoformat() if e.added_at else None,
            })
        return result


class AddVideoRequest(BaseModel):
    url: str


@app.post("/api/video-lists/{list_name}/entries")
def add_video_entry(list_name: str, req: AddVideoRequest):
    assert _rt is not None
    from moppu.ingestion.youtube import parse_video_id
    try:
        vid = parse_video_id(req.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    with _rt.session_factory() as s:
        exists = s.query(VideoListEntry).filter_by(list_name=list_name, video_id=vid).one_or_none()
        if exists:
            return {"ok": True, "video_id": vid, "already_exists": True}
        s.add(VideoListEntry(list_name=list_name, video_id=vid, source_url=req.url if req.url != vid else None))
        s.commit()
    return {"ok": True, "video_id": vid, "already_exists": False}


@app.delete("/api/video-lists/{list_name}/entries/{video_id}")
def delete_video_entry(list_name: str, video_id: str):
    global _deleted_embedding_count
    assert _rt is not None
    with _rt.session_factory() as s:
        entry = s.query(VideoListEntry).filter_by(list_name=list_name, video_id=video_id).one_or_none()
        if entry is None:
            raise HTTPException(404, "항목을 찾을 수 없습니다.")
        n_emb = _delete_video_embeddings(s, [video_id])
        v = s.query(Video).filter_by(video_id=video_id).one_or_none()
        if v:
            s.delete(v)
        s.delete(entry)
        s.commit()
    _deleted_embedding_count += n_emb
    return {"ok": True, "deleted_embeddings": n_emb}


# -------------------------------------------------------------------- #
# Pipeline ingestion history                                            #
# -------------------------------------------------------------------- #


@app.get("/api/pipeline/ingestion-history")
def ingestion_history(page: int = 1, per_page: int = 10):
    assert _rt is not None
    with _rt.session_factory() as s:
        total = s.query(func.count(Video.id)).scalar() or 0
        videos = (
            s.query(Video)
            .order_by(desc(Video.created_at))
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        items = []
        for v in videos:
            ch_name = None
            if v.channel_fk:
                ch = s.query(Channel).filter_by(id=v.channel_fk).one_or_none()
                ch_name = ch.name if ch else None
            items.append({
                "video_id": v.video_id,
                "title": v.title,
                "status": v.status,
                "source_type": v.source_type,
                "channel_name": ch_name,
                "created_at": v.created_at.isoformat() if v.created_at else None,
                "published_at": v.published_at.isoformat() if v.published_at else None,
                "url": v.url or f"https://www.youtube.com/watch?v={v.video_id}",
                "error": v.error,
            })
    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page) if total else 0,
    }


@app.get("/api/pipeline/video/{video_id}")
def get_video_detail(video_id: str):
    assert _rt is not None
    with _rt.session_factory() as s:
        v = s.query(Video).filter(Video.video_id == video_id).one_or_none()
        if not v:
            raise HTTPException(404, "Video not found")
        ch_name = None
        if v.channel_fk:
            ch = s.query(Channel).filter_by(id=v.channel_fk).one_or_none()
            ch_name = ch.name if ch else None
        n_chunks = 0
        transcript_preview: str | None = None
        if v.transcript:
            chunks = sorted(v.transcript.chunks, key=lambda c: c.chunk_index)
            n_chunks = len(chunks)
            if chunks:
                transcript_preview = chunks[0].text[:800]
        return {
            "video_id": v.video_id,
            "title": v.title,
            "url": v.url or f"https://www.youtube.com/watch?v={v.video_id}",
            "source_type": v.source_type,
            "channel_name": ch_name,
            "status": v.status,
            "error": v.error,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "published_at": v.published_at.isoformat() if v.published_at else None,
            "n_chunks": n_chunks,
            "transcript_preview": transcript_preview,
        }


# -------------------------------------------------------------------- #
# Strategy Planner                                                      #
# -------------------------------------------------------------------- #

_strategy_running: bool = False
_strategy_run_msg: str = ""
_strategy_live_log: list[str] = []   # 실행 중 로그 스트림 (UI 스트리밍용)
_strategy_stop_requested: bool = False   # 중단 요청 플래그


def _strategy_history_dir() -> Path:
    base = _rt.cfg.app.data_dir if _rt else Path("data")
    return Path(base) / "strategy_history"


def _recover_interrupted_strategy() -> None:
    """서버 재시작 시 RUNNING.json이 있으면 중단된 실행을 에러 이력으로 기록."""
    try:
        hist_dir = _strategy_history_dir()
        running_marker = hist_dir / "RUNNING.json"
        if not running_marker.exists():
            return
        data = json.loads(running_marker.read_text(encoding="utf-8"))
        started_at = data.get("started_at", datetime.now(KST).isoformat())
        dry_run = data.get("dry_run", True)
        ts = datetime.now(KST).strftime("%Y-%m-%d_%H-%M-%S")
        (hist_dir / f"{ts}.json").write_text(
            json.dumps({
                "run_at": started_at,
                "dry_run": dry_run,
                "error": "서버 재시작으로 중단됨",
                "plan": {
                    "sells": [], "buys": [],
                    "summary": "서버 재시작으로 전략 수립이 중단되었습니다.",
                },
                "results": [],
            }, ensure_ascii=False)
        )
        running_marker.unlink()
        log.info("strategy.interrupted_recovered", started_at=started_at)
    except Exception as e:
        log.warning("strategy.recover_failed", err=str(e))


@app.get("/api/strategy/config")
def strategy_config():
    assert _rt is not None
    sp = _rt.cfg.strategy_planner
    return {
        "enabled": sp.enabled,
        "cron": sp.cron,
        "dry_run": sp.dry_run,
        "max_order_krw": sp.max_order_krw,
        "fund_request_wait_min": sp.fund_request_wait_min,
        "running": _strategy_running,
        "last_msg": _strategy_run_msg,
    }


class StrategyScheduleRequest(BaseModel):
    cron: str
    dry_run: bool
    enabled: bool = True


@app.post("/api/strategy/config")
def update_strategy_config(req: StrategyScheduleRequest):
    assert _rt is not None
    _rt.cfg.strategy_planner.cron = req.cron
    _rt.cfg.strategy_planner.dry_run = req.dry_run
    _rt.cfg.strategy_planner.enabled = req.enabled
    if _rt.strategy_planner is not None:
        _rt.strategy_planner._cfg.cron = req.cron        # noqa: SLF001
        _rt.strategy_planner._cfg.dry_run = req.dry_run  # noqa: SLF001
        _rt.strategy_planner._cfg.enabled = req.enabled  # noqa: SLF001
    return {"ok": True, "cron": req.cron, "dry_run": req.dry_run, "enabled": req.enabled}


@app.post("/api/strategy/run")
def run_strategy():
    global _strategy_running, _strategy_run_msg
    assert _rt is not None
    if _strategy_running:
        raise HTTPException(409, "전략 수립가가 이미 실행 중입니다.")
    if _is_emergency_stopped():
        raise HTTPException(503, "긴급 중단 상태입니다.")

    from moppu.agent.strategy_planner import StrategyPlannerAgent
    from moppu.config import StrategyPlannerConfig

    planner = _rt.strategy_planner
    if planner is None:
        sp_cfg = _rt.cfg.strategy_planner
        planner = StrategyPlannerAgent(
            cfg=StrategyPlannerConfig(
                enabled=True,
                dry_run=sp_cfg.dry_run,
                cron=sp_cfg.cron,
                max_order_krw=sp_cfg.max_order_krw,
                fund_request_wait_min=sp_cfg.fund_request_wait_min,
            ),
            settings=_rt.settings,
            llm=_rt.llm,
            trader_agent=_rt.agent,
            broker=_rt.broker,
            data_dir=_rt.cfg.app.data_dir,
        )

    def _run() -> None:
        import os as _os
        global _strategy_running, _strategy_run_msg, _strategy_live_log, _strategy_stop_requested
        _strategy_running = True
        _strategy_stop_requested = False
        _strategy_live_log = [f"[{datetime.now(KST).strftime('%H:%M:%S')}] 전략 수립 시작..."]
        _strategy_run_msg = "전략 수립 시작..."
        hist_dir = _strategy_history_dir()
        running_marker = hist_dir / "RUNNING.json"
        try:
            hist_dir.mkdir(parents=True, exist_ok=True)
            running_marker.write_text(json.dumps({
                "started_at": datetime.now(KST).isoformat(),
                "pid": _os.getpid(),
                "dry_run": planner._cfg.dry_run,  # noqa: SLF001
            }, ensure_ascii=False))
        except Exception:
            pass

        # planner.run() 은 내부에서 _append_log 로 self._log_lines 누적.
        # 다른 스레드에서 그걸 읽어 라이브 로그로 노출.
        import threading as _th
        def _poll_log():
            while _strategy_running:
                try:
                    lines = getattr(planner, "_log_lines", None)
                    if lines:
                        _strategy_live_log[:] = list(lines)
                except Exception:
                    pass
                import time as _t
                _t.sleep(0.5)
        _th.Thread(target=_poll_log, daemon=True).start()

        try:
            result = planner.run()
            # broker 미설정 등 오류를 반환값으로 전달하는 경우
            if result.get("error"):
                raise ValueError(result["error"])
            if result.get("usage"):
                _log_token_usage(
                    result.get("provider", _rt.cfg.llm.provider),
                    result.get("model", _rt.cfg.llm.model),
                    result["usage"],
                )
            n_sells = len((result.get("plan") or {}).get("sells", []))
            n_buys = len((result.get("plan") or {}).get("buys", []))
            _strategy_run_msg = f"완료 — 매도 {n_sells}건 / 매수 {n_buys}건"
        except Exception as e:
            err_str = str(e)
            _strategy_run_msg = f"오류: {err_str}"
            log.error("web.strategy_run_failed", err=err_str)
            # planner.run() 이 로그를 갖고 있으면 그걸 포함하여 저장
            log_text = "\n".join(getattr(planner, "_log_lines", []) or [])
            _save_error_with_log(err_str, log_text)
        finally:
            # 최종 로그 업데이트
            try:
                lines = getattr(planner, "_log_lines", None)
                if lines:
                    _strategy_live_log[:] = list(lines)
            except Exception:
                pass
            _strategy_running = False
            running_marker.unlink(missing_ok=True)

    def _save_error_with_log(err_str: str, log_text: str) -> None:
        try:
            hist_dir = _strategy_history_dir()
            hist_dir.mkdir(parents=True, exist_ok=True)
            from datetime import datetime as _dt
            ts = _dt.now(KST).strftime("%Y-%m-%d_%H-%M-%S")
            (hist_dir / f"{ts}.json").write_text(
                json.dumps({
                    "run_at": _dt.now(KST).isoformat(),
                    "dry_run": planner._cfg.dry_run,  # noqa: SLF001
                    "error": err_str,
                    "plan": {"sells": [], "buys": [], "summary": f"실행 실패: {err_str}"},
                    "results": [],
                    "log": log_text,
                }, ensure_ascii=False)
            )
            if log_text:
                (hist_dir / f"{ts}.log").write_text(log_text, encoding="utf-8")
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()
    return {"started": True, "message": "전략 수립을 시작했습니다."}


@app.get("/api/strategy/live-log")
def strategy_live_log():
    """실행 중 전략 수립가의 라이브 로그 반환."""
    return {
        "running": _strategy_running,
        "lines": _strategy_live_log[-500:],
        "msg": _strategy_run_msg,
    }


@app.post("/api/strategy/stop")
def stop_strategy():
    """전략 수립 중단 요청 (best-effort — LLM 호출 중에는 즉시 중단 안 될 수 있음)."""
    global _strategy_stop_requested
    if not _strategy_running:
        raise HTTPException(400, "실행 중인 전략 수립가가 없습니다.")
    _strategy_stop_requested = True
    return {"ok": True, "message": "중단 요청됨 (진행 중 단계 완료 후 종료)"}


@app.get("/api/strategy/history/{filename}")
def strategy_history_detail(filename: str):
    """이력 개별 항목의 상세 (JSON + 실행 로그 파일)."""
    import re as _re
    # 경로 트래버설 방지
    if not _re.fullmatch(r"[\w\-]+\.json", filename):
        raise HTTPException(400, "invalid filename")
    hist_dir = _strategy_history_dir()
    path = hist_dir / filename
    if not path.exists():
        raise HTTPException(404, "이력을 찾을 수 없습니다.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"이력 파싱 실패: {e}")

    # 별도 .log 파일이 있으면 병합
    log_path = path.with_suffix(".log")
    if log_path.exists():
        data["log"] = log_path.read_text(encoding="utf-8")

    return data


@app.get("/api/strategy/history")
def strategy_history(page: int = 1, per_page: int = 10):
    hist_dir = _strategy_history_dir()
    if not hist_dir.exists():
        return {"items": [], "total": 0, "page": page, "per_page": per_page, "total_pages": 0}

    files = sorted(hist_dir.glob("*.json"), reverse=True)
    total = len(files)
    start = (page - 1) * per_page
    page_files = files[start : start + per_page]

    items = []
    for f in page_files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            plan = data.get("plan") or {}
            error = data.get("error")
            sells = [
                {**s, "name": _ticker_name_cache.get(s.get("ticker", ""))}
                for s in plan.get("sells", [])
            ]
            buys = [
                {**b, "name": _ticker_name_cache.get(b.get("ticker", ""))}
                for b in plan.get("buys", [])
            ]
            items.append({
                "run_at": data.get("run_at"),
                "dry_run": data.get("dry_run", True),
                "status": "error" if error else "completed",
                "error": error,
                "summary": plan.get("summary", ""),
                "sectors_to_add": plan.get("sectors_to_add", []),
                "sectors_to_reduce": plan.get("sectors_to_reduce", []),
                "sells": sells,
                "buys": buys,
                "total_sell_krw": plan.get("total_sell_krw", 0),
                "total_buy_krw": plan.get("total_buy_krw", 0),
                "n_results": len(data.get("results", [])),
                "filename": f.name,
            })
        except Exception:
            pass

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page) if total else 0,
    }


# -------------------------------------------------------------------- #
# Local Collector API  (로컬 PC → EC2 자막 전송)                        #
# -------------------------------------------------------------------- #


@app.get("/api/collect/items")
def collect_items():
    """로컬 수집기가 수집해야 할 항목 목록 반환 (미완료 항목만).

    ``retry_items`` 는 재시도 요청을 받은 특정 video_id 들. 로컬 수집기는
    이들도 처리해야 한다 (기본 수집 로직과 독립된 경로).
    """
    global _local_retry_video_ids
    assert _rt is not None

    enabled_lists = {vl.name for vl in _rt.channels_cfg.video_lists if vl.enabled}
    with _rt.session_factory() as s:
        entries = (
            s.query(VideoListEntry)
            .filter(VideoListEntry.list_name.in_(enabled_lists))
            .all()
        )
        pending_list = []
        for e in entries:
            v = s.query(Video).filter_by(video_id=e.video_id).one_or_none()
            if v and v.status in {"embedded", "transcribed"}:
                continue
            pending_list.append({
                "video_id": e.video_id,
                "list_name": e.list_name,
                "source_url": e.source_url,
            })

        enabled_chs = s.query(Channel).filter_by(enabled=True).all()

        # 재시도 대기 큐: 인메모리 큐 + DB pending 채널 영상 합산
        retry_vid_ids: set[str] = set(_local_retry_video_ids)
        db_pending_channel = (
            s.query(Video)
            .filter(Video.status == "pending")
            .filter(~Video.source_type.startswith("list:"))
            .all()
        )
        for v in db_pending_channel:
            retry_vid_ids.add(v.video_id)

        retry_items = []
        for vid in retry_vid_ids:
            v = s.query(Video).filter_by(video_id=vid).one_or_none()
            if v is None:
                continue
            retry_items.append({
                "video_id": vid,
                "source_type": v.source_type or "channel",
                "title": v.title,
                "url": v.url or f"https://www.youtube.com/watch?v={vid}",
            })

    channel_items = []
    for ch in enabled_chs:
        spec = next(
            (sp for sp in _rt.channels_cfg.channels if sp.channel_id == ch.channel_id),
            None,
        )
        title_contains = (spec.title_contains if spec else None) or ch.title_contains
        channel_items.append({
            "channel_id": ch.channel_id,
            "handle": ch.handle,
            "name": ch.name,
            "title_contains": title_contains,
        })

    return {
        "video_list_items": pending_list,
        "channel_items": channel_items,
        "retry_items": retry_items,
        "preferred_languages": _rt.cfg.ingestion.transcript_languages,
    }


class TranscriptReceiveRequest(BaseModel):
    video_id: str
    source_type: str
    title: str | None = None
    url: str | None = None
    published_at: str | None = None
    duration_sec: int | None = None
    language: str = "ko"
    transcript_text: str


@app.post("/api/collect/transcript")
def receive_transcript(req: TranscriptReceiveRequest):
    """로컬에서 수집한 자막을 받아 EC2에서 임베딩·저장합니다."""
    assert _rt is not None
    import uuid as _uuid
    from moppu.ingestion.transcript import chunk_text
    from moppu.storage.db import Transcript as TrModel, TranscriptChunk

    pub_at = None
    if req.published_at:
        try:
            pub_at = datetime.fromisoformat(req.published_at.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            pass

    with _rt.session_factory() as s:
        is_list = req.source_type.startswith("list:")
        channel_fk = None
        if not is_list:
            ch = s.query(Channel).filter_by(channel_id=req.source_type).one_or_none()
            if ch:
                channel_fk = ch.id

        video = s.query(Video).filter_by(video_id=req.video_id).one_or_none()
        if video and video.status == "embedded":
            return {"ok": True, "skipped": True, "reason": "already embedded"}

        if not video:
            video = Video(
                video_id=req.video_id,
                channel_fk=channel_fk,
                source_type=req.source_type,
                title=req.title,
                published_at=pub_at,
                url=req.url,
                duration_sec=req.duration_sec,
                created_at=datetime.utcnow(),
                status="pending",
            )
            s.add(video)
            s.flush()
        else:
            if req.title:
                video.title = req.title
            if pub_at:
                video.published_at = pub_at

        video_pk = video.id

        old_tr = s.query(TrModel).filter_by(video_fk=video_pk).one_or_none()
        if old_tr:
            old_eids = [c.embedding_id for c in old_tr.chunks if c.embedding_id]
            if old_eids:
                _rt.vector_store.delete(old_eids)
            s.delete(old_tr)
            s.flush()

        transcript = TrModel(
            video_fk=video_pk,
            language=req.language,
            source="local_collector",
            text=req.transcript_text,
        )
        s.add(transcript)
        s.flush()

        chunks = chunk_text(
            req.transcript_text,
            _rt.cfg.embeddings.chunk_size,
            _rt.cfg.embeddings.chunk_overlap,
        )
        if not chunks:
            s.query(Video).filter_by(id=video_pk).update({"status": "failed", "error": "empty transcript"})
            s.commit()
            return {"ok": False, "reason": "empty transcript after chunking"}

        vectors = _rt.embedder.embed(chunks)
        ids = [f"{req.video_id}:{i}:{_uuid.uuid4().hex[:8]}" for i in range(len(chunks))]
        metadatas = [
            {
                "video_id": req.video_id,
                "source": req.source_type,
                "chunk_index": i,
                "published_at": pub_at.isoformat() if pub_at else None,
                "title": req.title or "",
            }
            for i in range(len(chunks))
        ]

        for i, (txt, eid) in enumerate(zip(chunks, ids)):
            s.add(TranscriptChunk(transcript_fk=transcript.id, chunk_index=i, text=txt, embedding_id=eid))

        s.query(Video).filter_by(id=video_pk).update({"status": "embedded", "error": None})
        s.commit()

    _rt.vector_store.upsert(ids=ids, embeddings=vectors, documents=chunks, metadatas=metadatas)
    log.info("collect.transcript_received", video_id=req.video_id, chunks=len(chunks))
    _write_pipeline_log(f"[LOCAL] 수신·임베딩: {req.video_id} ({len(chunks)}청크, {req.language})")
    return {"ok": True, "skipped": False, "video_id": req.video_id, "chunks": len(chunks)}


class CollectDoneRequest(BaseModel):
    success: int = 0
    total: int = 0
    message: str = "완료"
    videos: list[dict[str, Any]] = []   # [{video_id, title, url}]


@app.post("/api/collect/done")
def collect_done(req: CollectDoneRequest):
    """로컬 수집기가 작업 완료 후 호출 — 상태 업데이트 + 요약/페르소나 갱신 + Telegram 알림."""
    assert _rt is not None
    global _pipeline_run_msg
    _pipeline_run_msg = req.message
    _write_pipeline_log(f"[LOCAL 완료] {req.message}")

    if req.videos:
        lines = [f"📥 *수집 완료 ({req.success}/{req.total}건)*"]
        for v in req.videos[:10]:
            title = (v.get("title") or v.get("video_id") or "")[:35]
            url   = v.get("url") or f"https://www.youtube.com/watch?v={v.get('video_id','')}"
            lines.append(f"• [{title}]({url})")
        if len(req.videos) > 10:
            lines.append(f"_외 {len(req.videos) - 10}건_")
        _send_telegram("\n".join(lines))

        # 처리된 video_id 기준으로 요약 재생성 + 페르소나 점진 업데이트
        # created_at 날짜와 무관하게 실제 처리된 영상 반영 (재시도 포함)
        processed_ids = [v["video_id"] for v in req.videos if v.get("video_id")]

        def _do_update(video_ids: list[str]) -> None:
            from moppu.agent.daily_summary import generate_and_save
            from moppu.agent.persona import update_with_new
            try:
                _write_pipeline_log("[LOCAL] 수집 요약 재생성 중...")
                result = generate_and_save(
                    _rt.session_factory, _rt.llm, _rt.cfg.app.data_dir,
                    force=True, update_persona=False,  # 페르소나는 아래에서 별도 처리
                )
                if result:
                    usage = result.get("usage") or {}
                    if usage.get("input_tokens"):
                        _log_token_usage(_rt.cfg.llm.provider, _rt.cfg.llm.model, usage)
                _write_pipeline_log("[LOCAL] 수집 요약 재생성 완료")
            except Exception as e:
                _write_pipeline_log(f"[LOCAL][ERROR] 요약 재생성 실패: {e}")
                log.error("collect.summary_failed", err=str(e))

            try:
                _write_pipeline_log(f"[LOCAL] LSY 페르소나 업데이트 중 ({len(video_ids)}건)...")
                update_with_new(_rt.session_factory, _rt.llm, _rt.cfg.app.data_dir, video_ids)
                _write_pipeline_log("[LOCAL] LSY 페르소나 업데이트 완료")
            except Exception as e:
                _write_pipeline_log(f"[LOCAL][ERROR] 페르소나 업데이트 실패: {e}")
                log.error("collect.persona_failed", err=str(e))

        threading.Thread(target=_do_update, args=(processed_ids,), daemon=True).start()

    return {"ok": True}


@app.post("/api/notify/startup")
def notify_collector_startup():
    """로컬 수집기가 감시 모드 시작 시 호출 — Telegram 알림 전송."""
    _send_telegram("🖥 *수집 머신 시작*\n로컬 수집기 감시 모드가 시작되었습니다.")
    _write_pipeline_log("[LOCAL] 수집 머신 감시 모드 시작됨")
    return {"ok": True}


@app.post("/api/collect/request-run")
def request_local_run():
    """대시보드 → 로컬 수집기 실행 요청 (로컬이 폴링해서 감지)."""
    global _local_run_requested
    _local_run_requested = True
    _write_pipeline_log("[DASHBOARD] 로컬 수집기 실행 요청됨")
    return {"ok": True, "message": "로컬 수집기에 실행 신호 전송됨"}


@app.get("/api/collect/poll")
def poll_local_run():
    """로컬 수집기가 주기적으로 호출 — 실행 요청·재시도 큐 확인 및 heartbeat 갱신."""
    global _local_run_requested, _local_retry_video_ids, _local_last_heartbeat
    assert _rt is not None
    _local_last_heartbeat = datetime.now(timezone.utc)
    requested = _local_run_requested
    if requested:
        _local_run_requested = False

    # 인메모리 큐 + DB pending 채널 영상 합산 (큐 소진 후에도 재시도 가능)
    retry_ids: set[str] = set(_local_retry_video_ids)
    _local_retry_video_ids = []
    with _rt.session_factory() as s:
        db_pending = (
            s.query(Video.video_id)
            .filter(Video.status == "pending")
            .filter(~Video.source_type.startswith("list:"))
            .all()
        )
        for (vid,) in db_pending:
            retry_ids.add(vid)

    return {
        "requested": requested,
        "retry_video_ids": list(retry_ids),
    }


def _local_connection_status() -> dict[str, Any]:
    """로컬 수집기 연결 상태를 반환."""
    if _local_last_heartbeat is None:
        return {"connected": False, "last_seen": None, "stale_sec": None}
    delta = (datetime.now(timezone.utc) - _local_last_heartbeat).total_seconds()
    return {
        "connected": delta < LOCAL_HEARTBEAT_STALE_SEC,
        "last_seen": _local_last_heartbeat.isoformat(),
        "stale_sec": round(delta),
    }


@app.get("/api/collect/status")
def collect_status():
    """로컬 수집기 상태 (대시보드 상단 표시용)."""
    return _local_connection_status()


class RetryVideoRequest(BaseModel):
    video_id: str


@app.post("/api/pipeline/retry/{video_id}")
def retry_failed_video(video_id: str):
    """실패한 영상 1건 재시도 — 로컬 수집기에 신호 전달.

    - 영상 상태가 failed 가 아닌 경우 400
    - 로컬 수집기 연결 끊김이면 503 (Local Machine Error)
    """
    global _local_retry_video_ids
    assert _rt is not None
    with _rt.session_factory() as s:
        v = s.query(Video).filter_by(video_id=video_id).one_or_none()
        if v is None:
            raise HTTPException(404, "영상을 찾을 수 없습니다.")
        if v.status != "failed":
            raise HTTPException(400, f"재시도는 실패 상태에서만 가능합니다 (현재: {v.status}).")

        # 이전 임베딩·transcript 제거 (있다면)
        if v.transcript:
            old_eids = [c.embedding_id for c in v.transcript.chunks if c.embedding_id]
            if old_eids:
                try:
                    _rt.vector_store.delete(old_eids)
                except Exception as e:
                    log.warning("retry.vector_delete_failed", err=str(e))
            s.delete(v.transcript)
        v.status = "pending"
        v.error = None
        s.commit()

    conn = _local_connection_status()
    if not conn["connected"]:
        raise HTTPException(
            503,
            "Local Machine Error — 로컬 수집기에 연결할 수 없습니다. 수집 머신을 확인하세요.",
        )

    if video_id not in _local_retry_video_ids:
        _local_retry_video_ids.append(video_id)
    _write_pipeline_log(f"[RETRY] {video_id} — 로컬 수집기에 재시도 요청")
    return {"ok": True, "message": f"{video_id} 재시도 요청됨"}


@app.post("/api/collect/process")
def trigger_local_process():
    """로컬 수집 완료 후 요약 + 추천 질문 생성 트리거 (백그라운드)."""
    assert _rt is not None

    def _do() -> None:
        from moppu.agent.daily_summary import generate_and_save
        try:
            _write_pipeline_log("[LOCAL] 요약 생성 시작...")
            result = generate_and_save(_rt.session_factory, _rt.llm, _rt.cfg.app.data_dir, force=True)
            if result:
                usage = result.get("usage") or {}
                if usage.get("input_tokens"):
                    _log_token_usage(_rt.cfg.llm.provider, _rt.cfg.llm.model, usage)
            _write_pipeline_log("[LOCAL] 요약 생성 완료")
        except Exception as e:
            _write_pipeline_log(f"[LOCAL][ERROR] 요약 실패: {e}")
            log.error("collect.process_failed", err=str(e))

    threading.Thread(target=_do, daemon=True).start()
    return {"ok": True, "message": "요약 생성 시작됨 (백그라운드)"}
