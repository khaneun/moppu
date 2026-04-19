"""Configuration loading.

Two layers:

- Secrets and host-specific values come from environment variables (``.env``),
  loaded via :class:`Settings`.
- Everything else (what models to use, thresholds, paths, etc.) lives in
  ``config/config.yaml`` and is parsed into :class:`AppConfig`.

Kept separate on purpose: ``AppConfig`` is safe to dump/commit, ``Settings``
is not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# --------------------------------------------------------------------------- #
# Secrets / env                                                               #
# --------------------------------------------------------------------------- #


class Settings(BaseSettings):
    """Environment-backed secrets. Do not commit values of these fields."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # LLM providers
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    google_api_key: str | None = None

    # YouTube
    youtube_api_key: str | None = None

    # KIS 실전투자
    kis_app_key: str | None = None
    kis_app_secret: str | None = None
    kis_account_no: str | None = None
    kis_account_product_code: str = "01"
    kis_env: Literal["real", "paper"] = "paper"
    # KIS 모의투자 (별도 키/계좌 — 없으면 실전 값 공용)
    kis_paper_app_key: str | None = None
    kis_paper_app_secret: str | None = None
    kis_paper_account_no: str | None = None

    # Telegram
    telegram_bot_token: str | None = None
    telegram_allowed_chat_ids: str = ""

    # Paths
    moppu_config_path: Path = Path("config/config.yaml")
    moppu_channels_path: Path = Path("config/channels.yaml")
    log_level: str = "INFO"

    # YouTube cookies (Netscape format) — EC2 IP 차단 우회용
    youtube_cookies_file: Path | None = None

    # Dashboard auth
    dashboard_id: str = "moppu"
    dashboard_password: str = "Gksrlgns12!"

    @property
    def allowed_chat_ids(self) -> list[int]:
        raw = (self.telegram_allowed_chat_ids or "").strip()
        if not raw:
            return []
        return [int(x) for x in raw.split(",") if x.strip()]


# --------------------------------------------------------------------------- #
# YAML config                                                                 #
# --------------------------------------------------------------------------- #


class AppSection(BaseModel):
    name: str = "moppu"
    data_dir: Path = Path("./data")
    log_level: str = "INFO"


class VectorStoreConfig(BaseModel):
    provider: Literal["chroma"] = "chroma"
    persist_dir: Path = Path("./data/chroma")
    collection: str = "youtube_transcripts"


class StorageConfig(BaseModel):
    database_url: str = "sqlite:///./data/moppu.db"
    vector_store: VectorStoreConfig = VectorStoreConfig()


class EmbeddingsConfig(BaseModel):
    provider: Literal["sentence-transformers", "openai", "google"] = "sentence-transformers"
    model: str = "BAAI/bge-m3"
    chunk_size: int = 1200
    chunk_overlap: int = 150


class LLMProviderOverride(BaseModel):
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


class LLMConfig(BaseModel):
    provider: Literal["openai", "anthropic", "google"] = "anthropic"
    model: str = "claude-sonnet-4-6"
    temperature: float = 0.2
    max_tokens: int = 2048
    providers: dict[str, LLMProviderOverride] = Field(default_factory=dict)

    def resolved(self, provider: str | None = None) -> tuple[str, dict[str, Any]]:
        """Return (provider, kwargs) with per-provider overrides applied."""
        p = provider or self.provider
        override = self.providers.get(p, LLMProviderOverride())
        return p, {
            "model": override.model or self.model,
            "temperature": override.temperature if override.temperature is not None else self.temperature,
            "max_tokens": override.max_tokens if override.max_tokens is not None else self.max_tokens,
        }


class YtDlpOptions(BaseModel):
    skip_download: bool = True
    quiet: bool = True


class IngestionConfig(BaseModel):
    batch_size: int = 5
    poll_interval_sec: int = 900
    transcript_languages: list[str] = Field(default_factory=lambda: ["ko", "en"])
    ytdlp: YtDlpOptions = YtDlpOptions()


class AgentConfig(BaseModel):
    prompt_template: Path = Path("prompts/trader.system.example.md")
    retrieval_top_k: int = 8
    retrieval_min_score: float = 0.2
    max_order_krw: int = 1_000_000
    dry_run: bool = True


class KISBrokerConfig(BaseModel):
    base_url_real: str = "https://openapi.koreainvestment.com:9443"
    base_url_paper: str = "https://openapivts.koreainvestment.com:29443"
    ws_url_real: str = "ws://ops.koreainvestment.com:21000"
    ws_url_paper: str = "ws://ops.koreainvestment.com:31000"


class BrokerConfig(BaseModel):
    provider: Literal["kis"] = "kis"
    kis: KISBrokerConfig = KISBrokerConfig()


class TelegramConfig(BaseModel):
    enabled: bool = True
    webhook_url: str | None = None


class SchedulerConfig(BaseModel):
    enabled: bool = True
    poll_channels_cron: str = "0 15 * * *"
    # Cron for the upload_day job — runs at midnight, ingests channels whose
    # upload_day matches yesterday's date.
    upload_day_cron: str = "0 0 * * *"


class StrategyPlannerConfig(BaseModel):
    enabled: bool = False
    # 장 시작 후 30분 (KST 09:30, 평일)
    cron: str = "30 9 * * 1-5"
    dry_run: bool = True
    # 전략 플래너는 여러 종목을 동시에 처리하므로 per-order 한도를 별도 설정
    max_order_krw: int = 5_000_000
    # 자금 요청 후 재확인까지 대기 시간 (분)
    fund_request_wait_min: int = 10


class AppConfig(BaseModel):
    app: AppSection = AppSection()
    storage: StorageConfig = StorageConfig()
    embeddings: EmbeddingsConfig = EmbeddingsConfig()
    llm: LLMConfig = LLMConfig()
    ingestion: IngestionConfig = IngestionConfig()
    agent: AgentConfig = AgentConfig()
    broker: BrokerConfig = BrokerConfig()
    telegram: TelegramConfig = TelegramConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    strategy_planner: StrategyPlannerConfig = StrategyPlannerConfig()

    @field_validator("app", mode="after")
    @classmethod
    def _mkdir_data(cls, v: AppSection) -> AppSection:
        v.data_dir.mkdir(parents=True, exist_ok=True)
        return v


# --------------------------------------------------------------------------- #
# Channels                                                                    #
# --------------------------------------------------------------------------- #


class ChannelSpec(BaseModel):
    channel_id: str | None = None
    handle: str | None = None
    name: str | None = None
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    # Ingestion filter — only ingest if video title contains this string
    title_contains: str | None = None

    @field_validator("handle")
    @classmethod
    def _norm_handle(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return v if v.startswith("@") else f"@{v}"


class VideoListSpec(BaseModel):
    """A named, manually-curated list of YouTube video URLs/IDs.

    Unlike a channel, there is no polling — only the URLs listed here are
    ever ingested. Adding a new URL to the list and re-running
    ``sync-video-lists`` will pick up only the new entries.
    """

    name: str
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    # Each item can be a full URL or a bare 11-char video ID.
    videos: list[str] = Field(default_factory=list)


class ChannelsConfig(BaseModel):
    channels: list[ChannelSpec] = Field(default_factory=list)
    video_lists: list[VideoListSpec] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Loaders                                                                     #
# --------------------------------------------------------------------------- #


def load_app_config(path: Path | str | None = None) -> AppConfig:
    path = Path(path) if path else Settings().moppu_config_path
    if not path.exists():
        # Fall back to defaults so boot doesn't require a config file.
        return AppConfig()
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return AppConfig.model_validate(data)


def load_channels(path: Path | str | None = None) -> ChannelsConfig:
    path = Path(path) if path else Settings().moppu_channels_path
    if not path.exists():
        return ChannelsConfig()
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return ChannelsConfig.model_validate(data)
