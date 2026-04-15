"""Typer-based CLI.

Examples::

    moppu sync-channels
    moppu backfill --channel-id UCxxxx
    moppu poll
    moppu ask "오늘 S&P 500 관련 어떻게 봐?"
    moppu bot
    moppu scheduler
    moppu dashboard
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from typing import Annotated

import typer

from moppu.runtime import build_runtime

app = typer.Typer(add_completion=False, help="Moppu: YouTube-context trading agent")


def _try_generate_summary(rt) -> None:
    """Generate and persist today's ingestion summary (swallows errors)."""
    from moppu.agent.daily_summary import generate_and_save
    try:
        result = generate_and_save(rt.session_factory, rt.llm, rt.cfg.app.data_dir)
        if result:
            typer.echo("  요약 생성 완료 → " + rt.cfg.app.data_dir.as_posix() +
                       f"/daily_summary_{result['date']}.json")
    except Exception as e:
        typer.echo(f"  [WARN] 요약 생성 실패: {e}")


def _kill_port(port: int) -> None:
    """SIGTERM any process listening on *port*, then wait 1 s."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True,
        )
        pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
        for pid_str in pids:
            try:
                os.kill(int(pid_str), signal.SIGTERM)
            except ProcessLookupError:
                pass
        if pids:
            import time
            time.sleep(1)
            typer.echo(f"  기존 프로세스 종료 (port {port})")
    except Exception:
        pass


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
    if n > 0:
        _try_generate_summary(rt)


@app.command("update-persona")
def update_persona(
    force: Annotated[bool, typer.Option("--force", help="기존 페르소나 무시하고 전체 재생성")] = False,
) -> None:
    """수집된 transcript로부터 LSY Agent 페르소나를 생성·업데이트합니다."""
    from moppu.agent.persona import generate
    rt = build_runtime()
    typer.echo("페르소나 생성 중 (LLM 호출)...")
    result = generate(rt.session_factory, rt.llm, rt.cfg.app.data_dir, force=force)
    if result:
        path = rt.cfg.app.data_dir / "agent_persona.md"
        typer.echo(f"✓ 페르소나 저장: {path} ({len(result)}자)")
    else:
        typer.echo("수집된 영상이 없어 페르소나를 생성할 수 없습니다.")


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
    stop_file = rt.cfg.app.data_dir / ".emergency_stop"

    def guarded(fn):
        def wrapper():
            if stop_file.exists():
                typer.echo("  [SKIP] emergency stop active")
                return
            return fn()
        return wrapper

    def upload_day_job():
        """Poll upload-day channels, then generate daily summary."""
        if stop_file.exists():
            typer.echo("  [SKIP] emergency stop active")
            return
        from datetime import datetime as _dt
        today_str = _dt.now().strftime("%Y-%m-%d")
        log_file = rt.cfg.app.data_dir / "pipeline.log"

        def wlog(msg: str) -> None:
            ts = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_file, "a", encoding="utf-8") as _f:
                _f.write(f"[{ts}] [SCHEDULER] {msg}\n")

        # 수동 실행 마커가 있으면 당일 자정 자동 실행 건너뜀
        ran_file = rt.cfg.app.data_dir / f".pipeline_ran_{today_str}"
        if ran_file.exists():
            wlog(f"[SKIP] 오늘 이미 수동 실행됨 ({today_str})")
            typer.echo(f"  [SKIP] 오늘 이미 수동 실행됨 ({today_str})")
            return
        wlog("=== 자동 실행 시작 (upload_day_poll) ===")
        n = rt.pipeline.poll_upload_day_channels()
        wlog(f"수집 완료: {n}건")
        if n > 0:
            wlog("요약 생성 중...")
            _try_generate_summary(rt)
            wlog("요약 생성 완료")
        wlog("=== 자동 실행 완료 ===")

    sched = BlockingScheduler()

    cron = rt.cfg.scheduler.poll_channels_cron
    sched.add_job(guarded(rt.pipeline.poll_new), CronTrigger.from_crontab(cron), id="poll_channels")
    typer.echo(f"  poll_channels   : {cron}")

    upload_day_cron = rt.cfg.scheduler.upload_day_cron
    sched.add_job(upload_day_job, CronTrigger.from_crontab(upload_day_cron), id="upload_day_poll")
    typer.echo(f"  upload_day_poll : {upload_day_cron}")

    typer.echo("Scheduler started. Ctrl-C to stop.")
    sched.start()


@app.command("dashboard")
def dashboard(
    host: Annotated[str, typer.Option(help="Bind address")] = "0.0.0.0",
    port: Annotated[int, typer.Option(help="Port number")] = 8000,
) -> None:
    """Start the Moppu Monitor web dashboard (kills existing process on port first)."""
    import uvicorn

    _kill_port(port)
    typer.echo(f"Starting Moppu Monitor at http://{host}:{port}")
    uvicorn.run("moppu.web.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    app()
