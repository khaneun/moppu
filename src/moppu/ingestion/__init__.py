"""YouTube ingestion: channel listing, transcripts, new-video detection."""

from moppu.ingestion.transcript import TranscriptFetcher, TranscriptResult
from moppu.ingestion.watcher import ChannelWatcher, NewVideoEvent
from moppu.ingestion.youtube import VideoInfo, YoutubeClient

__all__ = [
    "YoutubeClient",
    "VideoInfo",
    "TranscriptFetcher",
    "TranscriptResult",
    "ChannelWatcher",
    "NewVideoEvent",
]
