#!/usr/bin/env bash
# 독립 리스크 리뷰 — 일 1회 06:37 KST (2026-09-02 신규, 회사형 AI 에이전트
# 레이어 레인 2). US 마감 종합 체인(session-pnl 06:10 → equity 06:15 →
# daily-feedback 06:25 → delivery-check 06:35)이 끝난 뒤 — 그날 원장이
# 확정된 상태에서 리뷰한다. 06:35 이 아니라 06:37 인 이유: delivery_check.sh
# 가 이미 06:35 를 쓰고 있어(crontab.txt) 같은 분에 몰지 않는다(1.8GB 박스,
# 08-31 몰림 분산과 같은 관례).
#
# ops_watch.sh(매시, 규칙 기반)·ops_judge.sh(하루 2회, 운영 정합성 교차검증)와
# 다르다 — 이쪽은 드로다운·집중도·상쇄쌍 노출·연속 손실만 전담하는 **별도
# 스크립트/프롬프트**다(리스크는 트레이딩·보고 라인과 분리 — quant.control.
# risk_review 모듈 docstring). 임계 초과 여부는 결정론 코드가 정하고, LLM 은
# 상위 3문제+권고 서술만 맡는다.
#
# 임계 초과(BREACH: yes)면 notify_auto(장중이면 미뤄지고 장외면 즉시), 아니면
# notify_defer(마감 HTML 로만 — 텔레그램 즉시 발송 없음).
#
# 테스트: DRY_RUN=1 ./server/scripts/risk_review.sh
#         (LLM 은 실제로 호출된다 — OPENROUTER_API_KEY 없으면 이슈 없이
#          결정론 판정만으로 카드가 나간다. 실제 텔레그램 발송만 DRY_RUN 이 막는다.)
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
LOG="data/risk_review.log"
mkdir -p data data/ledger

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
. "$(dirname "$0")/lib/notify.sh"

if [ "$(date +%z)" != "+0900" ]; then
  log "호스트 TZ 가 KST 가 아님($(date +%z)) — 중단"
  exit 1
fi

OUT="$(timeout 180 "$PY" -m quant.apps.cli risk-review 2>>"$LOG")"
RC=$?

if [ "$RC" -ne 0 ] || [ -z "$OUT" ]; then
  log "판단 명령 실패/무출력(rc=$RC) — 결근 처리"
  exit 0
fi

BREACH="$(printf '%s\n' "$OUT" | grep -E '^BREACH:' | head -1 | sed 's/^BREACH:[[:space:]]*//')"
CARD="$(printf '%s\n' "$OUT" | grep -v '^BREACH:')"

log "판정 완료 — breach=${BREACH:-알수없음}"
echo "$CARD"

if [ "$BREACH" = "yes" ]; then
  notify_auto "risk_review" "$CARD"
else
  notify_defer "risk_review" "$CARD"
fi
