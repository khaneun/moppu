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


@app.command("strategy")
def strategy(
    dry_run: Annotated[bool | None, typer.Option("--dry-run/--live", help="dry_run 강제 설정")] = None,
) -> None:
    """전략 수립가를 즉시 실행합니다 (포트폴리오 분석 → 계획 수립 → 매매)."""
    rt = build_runtime()
    if rt.strategy_planner is None:
        from moppu.agent.strategy_planner import StrategyPlannerAgent
        from moppu.config import StrategyPlannerConfig
        planner = StrategyPlannerAgent(
            cfg=StrategyPlannerConfig(enabled=True, dry_run=dry_run if dry_run is not None else True),
            settings=rt.settings,
            llm=rt.llm,
            trader_agent=rt.agent,
            broker=rt.broker,
            data_dir=rt.cfg.app.data_dir,
        )
    else:
        planner = rt.strategy_planner
        if dry_run is not None:
            planner._cfg.dry_run = dry_run  # noqa: SLF001

    typer.echo(f"전략 수립가 시작 (dry_run={planner._cfg.dry_run})...")  # noqa: SLF001
    result = planner.run()
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


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
    """Run APScheduler in-process on KST.

    **주의**: EC2 에서 YouTube 에 직접 접근하는 job 은 생성하지 않는다.
    로컬 수집기가 YouTube 수집을 담당하므로, 자정 job 은 로컬 수집기에
    실행 신호만 보내고 요약 생성을 대기한다.
    """
    from zoneinfo import ZoneInfo
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    KST = ZoneInfo("Asia/Seoul")
    rt = build_runtime()
    stop_file = rt.cfg.app.data_dir / ".emergency_stop"

    def upload_day_job() -> None:
        """자정 자동 실행 — 로컬 수집기에 실행 신호만 송신."""
        if stop_file.exists():
            typer.echo("  [SKIP] emergency stop active")
            return
        from datetime import datetime as _dt
        log_file = rt.cfg.app.data_dir / "pipeline.log"

        def wlog(msg: str) -> None:
            ts = _dt.now(KST).strftime("%Y-%m-%d %H:%M:%S")
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as _f:
                _f.write(f"[{ts}] [SCHEDULER] {msg}\n")

        wlog("=== 자정 자동 실행 시작 — 로컬 수집기 신호 ===")
        # EC2 에서는 YouTube 를 절대 호출하지 않음. 로컬 수집기에 신호만 전달.
        try:
            rt.pipeline.sync_video_lists()
            wlog("video_list 동기화 완료")
        except Exception as e:
            wlog(f"[ERROR] sync_video_lists 실패: {e}")
        # 로컬 수집기 실행 요청 플래그는 웹 프로세스가 관리하므로 스케줄러에서
        # 트리거할 방법은 없다. 로컬 수집기는 자체 KST 자정 타이머로 동작한다.
        wlog("=== 로컬 수집기 자체 타이머로 수집 진행 ===")

    sched = BlockingScheduler(timezone=KST)

    upload_day_cron = rt.cfg.scheduler.upload_day_cron
    sched.add_job(
        upload_day_job,
        CronTrigger.from_crontab(upload_day_cron, timezone=KST),
        id="upload_day_poll",
    )
    typer.echo(f"  upload_day_poll : {upload_day_cron} (KST)")

    # 전략 수립가 스케줄 (설정에서 enabled=true 일 때만) — KST 기준
    sp_cfg = rt.cfg.strategy_planner
    if sp_cfg.enabled:
        from moppu.agent.strategy_planner import StrategyPlannerAgent

        if rt.strategy_planner is None:
            rt.strategy_planner = StrategyPlannerAgent(
                cfg=sp_cfg,
                settings=rt.settings,
                llm=rt.llm,
                trader_agent=rt.agent,
                broker=rt.broker,
                data_dir=rt.cfg.app.data_dir,
            )
        planner = rt.strategy_planner

        def strategy_job() -> None:
            if stop_file.exists():
                typer.echo("  [SKIP] emergency stop active (strategy_planner)")
                return
            typer.echo("  [strategy_planner] 전략 수립 시작 (KST)...")
            try:
                planner.run()
            except Exception as e:
                typer.echo(f"  [strategy_planner] 오류: {e}")

        sched.add_job(
            strategy_job,
            CronTrigger.from_crontab(sp_cfg.cron, timezone=KST),
            id="strategy_planner",
        )
        typer.echo(f"  strategy_planner: {sp_cfg.cron} (KST, dry_run={sp_cfg.dry_run})")
    else:
        typer.echo("  strategy_planner: disabled (config: strategy_planner.enabled=false)")

    typer.echo("Scheduler started (timezone=Asia/Seoul). Ctrl-C to stop.")
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
