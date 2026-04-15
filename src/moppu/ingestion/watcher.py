"""New-video detection.

Two strategies:

1. **Polling** (default): hit the channel RSS feed every N seconds, diff against
   the DB, emit events for new video IDs. Low-overhead and requires zero auth.
2. **Push** (optional): YouTube supports WebSub/PubSubHubbub callbacks for
   channel feeds. Hook your HTTP endpoint up to
   ``https://pubsubhubbub.appspot.com/`` and call :meth:`ChannelWatcher.handle_push`
   when the hub POSTs an Atom feed.

Both paths end at the same place: a stream of :class:`NewVideoEvent` for the
pipeline to consume.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from moppu.ingestion.youtube import VideoInfo, YoutubeClient
from moppu.logging_setup import get_logger
from moppu.storage.db import Channel, Video

log = get_logger(__name__)


@dataclass(slots=True)
class NewVideoEvent:
    channel_id: str
    video: VideoInfo


class ChannelWatcher:
    def __init__(self, session_factory, youtube: YoutubeClient) -> None:
        self._sf = session_factory
        self._yt = youtube

    # ------------------------------------------------------------------ #
    # Pull                                                                #
    # ------------------------------------------------------------------ #

    def poll_once(self, channel_ids: Iterable[str]) -> Iterator[NewVideoEvent]:
        for cid in channel_ids:
            try:
                recent = self._yt.list_recent_via_rss(cid)
            except Exception as e:  # noqa: BLE001
                log.warning("watcher.rss_failed", channel_id=cid, err=str(e))
                continue
            yield from self._emit_new(cid, recent)

    # ------------------------------------------------------------------ #
    # Push (WebSub)                                                       #
    # ------------------------------------------------------------------ #

    def handle_push(self, channel_id: str, videos: list[VideoInfo]) -> list[NewVideoEvent]:
        return list(self._emit_new(channel_id, videos))

    # ------------------------------------------------------------------ #
    # Shared                                                              #
    # ------------------------------------------------------------------ #

    def _emit_new(self, channel_id: str, videos: list[VideoInfo]) -> Iterator[NewVideoEvent]:
        with self._sf() as session:  # type: Session
            existing = {
                row[0]
                for row in session.query(Video.video_id).filter(Video.video_id.in_([v.video_id for v in videos])).all()
            }
            ch = session.query(Channel).filter_by(channel_id=channel_id).one_or_none()
            if ch is None:
                log.warning("watcher.unknown_channel", channel_id=channel_id)
                return
            ch.last_polled_at = datetime.utcnow()
            session.commit()

        for v in videos:
            if v.video_id in existing:
                continue
            yield NewVideoEvent(channel_id=channel_id, video=v)
