#!/usr/bin/env bash
# 마감 포지션 리포트 1회 생성(서브프로젝트 R). systemd 타이머가 13:40 KST 고정으로 호출한다.
#
#   run_close_report.sh KR   → 13:40 KST 빌드 → 13:50 전후 발행 (14:00 진입 준비 시간 확보)
#
# **run_report.sh(아침판)와 달리 발행 시각까지 대기하지 않는다** — 아침판은
# 개장 60분 전이라는 이동 목표(DST 등)를 systemd OnCalendar 로 못 맞춰 셸이
# 정확한 시각까지 sleep 하지만, 마감판은 고정 KST 13:40 이라 타이머 자체가
# 발행 시각이다(대기 로직 자체가 필요 없다).
#
# KR 전용이다 — US 오후판은 없다(정규장 구조가 다르다, 설계 스펙 §비목표).
set -u
cd "$(dirname "$0")/../.."

MARKET="${1:-}"
case "$MARKET" in
  KR) ;;
  *) echo "usage: $0 KR (마감 리포트는 KR 전용)" >&2; exit 2 ;;
esac

PY=.venv/bin/python
LOG="data/report.log"
mkdir -p data

log() { echo "[$(date '+%F %T')] [$MARKET 마감] $*" >> "$LOG"; }

# 발행 알림 — run_report.sh notify() 와 같은 패턴(토큰/챗ID 없으면 조용히 건너뛴다).
_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }

notify() {
  local token chat url text SUMMARY
  token="$(_env TELEGRAM_BOT_TOKEN)"
  chat="$(_env TELEGRAM_CHAT_ID)"
  if [ -z "$token" ] || [ -z "$chat" ]; then
    log "발행 알림 건너뜀 (.env.local에 TELEGRAM_BOT_TOKEN/CHAT_ID 없음)"
    return 0
  fi
  url="${REPORT_URL_BASE:-https://ip-172-31-63-20.tailfee6e9.ts.net}/$(date +%Y/%m/%d)/${MARKET}_close_report.html"
  if [ "$1" = "ok" ]; then
    text="📄 ${MARKET} 마감 포지션 리포트 발행 (14:00~15:30 참고용)
${url}"
    # 결정론 요약(G Task 5 확장, R) — 실패해도 발행 알림 자체는 막지 않는다.
    SUMMARY="$($PY -m quant.apps.report_cli summary --market "$MARKET" --session close 2>/dev/null || true)"
    if [ -n "$SUMMARY" ]; then
      text="${text}
${SUMMARY}"
    fi
  else
    text="⚠️ ${MARKET} 마감 리포트 생성 실패 — data/report.log 확인"
  fi
  curl -s -m 10 "https://api.telegram.org/bot${token}/sendMessage" \
    -d "chat_id=${chat}" --data-urlencode "text=${text}" >/dev/null 2>&1 || true
}

# TZ 가드 — 발행 시각이 KST 전제다(run_report.sh 와 동일한 방어).
if [ "$(date +%z)" != "+0900" ]; then
  log "호스트 TZ 가 KST 가 아님($(date +%z)) — 중단"
  exit 1
fi

log "빌드 시작"
START=$(date +%s)
if "$PY" -m quant.apps.report_cli build --market "$MARKET" --session close >> "$LOG" 2>&1; then
  log "빌드 완료 ($(($(date +%s) - START))초)"
  notify ok
else
  RC=$?
  log "빌드 실패 (exit $RC)"
  notify fail
  exit 1
fi
