#!/usr/bin/env bash
# 일일 피드백 — 오늘 진입 타이밍 규칙 판정(고점매수/거래 소강 진입/늦은 진입)을
# 전략별로 텔레그램에 (2026-08-26 소유자 조직도 역할 5).
# 사용법: server/scripts/daily_feedback.sh {KR|US}
#
# experiments_daily.sh 와 같은 패턴: `cli daily-feedback` 는 오늘 진입 체결이
# 없으면 아무것도 출력하지 않고, 이 스크립트는 stdout 이 비면 텔레그램을
# 보내지 않는다(조용한 것이 기본값 — 매일 "거래 없음"을 보내면 사람이 안 읽게
# 된다). 원장 append(`data/ledger/daily_feedback.jsonl`)는 CLI 쪽에서 (날짜,
# 시장) 키로 멱등이라 재실행해도 중복 기록은 안 남는다 — 재실행되면 텔레그램만
# 다시 나간다(중복 방지 해시는 두지 않는다: 이 리포트는 매일 신선한 판정이라
# 하루 안에 재실행될 사유 자체가 드물다).
#
# KR 16:45 — 마감 백필(16:35 backfill_kr_stock_daily.sh) 이후, 그날 1분봉이
# 확실히 잡힌 뒤. US 06:25 — 자본 곡선(06:15 equity-snapshot) 이후, US 세션
# 마감(05:00 EDT/06:00 EST)이 두 서머타임 체계 모두에서 지난 시각이다.
set -u
cd "$(dirname "$0")/../.."

MARKET="${1:-}"
if [ "$MARKET" != "KR" ] && [ "$MARKET" != "US" ]; then
  echo "사용법: $0 {KR|US}" >&2
  exit 2
fi

LOG="data/daily_feedback.log"
mkdir -p data

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
# 알림은 notify_defer (역할별 게이트 — server/scripts/lib/notify.sh): 요약·정보성
# 이라 텔레그램으로는 **절대 나가지 않는다**. data/notify_queue.jsonl 에 쌓여
# 마감 HTML 리포트로만 간다.
. "$(dirname "$0")/lib/notify.sh"

OUT="$(timeout 120 .venv/bin/python -m quant.apps.cli daily-feedback --market "$MARKET" 2>>"$LOG")"
RC=$?

if [ "$RC" -ne 0 ]; then
  notify_defer "daily_feedback" "⚠️ 일일 피드백(${MARKET}) 실패 (exit ${RC}) — ${LOG} 확인"
  echo "[$(date '+%F %T')] 실패 exit=$RC"
  exit "$RC"
fi

if [ -z "$OUT" ]; then
  echo "[$(date '+%F %T')] ${MARKET} 오늘 진입 없음 — 조용히 대기"
  exit 0
fi

echo "$OUT"
notify_defer "daily_feedback" "${OUT:0:3900}"
