"""Anthropic (Claude) chat provider."""

from __future__ import annotations

from typing import Any

from moppu.llm.base import ChatMessage, LLMResponse


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, *, temperature: float = 0.2, max_tokens: int = 2048) -> None:
        from anthropic import Anthropic

        self._client = Anthropic(api_key=api_key)
        self.model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        # Anthropic wants system as a top-level kwarg, not a message.
        anthropic_messages: list[dict[str, Any]] = []
        implicit_system: list[str] = []
        for m in messages:
            if m.role == "system":
                implicit_system.append(m.content)
                continue
            anthropic_messages.append({"role": m.role, "content": m.content})

        merged_system = "\n\n".join([s for s in [system, *implicit_system] if s]) or None

        resp = self._client.messages.create(
            model=self.model,
            system=merged_system,
            messages=anthropic_messages,
            temperature=self._temperature if temperature is None else temperature,
            max_tokens=self._max_tokens if max_tokens is None else max_tokens,
            tools=tools,
            **kwargs,
        )

        # Anthropic returns a list of content blocks; join text blocks.
        text_parts: list[str] = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
        usage = {}
        if resp.usage:
            usage = {
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
                "total_tokens": resp.usage.input_tokens + resp.usage.output_tokens,
            }
        return LLMResponse(
            text="".join(text_parts),
            model=self.model,
            provider=self.name,
            raw=resp,
            usage=usage,
        )
