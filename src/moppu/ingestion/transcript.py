"""Transcript extraction.

Primary path uses ``youtube-transcript-api`` (fast, no audio download). If a
video has no captions, callers can fall back to Whisper on yt-dlp-downloaded
audio — wired as a TODO stub since it's heavier to operate.
"""

from __future__ import annotations

from dataclasses import dataclass

from tenacity import retry, stop_after_attempt, wait_exponential
from youtube_transcript_api import (
    NoTranscriptFound,
    TranscriptsDisabled,
    YouTubeTranscriptApi,
)

from moppu.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class TranscriptResult:
    video_id: str
    language: str
    text: str
    source: str = "youtube_transcript_api"


class TranscriptFetcher:
    def __init__(self, preferred_languages: list[str] | None = None) -> None:
        self.preferred_languages = preferred_languages or ["ko", "en"]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10), reraise=True)
    def fetch(self, video_id: str) -> TranscriptResult | None:
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        except TranscriptsDisabled:
            log.info("transcript.disabled", video_id=video_id)
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("transcript.list_failed", video_id=video_id, err=str(e))
            raise

        transcript = None
        # Try manual captions first, then auto-generated, in the user's
        # language preference order.
        for lang in self.preferred_languages:
            try:
                transcript = transcript_list.find_manually_created_transcript([lang])
                break
            except NoTranscriptFound:
                continue
        if transcript is None:
            for lang in self.preferred_languages:
                try:
                    transcript = transcript_list.find_generated_transcript([lang])
                    break
                except NoTranscriptFound:
                    continue
        if transcript is None:
            # Fall back to whatever the first available transcript is,
            # optionally translated to our first preferred language.
            try:
                transcript = next(iter(transcript_list))
                if self.preferred_languages and transcript.is_translatable:
                    transcript = transcript.translate(self.preferred_languages[0])
            except StopIteration:
                return None

        entries = transcript.fetch()
        text = " ".join(_clean(e["text"]) for e in entries if e.get("text"))
        return TranscriptResult(
            video_id=video_id,
            language=transcript.language_code,
            text=text,
        )


def _clean(s: str) -> str:
    return s.replace("\n", " ").strip()


def chunk_text(text: str, chunk_size: int = 1200, overlap: int = 150) -> list[str]:
    """Simple char-based chunker good enough for transcript retrieval.

    Sentence-aware chunking can be added later; long-form spoken content
    already breaks naturally on caption boundaries.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size")

    chunks: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        end = min(i + chunk_size, n)
        chunks.append(text[i:end])
        if end == n:
            break
        i = end - overlap
    return chunks
