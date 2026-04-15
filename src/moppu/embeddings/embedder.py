"""Swappable embedding providers.

Default is a local ``sentence-transformers`` model (no network, no cost). For
production or multilingual workloads swap to OpenAI / Google via config.
"""

from __future__ import annotations

from typing import Protocol

from moppu.config import EmbeddingsConfig, Settings


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class SentenceTransformerEmbedder:
    def __init__(self, model: str) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model)
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vecs = self._model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        return vecs.tolist()


class OpenAIEmbedder:
    def __init__(self, model: str, api_key: str) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self._model = model
        # text-embedding-3-small → 1536, -large → 3072. Set on first call.
        self.dim = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.embeddings.create(model=self._model, input=texts)
        vectors = [d.embedding for d in resp.data]
        if vectors and not self.dim:
            self.dim = len(vectors[0])
        return vectors


class GoogleEmbedder:
    def __init__(self, model: str, api_key: str) -> None:
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        self._genai = genai
        self._model = model
        self.dim = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for t in texts:
            r = self._genai.embed_content(model=self._model, content=t)
            vec = r["embedding"] if isinstance(r, dict) else r.embedding
            vectors.append(list(vec))
        if vectors and not self.dim:
            self.dim = len(vectors[0])
        return vectors


def build_embedder(cfg: EmbeddingsConfig, settings: Settings | None = None) -> Embedder:
    settings = settings or Settings()
    if cfg.provider == "sentence-transformers":
        return SentenceTransformerEmbedder(cfg.model)
    if cfg.provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required for openai embeddings")
        return OpenAIEmbedder(cfg.model, settings.openai_api_key)
    if cfg.provider == "google":
        if not settings.google_api_key:
            raise RuntimeError("GOOGLE_API_KEY is required for google embeddings")
        return GoogleEmbedder(cfg.model, settings.google_api_key)
    raise ValueError(f"Unknown embeddings provider: {cfg.provider}")
