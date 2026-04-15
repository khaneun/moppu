#!/usr/bin/env bash
# EC2 초기 설정 스크립트 (Amazon Linux 2023 / Ubuntu 22.04)
# 최초 1회만 실행
set -euo pipefail

REPO_URL="https://github.com/khaneun/moppu.git"   # ← 실제 repo URL로 변경
APP_DIR="/opt/moppu"
APP_USER="ec2-user"   # Ubuntu면 "ubuntu" 로 변경

echo "=== [1/6] 시스템 패키지 업데이트 ==="
sudo dnf update -y 2>/dev/null || sudo apt-get update -y

echo "=== [2/6] Python / Git / 필수 도구 설치 ==="
sudo dnf install -y git python3.11 python3.11-pip 2>/dev/null || \
sudo apt-get install -y git python3.11 python3.11-pip python3.11-venv

echo "=== [3/6] uv 설치 ==="
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

echo "=== [4/6] 코드 클론 ==="
sudo mkdir -p "$APP_DIR"
sudo chown "$APP_USER:$APP_USER" "$APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
    cd "$APP_DIR" && git pull
else
    git clone "$REPO_URL" "$APP_DIR"
fi

echo "=== [5/6] 가상환경 + 패키지 설치 ==="
cd "$APP_DIR"
uv venv .venv
uv pip install -e ".[dev]"
uv pip install boto3   # Secrets Manager 접근용

echo "=== [6/6] Secrets Manager에서 .env 생성 ==="
python3 scripts/secrets.py --secret moppu/prod --region ap-northeast-2

echo "=== 설정 파일 초기화 ==="
[ -f config/config.yaml ] || cp config/config.example.yaml config/config.yaml
[ -f config/channels.yaml ] || cp config/channels.example.yaml config/channels.yaml
[ -f config/prompts/trader.system.md ] || cp config/prompts/trader.system.example.md config/prompts/trader.system.md

echo "=== systemd 서비스 등록 ==="
sudo cp scripts/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable moppu-dashboard moppu-scheduler moppu-bot

echo ""
echo "✓ 초기 설정 완료!"
echo "  서비스 시작: sudo systemctl start moppu-dashboard moppu-scheduler moppu-bot"
echo "  서비스 상태: sudo systemctl status moppu-dashboard"
