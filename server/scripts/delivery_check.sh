#!/usr/bin/env bash
# 소식통 배달 점검 — 하루 한 바퀴(US 마감 정산 뒤) 리포트/브리핑이 실제로
# 대표님에게 닿았는가만 본다. 2026-08-26, 소유자 조직도 역할 6.
# 크론: 35 6 * * 2-6 (화~토 06:35 KST — 05:50 US_wrap 발행 뒤, 전날 KR+US
# 사이클이 다 끝난 시점. 날짜 산수 근거는 quant.control.delivery_check
# 상단 docstring 참고).
#
# 판정은 quant.apps.cli delivery-check(순수 함수 quant.control.delivery_check
# 위에 I/O만 얹은 것)가 한다. **조용한 것이 기본값**(experiments_daily.sh/
# ai_trader.sh와 같은 관례) — 전부 정상이면 stdout 이 비고 텔레그램도 안
# 나간다. 하나라도 미배달/확인 못 함이 있으면 그 목록을 그대로 보낸다.
#
# 테스트: DRY_RUN=1 ./server/scripts/delivery_check.sh
#         DRY_RUN=1 .venv/bin/python -m quant.apps.cli delivery-check --date 2026-08-25
set -u
cd "$(dirname "$0")/../.."

LOG="data/delivery_check.log"
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

# TZ 가드 — 날짜 산수(오늘/전날)가 전부 KST 전제다.
if [ "$(date +%z)" != "+0900" ]; then
  notify_defer "delivery_check" "⚠️ delivery_check: 호스트 TZ가 KST가 아님($(date +%z)) — 점검 건너뜀"
  log "TZ 비정상 — 중단"
  exit 1
fi

OUT="$(timeout 60 .venv/bin/python -m quant.apps.cli delivery-check 2>>"$LOG")"
RC=$?

if [ -n "$OUT" ]; then
  notify_defer "delivery_check" "$OUT"
  log "미배달/확인못함 발견(exit=$RC): $(printf '%s' "$OUT" | tr '\n' ' ')"
elif [ "$RC" -ge 3 ]; then
  # 0=정상(무출력이 정상) / 1=미배달 / 2=확인못함(둘 다 $OUT이 채워진다) —
  # 그 밖의 코드는 도구 자체 오류(임포트 실패 등)이지 "정상 침묵"이 아니다.
  notify_defer "delivery_check" "🛑 delivery-check 도구 오류(exit ${RC}) — ${LOG} 확인"
  log "도구 오류 exit=$RC"
else
  log "정상 — 무출력"
fi
