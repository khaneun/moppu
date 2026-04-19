"""Relational storage.

Tracks channels, videos, and transcript chunks that we've already ingested so
that polling is idempotent and the watcher can tell what's new.

Embedding *vectors* live in the vector store (Chroma). We keep the raw text
and per-chunk metadata here so we can always re-embed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Engine,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)


class Base(DeclarativeBase):
    pass


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    handle: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    tags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    title_contains: Mapped[str | None] = mapped_column(String(256), nullable=True)

    videos: Mapped[list["Video"]] = relationship(back_populates="channel", cascade="all, delete-orphan")


class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (UniqueConstraint("video_id", name="uq_videos_video_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # Nullable: videos sourced from a video_list have no channel.
    channel_fk: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), nullable=True)
    # "channel" | "list:<list_name>"
    source_type: Mapped[str] = mapped_column(String(128), default="channel")
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|transcribed|embedded|failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    channel: Mapped["Channel | None"] = relationship(back_populates="videos")
    transcript: Mapped["Transcript | None"] = relationship(
        back_populates="video", cascade="all, delete-orphan", uselist=False
    )


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_fk: Mapped[int] = mapped_column(ForeignKey("videos.id"), unique=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="youtube_transcript_api")
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    video: Mapped[Video] = relationship(back_populates="transcript")
    chunks: Mapped[list["TranscriptChunk"]] = relationship(
        back_populates="transcript", cascade="all, delete-orphan"
    )


class TranscriptChunk(Base):
    __tablename__ = "transcript_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transcript_fk: Mapped[int] = mapped_column(ForeignKey("transcripts.id"))
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    # ID in the vector store (Chroma doc id) — lets us re-link without re-embedding.
    embedding_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    transcript: Mapped[Transcript] = relationship(back_populates="chunks")


class VideoListEntry(Base):
    """Tracks which video IDs belong to a named video list.

    The unique constraint on (list_name, video_id) means syncing the same
    list twice is idempotent. New rows only appear when new URLs are added
    to the list in ``channels.yaml``.
    """

    __tablename__ = "video_list_entries"
    __table_args__ = (UniqueConstraint("list_name", "video_id", name="uq_vle_list_video"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    list_name: Mapped[str] = mapped_column(String(256), index=True)
    video_id: Mapped[str] = mapped_column(String(32), index=True)
    # Original URL as written in config — kept for auditability.
    source_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


def create_engine_and_session(database_url: str) -> tuple[Engine, sessionmaker[Session]]:
    engine = create_engine(database_url, future=True)
    SessionLocal = sessionmaker(engine, expire_on_commit=False, class_=Session)
    return engine, SessionLocal


def init_db(engine: Engine) -> None:
    """Create all tables. For production use Alembic migrations instead."""
    Base.metadata.create_all(engine)
