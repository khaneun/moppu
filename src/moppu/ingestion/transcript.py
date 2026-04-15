"""Transcript extraction.

Primary path: ``youtube-transcript-api`` (fast, unauthenticated).
Fallback path: ``yt-dlp`` subtitle download — works on cloud IPs where
YouTube blocks the transcript API.

The fallback is triggered automatically on any exception from the primary
path, so cloud deployments (AWS, GCP, etc.) work without extra config.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

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
        self._api = YouTubeTranscriptApi()

    def fetch(self, video_id: str) -> TranscriptResult | None:
        """Fetch transcript, falling back to yt-dlp on cloud IP blocks."""
        try:
            return self._fetch_via_api(video_id)
        except TranscriptsDisabled:
            log.info("transcript.disabled", video_id=video_id)
            return None
        except Exception as e:
            log.warning("transcript.api_failed_trying_ytdlp", video_id=video_id, err=str(e))
            try:
                return self._fetch_via_ytdlp(video_id)
            except Exception as e2:
                log.warning("transcript.ytdlp_failed", video_id=video_id, err=str(e2))
                raise RuntimeError(str(e)) from e2  # re-raise original error

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10), reraise=True)
    def _fetch_via_api(self, video_id: str) -> TranscriptResult | None:
        transcript_list = self._api.list(video_id)

        transcript = None
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
            try:
                transcript = next(iter(transcript_list))
                if self.preferred_languages and transcript.is_translatable:
                    transcript = transcript.translate(self.preferred_languages[0])
            except StopIteration:
                return None

        entries = transcript.fetch()
        text = " ".join(_clean(e.text) for e in entries if e.text)
        return TranscriptResult(
            video_id=video_id,
            language=transcript.language_code,
            text=text,
        )

    def _fetch_via_ytdlp(self, video_id: str) -> TranscriptResult | None:
        """Download subtitles via yt-dlp (works on cloud IPs)."""
        import yt_dlp

        url = f"https://www.youtube.com/watch?v={video_id}"

        with tempfile.TemporaryDirectory() as tmpdir:
            # 선호 언어 순서로 자막 시도
            sub_langs = self.preferred_languages + ["en"]
            opts = {
                "skip_download": True,
                "quiet": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": sub_langs,
                "subtitlesformat": "vtt",
                "outtmpl": str(Path(tmpdir) / "%(id)s.%(ext)s"),
                "ignoreerrors": True,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

            # .vtt 파일 찾기 (선호 언어 우선)
            vtt_files = list(Path(tmpdir).glob(f"{video_id}*.vtt"))
            if not vtt_files:
                raise RuntimeError("yt-dlp로 자막 파일을 찾을 수 없습니다.")

            # 선호 언어 파일 우선 선택
            chosen = vtt_files[0]
            for lang in self.preferred_languages:
                matches = [f for f in vtt_files if f".{lang}." in f.name]
                if matches:
                    chosen = matches[0]
                    break

            text = _parse_vtt(chosen.read_text(encoding="utf-8"))
            lang_code = chosen.stem.split(".")[-1] if "." in chosen.stem else "unknown"

            log.info("transcript.ytdlp_ok", video_id=video_id, lang=lang_code)
            return TranscriptResult(
                video_id=video_id,
                language=lang_code,
                text=text,
                source="yt_dlp",
            )


def _parse_vtt(vtt: str) -> str:
    """VTT 자막에서 텍스트만 추출 (타임스탬프·태그 제거)."""
    import re
    lines = []
    for line in vtt.splitlines():
        line = line.strip()
        if not line or line.startswith("WEBVTT") or "-->" in line:
            continue
        # HTML 태그 제거
        line = re.sub(r"<[^>]+>", "", line)
        # 중복 줄 제거 (VTT는 같은 텍스트를 여러 번 출력하는 경우 있음)
        if lines and lines[-1] == line:
            continue
        if line:
            lines.append(line)
    return " ".join(lines)


def _clean(s: str) -> str:
    return s.replace("\n", " ").strip()


def chunk_text(text: str, chunk_size: int = 1200, overlap: int = 150) -> list[str]:
    """Simple char-based chunker good enough for transcript retrieval."""
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
