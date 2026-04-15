#!/usr/bin/env bash
# 코드 배포 및 서비스 재시작
# git push 후 EC2에서 실행하거나 로컬에서 SSH로 호출
set -euo pipefail

APP_DIR="/opt/moppu"
cd "$APP_DIR"

echo "=== [1/4] git pull ==="
git pull origin main

echo "=== [2/4] 패키지 업데이트 ==="
export PATH="$HOME/.local/bin:$PATH"
uv pip install -e ".[dev]" -q

echo "=== [3/4] Secrets Manager에서 .env 갱신 ==="
python3 scripts/secrets.py --secret moppu/prod --region ap-northeast-2

echo "=== [4/4] 서비스 재시작 ==="
sudo systemctl restart moppu-dashboard moppu-scheduler moppu-bot

echo ""
echo "✓ 배포 완료!"
sudo systemctl status moppu-dashboard --no-pager -l | head -20
