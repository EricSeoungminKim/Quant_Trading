#!/usr/bin/env bash
# 데일리 매매 리뷰 — 오늘 진입한 전략들의 진입/청산 타점 + 손익절 범위 +
# 진입 사유를 (전략, 종목) 카드로 낸다 (2026-09-07 오너 요청).
# 사용법: server/scripts/trade_review.sh {KR|US}
# 크론:  KR 16:05 월-금 (세션 손익 15:35·daily_wrap 16:55 사이 — daily_wrap이
#         이 산출물의 JSON을 재사용하므로 반드시 그 앞이어야 한다)
#        US 06:17 화-토 (session_pnl 06:10 뒤, equity-snapshot/macro_collect
#         06:15와 겹치지 않게 2분 띄움, daily_wrap 06:55 앞)
#
# 체결이 없는 날(휴장일 등)은 report_cli trade-review 자체가 조용히 스킵하고
# stdout이 빈다 — 그러면 이 스크립트도 조용히 끝난다(다른 리포트 크론과 같은
# 관례, report_review_daily.sh 참고).
set -u
cd "$(dirname "$0")/../.."

MARKET="${1:-}"
if [ "$MARKET" != "KR" ] && [ "$MARKET" != "US" ]; then
  echo "사용법: $0 {KR|US}" >&2
  exit 2
fi

LOG="data/trade_review.log"
mkdir -p data

log() { echo "[$(date '+%F %T')] [$MARKET] $*" >> "$LOG"; }

. "$(dirname "$0")/lib/notify.sh"
NOTIFY_LANE="briefs"  # 텔레그램 포럼 토픽 레인 — docs/runbooks/telegram-rooms.md

# 발행 대상 날짜 — 크론이 도는 시각의 그 시장 로컬 오늘. US는 크론 시각이
# 이미 다음날 KST 새벽이므로 오늘(KST) 그대로 쓴다(session_pnl.sh 등 다른
# 마감 크론과 같은 전제 — CLI 내부에서 시장별 세션 창으로 다시 거르므로
# 날짜 하나만 맞으면 된다).
DATE="$(date +%F)"

URL_BASE="${REPORT_URL_BASE:-https://ip-172-31-63-20.tailfee6e9.ts.net}"

OUT="$(timeout 300 .venv/bin/python -m quant.apps.report_cli trade-review \
  --market "$MARKET" --date "$DATE" --url-base "$URL_BASE" 2>>"$LOG")"
RC=$?
if [ "$RC" -ne 0 ]; then
  log "실패(rc=$RC)"
  exit 0   # 요약 실패는 경보가 아니다 — 다른 리포트 크론과 같은 계약
fi

if [ -z "$OUT" ]; then
  log "체결 없음 — 발송 없음"
  exit 0
fi

log "발행 — 큐 적재(장중이면 미룸)"
notify_auto "trade_review" "$OUT"
