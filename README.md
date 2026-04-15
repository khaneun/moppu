# moppu

YouTube 채널·영상의 자막을 지속 수집·임베딩하고, 그 context 위에서  
교체 가능한 LLM (Google / Anthropic / OpenAI) 으로 동작하는 **주식 매매 에이전트**.  
한국투자증권(KIS) OpenAPI 로 실제 주문을 실행하고, Telegram 으로 통제합니다.

## 아키텍처

```
YouTube 채널 (poll/WebSub)  ─┐
YouTube 영상 URL 목록        ─┤──▶  Ingestion  ──▶  Storage (SQLite + Chroma)
                              │    (yt-dlp / YTAPI         │
                              │     transcript-api)         │ RAG
                              │                             ▼
                              └────────────────────▶  Trader Agent
                                                      (LLM factory)
                                                           │
                              KIS Broker (실전/모의) ◀────┤
                              Telegram Bot ◀──────────────┘
```

## 준비

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
cp config/config.example.yaml config/config.yaml
cp config/channels.example.yaml config/channels.yaml
```

`.env` 에 각 프로바이더 API 키와 KIS / Telegram 자격증명을 채워주세요.  
`config/channels.yaml` 에 수집 대상 채널과 영상 목록을 넣고 시작합니다.

## 데이터 소스 설정

### 채널 추적 (자동 폴링)

```yaml
# config/channels.yaml
channels:
  - handle: "@someinvestor"
    name: 어떤 투자자
    tags: [kr-stocks]
    enabled: true
```

### 영상 URL 목록 (수동 큐레이션)

채널 전체가 아닌 **특정 영상만** 지정할 때 사용합니다.  
목록에 URL을 추가하고 `sync-video-lists && ingest-lists` 를 실행하면 **새로 추가된 것만** 수집합니다.

```yaml
# config/channels.yaml
video_lists:
  - name: semicon-highlights
    tags: [kr-stocks, semiconductor]
    enabled: true
    videos:
      - https://www.youtube.com/watch?v=VIDEO_ID
      - https://youtu.be/VIDEO_ID2
      - dQw4w9WgXcQ          # bare 11자 ID도 가능
```

지원 URL 형식:
- `https://www.youtube.com/watch?v=ID`
- `https://youtu.be/ID`
- `https://www.youtube.com/shorts/ID`
- `https://www.youtube.com/live/ID`
- 11자 bare ID

## 주로 쓰는 명령

### 채널 수집

```bash
moppu sync-channels                  # channels.yaml → DB 동기화
moppu backfill --channel-id UC...    # 특정 채널 전체 영상 초회 수집
moppu poll                           # 신규 영상 1회 폴링
moppu scheduler                      # APScheduler 로 주기적 자동 폴링
```

### 영상 목록 수집

```bash
moppu sync-video-lists               # channels.yaml의 video_lists → DB 등록 (멱등)
moppu ingest-lists                   # 미수집 영상 전체 처리
moppu ingest-lists --list-name semicon-highlights  # 특정 목록만
```

### 에이전트 & 봇

```bash
moppu ask "오늘 반도체 섹터 어때?"    # 에이전트 1회 실행 (JSON 결정 출력)
moppu bot                            # Telegram 봇 기동
```

### Telegram 커맨드

| 커맨드 | 설명 |
|--------|------|
| `/status` | 채널·영상 수 현황 |
| `/poll` | 즉시 폴링 1회 |
| `/backfill <channel_id\|all>` | 채널 백필 |
| `/ingestlist [list_name]` | 영상 목록 수집 |
| `/ask <질문>` | 에이전트 실행 |
| `/dryrun on\|off` | 실주문 여부 토글 |

## 설정 포인트

| 항목 | 위치 |
|------|------|
| LLM 프로바이더·모델 교체 | `config/config.yaml` → `llm.provider`, `llm.providers.*.model` |
| 임베딩 프로바이더 교체 | `config/config.yaml` → `embeddings.provider` |
| KIS 실전/모의 전환 | `.env` → `KIS_ENV=real` (기본 `paper`) |
| 주문 안전장치 | `config/config.yaml` → `agent.dry_run`, `agent.max_order_krw` |
| 폴링 주기 | `config/config.yaml` → `scheduler.poll_channels_cron` |

> `agent.dry_run: true` (기본값) 상태에서는 실주문이 절대 나가지 않습니다.

## 테스트 & 린트

```bash
pytest
ruff check .
```

## 로드맵

- [x] 채널 수집 파이프라인 (RSS 폴링 + 초회 백필)
- [x] 영상 URL 목록 수집 (수동 큐레이션, 신규만 처리)
- [x] 임베딩 + Chroma RAG
- [x] LLM 팩토리 (OpenAI / Anthropic / Google 교체 가능)
- [x] KIS OpenAPI — 주문·잔고·시세
- [x] Telegram 봇 (`/status`, `/poll`, `/backfill`, `/ingestlist`, `/ask`, `/dryrun`)
- [ ] WebSub push 수신 엔드포인트 (FastAPI)
- [ ] Dashboard (Streamlit 또는 Next.js)
- [ ] 백테스트 / 전략 평가 프레임워크
