#!/usr/bin/env bash
# PnL 귀속 요약 — 일 2회, 마감 후(KR 16:58 / US 06:58 KST) (2026-09-02 신규,
# 회사형 AI 에이전트 레이어 레인 3). daily_wrap.sh(KR 16:55 / US 06:55) 직후 —
# 그 세션 원장이 확정된 뒤 4줄 요약을 낸다. **LLM 없음** — 결정론 산수만
# (quant.control.pnl_attribution 모듈 docstring: "숫자 요약에 환각 리스크를
# 질 이유가 없다").
#
# 장외 시각에 도니 notify_auto 는 사실상 즉시 발송이다(own_brief.sh 와 같은
# 게이트 — server/scripts/lib/notify.sh).
#
# 테스트: DRY_RUN=1 ./server/scripts/pnl_attribution.sh KR
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
LOG="data/pnl_attribution.log"
mkdir -p data

MARKET="${1:?사용법: pnl_attribution.sh KR|US}"
case "$MARKET" in
  KR|US) ;;
  *) echo "usage: $0 {KR|US}" >&2; exit 2 ;;
esac

log() { echo "[$(date '+%F %T')] [$MARKET] $*" >> "$LOG"; }

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
. "$(dirname "$0")/lib/notify.sh"

if [ "$(date +%z)" != "+0900" ]; then
  log "호스트 TZ 가 KST 가 아님($(date +%z)) — 중단"
  exit 1
fi

OUT="$(timeout 60 "$PY" -m quant.apps.cli pnl-attribution --market "$MARKET" 2>>"$LOG")"
RC=$?

if [ "$RC" -ne 0 ]; then
  log "명령 실패(rc=$RC)"
  exit 0
fi

if [ -z "$OUT" ]; then
  log "이 세션 체결 없음 — 무출력"
  exit 0
fi

log "요약 완료 — 알림 발송"
notify_auto "pnl_attribution" "$OUT"
echo "$OUT"
