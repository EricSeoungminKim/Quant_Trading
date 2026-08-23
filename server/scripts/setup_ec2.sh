#!/usr/bin/env bash
# EC2 최초 셋업 (idempotent — 재실행해도 안전). 실행 위치: 서버 (ssh ubuntu@<ElasticIP>).
# 사전 조건: server/README.md의 §1(콘솔 작업 — Elastic IP, Toss 허용목록 등록)이 끝나 있을 것.
set -euo pipefail

REPO_URL="https://github.com/EricSeoungminKim/quant_trading_kiwoom.git"
REPO_DIR="$HOME/quant_trading_kiwoom"

echo "[setup_ec2] 타임존 확인 (Asia/Seoul)"
if [ "$(timedatectl show --property=Timezone --value)" != "Asia/Seoul" ]; then
  sudo timedatectl set-timezone Asia/Seoul
fi

echo "[setup_ec2] apt 업데이트 + git 설치"
sudo apt-get update -y
if ! command -v git >/dev/null 2>&1; then
  sudo apt-get install -y git
fi

echo "[setup_ec2] uv 설치 확인"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # shellcheck disable=SC1090
  source "$HOME/.local/bin/env" 2>/dev/null || export PATH="$HOME/.local/bin:$PATH"
fi

echo "[setup_ec2] 레포 clone 또는 pull"
if [ -d "$REPO_DIR/.git" ]; then
  git -C "$REPO_DIR" pull
else
  git clone "$REPO_URL" "$REPO_DIR"
fi

echo "[setup_ec2] uv sync"
cd "$REPO_DIR"
uv sync

echo "[setup_ec2] systemd 유닛 설치"
sudo cp server/systemd/quant-engine.service /etc/systemd/system/
sudo cp server/systemd/tg-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now quant-engine
sudo systemctl enable --now tg-bridge

echo "[setup_ec2] crontab 설치"
crontab server/crontab.txt

echo ""
echo "[setup_ec2] 완료. 남은 수동 작업:"
echo "  1. .env.local이 아직 없다면 Mac에서 scp로 전달:"
echo "       scp .env.local ubuntu@<ElasticIP>:$REPO_DIR/"
echo "     전달 후: sudo systemctl restart quant-engine tg-bridge"
echo "  2. 이 서버의 Elastic IP가 Toss Open API 허용목록에 등록돼 있는지 확인"
echo "     (등록 안 됐으면 엔진은 403으로 대기한다)."
