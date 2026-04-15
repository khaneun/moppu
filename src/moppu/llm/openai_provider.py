"""OpenAI chat provider."""

from __future__ import annotations

from typing import Any

from moppu.llm.base import ChatMessage, LLMResponse


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str, *, temperature: float = 0.2, max_tokens: int = 2048) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
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
        payload: list[dict[str, Any]] = []
        if system:
            payload.append({"role": "system", "content": system})
        payload.extend(
            {"role": m.role, "content": m.content, **({"name": m.name} if m.name else {})}
            for m in messages
        )

        resp = self._client.chat.completions.create(
            model=self.model,
            messages=payload,
            temperature=self._temperature if temperature is None else temperature,
            max_tokens=self._max_tokens if max_tokens is None else max_tokens,
            tools=tools,
            **kwargs,
        )
        msg = resp.choices[0].message
        text = msg.content or ""
        usage = {}
        if resp.usage:
            usage = {
                "input_tokens": resp.usage.prompt_tokens,
                "output_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }
        return LLMResponse(text=text, model=self.model, provider=self.name, raw=resp, usage=usage)
