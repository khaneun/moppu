"""Runtime wiring — builds the whole object graph from config.

Centralizes construction so CLI, scheduler, and tests all go through the same
entry point and we don't accidentally build two DB engines or two vector
stores per process.
"""

from __future__ import annotations

from dataclasses import dataclass

from moppu.agent import PromptBuilder, RAGRetriever, TraderAgent
from moppu.agent.strategy_planner import StrategyPlannerAgent
from moppu.broker import KISBroker
from moppu.broker.base import Broker
from moppu.config import AppConfig, ChannelsConfig, Settings, load_app_config, load_channels
from moppu.embeddings import build_embedder
from moppu.embeddings.embedder import Embedder
from moppu.ingestion import ChannelWatcher, TranscriptFetcher, YoutubeClient
from moppu.llm import build_llm
from moppu.llm.base import LLMProvider
from moppu.logging_setup import configure_logging
from moppu.pipeline import Pipeline
from moppu.storage import ChromaVectorStore, create_engine_and_session, init_db
from moppu.storage.vectorstore import VectorStore


@dataclass
class Runtime:
    settings: Settings
    cfg: AppConfig
    channels_cfg: ChannelsConfig
    session_factory: object
    embedder: Embedder
    vector_store: VectorStore
    llm: LLMProvider
    pipeline: Pipeline
    agent: TraderAgent
    broker: Broker | None
    strategy_planner: StrategyPlannerAgent | None


def build_runtime() -> Runtime:
    settings = Settings()
    cfg = load_app_config(settings.moppu_config_path)
    channels_cfg = load_channels(settings.moppu_channels_path)

    configure_logging(cfg.app.log_level or settings.log_level)

    engine, SessionLocal = create_engine_and_session(cfg.storage.database_url)
    init_db(engine)

    embedder = build_embedder(cfg.embeddings, settings)
    vector_store = ChromaVectorStore(
        persist_dir=cfg.storage.vector_store.persist_dir,
        collection=cfg.storage.vector_store.collection,
    )

    youtube = YoutubeClient(cfg.ingestion.ytdlp.model_dump())
    transcripts = TranscriptFetcher(
        cfg.ingestion.transcript_languages,
        cookies_file=settings.youtube_cookies_file,
    )
    watcher = ChannelWatcher(SessionLocal, youtube)

    pipeline = Pipeline(
        cfg=cfg,
        channels_cfg=channels_cfg,
        session_factory=SessionLocal,
        youtube=youtube,
        transcripts=transcripts,
        watcher=watcher,
        embedder=embedder,
        vector_store=vector_store,
    )

    llm = build_llm(cfg.llm, settings)
    prompt_builder = PromptBuilder(
        cfg.agent.prompt_template,
        SessionLocal,
        persona_path=cfg.app.data_dir / "agent_persona.md",
    )
    retriever = RAGRetriever(
        embedder=embedder,
        vector_store=vector_store,
        session_factory=SessionLocal,
        top_k=cfg.agent.retrieval_top_k,
        min_score=cfg.agent.retrieval_min_score,
    )

    broker: Broker | None = None
    if cfg.broker.provider == "kis" and settings.kis_app_key:
        broker = KISBroker(cfg.broker.kis, settings)

    agent = TraderAgent(
        cfg=cfg.agent,
        llm=llm,
        prompt_builder=prompt_builder,
        retriever=retriever,
        broker=broker,
    )

    strategy_planner: StrategyPlannerAgent | None = None
    if cfg.strategy_planner.enabled:
        strategy_planner = StrategyPlannerAgent(
            cfg=cfg.strategy_planner,
            settings=settings,
            llm=llm,
            trader_agent=agent,
            broker=broker,
            data_dir=cfg.app.data_dir,
        )

    return Runtime(
        settings=settings,
        cfg=cfg,
        channels_cfg=channels_cfg,
        session_factory=SessionLocal,
        embedder=embedder,
        vector_store=vector_store,
        llm=llm,
        pipeline=pipeline,
        agent=agent,
        broker=broker,
        strategy_planner=strategy_planner,
    )
