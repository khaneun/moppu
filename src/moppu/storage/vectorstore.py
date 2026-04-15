"""Vector store abstraction.

We only ship a Chroma-backed implementation for now. The :class:`VectorStore`
protocol keeps it easy to swap in pgvector/Qdrant/Weaviate later.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(slots=True)
class Hit:
    id: str
    score: float
    text: str
    metadata: dict[str, Any]


class VectorStore(Protocol):
    def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None: ...

    def query(
        self,
        embedding: list[float],
        top_k: int = 8,
        where: dict[str, Any] | None = None,
    ) -> list[Hit]: ...

    def delete(self, ids: list[str]) -> None: ...


class ChromaVectorStore:
    """Chroma-backed :class:`VectorStore`. Uses persistent client on disk."""

    def __init__(self, persist_dir: Path | str, collection: str) -> None:
        # Imported lazily so unit tests can run without chromadb installed.
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        persist_dir = Path(persist_dir)
        persist_dir.mkdir(parents=True, exist_ok=True)

        self._client = chromadb.PersistentClient(
            path=str(persist_dir),
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self._collection = self._client.get_or_create_collection(collection)

    def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None:
        if not ids:
            return
        self._collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )

    def query(
        self,
        embedding: list[float],
        top_k: int = 8,
        where: dict[str, Any] | None = None,
    ) -> list[Hit]:
        res = self._collection.query(
            query_embeddings=[embedding],
            n_results=top_k,
            where=where,
        )
        hits: list[Hit] = []
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        # Chroma returns L2 distances; convert to a similarity-ish score.
        dists = (res.get("distances") or [[0.0] * len(ids)])[0]
        for i, d, m, dist in zip(ids, docs, metas, dists, strict=False):
            score = 1.0 / (1.0 + float(dist)) if dist is not None else 0.0
            hits.append(Hit(id=i, score=score, text=d or "", metadata=m or {}))
        return hits

    def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        self._collection.delete(ids=ids)
