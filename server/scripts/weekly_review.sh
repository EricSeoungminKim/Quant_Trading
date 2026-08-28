#!/usr/bin/env bash
# 토요일 06:25 KST — 주간 재검토 세션 (2026-08-26 소유자 지시).
#
# "금~토 넘어가는 6AM, 한 주의 양시장이 다 끝났을 때 일주일을 재검토한다."
# 06:00 이 아니라 06:25 인 이유: 토요일 06:10(US 세션 손익)·06:15(자본 곡선)
# 기록이 끝난 **뒤**에 돌아야 그 주 마지막 데이터까지 재검토에 들어간다 —
# 의도(주간 마감 후)는 그대로, 시각만 데이터 완결 뒤로 25분 민 것.
set -u
cd "$(dirname "$0")/../.."

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
# 알림은 notify_defer (역할별 게이트 — server/scripts/lib/notify.sh): 요약·정보성
# 이라 텔레그램으로는 **절대 나가지 않는다**. data/notify_queue.jsonl 에 쌓여
# 마감 HTML 리포트로만 간다.
. "$(dirname "$0")/lib/notify.sh"

OUT="$(timeout 300 .venv/bin/python -m quant.apps.cli weekly-review 2>/dev/null)"
RC=$?
if [ "$RC" -ne 0 ] || [ -z "$OUT" ]; then
  OUT="⚠️ 주간 재검토 생성 실패 (exit ${RC}) — data/weekly_review.log 확인"
fi
echo "$OUT"
notify_defer "weekly_review" "${OUT}"
