#!/usr/bin/env bash
# 평일 개장 전 — **자체 리포트**(market_report) 기반 브리핑 + 관심종목 자동 편입.
#
#   own_brief.sh KR       # 08:12 KST — KR 동시호가 08:30, 유니버스 롤 08:27
#   own_brief.sh US       # 21:50 KST — US 개장 22:30(EDT)/23:30(EST), 유니버스 롤 22:10
#   own_brief.sh KR close # 13:55 KST — KR_close_engine.json, 유니버스 롤 14:00 (서브프로젝트 T Task 2)
#
# 2026-08-17: KR 체인을 08:30 동시호가 전에 완결시키도록 전진(watch-reset 08:05 →
# own_brief 08:12 → 유니버스 롤 08:27). 사용자 세션 표: 08:30~09:00은 동시호가라
# 그 전에 편입이 끝나야 그날 새 후보가 개장가부터 반영된다.
#
# **close 세션(KR 전용)**: 13:50 발행 오후 마감 포지션 리포트(close_engine.json)의
# 당일 단타 후보(intraday_scorer v4 top-N, EVENT_SCALP 태그)를 14:00~15:30 마감
# 구간에 태울 수 있게, 13:58 데드라인 → 14:00 유니버스 롤(quant.trade.loop
# ._universe_roll_bucket)로 흡수한다. brief_from_report.py --session close가
# 파일명·브리핑 포맷을 바꾼다 — TOKENS/FRGN_EXIT 계약은 동일.
#
# 무엇이 바뀌었나 (daily_brief.sh 대비):
# - 입력이 **회사 리포트 → 우리 리포트**다. 회사 자산 의존을 끊는다(2026-08-12 지시).
# - **LLM이 경로에서 빠졌다.** 회사 리포트는 산문이라 Claude 세션이 후보를 뽑아야
#   했지만, 자체 리포트는 `AUTO_WATCH:` 줄을 이미 결정론적으로 계산해 낸다. 그래서
#   프롬프트 주입면·600초 타임아웃·세션 실패 모드가 통째로 사라진다. ADR-0002를
#   어기는 게 아니라 LLM을 아예 안 쓰는 쪽이다.
# - 입력이 같은 박스의 파일이다 — 평문 HTTP로 받은 제3자 문서가 아니다.
#
# 그래도 3중 가드는 유지한다(리포트 생성기의 버그가 관심종목에 흘러드는 경로):
#   market_brief 형식/시장/상한 검증 → watch-score 확신도 게이트 → watch-add(flock).
#
# 리포트가 없거나 낡았을 때: **자동 편입을 멈추지 않는다.** 랭킹 발굴(--discover-*)
# 만으로 계속 돌리고, 리포트가 빠졌다는 사실을 알린다 — 리포트 파이프라인 장애가
# 조용히 "오늘은 후보 0건"으로 위장되면 며칠씩 모른다.
#
# 테스트: DRY_RUN=1 BRIEF_DATE=2026-08-13 ./server/scripts/own_brief.sh KR
set -u
cd "$(dirname "$0")/../.."

MARKET="${1:-}"
case "$MARKET" in
  KR|US) ;;
  *) echo "usage: $0 {KR|US} [close]" >&2; exit 2 ;;
esac

SESSION="${2:-live}"
case "$SESSION" in
  live) ;;
  close) ;;
  *) echo "usage: $0 {KR|US} [close]" >&2; exit 2 ;;
esac
# close 세션은 KR 전용 — US close_engine.json 자체가 없다(intraday_scorer v4가
# KR 전용, report_cli._build_intraday_view 시장 가드와 동일).
if [ "$SESSION" = "close" ] && [ "$MARKET" != "KR" ]; then
  echo "close 세션은 KR 전용이다" >&2; exit 2
fi

PY=.venv/bin/python
LOG="data/own_brief.log"
mkdir -p data
# 2026-08-17 수정: 기본값이 옛 ~/market_report 체크아웃이었다. 2026-08-13 저장소
# 흡수로 리포트는 이 저장소의 out/ 에 쌓이는데 옛 경로가 남아, 08-14부터 매일
# rc=3(리포트 없음) → 랭킹 폴백만 돌았다 — 자체 리포트의 NEWS 후보가 나흘간
# 유니버스에 못 들어갔다(실측: 08-17 US 편입 전원 TREND 태그). 이 스크립트는
# 위에서 저장소 루트로 cd 하므로 "." 이 곧 이 저장소다.
MARKET_REPORT_DIR="${MARKET_REPORT_DIR:-.}"
# 리포트 웹서버는 Tailscale 인터페이스에만 바인딩돼 있다(공개 노출 없음).
REPORT_URL_BASE="${REPORT_URL_BASE:-https://ip-172-31-63-20.tailfee6e9.ts.net}"

_SESSION_TAG=""
[ "$SESSION" = "close" ] && _SESSION_TAG="/close"
log() { echo "[$(date '+%F %T')] [$MARKET$_SESSION_TAG] $*" >> "$LOG"; }

# .env.local에서 텔레그램 토큰 2개만 지역 변수로 — export 금지(daily_brief.sh와
# 같은 계약). 하위 프로세스가 시크릿을 env로 물려받지 않는다.
_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"

# DRY_RUN에서는 발송하지 않고 찍기만 한다 — 리허설이 사용자 폰을 울리면
# 리허설을 안 하게 된다(그래서 검증 없이 배포하게 된다).
tg() {
  if [ "${DRY_RUN:-0}" = "1" ]; then
    printf '[DRY_RUN][TG]\n%s\n\n' "$1"
    return 0
  fi
  curl -s -m 10 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    -d "chat_id=${TG_CHAT}" --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

# TZ 가드 — 편입 데드라인·유니버스 롤 경계가 전부 KST 전제다.
if [ "$(date +%z)" != "+0900" ]; then
  tg "⚠️ own_brief(${MARKET}): 호스트 TZ가 KST가 아님($(date +%z)) — 자동 편입 건너뜀"
  log "TZ 비정상 — 중단"
  exit 1
fi

# 시장별 편입 데드라인. 이 시각을 넘기면 그 세션의 유니버스 롤에 못 태우므로
# 등록해봐야 다음 경계까지 놀고, 사용자는 편입된 줄 안다 — 차라리 건너뛰고 알린다.
# **반드시 0으로 채운 4자리로 쓴다.** `date +%H%M`이 오전에 "0841"을 내므로
# "1$(date +%H%M)"은 10841(5자리)이 되는데, 데드라인을 855로 쓰면 1855(4자리)가
# 돼 10841 >= 1855가 참이 된다 — 오전 내내 "데드라인 초과"로 오판한다.
# 2026-08-13 08:40 첫 실전 실행이 이 버그로 편입을 통째로 건너뛰었다.
if [ "$SESSION" = "close" ]; then
  DEADLINE=1452; ROLL="14:53"  # 14:50 발행 장중(종가배팅) 리포트 → 14:52 데드라인 → 14:53 롤 → 14:55 진입 창
else
  case "$MARKET" in
    KR) DEADLINE=0825; ROLL="08:27" ;;
    US) DEADLINE=2205; ROLL="22:10" ;;
  esac
fi

# --- 1. 리포트 읽기 → 브리핑 본문 + 후보 토큰 ---
DATE_ARG=""
[ -n "${BRIEF_DATE:-}" ] && DATE_ARG="--date $BRIEF_DATE"
SESSION_ARG=""
[ "$SESSION" = "close" ] && SESSION_ARG="--session close"
# shellcheck disable=SC2086
BRIEF_OUT="$("$PY" server/scripts/brief_from_report.py --market "$MARKET" \
  --report-dir "$MARKET_REPORT_DIR" --url-base "$REPORT_URL_BASE" $DATE_ARG $SESSION_ARG 2>>"$LOG")"
BRIEF_RC=$?

TOKENS="$(printf '%s\n' "$BRIEF_OUT" | grep -E '^TOKENS:' | tail -1 | sed 's/^TOKENS:[[:space:]]*//')"
FRGN_EXIT_LINE="$(printf '%s\n' "$BRIEF_OUT" | grep -E '^FRGN_EXIT:' | tail -1 | sed 's/^FRGN_EXIT:[[:space:]]*//')"
BRIEF="$(printf '%s\n' "$BRIEF_OUT" | grep -v '^TOKENS:' | grep -v '^FRGN_EXIT:')"
log "리포트 rc=$BRIEF_RC 후보: ${TOKENS:-<없음>} FRGN_EXIT: ${FRGN_EXIT_LINE:-<없음>}"

case "$BRIEF_RC" in
  0) tg "$BRIEF" ;;
  3) tg "🗞 ${MARKET} 자체 리포트 없음 — 랭킹 발굴만으로 진행합니다 (리포트 생성 실패 의심: systemctl status market-report@${MARKET})" ;;
  4) tg "🗞 ${MARKET} 자체 리포트가 오늘 것이 아님 (낡은 스냅샷) — 랭킹 발굴만으로 진행합니다" ;;
  *) tg "🗞 ${MARKET} 자체 리포트 읽기 실패 (rc=${BRIEF_RC}) — 랭킹 발굴만으로 진행합니다" ;;
esac

# --- 2. 데드라인 ---
# 1 접두: 08xx를 8진수로 해석하지 않게 한다(daily_brief.sh와 같은 관례).
if [ -n "${BRIEF_DATE:-}" ]; then
  :  # 수동 테스트 경로는 시각 무관
elif [ "1$(date +%H%M)" -ge "1${DEADLINE}" ]; then
  tg "⏰ ${MARKET} 편입 지연 — 데드라인(${DEADLINE}) 초과, 자동 편입 건너뜀 (${ROLL} 유니버스 롤에 못 태움)"
  log "데드라인 초과 — 편입 생략"
  exit 0
fi

# --- 2.5. 외국인 이탈 태그 갱신 (watch-score를 우회 — 이미 등록된 종목만) ---
# FRGN_EXIT는 신규 편입 판정이 아니라 기존 관심종목의 태그 갱신이라 확신도 게이트를
# 타지 않는다(market_brief.foreign_flow_tokens docstring). KR 6자리 숫자 형태만
# 통과시킨다 — FRGN_EXIT는 KR 전용(외국인 수급 원장 자체가 KR만 있다).
if [ -n "$FRGN_EXIT_LINE" ]; then
  FRGN_EXIT_SYMS="$(printf '%s' "$FRGN_EXIT_LINE" | tr ' ' '\n' | grep -E '^[0-9]{6}$' | tr '\n' ' ' | sed 's/ $//')"
  if [ -n "$FRGN_EXIT_SYMS" ]; then
    if [ "${DRY_RUN:-0}" = "1" ]; then
      echo "[DRY_RUN][$MARKET] FRGN_EXIT 갱신 대상(이미 등록된 종목만 실제 반영): $FRGN_EXIT_SYMS"
    else
      # shellcheck disable=SC2086
      R_EXIT="$(timeout 60 "$PY" server/scripts/tg_bridge.py watch-add --tags-only --tags FRGN_EXIT $FRGN_EXIT_SYMS \
           2>>"$LOG" || echo "이탈 태그 갱신 실패 — data/own_brief.log 확인")"
      log "FRGN_EXIT 태그 갱신: $FRGN_EXIT_SYMS"
      tg "📉 ${MARKET} 외국인 이탈 태그 갱신: ${FRGN_EXIT_SYMS}
${R_EXIT:0:600}"
    fi
  fi
fi

# --- 3. 확신도 엔진 (결정론적) ---
# 리포트 후보가 0건이어도 --discover-*는 항상 켠다 — 후보 출처를 리포트 하나에
# 종속시키지 않는다(2026-08-10 사용자 지시, daily_brief.sh와 같은 원칙).
case "$MARKET" in
  KR) DISCOVER=--discover-kr; HARD_CAP=15 ;;
  US) DISCOVER=--discover-us; HARD_CAP=5  ;;
esac

SCORE_OUT="$(timeout 180 "$PY" -m quant.apps.cli watch-score \
  --symbols "$TOKENS" "$DISCOVER" 2>>"$LOG")"
SCORE_RC=$?
PASS="$(printf '%s\n' "$SCORE_OUT" | grep -E '^PASS:' | tail -1 | sed 's/^PASS:[[:space:]]*//')"
SCORE_LINES="$(printf '%s\n' "$SCORE_OUT" | grep -v '^PASS:')"

# PASS 재검증: 엔진 stdout이 오염돼도 심볼[:태그] 형식만 통과시킨다. 시장 형태까지
# 본다 — KR 크론이 US 티커를 등록하는 사고를 셸에서도 막는다.
# 태그 문자 클래스에 `_`를 포함한다(2026-08-17, 서브프로젝트 T) — EVENT_SCALP/
# FRGN_EXIT처럼 언더스코어가 든 태그가 이 형태 검증에서 통째로 탈락하지 않게.
case "$MARKET" in
  KR) SHAPE='^[0-9]{6}(:[A-Za-z_+]{1,20})?$' ;;
  US) SHAPE='^[A-Za-z][A-Za-z.]{0,5}(:[A-Za-z_+]{1,20})?$' ;;
esac
PASS="$(printf '%s' "$PASS" | tr ' ' '\n' | grep -E "$SHAPE" | head -"$HARD_CAP" | tr '\n' ' ' | sed 's/ $//')"

log "watch-score rc=$SCORE_RC 통과: ${PASS:-없음}"

if [ "$SCORE_RC" -ne 0 ]; then
  # 인프라 실패를 "전원 탈락"으로 위장하지 않는다.
  tg "🛑 ${MARKET} 확신도 엔진 오류(exit ${SCORE_RC}) — 자동 편입 건너뜀. data/own_brief.log 확인"
  exit 1
fi

if [ -z "$PASS" ]; then
  tg "🤖 ${MARKET} 확신도 엔진 통과 0건 — 자동 편입 없음
${SCORE_LINES:0:2800}"
  exit 0
fi

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[DRY_RUN][$MARKET] 리포트 후보: ${TOKENS:-<없음>}"
  echo "[DRY_RUN][$MARKET] 등록 대상: $PASS"
  echo "$SCORE_LINES"
  exit 0
fi

# --- 4. 등록 (태그별로 묶어 호출) ---
# watch-add --tags는 호출 1건에 값 하나다(tg_bridge.py의 전역 플래그 시맨틱) —
# 태그가 다른 심볼을 한 콜에 섞을 수 없으므로 태그별로 나눠 부른다.
RESULT=""
TAG_KEYS="$(printf '%s\n' "$PASS" | tr ' ' '\n' | awk -F: '{print ($2==""?"__NOTAG__":$2)}' | sort -u)"
for key in $TAG_KEYS; do
  if [ "$key" = "__NOTAG__" ]; then
    GROUP="$(printf '%s\n' "$PASS" | tr ' ' '\n' | awk -F: '$2==""{print $1}' | tr '\n' ' ' | sed 's/ $//')"
    TAG_FLAG=""
  else
    GROUP="$(printf '%s\n' "$PASS" | tr ' ' '\n' | awk -F: -v k="$key" '$2==k{print $1}' | tr '\n' ' ' | sed 's/ $//')"
    TAG_FLAG="--tags $key"
  fi
  [ -z "$GROUP" ] && continue
  # shellcheck disable=SC2086
  R="$(timeout 60 "$PY" server/scripts/tg_bridge.py watch-add --source auto $TAG_FLAG $GROUP \
       2>>"$LOG" || echo "자동 편입 실패(${key}) — data/own_brief.log 확인")"
  RESULT="${RESULT}${R}
"
done

DISPLAY="$(printf '%s\n' "$PASS" | tr ' ' '\n' | awk -F: '{print $1}' | tr '\n' ' ')"
# 종목별 배정 전략 표시(2026-08-26, 소유자 조직도 역할 3) — TAG_ASSIGNMENT 재사용.
# 실패해도(헬퍼 임포트 에러 등) 기존 메시지($DISPLAY, 심볼만)로 그대로 나간다.
ASSIGNED="$("$PY" server/scripts/format_tag_assignment.py "$PASS" 2>>"$LOG")"
[ -z "$ASSIGNED" ] && ASSIGNED="$DISPLAY"
tg "🤖 ${MARKET} 확신도 엔진 통과 → 자동 편입: ${ASSIGNED}
${ROLL} 유니버스 갱신 후 매매 대상이 됩니다.

${SCORE_LINES:0:2500}
${RESULT:0:600}
(원치 않으면 /unwatch <심볼>)"
log "편입 완료: $DISPLAY"
