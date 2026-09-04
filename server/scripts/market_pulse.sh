#!/usr/bin/env bash
# 시장 펄스 다이제스트 (2026-09-03 소유자 요청: "프로그램이 매매 안 할 때도
# 참고할 시그널을 주기적으로 텔레그램으로" — 기존 'KODEX 과매도' 알림 스타일을
# 일반화해 지수·금리·달러·유가 과매수/과매도를 한 메시지로). 자동매매와 무관 —
# 참고용, 선정 원장에도 쓰지 않는다.
#
# 사용법: server/scripts/market_pulse.sh {US|KR}
# 크론: US 23:00/01:00/03:00/05:00 KST 화~토, KR 09:40/12:00/14:30 KST 월~금
#       (server/crontab.txt 참고).
#
# --changes-only(기본 꺼짐)는 quant.apps.cli market-pulse 자체가 지원한다 —
# 라벨이 지난 실행과 전부 같으면 stdout 이 빈다(ai_trader.sh 등과 같은
# "무출력=무발송" 관례). 이 스크립트는 기본적으로 --changes-only 없이 부른다
# (소유자가 주기적 다이제스트를 원했으므로 기본은 매번 발송) — 켜려면
# MARKET_PULSE_CHANGES_ONLY=1.
#
# 텍스트 생성(파이썬, quant.apps.cli market-pulse)과 발송(여기, notify.sh)을
# 분리한다(manual_recs.sh와 같은 관례).
#
# 테스트: DRY_RUN=1 ./server/scripts/market_pulse.sh US
set -u
cd "$(dirname "$0")/../.."

MARKET="${1:-}"
if [ "$MARKET" != "KR" ] && [ "$MARKET" != "US" ]; then
  echo "사용법: $0 {KR|US}" >&2
  exit 2
fi

LOG="data/market_pulse.log"
mkdir -p data

# notify_now (server/scripts/lib/notify.sh) — 주기 다이제스트는 정해진 시각에 바로
# 도착해야 의미가 있다. notify_auto 는 장중이면 큐에 넣어 마감 wrap 때 보내므로 쓰지 않는다(2026-09-03 실측).
. "$(dirname "$0")/lib/notify.sh"

# TZ 가드 — 크론 시각(위 주석)은 호스트가 KST라는 전제다.
if [ "$(date +%z)" != "+0900" ]; then
  notify_now "⚠️ market_pulse(${MARKET}): 호스트 TZ가 KST가 아님($(date +%z))"
fi

DRY_RUN_FLAG=""
if [ "${DRY_RUN:-0}" = "1" ]; then
  DRY_RUN_FLAG="--dry-run"
fi

CHANGES_FLAG=""
if [ "${MARKET_PULSE_CHANGES_ONLY:-0}" = "1" ]; then
  CHANGES_FLAG="--changes-only"
fi

OUT="$(timeout 120 .venv/bin/python -m quant.apps.cli market-pulse --market "$MARKET" $DRY_RUN_FLAG $CHANGES_FLAG 2>>"$LOG")"
if [ -n "$OUT" ]; then
  if notify_now "${OUT}"; then  # 2026-09-03: 주기 다이제스트는 큐에 넣지 않고 즉시 보낸다(장중 큐잉이면 마감 wrap 때야 도착)
  echo "[$(date "+%F %T")] ${MARKET} 전송 성공" >> "$LOG"
else
  echo "[$(date "+%F %T")] ${MARKET} 전송 실패(텔레그램 ok:true 아님 또는 토큰 없음)" >> "$LOG"
fi
elif [ -z "$CHANGES_FLAG" ]; then
  # --changes-only 가 꺼져 있는데도 무출력이면 실패다(켜져 있으면 "변경 없음"이
  # 정상적인 무출력이라 여기서 경보하지 않는다).
  notify_now "market-pulse(${MARKET}) 생성 실패 — ${LOG} 확인"
fi
