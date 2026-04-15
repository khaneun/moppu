"""Factory that turns LLM config into an :class:`LLMProvider` instance."""

from __future__ import annotations

from moppu.config import LLMConfig, Settings
from moppu.llm.base import LLMProvider


def build_llm(cfg: LLMConfig, settings: Settings | None = None, *, provider: str | None = None) -> LLMProvider:
    """Construct an LLM provider from config.

    Pass ``provider`` to override the default for a single call site — handy
    for routing (e.g. cheap model for classification, strong model for trading
    decisions).
    """
    settings = settings or Settings()
    name, params = cfg.resolved(provider)

    if name == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is missing")
        from moppu.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(settings.openai_api_key, **params)

    if name == "anthropic":
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is missing")
        from moppu.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings.anthropic_api_key, **params)

    if name == "google":
        if not settings.google_api_key:
            raise RuntimeError("GOOGLE_API_KEY is missing")
        from moppu.llm.google_provider import GoogleProvider

        return GoogleProvider(settings.google_api_key, **params)

    raise ValueError(f"Unknown LLM provider: {name}")
