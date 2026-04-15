"""Prompt assembly.

The *system* prompt template is a markdown file on disk. We inline small
"living" sections that change as new videos arrive:

- A channels summary (1 line per tracked channel)
- The N most recent video titles

Heavier context (retrieved transcript excerpts) gets injected at query time by
:class:`TraderAgent`, not baked into the system prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import desc
from sqlalchemy.orm import Session

from moppu.storage.db import Channel, Video


@dataclass(slots=True)
class PromptContext:
    channels_summary: str
    recent_videos: str


class PromptBuilder:
    def __init__(
        self,
        template_path: Path | str,
        session_factory,
        *,
        recent_video_count: int = 20,
        persona_path: Path | str | None = None,
    ) -> None:
        self._template_path = Path(template_path)
        self._sf = session_factory
        self._recent_n = recent_video_count
        self._persona_path = Path(persona_path) if persona_path else None

    def context(self) -> PromptContext:
        with self._sf() as session:  # type: Session
            channels = session.query(Channel).filter_by(enabled=True).all()
            chan_lines = [
                f"- {c.name or c.channel_id} ({c.channel_id}) tags={','.join(c.tags or []) if isinstance(c.tags, list) else ''}"
                for c in channels
            ]

            recent = (
                session.query(Video)
                .order_by(desc(Video.published_at.is_(None)), desc(Video.published_at))
                .limit(self._recent_n)
                .all()
            )
            video_lines = [
                f"- [{_fmt_date(v.published_at)}] {v.title or v.video_id} ({v.video_id})" for v in recent
            ]

        return PromptContext(
            channels_summary="\n".join(chan_lines) or "(no channels tracked)",
            recent_videos="\n".join(video_lines) or "(no videos ingested)",
        )

    def build_system_prompt(self) -> str:
        # 페르소나 파일이 있으면 우선 사용 (합성된 행동 양식)
        if self._persona_path and self._persona_path.exists():
            return self._persona_path.read_text(encoding="utf-8")

        # 페르소나 없으면 기존 템플릿 fallback
        tmpl = self._template_path.read_text(encoding="utf-8")
        ctx = self.context()
        return (
            tmpl.replace("{{channels_summary}}", ctx.channels_summary)
            .replace("{{recent_videos}}", ctx.recent_videos)
        )


def _fmt_date(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "????-??-??"
