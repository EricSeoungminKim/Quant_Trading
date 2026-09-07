#!/usr/bin/env bash
# 매주 일요일 03:20 KST — 에폭/시드/수동 백업 사본 정리 (2026-09-07 라이브
# 준비 세션).
#
# 왜: `data/state`·`data/ledger`에 `*.pre-epoch-*`(paper-epoch 리셋 안전망)·
# `*.pre_seed*`(시드 재적재 안전망)·`*.bak*`/`regime.json.bak-*`(수동 복구
# 사본)가 정책 없이 계속 쌓인다 — 지운 적이 없어 EC2 디스크를 야금야금 먹는다.
#
# 순서: 03:00 로그/캐시 정리(server/crontab.txt) 뒤, 03:30 아티팩트 백업
# (backup.sh) 전에 돈다 — 먼저 지우고 나중에 백업해야 오늘 백업이 "정리 후
# 마지막 사본"을 담는다(반대 순서면 지우기 직전의 사본이 이번 백업에 없는
# 채로 다음 주까지 간다).
#
# 판정(quant.control.backup.prune_state_backups, keep_days=30)은 순수 함수라
# 파일을 지우지 않는다 — 지울 경로 목록만 돌려주고, 실제 삭제는 이 스크립트가
# 한다(그 함수의 "지우지 않는다" 계약, 함수 docstring 참고).
#
# 테스트: DRY_RUN=1 ./server/scripts/state_backup_prune.sh   (지우지 않고 목록만)
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
LOG="data/state_backup_prune.log"
mkdir -p data
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

FILES="$("$PY" -c '
from quant.control.backup import prune_state_backups
for p in prune_state_backups("."):
    print(p)
' 2>>"$LOG")"
RC=$?
if [ "$RC" -ne 0 ]; then
  log "판정 실패 exit=$RC — 지우지 않음"
  exit 1
fi

if [ -z "$FILES" ]; then
  log "정리 대상 없음"
  exit 0
fi

COUNT="$(printf '%s\n' "$FILES" | grep -c .)"

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[DRY_RUN] 정리 대상 ${COUNT}개:"
  printf '%s\n' "$FILES"
  exit 0
fi

printf '%s\n' "$FILES" | while IFS= read -r f; do
  [ -n "$f" ] && rm -f -- "$f"
done
log "정리 완료 · ${COUNT}개 삭제"
exit 0
