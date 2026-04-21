# moppu v0.3.0

YouTube 채널·영상의 자막을 지속 수집·임베딩하고, 수집된 내용으로부터 합성된  
**LSY Agent 페르소나** 위에서 한국 주식 시장을 분석하는 AI 에이전트.  
한국투자증권(KIS) OpenAPI로 실제 주문을 실행하고,  
웹 대시보드(Moppu Monitor)와 Telegram으로 통제합니다.

---

## 아키텍처

```
[로컬 PC] ─────────────────────────────────────────────────────────────
  local_collector.py (매일 00:05, Watch 모드)
  ├── EC2에서 수집 대상 조회 (GET /api/collect/items)
  ├── YouTube 자막 수집 (youtube-transcript-api, yt-dlp fallback)
  └── EC2로 전송 (POST /api/collect/transcript)
        │
[EC2] ──┼─────────────────────────────────────────────────────────────
        │                     ┌── sentence-transformers (임베딩)
        └─→ SQLite + Chroma ──┤
                               └── LLM (OpenAI/Anthropic/Google)
                                     │
                           ┌─────────┴────────────┐
                     LSY Agent 페르소나       일일 요약 생성
                   (agent_persona.md)      (daily_summary_*.json)
                           │
                  Moppu Monitor (대시보드)
                  Telegram Bot
                  KIS OpenAPI (주문 실행)
```

---

## 구성 요소

### 로컬 수집기 (`scripts/local_collector.py`)
- Windows PC에서 실행, YouTube IP 차단 우회
- EC2에서 수집 대상(영상 목록·채널) 조회 → 자막 수집 → EC2 전송
- 수집 완료 시 Telegram 알림 (영상 제목·링크 포함)
- `--watch` 모드: 대시보드 수동 트리거 실시간 감지 (60초 폴링)
- Windows Task Scheduler 자동 등록 (`--setup`)

### Moppu Monitor (웹 대시보드)
- **종합 현황**: 예수금·주식평가·총평가·매입원가·평가손익·수익률 (KIS 직접 파싱), 보유 종목 행 클릭 → 과거 매매 이력(실현손익·수익률) 팝업
- **전략 수립가**: LSY 강경도(1-10) 배지, 라이브 로그 패널(고정 200px 자동 스크롤), 새로고침/중단 아이콘 버튼, 실패 이력 로그 팝업, 매수/매도 계획 2줄 상세 표시
- **파이프라인**: 실패 건 재시도 버튼, 로컬 수집기 연결 상태 배너, 수집 이력 상태 컬럼
- FastAPI + 순수 HTML/CSS/JS, 포트 8000

### LSY Agent (전략 수립가)
- **페르소나 기반**: 수집된 영상으로부터 이선엽 애널리스트의  
  시장 분석 철학·종목 관점·의사결정 원칙·소통 스타일을 합성
- **RAG 검색**: 질문과 관련 자막이 있을 때만 컨텍스트 주입  
  (관련 없으면 페르소나 지식만으로 답변)
- **강경도(conviction 1-10)**: 강경도에 따라 추가 투자 요청 톤 조절  
  (8-10 🔥 긴급, 5-7 💰 권유, 1-4 📝 알림), 강경도 8+ 이체 미확인 시 follow-up
- 영상 수집 시마다 페르소나 자동 점진적 업데이트

### Telegram 봇
| 커맨드 | 설명 |
|--------|------|
| `/help` | 전체 커맨드 목록 |
| `/dashboard` | 대시보드 접속 링크 (클릭 가능) |
| `/status` | 채널·영상·임베딩 현황 |
| `/appstatus` | EC2 서비스 실행 상태 |
| `/run` | 파이프라인 즉시 실행 |
| `/summary` | 오늘의 영상 요약 |
| `/ask <질문>` | LSY Agent에게 질문 |
| `/model` | 현재 LLM 모델 |
| `/mode` | KIS 투자 모드 |
| `/emergency` / `/resume` | 긴급 중단 / 재개 |

---

## 빠른 시작

### EC2 최초 배포

```bash
# 1. EC2 접속 후 setup
git clone https://github.com/khaneun/moppu.git /home/ec2-user/moppu
cd /home/ec2-user/moppu
bash scripts/setup.sh

# 2. .env 구성 (또는 Secrets Manager 연동)
cp .env.example .env
# .env 편집

# 3. 설정 파일 복사
cp config/config.example.yaml config/config.yaml
cp config/channels.example.yaml config/channels.yaml
cp config/prompts/trader.system.example.md config/prompts/trader.system.md

# 4. 서비스 시작
sudo systemctl start moppu-dashboard moppu-scheduler moppu-bot
```

### 로컬 수집기 설정 (Windows)

```cmd
cd moppu\scripts
pip install -r requirements-local.txt
python local_collector.py --setup   # 설정 마법사 + Task Scheduler 등록
```

`scripts/collector_config.json` 에서 `"shutdown_after_run": true` 로 변경하면  
수집 완료 3분 후 자동 종료됩니다.

### 코드 업데이트 배포

```bash
# EC2
ssh -i ~/kitty-key.pem ec2-user@<IP> "cd /opt/moppu && bash scripts/deploy.sh"

# 로컬 PC
cd moppu\scripts && git pull
```

### LSY Agent 페르소나 생성

```bash
moppu update-persona          # 전체 transcript 기반 생성
moppu update-persona --force  # 강제 재생성
```

---

## 설정

### 두 레이어 구조

| 파일 | 역할 |
|------|------|
| `.env` | 시크릿 (API 키, KIS 자격증명, Telegram 토큰) |
| `config/config.yaml` | 비시크릿 설정 (LLM 모델, 임베딩, 스케줄) |
| `config/channels.yaml` | 수집 대상 채널·영상 목록 |

### 주요 설정 포인트

| 항목 | 위치 |
|------|------|
| LLM 프로바이더·모델 | `config.yaml → llm.provider`, `llm.model` |
| 임베딩 프로바이더 | `config.yaml → embeddings.provider` |
| KIS 실전/모의 전환 | `.env → KIS_ENV=real` (기본 `paper`) |
| 모의투자 별도 키 | `.env → KIS_PAPER_APP_KEY`, `KIS_PAPER_APP_SECRET`, `KIS_PAPER_ACCOUNT_NO` |
| 안전장치 | `config.yaml → agent.dry_run`, `agent.max_order_krw` |
| 수집 스케줄 | `config.yaml → scheduler.upload_day_cron` (기본 KST 매일 00:05) |
| 타임존 | APScheduler 및 CronTrigger 모두 `Asia/Seoul` 명시 |
| 대시보드 로그인 | `.env → DASHBOARD_ID`, `DASHBOARD_PASSWORD` |

### 채널 설정 예시

```yaml
# config/channels.yaml
channels:
  - handle: "@leesunyeup"
    name: 이선엽
    enabled: true
    title_contains: "이선엽"   # 제목에 이 문자열이 포함된 영상만 수집

video_lists:
  - name: leesunyeup
    enabled: true
    videos:
      - https://www.youtube.com/watch?v=VIDEO_ID
```

---

## 개발

```bash
pip install -e ".[dev]"

pytest                                     # 전체 테스트
pytest tests/test_config.py -k resolve    # 단일 테스트
ruff check .                               # 린트
ruff format .                              # 포맷
mypy src                                   # 타입 체크
moppu update-persona --force              # 페르소나 재생성
```

---

## 로드맵

- [x] 채널·영상 수집 파이프라인
- [x] 로컬 수집기 (Windows, YouTube IP 차단 우회)
- [x] 임베딩 + Chroma RAG
- [x] LLM 팩토리 (OpenAI / Anthropic / Google)
- [x] LSY Agent 페르소나 합성 시스템
- [x] KIS OpenAPI (주문·잔고·시세, 모의/실전 분리)
- [x] Moppu Monitor 웹 대시보드
- [x] Telegram 봇 (상태·수집·에이전트·제어)
- [x] EC2 배포 (systemd, Secrets Manager)
- [ ] WebSub push 수신 엔드포인트 (FastAPI)
- [ ] 백테스트 / 전략 평가 프레임워크
- [ ] 대시보드 모바일 최적화
