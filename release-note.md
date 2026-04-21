# Moppu Release Notes

---

## v0.3.0 — 2026-04-21

### 주요 기능

#### 💰 종합 현황 개선
- **자산 계산 버그 수정**: `inquire-balance` TR 응답 `output2` 필드 직접 파싱으로 전면 교체
  - 10개 필드 정확 파싱: 예수금·D+2예수금·주식평가·총평가·매입원가·평가손익·수익률·순자산·전일대비·전일대비율
- **보유 종목 간소화**: 테이블 4컬럼 (종목명·수량·평균가·평가손익)
- **과거 매매 이력 팝업**: 종목 행 클릭 → 90일 매매 내역 + 평균 매입가 기반 실현손익·수익률 계산
- **새 API**: `GET /api/positions/{ticker}/trades`

#### 🤖 전략 수립가 강화
- **LSY 강경도(conviction 1-10)**: 시스템 프롬프트에 구간별 투자 가이드 주입
  - 8-10: 공격적 포지션, 5-7: 균형 분할매수, 1-4: 신중 관망
- **강경도 기반 추가 투자 요청**: 예산 부족 시 Telegram 자동 알림
  - 🔥 긴급(8-10) / 💰 권유(5-7) / 📝 알림(1-4) 톤 차별화
  - 강경도 8+ 이체 미확인 시 follow-up 메시지
- **라이브 로그 패널**: 전략 실행 중 실시간 로그 스트리밍 (고정 200px, 자동 스크롤)
- **아이콘 버튼**: 새로고침(🔄)·중단(⏹) 버튼으로 실행 제어
- **실패 이력 로그 팝업**: 실패한 전략 이력 행 클릭 → `.log` 파일 내용 표시
- **완료 상세 2줄 표시**: 매수/매도 계획 각각 독립 행으로 표시
- **새 API**: `GET /api/strategy/live-log`, `POST /api/strategy/stop`, `GET /api/strategy/history/{filename}`

#### 🔧 파이프라인 개선
- **EC2 YouTube 직접 호출 제거**: `poll_channels` job 완전 삭제 (AWS IP 차단 대응)
  - `config.yaml`에서 `poll_channels_cron` 제거, `upload_day_cron`만 유지
  - `upload_day` job은 `sync_video_lists()`만 수행, YouTube 호출 없음
- **실패 건 재시도**: 파이프라인 이력에서 실패 영상 재시도 버튼 (상태 `pending` 재전환 + 로컬 큐 전달)
- **로컬 수집기 연결 상태 배너**: 5분 이상 heartbeat 없으면 `disconnected` 표시
- **수집 이력 상태 컬럼 추가**

#### ⏰ 스케줄러 타임존 수정
- `BlockingScheduler(timezone=KST)` + `CronTrigger.from_crontab(..., timezone=KST)` 명시
- KST 자정(`"5 0 * * *"`) 정확 실행 보장

#### 🏦 KIS 브로커 API 확장
- `AccountSummary` 데이터클래스: 10개 자산 필드
- `TradeFill` 데이터클래스: 체결 내역 (날짜·종목·수량·가격·구분)
- `get_account_summary()`: `inquire-balance` TR 직접 파싱
- `get_daily_trades()`: `TTTC8001R`/`VTTC8001R` TR, 90일 이내 체결 내역

### 버그 수정
- 4/20 09시 2건 중복 수집: `poll_channels_cron` 설정값(`*/15`) → EC2에서 YouTube RSS 직접 호출 → 제거로 해결
- 예수금 천만원 표시 오류: 수동 계산 로직 제거, KIS API 필드 직접 사용

---

## v0.2.0 — 2026-04-15

### 주요 기능

#### 🖥 Moppu Monitor 웹 대시보드
- FastAPI 기반 웹 대시보드 (`moppu dashboard`, 포트 8000)
- **종합 현황**: KIS 자산 평가·보유 종목·평가손익·수집 요약
- **Agent**: LSY Agent 대화, 시스템 프롬프트 조회, 파이프라인 현황
- **설정**: LLM 모델/투자 모드 변경, 긴급 중단, 요금 현황
- 로그인 인증 (DASHBOARD_ID / DASHBOARD_PASSWORD)
- 파이프라인 실행·로그·앱 로그 조회 (최대 500줄)
- Markdown 렌더링 (marked.js)
- 블랙 다크 테마

#### 🤖 LSY Agent 페르소나 시스템
- 수집된 YouTube 자막으로부터 이선엽 애널리스트 페르소나를 LLM으로 합성
- 시스템 프롬프트 = 합성된 행동 양식 (`data/agent_persona.md`)
- 영상 수집 시마다 페르소나 점진적 자동 업데이트
- RAG: 관련 자막 있을 때만 컨텍스트 주입 (없으면 페르소나 지식으로 답변)
- CLI: `moppu update-persona [--force]`

#### 🏠 로컬 수집기 (`scripts/local_collector.py`)
- YouTube IP 차단 우회: 로컬 PC에서 자막 수집 → EC2로 전송
- EC2에서 수집 대상 조회 (영상 목록·채널)
- `--watch` 모드: 대시보드 수동 트리거 실시간 감지
- `--setup` 마법사: 설정·패키지 설치·Windows Task Scheduler 자동 등록
- 수집 완료 3분 후 자동 종료 (`shutdown_after_run: true`)
- 수집 완료 시 EC2 완료 신호 + Telegram 알림

#### 📱 Telegram 봇 강화
- `/help`: 전체 커맨드 목록
- `/dashboard`: 클릭 가능한 인라인 버튼 링크
- `/appstatus`: EC2 서비스 실행 상태 (🟢🔴)
- 수집 머신 시작 알림 (Watch 모드 시작 시)
- 수집 완료 알림 (영상 제목·YouTube 링크 목록)

#### ☁️ EC2 배포
- 서울 리전 (ap-northeast-2), t3.medium, Amazon Linux 2023
- systemd 서비스: `moppu-dashboard`, `moppu-scheduler`, `moppu-bot`
- 배포 스크립트: `scripts/setup.sh`, `scripts/deploy.sh`, `scripts/restart.sh`
- AWS Secrets Manager 연동: `scripts/secrets.py`

#### 🔧 파이프라인 개선
- 대시보드 파이프라인 실행 버튼 → 로컬 수집기 트리거 + 상태 실시간 폴링
- 채널 수집: `upload_day` 제거, 매일 자정 ALL 채널 대상, 전일 업로드 + title_contains 필터
- 일일 수집 요약 + 추천 질문 3개 자동 생성 (수집 후 1회, 파일 캐싱)
- 파이프라인 로그 (`data/pipeline.log`) + 앱 로그 (journald/파일)

#### 💰 KIS 브로커
- 모의투자 / 실전투자 별도 App Key·계좌번호 지원
- 대시보드에서 즉시 모드 전환

### 변경 사항
- `ChannelSpec.upload_day` 제거 (매일 자동 수집으로 대체)
- 시스템 프롬프트: 영상 목록 주입 제거 → 페르소나 파일 우선 사용
- `youtube-transcript-api` v1.x API 호환성 수정 (`list_transcripts` → `.list()`)
- `TranscriptFetcher`: EC2 IP 차단 시 yt-dlp fallback

### 의존성 추가
- `fastapi>=0.115`, `uvicorn>=0.30` (대시보드)
- `boto3` (Secrets Manager, 로컬 선택)

---

## v0.1.0 — 2026-04-15 (초기 스캐폴드)

### 포함 기능
- YouTube 채널 RSS 폴링 + 초회 백필 (yt-dlp)
- YouTube 자막 수집 (youtube-transcript-api)
- 텍스트 청킹 + sentence-transformers 임베딩
- Chroma 벡터 스토어 + SQLite 메타데이터
- RAG 기반 에이전트 (`TraderAgent`)
- LLM 팩토리 (OpenAI / Anthropic / Google)
- KIS OpenAPI 스캐폴드 (주문·잔고·시세)
- Telegram 봇 기본 커맨드
- APScheduler 기반 스케줄러
- Typer CLI (`sync-channels`, `backfill`, `poll`, `ask`, `bot`, `scheduler`)
- 수동 큐레이션 영상 목록 (`video_lists`)
