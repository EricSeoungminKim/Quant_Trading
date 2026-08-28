#!/usr/bin/env bash
# 장중 거래대금 발굴 — KR/US 정규장 중 30분 간격 (2026-08-28 소유자 지시).
#
# 아침 리포트(own_brief.sh)가 못 잡은 종목이라도 장중에 거래대금이 쏠리면
# 발굴해 워치리스트에 편입하고, 전략들이 시그널을 감시하게 한다. 기존 자동
# 편입 체인(own_brief: 후보 → watch-score 확신도 게이트 → watch-add → 유니버스
# 롤)을 그대로 재사용한다 — 게이트 없는 편입은 금지(CLAUDE.md ② "아무거나
# 선정하지 않는다"). LLM 사용 없음 — 전부 결정론.
#
# 흐름: flow-scan(랭킹 발굴) → watch-score(확신도 게이트) → watch-add(편입).
# 세션 밖 실행 방지는 크론 시각(server/crontab.txt)으로 충분하므로 스크립트
# 안에서 별도 세션 판정은 하지 않는다(단순함 우선).
set -u
cd "$(dirname "$0")/../.."

if [ "$(date +%z)" != "+0900" ]; then
  echo "[$(date '+%F %T')] 호스트 TZ가 KST가 아님($(date +%z)) — 중단" >&2
  exit 1
fi

MARKET="${1:?사용법: flow_scan.sh KR|US}"
case "$MARKET" in
  KR|US) ;;
  *) echo "사용법: flow_scan.sh KR|US" >&2; exit 2 ;;
esac

PY=.venv/bin/python
LOG="data/flow_scan.log"
mkdir -p data
log() { echo "[$(date '+%F %T')] [$MARKET] $*" >> "$LOG"; }

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
# 알림은 전부 notify_auto (역할별 게이트 — server/scripts/lib/notify.sh):
# 편입·픽은 알아야 하지만 급하지 않다. **장중이면 data/notify_queue.jsonl 로
# 미뤄져 마감 HTML 리포트로 나가고**, 장외면 지금처럼 즉시 발송된다.
. "$(dirname "$0")/lib/notify.sh"

# --- 1. 발굴 (결정론적 랭킹 스캔) ---
FLOW_OUT="$(timeout 120 "$PY" -m quant.apps.cli flow-scan --market "$MARKET" 2>>"$LOG")"
FLOW_RC=$?
if [ "$FLOW_RC" -ne 0 ]; then
  log "flow-scan 실패 exit=$FLOW_RC — 건너뜀"
  exit 0
fi

CANDS="$(printf '%s\n' "$FLOW_OUT" | grep -E '^FLOW:' | tail -1 | sed 's/^FLOW:[[:space:]]*//')"
if [ -z "$CANDS" ]; then
  log "신규 후보 없음 — 침묵"
  exit 0
fi
log "발굴 후보: $CANDS"

# --- 2. 확신도 게이트 (own_brief.sh L177 관례) ---
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
if [ -z "$PASS" ] || [ "$PASS" = "없음" ]; then
  log "전원 확신도 게이트 탈락 — 편입 없음"
  exit 0
fi
log "게이트 통과: $PASS"

# --- 3. 편입 (RANK 태그로 발굴 출처를 남긴다) ---
# **회차당 상한**(2026-08-28 첫 실전 실행에서 드러남): 게이트를 14종목이 한 번에
# 통과했다. 30분마다 그러면 하루 100종목이 넘게 붙어 유니버스가 폭증하고,
# scalp_1m 사이클(이미 3.8초/분)이 더 느려지며 전략당 자본이 희석된다. 발굴은
# "오늘 시장이 쳐다보는 상위 몇 개"를 잡는 보너스 경로지 유니버스 확장기가
# 아니다. 거래대금 순위 순서(flow-scan 출력 순서)가 보존되므로 앞에서 자른다.
FLOW_MAX_ADD="${FLOW_MAX_ADD:-3}"
KEPT="$(printf '%s\n' "$PASS" | tr ' ' '\n' | head -n "$FLOW_MAX_ADD" | tr '\n' ' ' | sed 's/ $//')"
DROPPED="$(printf '%s\n' "$PASS" | tr ' ' '\n' | tail -n +"$((FLOW_MAX_ADD + 1))" | tr '\n' ' ' | sed 's/ $//')"
[ -n "$DROPPED" ] && log "상한(${FLOW_MAX_ADD}) 초과로 보류: $DROPPED"
PASS="$KEPT"

SYMS_ONLY="$(printf '%s\n' "$PASS" | tr ' ' '\n' | awk -F: '{print $1}' | tr '\n' ' ' | sed 's/ $//')"
# shellcheck disable=SC2086
ADD_OUT="$(timeout 60 "$PY" server/scripts/tg_bridge.py watch-add --source auto --tags RANK $SYMS_ONLY \
  2>>"$LOG")"
ADD_RC=$?
if [ "$ADD_RC" -ne 0 ]; then
  log "watch-add 실패 exit=$ADD_RC — $ADD_OUT"
  exit 0
fi
log "편입 완료: $SYMS_ONLY"

# 토큰 유무 검사는 게이트가 한다 — 여기서 걸러버리면 토큰이 없는 날 큐 적재까지
# 같이 사라진다(장중 편입은 미뤄서 마감 리포트에 실려야 한다).
notify_auto "flow_scan" "🌊 장중 거래대금 편입(${MARKET}): ${SYMS_ONLY} — 확신도 게이트 통과. 전략들이 시그널 감시를 시작한다." || true   # 발송 실패가 크론 exit 코드를 바꾸지 않게
