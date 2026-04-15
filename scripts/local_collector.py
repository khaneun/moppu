#!/usr/bin/env python3
"""
Moppu Local Collector
======================
로컬 Windows PC에서 YouTube 자막을 수집하여 EC2로 전송합니다.

역할 분리:
  로컬 PC (이 스크립트) — YouTube 자막·메타데이터 수집 (IP 차단 없음)
  EC2                   — 임베딩, 요약 생성, 대시보드, 봇

실행 모드:
  python local_collector.py          한 번 실행 후 종료 (Task Scheduler 용)
  python local_collector.py --watch  감시 모드 — EC2 폴링하며 대기 (수동 트리거 지원)
  python local_collector.py --setup  초기 설정 + Windows Task Scheduler 등록

필요 패키지:
  pip install -r requirements-local.txt
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ------------------------------------------------------------------ #
# 경로 / 로거
# ------------------------------------------------------------------ #

SCRIPT_DIR  = Path(__file__).parent.resolve()
CONFIG_FILE = SCRIPT_DIR / "collector_config.json"
LOG_FILE    = SCRIPT_DIR / "collector.log"
KST         = timezone(timedelta(hours=9))

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
        import requests
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
        self._session.headers["Authorization"] = f"Bearer {token}"
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
                log.warning(f"  전송 실패 [{payload['video_id']}]: {resp.status_code} {resp.text[:100]}")
                return False
            r = resp.json()
            if r.get("skipped"):
                log.info(f"  스킵 (이미 임베딩됨): {payload['video_id']}")
            else:
                log.info(f"  ✓ 임베딩 완료: {payload['video_id']} ({r.get('chunks','?')}청크)")
            return True
        except Exception as e:
            log.warning(f"  전송 오류 [{payload['video_id']}]: {e}")
            return False

    def trigger_process(self) -> None:
        try:
            self._session.post(f"{self.base_url}/api/collect/process", timeout=10)
            log.info("EC2 요약 생성 트리거 완료")
        except Exception as e:
            log.warning(f"처리 트리거 실패: {e}")

    def poll_run_request(self) -> bool:
        """대시보드에서 실행 요청이 왔는지 확인 (감시 모드용)."""
        try:
            resp = self._session.get(f"{self.base_url}/api/collect/poll", timeout=5)
            return resp.ok and resp.json().get("requested", False)
        except Exception:
            return False


# ------------------------------------------------------------------ #
# YouTube 자막 수집 (로컬 — IP 차단 없음)
# ------------------------------------------------------------------ #

def fetch_transcript(video_id: str, preferred_langs: list[str]) -> dict[str, Any] | None:
    from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled, YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    try:
        transcript_list = api.list(video_id)
    except TranscriptsDisabled:
        log.info(f"  자막 비활성화: {video_id}")
        return None
    except Exception as e:
        log.warning(f"  자막 목록 실패 [{video_id}]: {e} — yt-dlp 시도")
        return _fetch_via_ytdlp(video_id, preferred_langs)

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


def _fetch_via_ytdlp(video_id: str, preferred_langs: list[str]) -> dict[str, Any] | None:
    import re, tempfile
    import yt_dlp

    with tempfile.TemporaryDirectory() as tmpdir:
        opts = {
            "skip_download": True, "quiet": True,
            "writesubtitles": True, "writeautomaticsub": True,
            "subtitleslangs": preferred_langs + ["en"],
            "subtitlesformat": "vtt",
            "outtmpl": str(Path(tmpdir) / "%(id)s.%(ext)s"),
            "ignoreerrors": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

        vtt_files = list(Path(tmpdir).glob(f"{video_id}*.vtt"))
        if not vtt_files:
            return None

        chosen = vtt_files[0]
        for lang in preferred_langs:
            m = [f for f in vtt_files if f".{lang}." in f.name]
            if m:
                chosen = m[0]; break

        lines = []
        for line in chosen.read_text(encoding="utf-8").splitlines():
            line = re.sub(r"<[^>]+>", "", line.strip())
            if not line or "-->" in line or line.startswith("WEBVTT"):
                continue
            if lines and lines[-1] == line:
                continue
            lines.append(line)
        lang_code = chosen.stem.split(".")[-1] if "." in chosen.stem else "unknown"
        return {"language": lang_code, "text": " ".join(lines)}


# ------------------------------------------------------------------ #
# 채널 영상 목록 수집 (YouTube Data API 또는 RSS fallback)
# ------------------------------------------------------------------ #

def list_channel_videos_api(channel_id: str, api_key: str, max_results: int = 50) -> list[dict[str, Any]]:
    """YouTube Data API v3 로 채널 최신 영상 목록 수집."""
    import requests

    videos = []
    # 1) 업로드 재생목록 ID 조회
    r = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "contentDetails", "id": channel_id, "key": api_key},
        timeout=10,
    )
    r.raise_for_status()
    items = r.json().get("items", [])
    if not items:
        return []
    playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    # 2) 재생목록 아이템 조회
    next_page = None
    while True:
        params: dict = {
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": min(max_results, 50),
            "key": api_key,
        }
        if next_page:
            params["pageToken"] = next_page
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/playlistItems",
            params=params, timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        for item in data.get("items", []):
            sn = item["snippet"]
            vid = sn["resourceId"]["videoId"]
            pub = sn.get("publishedAt")
            videos.append({
                "video_id": vid,
                "title": sn.get("title"),
                "url": f"https://www.youtube.com/watch?v={vid}",
                "published_at": pub,
            })
            if len(videos) >= max_results:
                return videos
        next_page = data.get("nextPageToken")
        if not next_page:
            break
    return videos


def list_channel_videos_rss(channel_id: str) -> list[dict[str, Any]]:
    """RSS로 채널 최신 영상 목록 수집 (~15건)."""
    import feedparser
    feed = feedparser.parse(
        f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    )
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


def fetch_video_metadata_api(video_id: str, api_key: str) -> dict[str, Any]:
    """YouTube Data API v3로 영상 메타데이터 수집."""
    import requests
    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "snippet,contentDetails", "id": video_id, "key": api_key},
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            return {}
        sn = items[0]["snippet"]
        cd = items[0].get("contentDetails", {})
        duration = _parse_iso_duration(cd.get("duration", ""))
        return {
            "title": sn.get("title"),
            "published_at": sn.get("publishedAt"),
            "duration_sec": duration,
            "url": f"https://www.youtube.com/watch?v={video_id}",
        }
    except Exception as e:
        log.warning(f"  Data API 메타데이터 실패 [{video_id}]: {e}")
        return {}


def fetch_video_metadata_ytdlp(video_id: str) -> dict[str, Any]:
    """yt-dlp로 영상 메타데이터 수집."""
    import yt_dlp
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL({"skip_download": True, "quiet": True}) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        ts = info.get("timestamp")
        pub = datetime.utcfromtimestamp(int(ts)).isoformat() if ts else None
        return {
            "title": info.get("title"),
            "published_at": pub,
            "duration_sec": info.get("duration"),
            "url": url,
        }
    except Exception as e:
        log.warning(f"  yt-dlp 메타데이터 실패 [{video_id}]: {e}")
        return {"url": url}


def _parse_iso_duration(s: str) -> int | None:
    """PT1H30M15S → 5415초"""
    import re
    if not s:
        return None
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s)
    if not m:
        return None
    h, mi, sec = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + sec


# ------------------------------------------------------------------ #
# 수집 실행
# ------------------------------------------------------------------ #

def run_collect(client: EC2Client, cfg: dict[str, Any]) -> None:
    preferred_langs: list[str] = cfg.get("preferred_languages", ["ko", "en"])
    youtube_api_key: str | None = cfg.get("youtube_api_key")

    log.info("=" * 55)
    log.info("Moppu Local Collector — 수집 시작")
    log.info("=" * 55)

    try:
        items_data = client.get_items()
    except Exception as e:
        log.error(f"EC2 항목 조회 실패: {e}")
        return

    video_list_items: list[dict] = items_data.get("video_list_items", [])
    channel_items:    list[dict] = items_data.get("channel_items", [])

    tasks: list[dict[str, Any]] = []

    # 1) 영상 목록 항목 (video_lists)
    for item in video_list_items:
        tasks.append({
            "video_id":    item["video_id"],
            "source_type": f"list:{item['list_name']}",
            "url":         item.get("source_url"),
        })

    # 2) 채널 항목 — upload_day 기준 필터링
    yesterday_day = (datetime.now(KST) - timedelta(days=1)).day
    for ch in channel_items:
        upload_day = ch.get("upload_day")
        if upload_day and upload_day != yesterday_day:
            continue

        channel_id   = ch["channel_id"]
        title_filter = ch.get("title_contains")
        log.info(f"채널 폴링: {ch.get('name', channel_id)}")

        try:
            if youtube_api_key:
                videos = list_channel_videos_api(channel_id, youtube_api_key)
            else:
                videos = list_channel_videos_rss(channel_id)
        except Exception as e:
            log.warning(f"  채널 목록 실패 [{channel_id}]: {e}")
            continue

        for v in videos:
            if title_filter and title_filter not in (v.get("title") or ""):
                continue
            tasks.append({
                "video_id":    v["video_id"],
                "source_type": channel_id,
                "url":         v["url"],
                "title":       v.get("title"),
                "published_at": v.get("published_at"),
            })

    if not tasks:
        log.info("수집할 항목 없음")
        return

    log.info(f"수집 대상: {len(tasks)}건")
    success = 0

    for task in tasks:
        video_id = task["video_id"]
        log.info(f"처리: {video_id}")

        # 메타데이터 보완
        if not task.get("title"):
            if youtube_api_key:
                meta = fetch_video_metadata_api(video_id, youtube_api_key)
            else:
                meta = fetch_video_metadata_ytdlp(video_id)
            task.update({k: v for k, v in meta.items() if v is not None})

        # 자막 수집
        tr = fetch_transcript(video_id, preferred_langs)
        if tr is None:
            log.warning(f"  자막 없음, 스킵: {video_id}")
            continue

        # EC2로 전송 (EC2에서 임베딩)
        payload: dict[str, Any] = {
            "video_id":       video_id,
            "source_type":    task["source_type"],
            "title":          task.get("title"),
            "url":            task.get("url"),
            "published_at":   task.get("published_at"),
            "duration_sec":   task.get("duration_sec"),
            "language":       tr["language"],
            "transcript_text": tr["text"],
        }
        if client.send_transcript(payload):
            success += 1

    log.info(f"완료: {success}/{len(tasks)}건 전송")
    if success > 0:
        client.trigger_process()


# ------------------------------------------------------------------ #
# 감시 모드 (대시보드 수동 트리거 지원)
# ------------------------------------------------------------------ #

def watch_mode(client: EC2Client, cfg: dict[str, Any], poll_interval: int = 60) -> None:
    log.info(f"감시 모드 시작 (폴링 간격: {poll_interval}초) — Ctrl+C로 종료")
    next_midnight_run = _next_midnight_kst()
    log.info(f"다음 자정 자동 실행: {next_midnight_run.strftime('%Y-%m-%d %H:%M:%S KST')}")

    while True:
        try:
            # 대시보드 트리거 확인
            if client.poll_run_request():
                log.info("▶ 대시보드 실행 요청 수신 — 즉시 실행")
                run_collect(client, cfg)
                next_midnight_run = _next_midnight_kst()

            # 자정 자동 실행
            now = datetime.now(KST)
            if now >= next_midnight_run:
                log.info("▶ 자정 자동 실행")
                run_collect(client, cfg)
                next_midnight_run = _next_midnight_kst()

        except Exception as e:
            log.warning(f"감시 루프 오류: {e}")

        time.sleep(poll_interval)


def _next_midnight_kst() -> datetime:
    now = datetime.now(KST)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return midnight


# ------------------------------------------------------------------ #
# Windows Task Scheduler 등록
# ------------------------------------------------------------------ #

def _run_powershell(ps_lines: list[str]) -> tuple[bool, str, str]:
    """PowerShell 명령을 임시 .ps1 파일로 실행 (인자 이스케이프 문제 우회)."""
    import subprocess, tempfile
    ps_content = "\n".join(ps_lines)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".ps1",
                                     delete=False, encoding="utf-8") as f:
        f.write(ps_content)
        tmp = f.name
    try:
        result = subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", tmp],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        return result.returncode == 0, result.stdout, result.stderr
    finally:
        Path(tmp).unlink(missing_ok=True)


def register_task_scheduler(run_time: str = "00:05") -> bool:
    """Windows 작업 스케줄러에 일일 실행 작업 등록."""
    if sys.platform != "win32":
        log.warning("Windows가 아닙니다. Task Scheduler 등록 건너뜀.")
        return False

    python_exe  = sys.executable
    script_path = str(Path(__file__).resolve())
    work_dir    = str(Path(__file__).parent)

    ps_lines = [
        '$ErrorActionPreference = "Stop"',
        f'$exe     = "{python_exe}"',
        f'$script  = "{script_path}"',
        f'$workdir = "{work_dir}"',
        f'$time    = "{run_time}"',
        '$action   = New-ScheduledTaskAction -Execute $exe -Argument $script -WorkingDirectory $workdir',
        '$trigger  = New-ScheduledTaskTrigger -Daily -At $time',
        '$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew',
        'Register-ScheduledTask -TaskName "MoppuLocalCollector" '
        '-Description "Moppu YouTube 자막 수집기" '
        '-Action $action -Trigger $trigger -Settings $settings -Force | Out-Null',
        'Write-Output "REGISTERED"',
    ]
    ok, stdout, stderr = _run_powershell(ps_lines)
    if "REGISTERED" in stdout:
        log.info(f"✓ Task Scheduler 등록 완료 (매일 {run_time})")
        return True
    log.warning(f"Task Scheduler 등록 실패 (rc={ok}):\nstdout={stdout[:200]}\nstderr={stderr[:200]}")
    return False


def register_watch_startup() -> bool:
    """로그인 시 감시 모드 자동 시작 등록."""
    if sys.platform != "win32":
        return False

    python_exe  = sys.executable
    script_path = str(Path(__file__).resolve())
    work_dir    = str(Path(__file__).parent)

    ps_lines = [
        '$ErrorActionPreference = "Stop"',
        f'$exe     = "{python_exe}"',
        f'$script  = "{script_path} --watch"',
        f'$workdir = "{work_dir}"',
        '$action   = New-ScheduledTaskAction -Execute $exe -Argument $script -WorkingDirectory $workdir',
        '$trigger  = New-ScheduledTaskTrigger -AtLogOn',
        '$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan) -MultipleInstances IgnoreNew',
        'Register-ScheduledTask -TaskName "MoppuLocalCollectorWatch" '
        '-Description "Moppu 감시 모드 (로그인 시 자동 시작)" '
        '-Action $action -Trigger $trigger -Settings $settings -Force | Out-Null',
        'Write-Output "REGISTERED"',
    ]
    ok, stdout, stderr = _run_powershell(ps_lines)
    if "REGISTERED" in stdout:
        log.info("✓ 감시 모드 시작 프로그램 등록 완료")
        return True
    log.warning(f"감시 모드 등록 실패:\nstdout={stdout[:200]}\nstderr={stderr[:200]}")
    return False


# ------------------------------------------------------------------ #
# 설정 마법사
# ------------------------------------------------------------------ #

def setup_wizard() -> None:
    print("\n" + "=" * 55)
    print("  Moppu Local Collector 초기 설정")
    print("=" * 55)

    cfg: dict[str, Any] = {}
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        print(f"\n기존 설정 파일 발견: {CONFIG_FILE}")

    def ask(prompt: str, default: Any = None) -> str:
        suffix = f" [{default}]" if default else ""
        val = input(f"{prompt}{suffix}: ").strip()
        return val if val else str(default or "")

    cfg["ec2_url"]            = ask("EC2 대시보드 URL", cfg.get("ec2_url", "http://54.180.107.217:8000"))
    cfg["dashboard_id"]       = ask("대시보드 ID", cfg.get("dashboard_id", "moppu"))
    cfg["dashboard_password"] = ask("대시보드 비밀번호", cfg.get("dashboard_password", ""))
    cfg["youtube_api_key"]    = ask("YouTube Data API 키 (없으면 Enter)", cfg.get("youtube_api_key", ""))
    if not cfg["youtube_api_key"]:
        cfg.pop("youtube_api_key", None)
    cfg["preferred_languages"] = ["ko", "en"]

    # EC2 연결 테스트
    print("\nEC2 연결 테스트 중...")
    try:
        import requests
        client = EC2Client(cfg["ec2_url"], cfg["dashboard_id"], cfg["dashboard_password"])
        client.login()
        print("✓ EC2 연결 성공!")
    except Exception as e:
        print(f"✗ EC2 연결 실패: {e}")
        if input("계속 진행하시겠습니까? (y/N): ").strip().lower() != "y":
            return

    # 설정 저장
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n✓ 설정 저장: {CONFIG_FILE}")

    # pip 설치 확인
    print("\n패키지 설치 중...")
    import subprocess
    req_file = SCRIPT_DIR / "requirements-local.txt"
    if req_file.exists():
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req_file), "-q"], check=False)
        print("✓ 패키지 설치 완료")

    if sys.platform == "win32":
        # Task Scheduler 등록
        run_time = ask("\n매일 자동 실행 시각 (HH:MM)", "00:05")
        if register_task_scheduler(run_time):
            print(f"✓ Task Scheduler 등록 완료 (매일 {run_time})")

        # 감시 모드 시작프로그램 등록
        if input("\n대시보드 수동 트리거를 위한 감시 모드를 시작 프로그램에 등록할까요? (Y/n): ").strip().lower() != "n":
            if register_watch_startup():
                print("✓ 감시 모드 시작 프로그램 등록 완료")

    print("\n" + "=" * 55)
    print("  설정 완료!")
    print(f"  즉시 실행:     python {Path(__file__).name}")
    print(f"  감시 모드:     python {Path(__file__).name} --watch")
    print("=" * 55 + "\n")


# ------------------------------------------------------------------ #
# 진입점
# ------------------------------------------------------------------ #

def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        print(f"\n설정 파일이 없습니다. 먼저 --setup 을 실행하세요.")
        print(f"  python {Path(__file__).name} --setup\n")
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    args = set(sys.argv[1:])

    if "--setup" in args:
        setup_wizard()
        sys.exit(0)

    cfg = load_config()
    client = EC2Client(cfg["ec2_url"], cfg["dashboard_id"], cfg["dashboard_password"])

    try:
        client.login()
    except Exception as e:
        log.error(f"EC2 로그인 실패 ({cfg.get('ec2_url')}): {e}")
        sys.exit(1)

    if "--watch" in args:
        poll_interval = int(cfg.get("poll_interval_sec", 60))
        watch_mode(client, cfg, poll_interval)
    else:
        run_collect(client, cfg)
