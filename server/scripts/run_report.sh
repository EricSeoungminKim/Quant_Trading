#!/usr/bin/env bash
# 리포트 1회 생성. systemd 타이머가 시장별로 호출한다.
#
#   run_report.sh KR    → 09:00 KST 개장 60분 전
#   run_report.sh US    → 09:30 ET  개장 65분 전 (서머타임 21:25 / 표준시 22:25 KST)
#
# **타이머는 이른 시각에 걸고, 정확한 발행 시각까지는 여기서 기다린다.**
# systemd OnCalendar 는 고정 시각이라 US 서머타임 전환을 못 따라간다. DST 계산은
# 이미 테스트로 고정된 quant/analyze/clock.py 에 맡기고, 셸은 그 값까지 sleep 만 한다.
# (거래 저장소가 고정 크론으로 겪은 '동계에 한 시간 밀림' 사고를 반복하지 않는다.)
set -u
cd "$(dirname "$0")/../.."

MARKET="${1:-}"
case "$MARKET" in
  KR|US) ;;
  *) echo "usage: $0 {KR|US}" >&2; exit 2 ;;
esac

# 서술 LLM 토큰 상한. **기본 700 으로는 섹션 AI 해석이 잘린다** — 한국 수급 체력·
# 시장 심리·기술적 지표·유동성/금리 네 섹션을 쓰다 두 번째에서 끊기고, all-or-nothing
# 파서가 성공한 두 섹션까지 통째로 버려 `section_advice`/`exec_summary` 가 5일 넘게
# 조용히 비어 있었다(2026-08-16~20 실측, 소유자가 "왜 요약을 안 하냐"고 물어 발견).
# deepdive·close_report 크론은 이미 4000 을 명시하고 있었는데 아침/오후 리포트만
# 빠져 있었다. 환경에서 이미 주면 그 값을 존중한다.
export OPENROUTER_MAX_TOKENS="${OPENROUTER_MAX_TOKENS:-4000}"

PY=.venv/bin/python
LOG="data/report.log"
mkdir -p data
MAX_WAIT="${REPORT_MAX_WAIT:-5400}"   # 최대 90분. 테스트에서 0으로 줄일 수 있다

log() { echo "[$(date '+%F %T')] [$MARKET] $*" >> "$LOG"; }

# 발행 알림 — 사용자 요구는 "일정 시간이 되면 리포트도 쏴주고"다. 08:40 브리핑
# (거래 저장소의 own_brief.sh)이 후보까지 정리해 보내지만, 그건 발행 40분 뒤다.
# 발행 자체는 발행 시점에 알린다.
#
# 키가 없으면 **조용히 건너뛴다** — 알림은 부가 기능이라 리포트 생성을 죽이지
# 않는다. 켜는 법은 .env.local.example 참고(거래 봇과 같은 봇을 써도 되고 별도
# 봇을 파도 된다 — 이 박스는 거래 시크릿을 갖고 있지 않다).
_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }

notify() {
  local token chat url text SUMMARY
  token="$(_env TELEGRAM_BOT_TOKEN)"
  chat="$(_env TELEGRAM_CHAT_ID)"
  if [ -z "$token" ] || [ -z "$chat" ]; then
    log "발행 알림 건너뜀 (.env.local에 TELEGRAM_BOT_TOKEN/CHAT_ID 없음)"
    return 0
  fi
  url="${REPORT_URL_BASE:-https://ip-172-31-63-20.tailfee6e9.ts.net}/$(date +%Y/%m/%d)/${MARKET}_report.html"
  if [ "$1" = "ok" ]; then
    text="📄 ${MARKET} 개장 전 리포트 발행
${url}"
    # 결정론 요약(G Task 5) — 실패해도 발행 알림 자체는 막지 않는다(부가 기능).
    SUMMARY="$($PY -m quant.apps.report_cli summary --market "$MARKET" 2>/dev/null || true)"
    if [ -n "$SUMMARY" ]; then
      text="${text}
${SUMMARY}"
    fi
  else
    text="⚠️ ${MARKET} 리포트 생성 실패 — data/report.log 확인"
  fi
  curl -s -m 10 "https://api.telegram.org/bot${token}/sendMessage" \
    -d "chat_id=${chat}" --data-urlencode "text=${text}" >/dev/null 2>&1 || true
}

# TZ 가드 — 발행 시각이 전부 KST 전제다. 호스트가 다른 존이면 조용히 엉뚱한
# 시각에 돌므로 진행하지 않는다.
if [ "$(date +%z)" != "+0900" ]; then
  log "호스트 TZ 가 KST 가 아님($(date +%z)) — 중단"
  exit 1
fi

# 발행 시각까지 대기. `when` 은 "KR 2026-08-12 발행: 2026-08-12 08:00 KST" 형식.
TARGET_HHMM="$("$PY" -m quant.apps.report_cli when --market "$MARKET" 2>/dev/null \
  | grep -oE '[0-9]{2}:[0-9]{2}' | tail -1)"
if [ -n "$TARGET_HHMM" ]; then
  NOW_EPOCH=$(date +%s)
  TARGET_EPOCH=$(date -d "today $TARGET_HHMM" +%s 2>/dev/null || echo "")
  if [ -n "$TARGET_EPOCH" ]; then
    # 빌드 리드(2026-08-17 사용자 지적): 발행 시각에 빌드를 **시작**하면 도착이
    # 5분+ 늦는다(실측 312초). 발행 시각 - 리드에 시작해 발행 시각에 **도착**하게
    # 한다. 수집이 그만큼 이른 데이터가 되는 트레이드오프는 문서화된 선택이다.
    # 480→1500(25분, 2026-08-18): 아침판이 Claude CLI 품질 레인(4곳, 실패 시
    # OpenRouter 폴백)으로 바뀌면서 콜당 소요가 늘었다 — EC2 실측치가 아직
    # 없어 보수적으로 잡았다. 수집이 그만큼 더 이른 데이터가 되는 트레이드오프는
    # 위와 같은 문서화된 선택이다(타이머도 07:30/19:30으로 20분 당겨졌다).
    BUILD_LEAD="${REPORT_BUILD_LEAD:-1500}"
    TARGET_EPOCH=$((TARGET_EPOCH - BUILD_LEAD))
    WAIT=$((TARGET_EPOCH - NOW_EPOCH))
    if [ "$WAIT" -gt 0 ] && [ "$WAIT" -le "$MAX_WAIT" ]; then
      log "발행 시각 $TARGET_HHMM 까지 ${WAIT}초 대기"
      sleep "$WAIT"
    elif [ "$WAIT" -gt "$MAX_WAIT" ]; then
      log "발행 시각까지 ${WAIT}초 — 상한(${MAX_WAIT}초) 초과라 즉시 실행"
    fi
  fi
fi

log "빌드 시작"
START=$(date +%s)
if "$PY" -m quant.apps.report_cli build --market "$MARKET" >> "$LOG" 2>&1; then
  log "빌드 완료 ($(($(date +%s) - START))초)"
  notify ok
else
  RC=$?
  log "빌드 실패 (exit $RC)"
  notify fail
  exit 1
fi
