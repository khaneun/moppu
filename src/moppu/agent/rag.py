"""RAG retriever.

Wraps the embedder + vector store so callers don't need to know which backend
is configured. Returns chunks already enriched with the originating video
metadata from the DB so the prompt can cite sources.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from moppu.embeddings import Embedder
from moppu.storage.db import Transcript, TranscriptChunk, Video
from moppu.storage.vectorstore import VectorStore


@dataclass(slots=True)
class RetrievedChunk:
    video_id: str
    video_title: str | None
    published_at_iso: str | None
    chunk_index: int
    text: str
    score: float


class RAGRetriever:
    def __init__(
        self,
        embedder: Embedder,
        vector_store: VectorStore,
        session_factory,
        *,
        top_k: int = 8,
        min_score: float = 0.0,
    ) -> None:
        self._embedder = embedder
        self._store = vector_store
        self._sf = session_factory
        self._top_k = top_k
        self._min_score = min_score

    def retrieve(self, query: str, *, top_k: int | None = None) -> list[RetrievedChunk]:
        [vec] = self._embedder.embed([query])
        hits = self._store.query(vec, top_k=top_k or self._top_k)

        # Hydrate with DB metadata for citation.
        chunk_ids = [h.id for h in hits if h.score >= self._min_score]
        if not chunk_ids:
            return []

        results: list[RetrievedChunk] = []
        with self._sf() as session:  # type: Session
            rows = (
                session.query(TranscriptChunk, Transcript, Video)
                .join(Transcript, Transcript.id == TranscriptChunk.transcript_fk)
                .join(Video, Video.id == Transcript.video_fk)
                .filter(TranscriptChunk.embedding_id.in_(chunk_ids))
                .all()
            )
            by_emb_id = {chunk.embedding_id: (chunk, _transcript, video) for chunk, _transcript, video in rows}
            for hit in hits:
                if hit.score < self._min_score:
                    continue
                row = by_emb_id.get(hit.id)
                if not row:
                    continue
                chunk, _transcript, video = row
                results.append(
                    RetrievedChunk(
                        video_id=video.video_id,
                        video_title=video.title,
                        published_at_iso=video.published_at.isoformat() if video.published_at else None,
                        chunk_index=chunk.chunk_index,
                        text=chunk.text,
                        score=hit.score,
                    )
                )
        return results
