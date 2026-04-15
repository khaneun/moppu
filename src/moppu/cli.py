"""Typer-based CLI.

Examples::

    moppu sync-channels
    moppu backfill --channel-id UCxxxx
    moppu poll
    moppu ask "오늘 S&P 500 관련 어떻게 봐?"
    moppu bot
    moppu scheduler
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

from moppu.runtime import build_runtime

app = typer.Typer(add_completion=False, help="Moppu: YouTube-context trading agent")


@app.command("sync-channels")
def sync_channels() -> None:
    """Reconcile channels.yaml into the DB."""
    rt = build_runtime()
    rows = rt.pipeline.sync_channels()
    typer.echo(f"Synced {len(rows)} channels.")


@app.command("backfill")
def backfill(
    channel_id: Annotated[str | None, typer.Option(help="UC... id; omit for all enabled")] = None,
) -> None:
    """Initial ingestion of every uploaded video for channel(s)."""
    rt = build_runtime()
    n = rt.pipeline.backfill(channel_ids=[channel_id] if channel_id else None)
    typer.echo(f"Backfill done. Videos processed: {n}")


@app.command("sync-video-lists")
def sync_video_lists() -> None:
    """Register video list entries from channels.yaml into the DB (idempotent)."""
    rt = build_runtime()
    result = rt.pipeline.sync_video_lists()
    for name, new_count in result.items():
        typer.echo(f"  {name}: {new_count} new entries registered")
    typer.echo(f"Done. Total lists: {len(result)}")


@app.command("ingest-lists")
def ingest_lists(
    list_name: Annotated[str | None, typer.Option(help="Restrict to a single list by name")] = None,
) -> None:
    """Ingest pending videos from video lists (runs sync first)."""
    rt = build_runtime()
    rt.pipeline.sync_video_lists()
    n = rt.pipeline.ingest_from_lists(list_name=list_name)
    typer.echo(f"Ingested: {n}")


@app.command("poll")
def poll() -> None:
    """One polling cycle for new videos."""
    rt = build_runtime()
    n = rt.pipeline.poll_new()
    typer.echo(f"Polled. New videos ingested: {n}")


@app.command("ask")
def ask(message: Annotated[str, typer.Argument(help="Question / instruction for the agent")]) -> None:
    """Run the agent once and print its structured decision + execution result."""
    rt = build_runtime()
    decision = rt.agent.decide(message)
    result = rt.agent.act(decision)
    typer.echo(json.dumps({"decision": decision.model_dump(), "result": result}, ensure_ascii=False, indent=2))


@app.command("bot")
def bot() -> None:
    """Start the Telegram bot (blocking, polling mode)."""
    from moppu.bot import TelegramBot

    rt = build_runtime()
    tbot = TelegramBot(settings=rt.settings, pipeline=rt.pipeline, agent=rt.agent)
    tbot.run_polling()


@app.command("scheduler")
def scheduler() -> None:
    """Run APScheduler in-process to poll channels on a cron."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    rt = build_runtime()
    sched = BlockingScheduler()

    cron = rt.cfg.scheduler.poll_channels_cron
    sched.add_job(rt.pipeline.poll_new, CronTrigger.from_crontab(cron), id="poll_channels")
    typer.echo(f"Scheduler started with cron '{cron}'. Ctrl-C to stop.")
    sched.start()


if __name__ == "__main__":
    app()
