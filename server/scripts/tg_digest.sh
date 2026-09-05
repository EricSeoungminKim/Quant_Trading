#!/usr/bin/env bash
# 텔레그램 인텔리전스 다이제스트(2026-09-05, 소유자 요구 (4)) — 정규장 중
# 30분마다 신규 텔레그램 메시지만 모아 KR/US 스탠스·관심종목 후보·방별 요약을
# 텔레그램으로 보낸다. TQQQ/SQQQ/SOXL/SOXS 는 별도 고정 레인이 처리하고 이
# 경로와는 무관하다 — 다른 전략들의 자동매매도 그대로 돌아간다(이 스크립트는
# 유니버스에 편입만 거들 뿐 매매를 결정하지 않는다).
#
# 사용법: server/scripts/tg_digest.sh {KR|US}
# 크론: telegram-collect.timer(*:00/30 + 최대 120초 지연)가 신규 메시지를
#       원장에 쌓은 **다음**에 돌아야 그 사이클 메시지를 놓치지 않는다 — 그래서
#       KR :05/:35, US :35/:05(server/crontab.txt 참고).
#
# 흐름: tg-digest(다이제스트+CANDS 후보 추출) → notify_now(다이제스트 발송) →
#       watch-score(확신도 게이트) → watch-add(편입, source=auto tags=NEWS).
#       flow_scan.sh 와 같은 게이트 체인 — 게이트 없는 편입은 없다(CLAUDE.md
#       "아무거나 선정하지 않는다").
#
# 테스트: DRY_RUN=1 ./server/scripts/tg_digest.sh KR
set -u
cd "$(dirname "$0")/../.."

MARKET="${1:-}"
if [ "$MARKET" != "KR" ] && [ "$MARKET" != "US" ]; then
  echo "사용법: $0 {KR|US}" >&2
  exit 2
fi

PY=.venv/bin/python
LOG="data/tg_digest.log"
mkdir -p data
log() { echo "[$(date '+%F %T')] [$MARKET] $*" >> "$LOG"; }

# notify_now (server/scripts/lib/notify.sh) — 30분 다이제스트는 정해진 시각에
# 바로 도착해야 의미가 있다(market_pulse.sh 와 같은 이유). notify_auto 는
# 장중이면 큐에 넣어 마감 wrap 때야 보내므로 쓰지 않는다.
. "$(dirname "$0")/lib/notify.sh"

# TZ 가드 — 크론 시각(위 주석)은 호스트가 KST라는 전제다(market_pulse.sh와 동일).
if [ "$(date +%z)" != "+0900" ]; then
  notify_now "⚠️ tg_digest(${MARKET}): 호스트 TZ가 KST가 아님($(date +%z))"
fi

DRY_RUN_FLAG=""
if [ "${DRY_RUN:-0}" = "1" ]; then
  DRY_RUN_FLAG="--dry-run"
fi

# --- 1. 다이제스트 생성(결정론 + 선택 LLM 스탠스) ---
DIGEST_OUT="$(timeout 120 "$PY" -m quant.apps.cli tg-digest --market "$MARKET" $DRY_RUN_FLAG 2>>"$LOG")"
DIGEST_RC=$?
if [ "$DIGEST_RC" -ne 0 ]; then
  log "tg-digest 실패 exit=$DIGEST_RC — 건너뜀"
  exit 0
fi

# CANDS 줄만 분리하고, 본문(발송할 다이제스트 텍스트)은 그 줄을 뺀 나머지다
# (flow_scan.sh 의 FLOW: 라인 분리 관례와 같되, 이쪽은 본문 자체도 보내야
# 하므로 grep -v 로 CANDS 줄을 제거한 나머지를 그대로 쓴다).
CANDS="$(printf '%s\n' "$DIGEST_OUT" | grep -E '^CANDS:' | tail -1 | sed 's/^CANDS:[[:space:]]*//')"
MESSAGE="$(printf '%s\n' "$DIGEST_OUT" | grep -v -E '^CANDS:')"

if [ -z "$MESSAGE" ]; then
  log "다이제스트 본문 없음 — 발송 건너뜀"
  exit 0
fi

if notify_now "$MESSAGE"; then
  log "발송 성공"
else
  log "발송 실패(텔레그램 ok:true 아님 또는 토큰 없음)"
fi

if [ -z "$CANDS" ]; then
  log "신규 후보 없음 — 편입 건너뜀"
  exit 0
fi
log "다이제스트 후보: $CANDS"

# --- 2. 확신도 게이트(flow_scan.sh L55 관례) ---
case "$MARKET" in
  KR) SHAPE='^[0-9]{6}(:[A-Za-z_+]{1,20})?$' ;;
  US) SHAPE='^[A-Za-z][A-Za-z.]{0,5}(:[A-Za-z_+]{1,20})?$' ;;
esac

SCORE_OUT="$(timeout 180 "$PY" -m quant.apps.cli watch-score --symbols "$CANDS" 2>>"$LOG")"
SCORE_RC=$?
if [ "$SCORE_RC" -ne 0 ]; then
  log "watch-score 실패 exit=$SCORE_RC — 편입 건너뜀"
  exit 0
fi

PASS="$(printf '%s\n' "$SCORE_OUT" | grep -E '^PASS:' | tail -1 | sed 's/^PASS:[[:space:]]*//')"
PASS="$(printf '%s' "$PASS" | tr ' ' '\n' | grep -E "$SHAPE" | tr '\n' ' ' | sed 's/ $//')"
REJECTED="$(printf '%s\n' "$CANDS" | tr ' ' '\n' | grep -vxF -f <(printf '%s\n' "$PASS" | tr ' ' '\n' | awk -F: '{print $1}') | tr '\n' ' ' | sed 's/ $//')"
if [ -n "$REJECTED" ]; then
  log "게이트 탈락: $REJECTED"
fi
if [ -z "$PASS" ] || [ "$PASS" = "없음" ]; then
  log "전원 확신도 게이트 탈락 — 편입 없음"
  exit 0
fi
log "게이트 통과: $PASS"

# --- 3. 편입 상한(flow_scan.sh L77 관례) — 30분마다 다이제스트가 여러
# 종목을 동시에 잡으면 유니버스가 폭증한다. 상위 3개만(발굴 순서 보존).
TG_DIGEST_MAX_ADD="${TG_DIGEST_MAX_ADD:-3}"
KEPT="$(printf '%s\n' "$PASS" | tr ' ' '\n' | head -n "$TG_DIGEST_MAX_ADD" | tr '\n' ' ' | sed 's/ $//')"
DROPPED="$(printf '%s\n' "$PASS" | tr ' ' '\n' | tail -n +"$((TG_DIGEST_MAX_ADD + 1))" | tr '\n' ' ' | sed 's/ $//')"
[ -n "$DROPPED" ] && log "상한(${TG_DIGEST_MAX_ADD}) 초과로 보류: $DROPPED"
PASS="$KEPT"

SYMS_ONLY="$(printf '%s\n' "$PASS" | tr ' ' '\n' | awk -F: '{print $1}' | tr '\n' ' ' | sed 's/ $//')"
if [ "${DRY_RUN:-0}" = "1" ]; then
  log "DRY_RUN — 편입 생략: $SYMS_ONLY"
  exit 0
fi

# shellcheck disable=SC2086
ADD_OUT="$(timeout 60 "$PY" server/scripts/tg_bridge.py watch-add --source auto --tags NEWS $SYMS_ONLY \
  2>>"$LOG")"
ADD_RC=$?
if [ "$ADD_RC" -ne 0 ]; then
  log "watch-add 실패 exit=$ADD_RC — $ADD_OUT"
  exit 0
fi
log "편입 완료: $SYMS_ONLY"
