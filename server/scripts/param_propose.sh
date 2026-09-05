#!/usr/bin/env bash
# 토 06:40 KST — 전략 파라미터 제안 (AI 트레이더 3단계, 2026-08-26).
#
# 주간 재검토(06:25) 직후, 한 주의 원장 숫자가 다 모인 시점에 돈다. CLI 가
# **제안만** 기록·출력한다 — 반영은 사람이 settings.yaml 로, 판정은 매일 16:30
# experiments 루프가 한다. LLM: Claude CLI 1순위 → OpenRouter 무료 폴백(CLI 내부).
#
# 조용한 것이 기본값(experiments_daily.sh 관례): 제안 없음/결근이면 stdout 이
# 비고 텔레그램도 안 나간다.
set -u
cd "$(dirname "$0")/../.."

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
# 알림은 notify_defer (역할별 게이트 — server/scripts/lib/notify.sh): 요약·정보성
# 이라 텔레그램으로는 **절대 나가지 않는다**. data/notify_queue.jsonl 에 쌓여
# 마감 HTML 리포트로만 간다.
. "$(dirname "$0")/lib/notify.sh"
NOTIFY_LANE="briefs"  # 텔레그램 포럼 토픽 레인 — docs/runbooks/telegram-rooms.md

if [ "$(date +%z)" != "+0900" ]; then
  echo "[$(date '+%F %T')] 호스트 TZ 가 KST 가 아님($(date +%z)) — 중단" >&2
  exit 1
fi

# Claude CLI 3~5분 + 폴백 여유.
OUT="$(timeout 600 .venv/bin/python -m quant.apps.cli param-propose 2>/dev/null)"
RC=$?

if [ "$RC" -ne 0 ]; then
  echo "[$(date '+%F %T')] 실패 exit=$RC (결근 처리 — 제안 미기록)"
  exit 0
fi

if [ -z "$OUT" ]; then
  echo "[$(date '+%F %T')] 제안 없음/결근 — 침묵"
  exit 0
fi

echo "[$(date '+%F %T')] 제안 발생:"
echo "$OUT"

notify_defer "param_propose" "${OUT}"
