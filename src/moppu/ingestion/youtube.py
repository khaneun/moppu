"""YouTube channel & video metadata fetching.

We deliberately avoid requiring a Google API key for the basic flow — yt-dlp
can list a channel's uploads and hit the YouTube RSS feed for fast "new
video" polling. If ``YOUTUBE_API_KEY`` is set, callers can plug the Data API
in for richer metadata; that path is optional.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import feedparser
import yt_dlp

from moppu.logging_setup import get_logger

log = get_logger(__name__)

_RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


@dataclass(slots=True)
class VideoInfo:
    video_id: str
    title: str | None
    url: str
    published_at: datetime | None
    duration_sec: int | None = None


class YoutubeClient:
    """Thin wrapper around yt-dlp + RSS for channel/video metadata."""

    def __init__(self, ytdlp_opts: dict[str, Any] | None = None) -> None:
        self._base_opts: dict[str, Any] = {
            "skip_download": True,
            "quiet": True,
            "extract_flat": "in_playlist",
            "ignoreerrors": True,
            **(ytdlp_opts or {}),
        }

    # ------------------------------------------------------------------ #
    # Channel resolution                                                  #
    # ------------------------------------------------------------------ #

    def resolve_channel_id(self, *, channel_id: str | None = None, handle: str | None = None) -> str:
        """Return a canonical UC... channel id given either input."""
        if channel_id:
            return channel_id
        if not handle:
            raise ValueError("Either channel_id or handle must be provided")

        url = f"https://www.youtube.com/{handle.lstrip('@')}"
        if not url.endswith("/"):
            url += "/"
        with yt_dlp.YoutubeDL({**self._base_opts, "extract_flat": True}) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
        cid = (info or {}).get("channel_id") or (info or {}).get("uploader_id")
        if not cid:
            raise RuntimeError(f"Could not resolve channel_id for handle={handle}")
        return cid

    # ------------------------------------------------------------------ #
    # Full listing (used for initial backfill)                           #
    # ------------------------------------------------------------------ #

    def list_all_videos(self, channel_id: str) -> list[VideoInfo]:
        """List every uploaded video for a channel. Use for initial backfill."""
        url = f"https://www.youtube.com/channel/{channel_id}/videos"
        with yt_dlp.YoutubeDL(self._base_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        entries = (info or {}).get("entries") or []
        out: list[VideoInfo] = []
        for e in entries:
            if not e:
                continue
            vid = e.get("id")
            if not vid:
                continue
            out.append(
                VideoInfo(
                    video_id=vid,
                    title=e.get("title"),
                    url=e.get("url") or f"https://www.youtube.com/watch?v={vid}",
                    published_at=_parse_ts(e.get("timestamp")),
                    duration_sec=e.get("duration"),
                )
            )
        log.info("channel.list_all", channel_id=channel_id, count=len(out))
        return out

    # ------------------------------------------------------------------ #
    # Fast "what's new" poll via RSS                                      #
    # ------------------------------------------------------------------ #

    def fetch_video_info(self, video_id: str) -> VideoInfo:
        """Fetch metadata for a single video by ID (title, duration, publish date)."""
        url = f"https://www.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL({**self._base_opts, "extract_flat": False}) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        return VideoInfo(
            video_id=video_id,
            title=info.get("title"),
            url=url,
            published_at=_parse_ts(info.get("timestamp")),
            duration_sec=info.get("duration"),
        )

    def list_recent_via_rss(self, channel_id: str) -> list[VideoInfo]:
        """RSS feed returns up to ~15 most-recent videos, cheap and unauthenticated."""
        feed = feedparser.parse(_RSS_URL.format(channel_id=channel_id))
        out: list[VideoInfo] = []
        for entry in feed.entries:
            vid = entry.get("yt_videoid") or entry.get("id", "").split(":")[-1]
            if not vid:
                continue
            published = None
            if entry.get("published_parsed"):
                published = datetime(*entry.published_parsed[:6])
            out.append(
                VideoInfo(
                    video_id=vid,
                    title=entry.get("title"),
                    url=entry.get("link") or f"https://www.youtube.com/watch?v={vid}",
                    published_at=published,
                )
            )
        return out


def parse_video_id(url_or_id: str) -> str:
    """Extract an 11-char video ID from any common YouTube URL format or a bare ID.

    Supported inputs:
    - ``https://www.youtube.com/watch?v=VIDEO_ID``
    - ``https://youtu.be/VIDEO_ID``
    - ``https://www.youtube.com/shorts/VIDEO_ID``
    - ``https://www.youtube.com/live/VIDEO_ID``
    - ``VIDEO_ID``  (bare 11-char alphanumeric)
    """
    s = url_or_id.strip()

    # Bare ID — YouTube video IDs are exactly 11 chars of [A-Za-z0-9_-].
    if re.fullmatch(r"[A-Za-z0-9_\-]{11}", s):
        return s

    parsed = urlparse(s)
    host = parsed.netloc.lower().lstrip("www.")

    # youtu.be/<id>
    if host == "youtu.be":
        vid = parsed.path.lstrip("/").split("/")[0]
        if vid:
            return vid

    # youtube.com/watch?v=<id>
    if host in {"youtube.com", "m.youtube.com"}:
        qs = parse_qs(parsed.query)
        if "v" in qs and qs["v"]:
            return qs["v"][0]

        # /shorts/<id>  or  /live/<id>  or  /embed/<id>
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in {"shorts", "live", "embed"}:
            return parts[1]

    raise ValueError(f"Cannot extract video ID from: {url_or_id!r}")


def _parse_ts(ts: Any) -> datetime | None:
    if ts is None:
        return None
    try:
        return datetime.utcfromtimestamp(int(ts))
    except (TypeError, ValueError):
        return None
