#!/usr/bin/env bash
# 수동 계좌 추천 (2026-09-03 소유자 결정: 자동매매는 단타·스캘핑만).
#
# 오버나이트/장기 보유가 전략 정의인 네 전략(frgn_accumulate/close_bet/
# overnight_drift/rsi2_dip)은 config/settings.yaml에서 비활성화됐다(코드·params·
# capital_fraction은 보존 — 자본 재분배 없음). 그 판단 로직은 quant/analyze/
# manual_recs.py로 옮겨져 여기서 텔레그램 추천으로만 나간다 — 이 스크립트는
# 주문을 내지 않는다, 소유자가 별도 계좌에서 직접 판단한다.
#
# 사용법: server/scripts/manual_recs.sh {KR|US}
# 크론: KR 15:40 KST 월~금 (정규장 마감 15:30 직후) / US 06:30 KST 화~토
#       (정규장 마감 직후 — 서머타임은 quant.apps.cli manual-recs 내부에서
#       America/New_York 기준으로 자동 반영, 이 스크립트는 크론 실행 시각만
#       맞추면 된다 — session_pnl.sh와 같은 관례).
#
# 텍스트 생성(파이썬, 순수 — quant.apps.cli manual-recs)과 발송(여기, I/O)을
# 분리한다(session_pnl.sh와 같은 관례). cmd_manual_recs의 stdout은 텔레그램
# 메시지 그 자체뿐이다(진단 로그는 stderr) — 여기서 그대로 통째로 보낸다.
#
# 테스트: DRY_RUN=1 ./server/scripts/manual_recs.sh KR
set -u
cd "$(dirname "$0")/../.."

MARKET="${1:-}"
if [ "$MARKET" != "KR" ] && [ "$MARKET" != "US" ]; then
  echo "사용법: $0 {KR|US}" >&2
  exit 2
fi

LOG="data/manual_recs.log"
mkdir -p data

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"

# notify_auto (server/scripts/lib/notify.sh) — 추천(픽)은 알아야 하지만 급하지
# 않다. 이 크론은 항상 그 시장의 정규장 마감 직후에 돌게 설계돼 있어(위 크론
# 시각 참고) 장외 판정에 걸려 즉시 발송된다. DRY_RUN에서는 찍기만 한다.
. "$(dirname "$0")/lib/notify.sh"

# TZ 가드 — 크론 시각(KR 15:40/US 마감 직후)은 호스트가 KST라는 전제다.
if [ "$(date +%z)" != "+0900" ]; then
  notify_auto "manual_recs" "⚠️ manual_recs(${MARKET}): 호스트 TZ가 KST가 아님($(date +%z)) — 크론이 마감 직후가 아닐 수 있음"
fi

# F4(2026-09-03) — DRY_RUN=1 이면 CLI에도 --dry-run을 넘겨 선정 원장 기록을
# 건너뛴다(이전엔 notify.sh만 DRY_RUN을 봐서 텔레그램은 안 나가도 선정 원장에는
# 그대로 쌓였다). notify.sh의 DRY_RUN 동작(발송 대신 찍기)은 그대로 유지.
DRY_RUN_FLAG=""
if [ "${DRY_RUN:-0}" = "1" ]; then
  DRY_RUN_FLAG="--dry-run"
fi

OUT="$(timeout 120 .venv/bin/python -m quant.apps.cli manual-recs --market "$MARKET" $DRY_RUN_FLAG 2>>"$LOG")"
if [ -n "$OUT" ]; then
  notify_auto "manual_recs" "${OUT:0:3900}"
else
  notify_auto "manual_recs" "manual-recs(${MARKET}) 생성 실패 — ${LOG} 확인"
fi
