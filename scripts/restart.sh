#!/usr/bin/env bash
# 서비스만 재시작 (코드 변경 없이)
set -euo pipefail

SERVICE=${1:-"all"}

if [ "$SERVICE" = "all" ]; then
    sudo systemctl restart moppu-dashboard moppu-scheduler moppu-bot
    echo "✓ 전체 서비스 재시작 완료"
else
    sudo systemctl restart "moppu-$SERVICE"
    echo "✓ moppu-$SERVICE 재시작 완료"
fi

sudo systemctl status moppu-dashboard moppu-scheduler moppu-bot --no-pager | grep -E "Active:|●"
