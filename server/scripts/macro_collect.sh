#!/usr/bin/env bash
# 매크로 금리·환율(FRED) 시계열 일일 수집 — US 마감 후 1회, 2026-08-28 소유자 지시
# ("시그널이 차트만 보는 게 아니라 rate 를 함께 보고, 데이터를 미리 수집해
# 시기별로 ML 학습"). ai_trader.sh 구조를 복제했다(TZ 가드, _env, 실패 시 조용히
# exit 0) — 다만 알림 방향은 반대다: ai_trader.sh 의 "결근"은 그 자체가 정상적인
# LLM 무판단이라 텔레그램을 울리지 않지만, 이 잡은 순수 수집이라 실패하면 원장이
# 낡은 채로 조용히 남는다 — 그래서 여긴 **실패 시에만** 텔레그램을 보낸다(성공은
# 이 저장소 관례대로 침묵).
#
# --days 10: 매일 도는 크론은 최근 10일치만 다시 받는다(주말·연휴 공백 + FRED
# 정정 발표 여유). 전체 백필(--days 0, 기본)은 최초 1회 사람이 수동으로 돌린다
# (quant.apps.cli macro-collect --root .).
#
# 테스트: DRY_RUN=1 ./server/scripts/macro_collect.sh
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
LOG="data/macro.log"

mkdir -p data
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
# 알림은 전부 notify_defer (역할별 게이트 — server/scripts/lib/notify.sh):
# 요약·정보성이라 텔레그램으로는 **절대 나가지 않는다**. data/notify_queue.jsonl
# 에 쌓여 마감 HTML 리포트로만 간다.
. "$(dirname "$0")/lib/notify.sh"
NOTIFY_LANE="ops"  # 텔레그램 포럼 토픽 레인 — docs/runbooks/telegram-rooms.md

if [ "$(date +%z)" != "+0900" ]; then
  echo "[$(date '+%F %T')] 호스트 TZ 가 KST 가 아님($(date +%z)) — 중단" >&2
  exit 1
fi

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[DRY_RUN] macro-collect --root . --days 10"
  "$PY" -m quant.apps.cli macro-collect --root . --days 10
  exit 0
fi

log "매크로 수집 시작"
OUT="$(timeout 120 "$PY" -m quant.apps.cli macro-collect --root . --days 10 2>>"$LOG")"
RC=$?
printf '%s\n' "$OUT" >> "$LOG"
log "매크로 수집 종료 rc=$RC"

if [ "$RC" -ne 0 ]; then
  notify_defer "macro_collect" "🚨 매크로 시계열(FRED) 수집 실패(exit ${RC}) — ${OUT:-<출력 없음>}
data/macro.log 확인. 국면 US_BOND_10Y 지표가 낡은 값으로 남는다."
  exit 0   # 크론 스케줄러 관점에서는 조용히 끝낸다(알림은 위 tg 로 이미 감)
fi

exit 0
