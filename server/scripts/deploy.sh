#!/usr/bin/env bash
# 로컬(Mac)에서 실행: 코드 변경을 push하고 서버에 반영한다.
# 사용법: QT_SSH_HOST=ubuntu@<ElasticIP> ./server/scripts/deploy.sh
set -euo pipefail

if [ -z "${QT_SSH_HOST:-}" ]; then
  echo "[deploy] QT_SSH_HOST 환경변수가 설정되지 않았습니다." >&2
  echo "  사용법: QT_SSH_HOST=ubuntu@<ElasticIP> ./server/scripts/deploy.sh" >&2
  exit 1
fi

REPO_DIR="quant_trading_kiwoom"

echo "[deploy] git push"
git push

echo "[deploy] 서버 반영: $QT_SSH_HOST"
# 비로그인 ssh 셸에는 uv가 PATH에 없다 (server/CLAUDE.md 불변식 — 2026-08-10
# 'uv: command not found'로 재시작이 조용히 스킵된 실측). 절대경로로 호출한다.
ssh "$QT_SSH_HOST" "cd $REPO_DIR && git pull && ~/.local/bin/uv sync \
  && sudo systemctl restart quant-engine tg-bridge \
  && systemctl is-active quant-engine tg-bridge"

echo "[deploy] 완료."
