# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Moppu is a Python pipeline that ingests YouTube channel transcripts, embeds them,
and feeds the retrieved context into a pluggable LLM agent that trades Korean
equities via the Korea Investment & Securities (KIS) OpenAPI. Telegram is the
primary control surface; a dashboard is planned but not built.

End-to-end flow: `ingestion → storage (SQLite + Chroma) → embeddings → RAG →
TraderAgent → Broker (KIS) / Telegram`. All components are constructed once in
`src/moppu/runtime.py::build_runtime`; CLI, scheduler, and bot all go through
that single entry point.

## Common commands

```bash
pip install -e ".[dev]"          # install with dev extras

moppu sync-channels              # reconcile channels.yaml:channels → DB
moppu backfill --channel-id UC…  # first-time ingest of all videos in a channel
moppu poll                       # one RSS-based new-video check
moppu scheduler                  # APScheduler loop (cron from config)
moppu sync-video-lists           # register channels.yaml:video_lists entries → DB (idempotent)
moppu ingest-lists               # ingest all pending videos from video lists
moppu ingest-lists --list-name semicon-highlights  # restrict to one list
moppu ask "<question>"           # one-shot agent run, prints JSON decision
moppu bot                        # Telegram bot (long-polling)

pytest                           # all tests
pytest tests/test_config.py -k resolve   # single test
ruff check .                     # lint
ruff format .                    # autoformat
mypy src                         # type-check (non-strict)
```

## Configuration model — two layers, don't mix them

- `.env` → `moppu.config.Settings` (pydantic-settings). Secrets, per-host paths,
  KIS env toggle (`paper` vs `real`). **Never commit populated values.**
- `config/config.yaml` → `moppu.config.AppConfig`. Non-secret knobs: LLM/embedding
  provider + model, thresholds, data dirs, cron. Safe to commit an example.
- `config/channels.yaml` → `moppu.config.ChannelsConfig`. List of tracked
  channels; either `channel_id` (`UC…`) or `handle` (`@…`) works.

When editing config, keep the YAML schema in `AppConfig` and its nested models
(`LLMConfig`, `EmbeddingsConfig`, `AgentConfig`, …) as the source of truth. YAML
unknown fields will fail validation — update the model first, then the YAML.

## Provider swap points (this is the whole point of the design)

- **LLM**: `moppu.llm.factory.build_llm` returns an `LLMProvider` (protocol in
  `moppu/llm/base.py`). Per-provider adapters in `openai_provider.py`,
  `anthropic_provider.py`, `google_provider.py`. Provider choice + per-provider
  model override live in `config.yaml:llm`. Add a provider by implementing the
  `LLMProvider` protocol and extending `build_llm`.
- **Embeddings**: `moppu.embeddings.embedder.build_embedder` mirrors the same
  pattern. Default is local `sentence-transformers` (no network) for dev.
- **Broker**: `moppu.broker.base.Broker` is a protocol. Only `KISBroker` exists
  today. TR codes (real vs paper) are class constants on `KISBroker`.
- **Vector store**: `VectorStore` protocol in `moppu/storage/vectorstore.py`.
  Only `ChromaVectorStore` exists; swap by implementing the protocol and
  wiring it in `runtime.py`.

## Ingestion model

Four paths, all converging on `Pipeline._ingest_one` in
`src/moppu/pipeline/orchestrator.py`:

1. **Backfill**: `Pipeline.backfill()` uses `YoutubeClient.list_all_videos` via
   yt-dlp to enumerate a channel's full upload history on first contact.
2. **Poll**: `Pipeline.poll_new()` via `ChannelWatcher.poll_once` hits the
   YouTube RSS feed (unauth, fast, ~15 latest). Cron-driven.
3. **Push**: `ChannelWatcher.handle_push` accepts WebSub/PubSubHubbub events;
   the HTTP endpoint to receive them is **not yet built** (planned FastAPI
   module). Wire it to call `Pipeline.handle_push_event`.
4. **Video list**: `Pipeline.ingest_from_lists()`. The list in
   `channels.yaml:video_lists[].videos` is the single source of truth — no
   polling. `sync_video_lists()` diffs config vs `VideoListEntry` DB table and
   registers new rows. Only newly registered rows get ingested on the next
   `ingest_from_lists()` call. `parse_video_id()` in
   `src/moppu/ingestion/youtube.py` handles all common URL formats plus bare IDs.

Transcripts come from `youtube-transcript-api`. If a video has no captions the
video is marked `failed` with a reason; a future Whisper fallback is a TODO on
`TranscriptFetcher`.

Chunks go both to SQL (`TranscriptChunk.embedding_id` links to Chroma id) and
to Chroma. Keep these in sync — re-embedding means deleting old Chroma ids
before upsert.

## Agent / prompt update semantics

The "living" system prompt is rebuilt from disk **every call** to
`TraderAgent.decide` via `PromptBuilder.build_system_prompt`. The base template
is `config/prompts/trader.system.example.md` (committed). Copy it to
`trader.system.md` (gitignored) for local edits, and point `agent.prompt_template`
there. `{{channels_summary}}` and
`{{recent_videos}}` are rendered from the DB so newly-ingested videos take
effect on the next call with no code change.

Retrieved transcript excerpts are injected into the **user** message, not the
system prompt — easier to cite, and it keeps the system prompt small enough to
cache across calls if/when caching is added.

The agent must output strict JSON matching `_DECISION_SCHEMA` (
`src/moppu/agent/trader_agent.py`). Parse failures return `HOLD` — never raise
— to keep Telegram responsive.

## Safety defaults

- `agent.dry_run: true` is the default in `config.example.yaml`. Keep it on
  until the user explicitly flips it (Telegram `/dryrun off` or config edit).
- `agent.max_order_krw` is enforced in `TraderAgent.act` **before** hitting the
  broker. Don't bypass this in code paths you add.
- `KIS_ENV=paper` is the default in `.env.example`. Switching to `real` changes
  both the base URL *and* the TR codes used for orders.

## Testing

`pytest` is configured via `pyproject.toml` with `pythonpath = ["src"]`.
SQLite in-memory is fine for DB-layer tests (`tests/test_db.py`). Avoid
network-dependent tests in the default suite — they'll be flaky in CI. When
adding provider tests, inject a fake `LLMProvider`/`Broker`/`Embedder`;
the factories are the only places that touch real SDKs.

## What's not built yet (don't assume it exists)

- FastAPI endpoint for WebSub push callbacks.
- Whisper fallback for videos without captions.
- Dashboard.
- Alembic migrations — `init_db` calls `Base.metadata.create_all` directly.
- Any form of backtest or strategy evaluation harness.
