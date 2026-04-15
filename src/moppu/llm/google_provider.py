"""Google Gemini chat provider."""

from __future__ import annotations

from typing import Any

from moppu.llm.base import ChatMessage, LLMResponse


_ROLE_MAP = {"user": "user", "assistant": "model", "tool": "tool"}


class GoogleProvider:
    name = "google"

    def __init__(self, api_key: str, model: str, *, temperature: float = 0.2, max_tokens: int = 2048) -> None:
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        self._genai = genai
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
        history: list[dict[str, Any]] = []
        implicit_system: list[str] = []
        for m in messages:
            if m.role == "system":
                implicit_system.append(m.content)
                continue
            role = _ROLE_MAP.get(m.role, "user")
            history.append({"role": role, "parts": [m.content]})

        merged_system = "\n\n".join([s for s in [system, *implicit_system] if s]) or None

        model = self._genai.GenerativeModel(
            self.model,
            system_instruction=merged_system,
            generation_config={
                "temperature": self._temperature if temperature is None else temperature,
                "max_output_tokens": self._max_tokens if max_tokens is None else max_tokens,
            },
            tools=tools,
        )
        resp = model.generate_content(history, **kwargs)
        text = getattr(resp, "text", "") or ""
        usage: dict[str, int] = {}
        if getattr(resp, "usage_metadata", None):
            um = resp.usage_metadata
            usage = {
                "input_tokens": getattr(um, "prompt_token_count", 0),
                "output_tokens": getattr(um, "candidates_token_count", 0),
                "total_tokens": getattr(um, "total_token_count", 0),
            }
        return LLMResponse(text=text, model=self.model, provider=self.name, raw=resp, usage=usage)
