"""LLM provider abstraction.

All providers implement :class:`LLMProvider`. Use :func:`build_llm` to
construct one from the app config — the choice of provider and model lives in
``config/config.yaml``.
"""

from moppu.llm.base import ChatMessage, LLMProvider, LLMResponse
from moppu.llm.factory import build_llm

__all__ = ["LLMProvider", "ChatMessage", "LLMResponse", "build_llm"]
