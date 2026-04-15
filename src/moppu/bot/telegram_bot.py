"""Telegram control surface.

Commands:

- ``/start``        — sanity check
- ``/status``       — # channels / videos / last poll time
- ``/poll``         — trigger a polling cycle now
- ``/backfill <ch>`` — backfill a channel by id or "all"
- ``/ask <msg>``    — route ``<msg>`` to the agent and reply with its JSON decision
- ``/dryrun on|off``— toggle agent.dry_run at runtime

Uses ``python-telegram-bot``'s v21 async API. Auth is IP-address agnostic; we
gate commands with an allowlist of chat IDs from ``TELEGRAM_ALLOWED_CHAT_IDS``.
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from moppu.agent.trader_agent import TraderAgent
from moppu.config import Settings
from moppu.logging_setup import get_logger
from moppu.pipeline import Pipeline

log = get_logger(__name__)


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
        self._app.add_handler(CommandHandler("start", g(TelegramBot._cmd_start).__get__(self)))
        self._app.add_handler(CommandHandler("status", g(TelegramBot._cmd_status).__get__(self)))
        self._app.add_handler(CommandHandler("poll", g(TelegramBot._cmd_poll).__get__(self)))
        self._app.add_handler(CommandHandler("backfill", g(TelegramBot._cmd_backfill).__get__(self)))
        self._app.add_handler(CommandHandler("ingestlist", g(TelegramBot._cmd_ingest_list).__get__(self)))
        self._app.add_handler(CommandHandler("ask", g(TelegramBot._cmd_ask).__get__(self)))
        self._app.add_handler(CommandHandler("dryrun", g(TelegramBot._cmd_dryrun).__get__(self)))

    def run_polling(self) -> None:
        log.info("telegram.starting")
        self._app.run_polling()

    # ------------------------------------------------------------------ #
    # Handlers                                                            #
    # ------------------------------------------------------------------ #

    async def _cmd_start(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("Moppu online. Try /status or /ask <question>.")

    async def _cmd_status(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        from moppu.storage.db import Channel, Video  # local import to avoid cycles

        with self._pipeline._sf() as s:  # noqa: SLF001 — intentional, avoid re-plumbing
            n_ch = s.query(Channel).count()
            n_vid = s.query(Video).count()
        await update.message.reply_text(f"channels={n_ch} videos={n_vid}")

    async def _cmd_poll(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        n = self._pipeline.poll_new()
        await update.message.reply_text(f"Polled. New videos ingested: {n}")

    async def _cmd_ingest_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Usage: /ingestlist [list_name]  — sync + ingest a video list."""
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
        decision = self._agent.decide(msg)
        result = self._agent.act(decision)
        await update.message.reply_text(
            f"Decision: {decision.action} {decision.ticker or ''} qty={decision.quantity}\n"
            f"Reason: {decision.reason}\nExecuted: {result.get('executed')}"
        )

    async def _cmd_dryrun(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if not args or args[0].lower() not in {"on", "off"}:
            await update.message.reply_text("Usage: /dryrun on|off")
            return
        self._agent._cfg.dry_run = args[0].lower() == "on"  # noqa: SLF001
        await update.message.reply_text(f"dry_run = {self._agent._cfg.dry_run}")  # noqa: SLF001
