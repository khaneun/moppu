"""Telegram control surface.

Commands:
- /help         — 전체 커맨드 목록
- /start        — 시작 (= /help)
- /status       — 채널·영상·임베딩 현황
- /dashboard    — 대시보드 링크 (클릭 가능)
- /appstatus    — EC2 서비스 실행 상태
- /run          — 파이프라인 수동 실행
- /summary      — 오늘의 영상 요약
- /ask <msg>    — 에이전트에게 질문
- /model        — 현재 LLM 모델
- /mode         — KIS 투자 모드
- /dryrun on|off— dry_run 토글
- /emergency    — 긴급 중단
- /resume       — 긴급 중단 해제
- /poll         — 채널 RSS 폴링 1회
- /backfill <ch>— 채널 백필
- /ingestlist   — 영상 목록 수집
"""

from __future__ import annotations

import subprocess
from functools import wraps
from typing import Any, Awaitable, Callable

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from moppu.agent.trader_agent import TraderAgent
from moppu.config import Settings
from moppu.logging_setup import get_logger
from moppu.pipeline import Pipeline

log = get_logger(__name__)

_HELP = """📊 *Moppu Bot 커맨드*

*현황 조회*
/status — 채널·영상·임베딩 현황
/dashboard — 대시보드 접속 링크
/appstatus — EC2 서비스 실행 상태
/model — 현재 LLM 모델
/mode — KIS 투자 모드

*수집 제어*
/run — 파이프라인 즉시 실행
/summary — 오늘의 영상 요약
/poll — RSS 폴링 1회
/ingestlist \\[이름\\] — 영상 목록 수집
/backfill <채널ID\\|all> — 초회 백필

*에이전트*
/ask <질문> — 애널리스트에게 질문

*시스템 제어*
/dryrun on\\|off — 실주문 토글
/emergency — 긴급 중단 \\(dry\\_run=ON\\)
/resume — 긴급 중단 해제
"""


def _guard(allowed_ids: set[int]):
    def decorator(fn: Callable[..., Awaitable[Any]]):
        @wraps(fn)
        async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            chat_id = update.effective_chat.id if update.effective_chat else None
            if not allowed_ids or chat_id in allowed_ids:
                return await fn(self, update, context)
            log.warning("telegram.unauthorized", chat_id=chat_id)
            if update.effective_message:
                await update.effective_message.reply_text("Unauthorized chat id.")
        return wrapper
    return decorator


def _get_public_ip() -> str:
    for url in [
        "http://169.254.169.254/latest/meta-data/public-ipv4",
        "https://api.ipify.org",
    ]:
        try:
            resp = httpx.get(url, timeout=2)
            if resp.status_code == 200:
                return resp.text.strip()
        except Exception:
            continue
    return "IP 조회 실패"


def send_telegram_message(settings: Settings, text: str, parse_mode: str = "Markdown") -> None:
    """봇 프로세스 밖에서도 텔레그램 메시지를 전송합니다 (API 직접 호출)."""
    if not settings.telegram_bot_token:
        return
    chat_ids = settings.allowed_chat_ids
    if not chat_ids:
        return
    for chat_id in chat_ids:
        try:
            httpx.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=5,
            )
        except Exception as e:
            log.warning("telegram.send_failed", chat_id=chat_id, err=str(e))


class TelegramBot:
    def __init__(
        self,
        *,
        settings: Settings,
        pipeline: Pipeline,
        agent: TraderAgent,
    ) -> None:
        if not settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required to run the bot")
        self._settings = settings
        self._pipeline = pipeline
        self._agent = agent
        self._allowed = set(settings.allowed_chat_ids)

        self._app = Application.builder().token(settings.telegram_bot_token).build()
        self._register()

    def _register(self) -> None:
        g = _guard(self._allowed)
        handlers = [
            ("start",       TelegramBot._cmd_help),
            ("help",        TelegramBot._cmd_help),
            ("status",      TelegramBot._cmd_status),
            ("dashboard",   TelegramBot._cmd_dashboard),
            ("appstatus",   TelegramBot._cmd_appstatus),
            ("run",         TelegramBot._cmd_run),
            ("summary",     TelegramBot._cmd_summary),
            ("ask",         TelegramBot._cmd_ask),
            ("model",       TelegramBot._cmd_model),
            ("mode",        TelegramBot._cmd_mode),
            ("dryrun",      TelegramBot._cmd_dryrun),
            ("emergency",   TelegramBot._cmd_emergency),
            ("resume",      TelegramBot._cmd_resume),
            ("poll",        TelegramBot._cmd_poll),
            ("backfill",    TelegramBot._cmd_backfill),
            ("ingestlist",  TelegramBot._cmd_ingest_list),
        ]
        for name, fn in handlers:
            self._app.add_handler(CommandHandler(name, g(fn).__get__(self)))

    def run_polling(self) -> None:
        log.info("telegram.starting")
        self._app.run_polling()

    # ------------------------------------------------------------------ #
    # Handlers                                                            #
    # ------------------------------------------------------------------ #

    async def _cmd_help(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(_HELP, parse_mode="MarkdownV2")

    async def _cmd_status(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        from moppu.storage.db import Channel, Video
        with self._pipeline._sf() as s:  # noqa: SLF001
            n_ch   = s.query(Channel).filter_by(enabled=True).count()
            n_vid  = s.query(Video).count()
            n_emb  = s.query(Video).filter(Video.status == "embedded").count()
            n_fail = s.query(Video).filter(Video.status == "failed").count()
        mode = self._settings.kis_env
        dry  = self._agent._cfg.dry_run  # noqa: SLF001
        await update.message.reply_text(
            f"📊 *현황*\n"
            f"채널: {n_ch}개 활성\n"
            f"영상: {n_vid}개 (임베딩 {n_emb} / 실패 {n_fail})\n"
            f"투자모드: {'실전' if mode == 'real' else '모의'}\n"
            f"Dry Run: {'ON' if dry else 'OFF'}",
            parse_mode="Markdown",
        )

    async def _cmd_dashboard(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        ip  = _get_public_ip()
        url = f"http://{ip}:8000"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🖥 대시보드 열기", url=url)]])
        await update.message.reply_text(
            f"*Moppu Monitor*\n`{url}`",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    async def _cmd_appstatus(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        services = ["moppu-dashboard", "moppu-scheduler", "moppu-bot"]
        lines = ["*서비스 상태*"]
        for svc in services:
            try:
                r = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=3,
                )
                st = r.stdout.strip()
            except Exception:
                st = "unknown"
            icon = "🟢" if st == "active" else "🔴"
            lines.append(f"{icon} `{svc}`: {st}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _cmd_run(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("⚙️ 파이프라인 실행 중...")
        try:
            self._pipeline.sync_video_lists()
            n = self._pipeline.ingest_from_lists()
            cfg = self._pipeline._cfg  # noqa: SLF001
            if n > 0:
                from moppu.agent.daily_summary import generate_and_save
                generate_and_save(
                    self._pipeline._sf,   # noqa: SLF001
                    self._agent._llm,     # noqa: SLF001
                    cfg.app.data_dir,
                    force=True,
                )
            await update.message.reply_text(
                f"✅ 완료: {n}건 수집" + (" + 요약 생성" if n > 0 else "")
            )
        except Exception as e:
            await update.message.reply_text(f"❌ 오류: {e}")

    async def _cmd_summary(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        from datetime import datetime, timedelta, timezone
        from moppu.agent.daily_summary import load
        KST = timezone(timedelta(hours=9))
        today_str = datetime.now(KST).strftime("%Y-%m-%d")
        saved = load(self._pipeline._cfg.app.data_dir, today_str)  # noqa: SLF001
        if not saved or not saved.get("summary"):
            await update.message.reply_text("오늘 수집된 요약이 없습니다.")
            return
        text = f"📋 *{today_str} 영상 요약*\n\n{saved['summary'][:1000]}"
        if len(saved["summary"]) > 1000:
            text += "\n\n_(요약 일부 표시)_"
        await update.message.reply_text(text, parse_mode="Markdown")

    async def _cmd_model(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        llm = self._agent._llm  # noqa: SLF001
        await update.message.reply_text(
            f"🤖 현재 LLM: `{llm.name}` / `{llm.model}`", parse_mode="Markdown"
        )

    async def _cmd_mode(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        mode = self._settings.kis_env
        dry  = self._agent._cfg.dry_run  # noqa: SLF001
        await update.message.reply_text(
            f"💰 투자 모드: *{'실전' if mode == 'real' else '모의'}*\n"
            f"Dry Run: *{'ON' if dry else 'OFF'}*",
            parse_mode="Markdown",
        )

    async def _cmd_poll(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        n = self._pipeline.poll_new()
        await update.message.reply_text(f"Polled. New videos ingested: {n}")

    async def _cmd_ingest_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        name = " ".join(context.args or []).strip() or None
        self._pipeline.sync_video_lists()
        n = self._pipeline.ingest_from_lists(list_name=name)
        label = f"'{name}'" if name else "all lists"
        await update.message.reply_text(f"Ingested {n} new video(s) from {label}.")

    async def _cmd_backfill(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: /backfill <channel_id|all>")
            return
        ids = None if args[0] == "all" else [args[0]]
        n = self._pipeline.backfill(channel_ids=ids)
        await update.message.reply_text(f"Backfill done. Processed: {n}")

    async def _cmd_ask(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = " ".join(context.args or []).strip()
        if not msg:
            await update.message.reply_text("Usage: /ask <your question>")
            return
        await update.message.reply_text("🤔 분석 중...")
        try:
            result = self._agent.chat(msg)
            text = result["text"][:3000]
            citations = result.get("citations", [])
            reply = text
            if citations:
                reply += "\n\n📎 " + " / ".join(
                    f"[{c.get('title', c['video_id'])[:20]}]({c['url']})"
                    for c in citations[:3]
                )
            await update.message.reply_text(
                reply, parse_mode="Markdown", disable_web_page_preview=True
            )
        except Exception as e:
            await update.message.reply_text(f"❌ 오류: {e}")

    async def _cmd_dryrun(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if not args or args[0].lower() not in {"on", "off"}:
            await update.message.reply_text("Usage: /dryrun on|off")
            return
        self._agent._cfg.dry_run = args[0].lower() == "on"  # noqa: SLF001
        await update.message.reply_text(f"dry_run = {self._agent._cfg.dry_run}")  # noqa: SLF001

    async def _cmd_emergency(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        stop_file = self._pipeline._cfg.app.data_dir / ".emergency_stop"  # noqa: SLF001
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.write_text("telegram emergency stop")
        self._agent._cfg.dry_run = True  # noqa: SLF001
        await update.message.reply_text("🚨 긴급 중단 활성화. dry_run=true 전환됨.")

    async def _cmd_resume(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        stop_file = self._pipeline._cfg.app.data_dir / ".emergency_stop"  # noqa: SLF001
        stop_file.unlink(missing_ok=True)
        await update.message.reply_text("✅ 긴급 중단 해제. 스케줄러 정상 운영 재개.")
