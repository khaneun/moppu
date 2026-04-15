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
_token_log_path: Path | None = None
_sessions: set[str] = set()
_pipeline_running: bool = False
_pipeline_run_msg: str = ""
_deleted_embedding_count: int = 0


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
        "cash_balance_krw": 0,
        "total_eval_krw": 0,
        "positions": [],
        "kis_mode": _rt.settings.kis_env,
        "dry_run": _rt.cfg.agent.dry_run,
        "emergency_stopped": _is_emergency_stopped(),
    }
    if _rt.broker is None:
        data["broker_error"] = "브로커 미설정 (API 키 확인 필요)"
        return data
    try:
        data["cash_balance_krw"] = _rt.broker.get_cash_balance_krw()
        positions = _rt.broker.get_positions()
        pos_list = []
        total_eval = data["cash_balance_krw"]
        for p in positions:
            eval_amt = p.avg_price * p.quantity + (p.unrealized_pl or 0)
            total_eval += eval_amt
            pl_rate = ((p.unrealized_pl or 0) / (p.avg_price * p.quantity) * 100) if p.avg_price * p.quantity > 0 else 0
            pos_list.append({
                "ticker": p.ticker,
                "quantity": p.quantity,
                "avg_price": p.avg_price,
                "eval_amount": eval_amt,
                "unrealized_pl": p.unrealized_pl or 0,
                "pl_rate": round(pl_rate, 2),
            })
        data["positions"] = pos_list
        data["total_eval_krw"] = total_eval
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
    }


@app.post("/api/pipeline/run")
def run_pipeline():
    """수동으로 파이프라인을 실행합니다 (비동기 백그라운드)."""
    global _pipeline_running
    assert _rt is not None
    if _pipeline_running:
        raise HTTPException(409, "파이프라인이 이미 실행 중입니다.")

    def _run() -> None:
        global _pipeline_running, _pipeline_run_msg
        _pipeline_running = True
        try:
            _write_pipeline_log("=== 수동 실행 시작 ===")
            _pipeline_run_msg = "영상 목록 동기화 중..."
            _write_pipeline_log(_pipeline_run_msg)
            _rt.pipeline.sync_video_lists()
            _pipeline_run_msg = "영상 수집 중..."
            _write_pipeline_log(_pipeline_run_msg)
            n = _rt.pipeline.ingest_from_lists()
            _write_pipeline_log(f"영상 수집 완료: {n}건")
            _pipeline_run_msg = f"수집 완료 ({n}건) — 요약 생성 중..."
            _write_pipeline_log("요약 및 추천 질문 생성 중...")
            from moppu.agent.daily_summary import generate_and_save
            generate_and_save(_rt.session_factory, _rt.llm, _rt.cfg.app.data_dir, force=True)
            _write_pipeline_log("요약 생성 완료")
            # 수동 실행 마커 생성 → 당일 자정 스케줄러 실행 방지
            today_str = datetime.now(KST).strftime("%Y-%m-%d")
            (_rt.cfg.app.data_dir / f".pipeline_ran_{today_str}").write_text(
                datetime.now(timezone.utc).isoformat()
            )
            _pipeline_run_msg = f"완료 ({n}건 수집)"
            _write_pipeline_log(f"=== 수동 실행 완료 ({n}건) ===")
        except Exception as e:
            _pipeline_run_msg = f"오류: {e}"
            _write_pipeline_log(f"[ERROR] {e}")
            log.error("pipeline.manual_run_failed", err=str(e))
        finally:
            _pipeline_running = False

    threading.Thread(target=_run, daemon=True).start()
    return {"started": True, "message": "파이프라인 실행을 시작했습니다."}


@app.get("/api/pipeline/log")
def pipeline_log():
    """파이프라인 실행 로그 파일의 마지막 200줄을 반환합니다."""
    assert _rt is not None
    path = _rt.cfg.app.data_dir / "pipeline.log"
    if not path.exists():
        return {"lines": [], "exists": False}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return {"lines": lines[-200:], "exists": True, "total": len(lines)}
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
        # Chroma 임베딩 삭제
        n_emb = _delete_video_embeddings(s, [video_id])
        # Video 레코드도 삭제 (연관 transcript/chunk cascade)
        v = s.query(Video).filter_by(video_id=video_id).one_or_none()
        if v:
            s.delete(v)
        s.delete(entry)
        s.commit()
    _deleted_embedding_count += n_emb
    return {"ok": True, "deleted_embeddings": n_emb}
