#!/usr/bin/env bash
# 세션(정규장) 마감 후 실화폐 손익 리포트를 텔레그램으로.
# 사용법: server/scripts/session_pnl.sh {KR|US}
# 크론: KR은 15:30 KST 마감 직후, US는 마감 직후(서머타임 EDT/EST 전환은
# quant.apps.cli session-pnl 내부에서 America/New_York 기준으로 자동 반영 —
# 이 스크립트는 크론 실행 시각만 맞추면 된다).
set -u
cd "$(dirname "$0")/../.."

MARKET="${1:-}"
if [ "$MARKET" != "KR" ] && [ "$MARKET" != "US" ]; then
  echo "사용법: $0 {KR|US}" >&2
  exit 2
fi

LOG="data/session_pnl.log"
mkdir -p data
# market_pulse.sh와 같은 관례(2026-09-04) — 발송 결과를 로그에 남긴다. 이
# 스크립트는 notify_defer만 쓰므로(항상 큐에만 쌓인다, "전송 성공"은 나올 수
# 없다) 성공/실패 두 상태만 있다.
_log_notify() { echo "[$(date "+%F %T")] $1" >> "$LOG"; }

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
# 알림은 전부 notify_defer (역할별 게이트 — server/scripts/lib/notify.sh):
# 요약·정보성이라 텔레그램으로는 **절대 나가지 않는다**. data/notify_queue.jsonl
# 에 쌓여 마감 HTML 리포트로만 간다.
. "$(dirname "$0")/lib/notify.sh"

# TZ 가드 — 크론 시각(KR 15:30/US 마감 직후)은 호스트가 KST라는 전제다. 세션
# 경계 자체는 session-pnl 내부에서 시장별 현지시간대로 계산되므로 서머타임에
# 영향받지 않지만, 호스트 TZ가 어긋나면 "마감 직후"라는 크론의 전제가 깨진다.
if [ "$(date +%z)" != "+0900" ]; then
  notify_defer "session_pnl" "⚠️ session_pnl(${MARKET}): 호스트 TZ가 KST가 아님($(date +%z)) — 크론이 마감 직후가 아닐 수 있음"
fi

# timeout 120(2026-09-04, L2 서술 도입 전엔 60이었다) — narrate()가 OpenRouter
# 실패 시 최대 1회(2s 대기 후) 재시도해 최악의 경우 서술만으로 ~40s를 쓸 수
# 있다(quant.adapters.narrate.OpenRouterNarrator.narrate 참고). 다른 서술
# 크론(manual_recs/market_pulse/daily_feedback)은 이미 120이라 맞춘다.
OUT="$(timeout 120 .venv/bin/python -m quant.apps.cli session-pnl --market "$MARKET" 2>>"$LOG")"
if [ -n "$OUT" ]; then
  if notify_defer "session_pnl" "${OUT:0:3900}"; then
    _log_notify "${MARKET} session-pnl 큐 적재"
  else
    _log_notify "${MARKET} session-pnl 큐 적재 실패"
  fi
else
  if notify_defer "session_pnl" "session-pnl(${MARKET}) 생성 실패 — ${LOG} 확인"; then
    _log_notify "${MARKET} session-pnl 생성 실패 알림 큐 적재"
  else
    _log_notify "${MARKET} session-pnl 생성 실패 알림 큐 적재 실패"
  fi
fi

# 전략별 성과(2026-08-19 사용자 요청: "각 전략마다 1000만원으로 시작, 텔레그램도
# 전략마다 볼 수 있게"). market 인자와 무관하게 항상 KR+US 통합 스냅샷을 낸다 —
# 새 크론을 만들지 않고 이 기존 발송 경로(세션 마감마다)에 얹는다.
#
# Phase C(2026-08-19): strategy-pnl은 이제 메시지 하나가 아니라 여러 개(요약 +
# 활동 있는 전략마다 1건씩)를 MESSAGE_SEPARATOR(\x1e, quant/control/
# strategy_books.py 참고)로 이어 stdout에 찍는다 — 여기서 그 문자로 다시 잘라
# 메시지마다 따로 보낸다. 텍스트 생성(파이썬, 순수)과 발송(여기, I/O)을 분리한다.
STRAT_OUT="$(timeout 60 .venv/bin/python -m quant.apps.cli strategy-pnl 2>>"$LOG")"
if [ -n "$STRAT_OUT" ]; then
  while IFS= read -r -d $'\x1e' msg; do
    if [ -n "$msg" ]; then
      if notify_defer "session_pnl" "${msg:0:3900}"; then
        _log_notify "${MARKET} strategy-pnl 큐 적재"
      else
        _log_notify "${MARKET} strategy-pnl 큐 적재 실패"
      fi
    fi
  done <<< "${STRAT_OUT}"$'\x1e'
else
  if notify_defer "session_pnl" "strategy-pnl 생성 실패 — ${LOG} 확인"; then
    _log_notify "${MARKET} strategy-pnl 생성 실패 알림 큐 적재"
  else
    _log_notify "${MARKET} strategy-pnl 생성 실패 알림 큐 적재 실패"
  fi
fi
