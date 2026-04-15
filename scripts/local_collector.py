#!/usr/bin/env python3
"""
Moppu Local Collector
======================
로컬 Windows PC에서 YouTube 자막을 수집하여 EC2로 전송합니다.

역할 분리:
  이 스크립트 (로컬 PC)  — YouTube 자막 수집 (IP 차단 없음)
  EC2                    — 임베딩, 요약, 대시보드, 봇

실행 방법:
  python local_collector.py

최초 실행 시 collector_config.json 이 생성됩니다.
EC2 주소와 로그인 정보를 입력 후 재실행하세요.

Windows 작업 스케줄러 설정:
  1. 작업 스케줄러 열기 → 기본 작업 만들기
  2. 트리거: 매일 00:05
  3. 동작: 프로그램 시작
     프로그램: C:\\Python312\\python.exe
     인수:     C:\\path\\to\\scripts\\local_collector.py
     시작 위치: C:\\path\\to\\scripts\\

필요 패키지 설치:
  pip install requests youtube-transcript-api yt-dlp feedparser
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import feedparser
import requests
from youtube_transcript_api import (
    NoTranscriptFound,
    TranscriptsDisabled,
    YouTubeTranscriptApi,
)

# ------------------------------------------------------------------ #
# 설정 파일 경로
# ------------------------------------------------------------------ #

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "collector_config.json"
LOG_FILE    = SCRIPT_DIR / "collector.log"
KST = timezone(timedelta(hours=9))

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# EC2 API 클라이언트
# ------------------------------------------------------------------ #

class EC2Client:
    def __init__(self, base_url: str, dashboard_id: str, dashboard_password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._id = dashboard_id
        self._pw = dashboard_password
        self._session = requests.Session()
        self._session.timeout = 60

    def login(self) -> None:
        resp = self._session.post(
            f"{self.base_url}/api/auth/login",
            json={"id": self._id, "password": self._pw},
        )
        resp.raise_for_status()
        token = resp.json()["token"]
        self._session.headers.update({"Authorization": f"Bearer {token}"})
        log.info("EC2 로그인 성공")

    def get_items(self) -> dict[str, Any]:
        resp = self._session.get(f"{self.base_url}/api/collect/items")
        resp.raise_for_status()
        return resp.json()

    def send_transcript(self, payload: dict[str, Any]) -> bool:
        try:
            resp = self._session.post(
                f"{self.base_url}/api/collect/transcript",
                json=payload,
            )
            if not resp.ok:
                log.warning(f"전송 실패 [{payload['video_id']}]: HTTP {resp.status_code} {resp.text[:100]}")
                return False
            result = resp.json()
            if result.get("skipped"):
                log.info(f"  스킵 (이미 임베딩됨): {payload['video_id']}")
                return True
            log.info(f"  ✓ 임베딩 완료: {payload['video_id']} ({result.get('chunks', '?')}청크)")
            return True
        except Exception as e:
            log.warning(f"전송 오류 [{payload['video_id']}]: {e}")
            return False

    def trigger_process(self) -> None:
        try:
            resp = self._session.post(f"{self.base_url}/api/collect/process", timeout=10)
            if resp.ok:
                log.info("EC2 요약 생성 트리거 완료")
        except Exception as e:
            log.warning(f"처리 트리거 실패: {e}")


# ------------------------------------------------------------------ #
# YouTube 자막 수집
# ------------------------------------------------------------------ #

def fetch_transcript(video_id: str, preferred_langs: list[str]) -> dict[str, Any] | None:
    """YouTube 자막 수집 (로컬 PC에서 실행 — IP 차단 없음)."""
    api = YouTubeTranscriptApi()
    try:
        transcript_list = api.list(video_id)
    except TranscriptsDisabled:
        log.info(f"  자막 비활성화: {video_id}")
        return None
    except Exception as e:
        log.warning(f"  자막 목록 조회 실패 [{video_id}]: {e}")
        # yt-dlp fallback 시도
        return _fetch_transcript_ytdlp(video_id, preferred_langs)

    transcript = None
    for lang in preferred_langs:
        try:
            transcript = transcript_list.find_manually_created_transcript([lang])
            break
        except NoTranscriptFound:
            continue
    if transcript is None:
        for lang in preferred_langs:
            try:
                transcript = transcript_list.find_generated_transcript([lang])
                break
            except NoTranscriptFound:
                continue
    if transcript is None:
        try:
            transcript = next(iter(transcript_list))
            if preferred_langs and transcript.is_translatable:
                transcript = transcript.translate(preferred_langs[0])
        except StopIteration:
            return None

    entries = transcript.fetch()
    text = " ".join(e.text.replace("\n", " ").strip() for e in entries if e.text)
    return {"language": transcript.language_code, "text": text}


def _fetch_transcript_ytdlp(video_id: str, preferred_langs: list[str]) -> dict[str, Any] | None:
    """yt-dlp로 자막 다운로드 (fallback)."""
    import tempfile, re
    import yt_dlp

    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory() as tmpdir:
        opts = {
            "skip_download": True,
            "quiet": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": preferred_langs + ["en"],
            "subtitlesformat": "vtt",
            "outtmpl": str(Path(tmpdir) / "%(id)s.%(ext)s"),
            "ignoreerrors": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        vtt_files = list(Path(tmpdir).glob(f"{video_id}*.vtt"))
        if not vtt_files:
            log.warning(f"  yt-dlp 자막 없음: {video_id}")
            return None

        chosen = vtt_files[0]
        for lang in preferred_langs:
            matches = [f for f in vtt_files if f".{lang}." in f.name]
            if matches:
                chosen = matches[0]
                break

        # VTT 파싱
        lines = []
        for line in chosen.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("WEBVTT") or "-->" in line:
                continue
            line = re.sub(r"<[^>]+>", "", line)
            if lines and lines[-1] == line:
                continue
            if line:
                lines.append(line)
        text = " ".join(lines)
        lang_code = chosen.stem.split(".")[-1] if "." in chosen.stem else "unknown"
        return {"language": lang_code, "text": text}


def fetch_video_metadata(video_id: str) -> dict[str, Any]:
    """yt-dlp로 영상 메타데이터 수집."""
    import yt_dlp
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        opts = {"skip_download": True, "quiet": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        ts = info.get("timestamp")
        pub_at = datetime.utcfromtimestamp(int(ts)).isoformat() if ts else None
        return {
            "title": info.get("title"),
            "published_at": pub_at,
            "duration_sec": info.get("duration"),
            "url": url,
        }
    except Exception as e:
        log.warning(f"  메타데이터 수집 실패 [{video_id}]: {e}")
        return {"title": None, "published_at": None, "duration_sec": None, "url": url}


def poll_rss(channel_id: str) -> list[dict[str, Any]]:
    """채널 RSS에서 최근 영상 목록 가져오기."""
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    feed = feedparser.parse(rss_url)
    videos = []
    for entry in feed.entries:
        vid = entry.get("yt_videoid") or entry.get("id", "").split(":")[-1]
        if not vid:
            continue
        pub = None
        if entry.get("published_parsed"):
            pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
        videos.append({
            "video_id": vid,
            "title": entry.get("title"),
            "url": entry.get("link") or f"https://www.youtube.com/watch?v={vid}",
            "published_at": pub,
        })
    return videos


# ------------------------------------------------------------------ #
# 메인 실행 로직
# ------------------------------------------------------------------ #

def run(client: EC2Client) -> None:
    log.info("=" * 50)
    log.info("Moppu Local Collector 시작")
    log.info("=" * 50)

    # EC2에서 수집 대상 가져오기
    try:
        items_data = client.get_items()
    except Exception as e:
        log.error(f"EC2 항목 조회 실패: {e}")
        return

    preferred_langs: list[str] = items_data.get("preferred_languages", ["ko", "en"])
    video_list_items: list[dict] = items_data.get("video_list_items", [])
    channel_items: list[dict] = items_data.get("channel_items", [])

    all_tasks: list[dict[str, Any]] = []

    # 1) 영상 목록 항목
    for item in video_list_items:
        all_tasks.append({
            "video_id": item["video_id"],
            "source_type": f"list:{item['list_name']}",
            "url": item.get("source_url"),
        })

    # 2) 채널 RSS (upload_day가 어제인 채널)
    yesterday_kst = datetime.now(KST) - timedelta(days=1)
    yesterday_day = yesterday_kst.day

    for ch in channel_items:
        upload_day = ch.get("upload_day")
        if upload_day and upload_day != yesterday_day:
            log.info(f"  채널 스킵 (upload_day {upload_day} ≠ 어제 {yesterday_day}): {ch.get('name')}")
            continue
        channel_id = ch["channel_id"]
        title_filter = ch.get("title_contains")
        log.info(f"채널 RSS 폴링: {ch.get('name', channel_id)}")
        try:
            rss_videos = poll_rss(channel_id)
        except Exception as e:
            log.warning(f"  RSS 실패 [{channel_id}]: {e}")
            continue
        for v in rss_videos:
            if title_filter and title_filter not in (v.get("title") or ""):
                continue
            all_tasks.append({
                "video_id": v["video_id"],
                "source_type": channel_id,
                "url": v["url"],
                "title": v.get("title"),
                "published_at": v.get("published_at"),
            })

    if not all_tasks:
        log.info("수집할 항목 없음 — 종료")
        return

    log.info(f"수집 대상: {len(all_tasks)}건")
    success = 0

    for task in all_tasks:
        video_id = task["video_id"]
        log.info(f"처리: {video_id}")

        # 메타데이터 보완
        if not task.get("title"):
            meta = fetch_video_metadata(video_id)
            task.update({k: v for k, v in meta.items() if v is not None})

        # 자막 수집
        result = fetch_transcript(video_id, preferred_langs)
        if result is None:
            log.warning(f"  자막 없음, 스킵: {video_id}")
            continue

        # EC2로 전송 (EC2에서 임베딩 처리)
        payload = {
            "video_id": video_id,
            "source_type": task["source_type"],
            "title": task.get("title"),
            "url": task.get("url"),
            "published_at": task.get("published_at"),
            "duration_sec": task.get("duration_sec"),
            "language": result["language"],
            "transcript_text": result["text"],
        }
        if client.send_transcript(payload):
            success += 1

    log.info(f"완료: {success}/{len(all_tasks)}건 전송")

    if success > 0:
        log.info("EC2 요약 생성 트리거 중...")
        client.trigger_process()

    log.info("=" * 50)


def load_or_create_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        default: dict[str, Any] = {
            "ec2_url": "http://54.180.107.217:8000",
            "dashboard_id": "moppu",
            "dashboard_password": "Gksrlgns12!",
            "preferred_languages": ["ko", "en"],
        }
        CONFIG_FILE.write_text(
            json.dumps(default, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\n설정 파일이 생성되었습니다: {CONFIG_FILE}")
        print("ec2_url, dashboard_id, dashboard_password 를 확인 후 재실행하세요.\n")
        sys.exit(0)
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    cfg = load_or_create_config()

    client = EC2Client(
        base_url=cfg["ec2_url"],
        dashboard_id=cfg["dashboard_id"],
        dashboard_password=cfg["dashboard_password"],
    )

    try:
        client.login()
    except Exception as e:
        log.error(f"EC2 로그인 실패 ({cfg['ec2_url']}): {e}")
        sys.exit(1)

    run(client)
