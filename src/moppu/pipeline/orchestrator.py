"""Pipeline orchestrator.

Responsibilities:

1. Sync the ``channels.yaml`` file into the DB (add/enable/disable channels).
2. Backfill: for any enabled channel, list all uploaded videos and ingest the
   ones we haven't seen.
3. Poll: call :class:`ChannelWatcher` to find new videos and ingest them.
4. Ingest a single video: fetch transcript → chunk → embed → upsert into the
   vector store and DB.

Exposes simple methods the CLI and the APScheduler job wrap.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from moppu.config import AppConfig, ChannelSpec, ChannelsConfig, VideoListSpec
from moppu.embeddings import Embedder
from moppu.ingestion.transcript import TranscriptFetcher, chunk_text
from moppu.ingestion.watcher import ChannelWatcher, NewVideoEvent
from moppu.ingestion.youtube import VideoInfo, YoutubeClient, parse_video_id
from moppu.logging_setup import get_logger
from moppu.storage.db import Channel, Transcript, TranscriptChunk, Video, VideoListEntry
from moppu.storage.vectorstore import VectorStore

log = get_logger(__name__)


class Pipeline:
    def __init__(
        self,
        *,
        cfg: AppConfig,
        channels_cfg: ChannelsConfig,
        session_factory,
        youtube: YoutubeClient,
        transcripts: TranscriptFetcher,
        watcher: ChannelWatcher,
        embedder: Embedder,
        vector_store: VectorStore,
    ) -> None:
        self._cfg = cfg
        self._channels_cfg = channels_cfg
        self._sf = session_factory
        self._youtube = youtube
        self._transcripts = transcripts
        self._watcher = watcher
        self._embedder = embedder
        self._store = vector_store

    # ------------------------------------------------------------------ #
    # Ingestion filters                                                   #
    # ------------------------------------------------------------------ #

    def _spec_for(self, channel_id: str) -> ChannelSpec | None:
        """Return the ChannelSpec from config that matches the given channel_id."""
        for spec in self._channels_cfg.channels:
            if spec.channel_id == channel_id:
                return spec
        return None

    def _passes_filter(self, info: VideoInfo, spec: ChannelSpec) -> bool:
        """Return False if info should be skipped (title_contains filter)."""
        if spec.title_contains is not None:
            if spec.title_contains not in (info.title or ""):
                log.info(
                    "ingest.skipped.title_filter",
                    video_id=info.video_id,
                    title=info.title,
                    filter=spec.title_contains,
                )
                return False
        return True

    # ------------------------------------------------------------------ #
    # Channels                                                            #
    # ------------------------------------------------------------------ #

    def sync_channels(self) -> list[Channel]:
        """Reconcile ``channels.yaml`` into the DB."""
        out: list[Channel] = []
        with self._sf() as session:  # type: Session
            for spec in self._channels_cfg.channels:
                cid = spec.channel_id or self._youtube.resolve_channel_id(handle=spec.handle)
                row = session.query(Channel).filter_by(channel_id=cid).one_or_none()
                if row is None:
                    row = Channel(
                        channel_id=cid,
                        handle=spec.handle,
                        name=spec.name,
                        tags=list(spec.tags),
                        enabled=spec.enabled,
                    )
                    session.add(row)
                else:
                    row.handle = spec.handle or row.handle
                    row.name = spec.name or row.name
                    row.tags = list(spec.tags)
                    row.enabled = spec.enabled
                out.append(row)
            session.commit()
        return out

    def backfill(self, *, channel_ids: list[str] | None = None) -> int:
        """Initial ingestion: for each enabled channel, pull and ingest every video."""
        processed = 0
        with self._sf() as session:
            q = session.query(Channel).filter_by(enabled=True)
            if channel_ids:
                q = q.filter(Channel.channel_id.in_(channel_ids))
            channels = q.all()

        for ch in channels:
            try:
                videos = self._youtube.list_all_videos(ch.channel_id)
            except Exception as e:  # noqa: BLE001
                log.warning("backfill.list_failed", channel_id=ch.channel_id, err=str(e))
                continue

            spec = self._spec_for(ch.channel_id)
            for v in videos:
                if spec and not self._passes_filter(v, spec):
                    continue
                if self._ingest_one(ch.channel_id, v):
                    processed += 1
        return processed

    def poll_new(self) -> int:
        """Run one polling cycle and ingest up to ``batch_size`` new videos."""
        batch = self._cfg.ingestion.batch_size
        processed = 0

        with self._sf() as session:
            channel_ids = [c.channel_id for c in session.query(Channel).filter_by(enabled=True).all()]

        spec_map = {s.channel_id: s for s in self._channels_cfg.channels if s.channel_id}
        for event in self._watcher.poll_once(channel_ids):
            spec = spec_map.get(event.channel_id)
            if spec and not self._passes_filter(event.video, spec):
                continue
            if self._ingest_one(event.channel_id, event.video):
                processed += 1
            if processed >= batch:
                break
        return processed

    def poll_upload_day_channels(self) -> int:
        """Midnight job: poll ALL enabled channels for recently uploaded videos.

        Runs at 00:00 each day. Polls every enabled channel's RSS feed and
        ingests new videos that pass the ``title_contains`` filter (if set).
        """
        with self._sf() as session:
            channel_ids = [
                c.channel_id
                for c in session.query(Channel).filter_by(enabled=True).all()
                if c.channel_id
            ]

        batch = self._cfg.ingestion.batch_size
        processed = 0
        spec_map = {s.channel_id: s for s in self._channels_cfg.channels if s.channel_id}

        for event in self._watcher.poll_once(channel_ids):
            spec = spec_map.get(event.channel_id)
            if spec and not self._passes_filter(event.video, spec):
                continue
            if self._ingest_one(event.channel_id, event.video):
                processed += 1
            if processed >= batch:
                break

        log.info("upload_day.done", channels=len(channel_ids), ingested=processed)
        return processed

    def handle_push_event(self, event: NewVideoEvent) -> bool:
        """Entry point for WebSub push callbacks."""
        return self._ingest_one(event.channel_id, event.video)

    # ------------------------------------------------------------------ #
    # Per-video ingestion                                                  #
    # ------------------------------------------------------------------ #

    def _ingest_one(self, source: str, info: VideoInfo) -> bool:
        """Ingest a single video from any source.

        ``source`` is either a channel_id (``UC…``) or ``"list:<list_name>"``
        for videos that came from a manually-curated video list.
        """
        is_list_source = source.startswith("list:")

        with self._sf() as session:  # type: Session
            existing = session.query(Video).filter_by(video_id=info.video_id).one_or_none()
            if existing and existing.status in {"transcribed", "embedded"}:
                return False

            channel_fk: int | None = None
            if not is_list_source:
                ch = session.query(Channel).filter_by(channel_id=source).one_or_none()
                if ch is None:
                    log.warning("ingest.unknown_channel", channel_id=source)
                    return False
                channel_fk = ch.id

            video = existing or Video(
                video_id=info.video_id,
                channel_fk=channel_fk,
                source_type=source,
                title=info.title,
                published_at=info.published_at,
                url=info.url,
                duration_sec=info.duration_sec,
                created_at=datetime.utcnow(),
            )
            session.add(video)
            session.commit()
            video_pk = video.id

        # Fetch transcript
        try:
            tr = self._transcripts.fetch(info.video_id)
        except Exception as e:  # noqa: BLE001
            self._mark_failed(video_pk, f"transcript: {e}")
            return False

        if tr is None:
            self._mark_failed(video_pk, "no transcript available")
            return False

        # Chunk & embed
        chunks = chunk_text(
            tr.text,
            chunk_size=self._cfg.embeddings.chunk_size,
            overlap=self._cfg.embeddings.chunk_overlap,
        )
        if not chunks:
            self._mark_failed(video_pk, "empty transcript after chunking")
            return False

        vectors = self._embedder.embed(chunks)
        ids = [f"{info.video_id}:{i}:{uuid.uuid4().hex[:8]}" for i in range(len(chunks))]
        metadatas = [
            {
                "video_id": info.video_id,
                "source": source,
                "chunk_index": i,
                "published_at": info.published_at.isoformat() if info.published_at else None,
                "title": info.title or "",
            }
            for i in range(len(chunks))
        ]

        with self._sf() as session:
            transcript = Transcript(
                video_fk=video_pk,
                language=tr.language,
                source=tr.source,
                text=tr.text,
            )
            session.add(transcript)
            session.flush()

            for i, (text_chunk, emb_id) in enumerate(zip(chunks, ids, strict=True)):
                session.add(
                    TranscriptChunk(
                        transcript_fk=transcript.id,
                        chunk_index=i,
                        text=text_chunk,
                        embedding_id=emb_id,
                    )
                )
            session.query(Video).filter_by(id=video_pk).update({"status": "embedded", "error": None})
            session.commit()

        self._store.upsert(ids=ids, embeddings=vectors, documents=chunks, metadatas=metadatas)
        log.info("ingest.ok", video_id=info.video_id, chunks=len(chunks))
        return True

    def _mark_failed(self, video_pk: int, err: str) -> None:
        with self._sf() as session:  # type: Session
            session.query(Video).filter_by(id=video_pk).update({"status": "failed", "error": err})
            session.commit()
        log.warning("ingest.failed", video_pk=video_pk, err=err)

    # ------------------------------------------------------------------ #
    # Video list ingestion                                                 #
    # ------------------------------------------------------------------ #

    def sync_video_lists(self) -> dict[str, int]:
        """Register all video list entries from config into the DB.

        Returns ``{list_name: new_entry_count}`` — already-known entries are
        skipped (idempotent). Only newly added URLs produce non-zero counts.
        """
        result: dict[str, int] = {}
        for spec in self._channels_cfg.video_lists:
            if not spec.enabled:
                continue
            new_count = 0
            for raw_url in spec.videos:
                try:
                    vid = parse_video_id(raw_url)
                except ValueError as e:
                    log.warning("video_list.bad_url", list=spec.name, url=raw_url, err=str(e))
                    continue

                with self._sf() as session:
                    exists = (
                        session.query(VideoListEntry)
                        .filter_by(list_name=spec.name, video_id=vid)
                        .one_or_none()
                    )
                    if exists is None:
                        session.add(
                            VideoListEntry(
                                list_name=spec.name,
                                video_id=vid,
                                source_url=raw_url if raw_url != vid else None,
                            )
                        )
                        session.commit()
                        new_count += 1

            result[spec.name] = new_count
            log.info("video_list.synced", list=spec.name, new=new_count)
        return result

    def ingest_from_lists(self, *, list_name: str | None = None) -> int:
        """Ingest every video in video_list_entries that hasn't been ingested yet.

        Pass ``list_name`` to restrict to one list; omit for all enabled lists.
        Returns the number of videos successfully ingested.
        """
        # Collect enabled list names from config.
        enabled_lists: set[str] = {
            s.name for s in self._channels_cfg.video_lists if s.enabled
        }
        if list_name:
            enabled_lists = {list_name} if list_name in enabled_lists else set()

        # Find video IDs that are in the list but not yet embedded/transcribed.
        with self._sf() as session:
            entries = (
                session.query(VideoListEntry)
                .filter(VideoListEntry.list_name.in_(enabled_lists))
                .all()
            )
            # Filter out video_ids that already have a successful Video record.
            pending: list[VideoListEntry] = []
            for entry in entries:
                existing = session.query(Video).filter_by(video_id=entry.video_id).one_or_none()
                if existing and existing.status in {"transcribed", "embedded"}:
                    continue
                pending.append(entry)

        processed = 0
        for entry in pending:
            try:
                info = self._youtube.fetch_video_info(entry.video_id)
            except Exception as e:  # noqa: BLE001
                log.warning("video_list.fetch_info_failed", video_id=entry.video_id, err=str(e))
                info = VideoInfo(
                    video_id=entry.video_id,
                    title=None,
                    url=entry.source_url or f"https://www.youtube.com/watch?v={entry.video_id}",
                    published_at=None,
                )

            if self._ingest_one(f"list:{entry.list_name}", info):
                processed += 1

        return processed

    # ------------------------------------------------------------------ #
    # Channel enable/disable (e.g. via Telegram)                           #
    # ------------------------------------------------------------------ #

    def set_channel_enabled(self, channel_id: str, enabled: bool) -> bool:
        with self._sf() as session:
            ch = session.query(Channel).filter_by(channel_id=channel_id).one_or_none()
            if ch is None:
                return False
            ch.enabled = enabled
            session.commit()
        return True

    def add_channel(self, spec: ChannelSpec) -> Channel:
        cid = spec.channel_id or self._youtube.resolve_channel_id(handle=spec.handle)
        with self._sf() as session:
            ch = session.query(Channel).filter_by(channel_id=cid).one_or_none()
            if ch is None:
                ch = Channel(
                    channel_id=cid,
                    handle=spec.handle,
                    name=spec.name,
                    tags=list(spec.tags),
                    enabled=spec.enabled,
                )
                session.add(ch)
                session.commit()
            return ch
