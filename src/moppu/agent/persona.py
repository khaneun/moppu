"""LSY Agent 페르소나 합성 및 관리.

수집된 영상 자막으로부터 이선엽 애널리스트의 핵심 행동 양식을
추출·합성하여 Agent 시스템 프롬프트로 저장합니다.

- 최초: 전체 transcript에서 페르소나 생성
- 이후: 새 영상마다 기존 페르소나를 점진적으로 갱신 (덮어쓰기 아님)
- 결과물: data/agent_persona.md

사용처: PromptBuilder.build_system_prompt() 가 이 파일을 우선 사용
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from moppu.logging_setup import get_logger

log = get_logger(__name__)

PERSONA_FILENAME = "agent_persona.md"

# 최초 페르소나 생성 프롬프트
_SYNTHESIS_SYSTEM = "당신은 YouTube 영상 자막 분석 전문가입니다. 한국어로 작성하세요."

_SYNTHESIS_USER = """아래는 한국 주식 애널리스트의 YouTube 영상 자막 모음입니다.
이 내용을 분석하여, 이 애널리스트를 가장 잘 구현하는 AI Agent의 시스템 프롬프트를 작성하세요.

**반드시 포함할 내용:**

## 시장 분석 철학
- 거시경제, 외국인 수급, 기업 실적 등 중시하는 요소
- 시장을 읽는 고유한 프레임워크

## 종목·섹터 관점
- 자주 언급되는 섹터·테마에 대한 일관된 관점
- 밸류에이션·모멘텀 판단 기준

## 의사결정 원칙
- 매수·매도·관망 판단 기준
- 리스크 관리 방식
- 확신 있을 때 vs 불확실할 때 태도 차이

## 소통 및 결론 도출 방식
- 논리 전개 패턴 (귀납적/연역적)
- 결론의 확실성 표현 방식
- 자주 사용하는 논거 구조

## 핵심 신념 및 자주 하는 조언
- 반복적으로 강조하는 투자 원칙
- 시장 참여자에게 자주 하는 경고·조언

**작성 형식:**
- 1인칭으로 작성 ("나는...", "내 분석에서...")
- AI가 이 사람처럼 행동할 수 있도록 구체적이고 실용적으로
- Markdown 형식, 각 섹션 제목 포함
- 추상적 원칙보다 구체적 행동 양식 위주

---
영상 자막:
{transcripts}
"""

# 점진적 업데이트 프롬프트
_UPDATE_SYSTEM = "기존 페르소나의 핵심을 유지하면서 새 내용을 자연스럽게 통합하세요. 한국어로 작성하세요."

_UPDATE_USER = """기존 애널리스트 페르소나에 새로 수집된 영상의 인사이트를 반영하여 업데이트하세요.

**기존 페르소나:**
{existing}

---
**새로 수집된 영상 내용:**
{new_content}

---
**업데이트 지침:**
- 기존 페르소나의 핵심 특성과 구조를 유지하세요
- 새 내용이 기존 관점을 **보완**하면 자연스럽게 통합하세요
- 새 내용이 기존 관점과 **모순**되면 최신 내용을 우선하되 일관성을 유지하세요
- 새로운 패턴이나 관점이 발견되면 적절한 섹션에 추가하세요
- 전체 길이와 구조는 기존과 유사하게 유지하세요
- 단순 나열이 아닌 통합된 하나의 페르소나로 작성하세요
"""


def _path(data_dir: Path) -> Path:
    return data_dir / PERSONA_FILENAME


def load(data_dir: Path) -> str | None:
    """저장된 페르소나를 반환합니다. 없으면 None."""
    p = _path(data_dir)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("persona.load_failed", err=str(e))
        return None


def generate(session_factory, llm, data_dir: Path, *, force: bool = False) -> str | None:
    """전체 transcript DB에서 페르소나를 새로 생성합니다.

    ``force=False`` 이면 이미 파일이 있을 때 스킵합니다.
    """
    from sqlalchemy import desc
    from moppu.llm.base import ChatMessage
    from moppu.storage.db import Transcript, Video

    p = _path(data_dir)
    if p.exists() and not force:
        log.info("persona.skip_exists", path=str(p))
        return p.read_text(encoding="utf-8")

    with session_factory() as s:
        transcripts = (
            s.query(Transcript)
            .join(Video, Video.id == Transcript.video_fk)
            .filter(Video.status == "embedded")
            .order_by(desc(Video.published_at.is_(None)), desc(Video.published_at))
            .limit(15)
            .all()
        )
        if not transcripts:
            log.info("persona.no_transcripts")
            return None

        parts = []
        for tr in transcripts:
            v = s.query(Video).filter_by(id=tr.video_fk).one()
            label = f"[{v.title or v.video_id}]"
            parts.append(f"{label}\n{tr.text[:2500]}")

    combined = "\n\n---\n\n".join(parts)
    log.info("persona.generating", transcript_count=len(parts))

    resp = llm.chat(
        messages=[ChatMessage(role="user", content=_SYNTHESIS_USER.format(transcripts=combined))],
        system=_SYNTHESIS_SYSTEM,
        max_tokens=3000,
    )
    persona = resp.text
    data_dir.mkdir(parents=True, exist_ok=True)
    p.write_text(persona, encoding="utf-8")
    log.info("persona.generated", length=len(persona))
    return persona


def update_with_new(
    session_factory,
    llm,
    data_dir: Path,
    new_video_ids: list[str],
) -> str | None:
    """새로 수집된 영상으로 기존 페르소나를 점진적 업데이트합니다."""
    from moppu.llm.base import ChatMessage
    from moppu.storage.db import Transcript, Video

    existing = load(data_dir)
    if not existing:
        # 기존 페르소나 없으면 전체 생성
        log.info("persona.no_existing_full_generate")
        return generate(session_factory, llm, data_dir, force=True)

    if not new_video_ids:
        return existing

    with session_factory() as s:
        parts = []
        for vid in new_video_ids:
            v = s.query(Video).filter_by(video_id=vid).one_or_none()
            if not v:
                continue
            tr = s.query(Transcript).filter_by(video_fk=v.id).one_or_none()
            if not tr:
                continue
            label = f"[{v.title or vid}]"
            parts.append(f"{label}\n{tr.text[:2500]}")

    if not parts:
        log.info("persona.no_new_transcripts")
        return existing

    new_content = "\n\n---\n\n".join(parts)
    log.info("persona.updating", new_videos=len(parts))

    resp = llm.chat(
        messages=[ChatMessage(
            role="user",
            content=_UPDATE_USER.format(existing=existing, new_content=new_content),
        )],
        system=_UPDATE_SYSTEM,
        max_tokens=3000,
    )
    updated = resp.text
    p = _path(data_dir)
    p.write_text(updated, encoding="utf-8")
    log.info("persona.updated", new_videos=len(parts))
    return updated
