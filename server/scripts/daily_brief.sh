#!/usr/bin/env bash
# 매일 08:40 KST — 회사 데일리 리포트를 Claude Code 세션으로 해석해 텔레그램 브리핑.
#
# 설계 원칙:
# - 거래 엔진(quant-engine.service)과 완전 분리된 별도 프로세스 (ADR-0002: LLM은
#   리포팅 레이어에만). 이 스크립트가 죽어도 거래는 아무 영향 없다.
# - Claude 세션은 **--disallowedTools로 도구를 명시 차단**하고, 이 스크립트는
#   .env.local을 전역 export하지 않는다(텔레그램 토큰 2개만 지역 변수로 읽음) —
#   리포트 본문(평문 HTTP, 신뢰 불가)이 프롬프트에 들어가므로, 주입이 성공해도
#   세션이 읽을 시크릿도 도구도 없게 한다. 2026-08-10 적대적 리뷰 C4 반영.
# - 자동 반영 경로는 관심종목 하나뿐이며 3중 가드를 거친다: 셸 하드캡+정규식
#   화이트리스트 → 결정론적 확신도 엔진(watch-score) → flock 경유 watch-add.
#
# 리포트 발견: 디렉토리 리스팅(실측 확인됨)에서 그날의 daily_report_*.txt 중
# 최신을 고른다. 없으면 사용자 지정 폴백 — **2회 추가 시도 후 종료**(사이 2분 대기;
# 리포트가 08:30 발행이라 08:40 첫 시도 실패는 대개 발행 지연이다).
#
# 테스트: REPORT_DATE=2026/08/07 ./server/scripts/daily_brief.sh
set -u
cd "$(dirname "$0")/../.."   # 리포 루트

BASE_URL="http://13.209.240.206:9999"
DATE_PATH="${REPORT_DATE:-$(date +%Y/%m/%d)}"
TODAY="${DATE_PATH//\//-}"
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
SKILL=".claude/skills/daily-market-brief/SKILL.md"
LOG="data/brief.log"
mkdir -p data

# .env.local에서 텔레그램 토큰 2개만 지역 변수로 — export 금지. Claude/파이썬
# 서브프로세스는 시크릿을 env로 물려받지 않는다(watch-score와 watch-add는 각자
# .env.local을 직접 읽는다 — load_settings / read_env_file).
_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"

# 알림은 전부 notify_auto (역할별 게이트 — server/scripts/lib/notify.sh):
# 편입·픽은 알아야 하지만 급하지 않다. **장중이면 data/notify_queue.jsonl 로
# 미뤄져 마감 HTML 리포트로 나가고**, 장외면 지금처럼 즉시 발송된다.
. "$(dirname "$0")/lib/notify.sh"

# TZ 가드 — 08:57 유니버스 경계·크론 시각 전부 KST 전제다. 호스트가 KST가 아니면
# "아침 브리핑"이 엉뚱한 시각에 돌고 자동 등록이 조용히 다음날로 밀린다.
if [ "$(date +%z)" != "+0900" ]; then
  notify_auto "daily_brief" "⚠️ daily_brief: 호스트 TZ가 KST가 아님($(date +%z)) — 브리핑은 진행하되 자동 /watch는 건너뜀"
  TZ_OK=0
else
  TZ_OK=1
fi

resolve_report() {
  curl -s -m 15 "$BASE_URL/$DATE_PATH/" 2>/dev/null \
    | grep -oE 'daily_report_[0-9]{8}_[0-9]{6}\.txt' | sort -u | tail -1
}

# --- 1. 리포트 발견 (첫 시도 + 추가 2회, 실패 시 = 끝) ---
REPORT_FILE="$(resolve_report)"
attempt=1
RETRY_SLEEP="${BRIEF_RETRY_SLEEP:-120}"   # 테스트에서 1로 줄일 수 있게 env 노출
while [ -z "$REPORT_FILE" ] && [ "$attempt" -le 2 ]; do
  sleep "$RETRY_SLEEP"
  attempt=$((attempt + 1))
  REPORT_FILE="$(resolve_report)"
done
if [ -z "$REPORT_FILE" ]; then
  echo "[$(date '+%F %T')] 리포트 없음 ($DATE_PATH, ${attempt}회 시도)" >> "$LOG"
  notify_auto "daily_brief" "🗞 ${TODAY} 회사 리포트 없음 (${attempt}회 시도) — 오늘 브리핑 건너뜀"
  exit 0
fi

REPORT_URL="$BASE_URL/$DATE_PATH/$REPORT_FILE"
notify_auto "daily_brief" "🗞 ${TODAY} 리포트 분석 세션 시작 — ${REPORT_FILE}"
echo "[$(date '+%F %T')] 세션 시작: $REPORT_URL" >> "$LOG"

# --- 2. 입력 조립: 스킬 지침 + 시스템 상태 + 리포트 본문 ---
REPORT_BODY="$(curl -s -m 30 "$REPORT_URL")"
if [ -z "$REPORT_BODY" ]; then
  notify_auto "daily_brief" "🗞 ${TODAY} 리포트 본문 다운로드 실패 — 브리핑 건너뜀"
  exit 0
fi

INPUT="$(cat "$SKILL")

===== [현재 시스템 상태] =====
[기계 국면 판단 data/state/regime.json]
$(cat data/state/regime.json 2>/dev/null || echo '(없음)')
[관심종목 data/watchlist.yaml]
$(cat data/watchlist.yaml 2>/dev/null || echo '(없음)')

===== [회사 리포트 본문: ${REPORT_FILE}] =====
$REPORT_BODY"

# --- 3. Claude 세션 (도구 없음, stdout = 텔레그램 본문) ---
# --disallowedTools: 리포트 본문(평문 HTTP, 신뢰 불가)이 프롬프트에 들어가므로
# 주입 성공을 가정하고 세션의 손발을 자른다. env에도 시크릿 없음(위 참고) — 리뷰 C4.
BRIEF="$(printf '%s' "$INPUT" | timeout 600 nice -n 10 "$CLAUDE_BIN" -p \
  --disallowedTools "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,NotebookEdit,Task,Agent,TodoWrite" \
  "위 스킬 지침(daily-market-brief)을 그대로 따라, 주어진 시스템 상태와 회사 리포트를 분석해 출력 형식대로만 답하라. 오늘 날짜는 ${TODAY}." \
  2>>"$LOG")"

if [ -z "$BRIEF" ]; then
  notify_auto "daily_brief" "🗞 ${TODAY} 분석 세션 실패 (출력 없음) — data/brief.log 확인"
  exit 0
fi

# --- 4. 전송 (텔레그램 4096자 제한 — 여유 두고 절단) ---
notify_auto "daily_brief" "${BRIEF:0:3900}"
echo "[$(date '+%F %T')] 브리핑 전송 완료 ($(printf '%s' "$BRIEF" | wc -c)자)" >> "$LOG"

# --- 5. AUTO_WATCH → 확신도 엔진 → 통과분만 자동 등록 (2026-08-10 사용자 설계) ---
# 스킬 출력 계약: 마지막에 "AUTO_WATCH: <후보 무제한>" 또는 "AUTO_WATCH: 없음".
# LLM은 후보를 '넓게' 뽑고, 등록 결정은 결정론적 엔진(watch-score: 추세·상대거래량·
# 변동성·유동성·비용, 일봉 기반)이 한다. LLM 출력이 엔진 입력 파일에 닿는 경로는
# 화이트리스트 정규식 + watch-score 게이트 + tg_bridge watch-add(flock)의 3중이다.
# 타이밍: 엔진이 08:57 KST에 유니버스를 다시 읽으므로(loop._universe_roll_bucket)
# 이 스크립트는 그 전에 끝나야 한다 — 재시도 포함 최악 ~08:55 + 스코어링 수십 초.
# AUTO_WATCH 줄은 항상 로그에 남긴다(계약 위반이 조용히 사라지지 않게) — 리뷰 M3.
AW_COUNT="$(printf '%s\n' "$BRIEF" | grep -cE '^AUTO_WATCH:')"
AUTO_LINE="$(printf '%s\n' "$BRIEF" | grep -E '^AUTO_WATCH:' | tail -1 | sed 's/^AUTO_WATCH:[[:space:]]*//')"
echo "[$(date '+%F %T')] AUTO_WATCH ${AW_COUNT}줄: ${AUTO_LINE:-<없음>}" >> "$LOG"
[ "$AW_COUNT" -ne 1 ] && notify_auto "daily_brief" "⚠️ 브리핑 AUTO_WATCH 줄이 ${AW_COUNT}개 — 출력 계약 위반(마지막 줄 기준으로 처리)"

if [ "$TZ_OK" -ne 1 ]; then
  : # TZ 비정상 — 위에서 이미 알림, 자동 등록 건너뜀
elif [ -n "${REPORT_DATE:-}" ]; then
  DEADLINE_OK=1   # 수동 테스트 경로(REPORT_DATE 지정)는 시각 무관
elif [ "1$(date +%H%M)" -ge 10855 ]; then
  DEADLINE_OK=0
  notify_auto "daily_brief" "⏰ 브리핑 지연 — 08:55 데드라인 초과, 자동 /watch 건너뜀 (08:57 유니버스 리로드에 못 태움. 수동 /watch는 다음 경계에 반영)"
else
  DEADLINE_OK=1
fi

if [ "${TZ_OK}" -eq 1 ] && [ "${DEADLINE_OK:-0}" -eq 1 ]; then
  # 토큰 형식: SYM 또는 SYM:TAG[+TAG][:YYYYMMDD]. 셸 하드캡 15개 — LLM/리포트 주입이
  # 뚫려도 상한은 셸이 보증한다(리뷰 C1: 캡을 제약 대상에게 위임하지 않는다).
  CANDS=""
  if [ -n "$AUTO_LINE" ] && [ "$AUTO_LINE" != "없음" ]; then
    CANDS="$(printf '%s' "$AUTO_LINE" | tr ' ' '\n' \
      | grep -E '^[A-Za-z0-9.]{1,10}(:[A-Za-z+]{1,20})?(:[0-9]{8})?$' | sort -u | head -15 | tr '\n' ' ' | sed 's/ $//')"
  fi
  # --discover-kr: 리포트 후보가 없어도 거래대금 랭킹 상위에서 발굴해 같은 엔진으로
  # 채점한다 — 리포트 하나에 종속되지 않기(2026-08-10 사용자 지시).
  if true; then
    SCORE_OUT="$(timeout 120 .venv/bin/python -m quant.apps.cli watch-score --symbols "$CANDS" --discover-kr 2>>"$LOG")"
    SCORE_RC=$?
    PASS="$(printf '%s\n' "$SCORE_OUT" | grep -E '^PASS:' | tail -1 | sed 's/^PASS:[[:space:]]*//')"
    # PASS 재검증: 엔진 stdout이 오염돼도 심볼[:태그[+태그]] 형식만 통과 — 리뷰 L2.
    # 태그 포맷은 CANDS(위)와 동일(SYMBOL[:TAG[+TAG]]) — watch-score가 입력 토큰의
    # 태그를 그대로 실어보낸다(2026-08-12, news_momentum의 EVENT 게이트용).
    PASS="$(printf '%s' "$PASS" | tr ' ' '\n' | grep -E '^[A-Za-z0-9.]{1,10}(:[A-Za-z+]{1,20})?$' | head -15 | tr '\n' ' ' | sed 's/ $//')"
    SCORE_LINES="$(printf '%s\n' "$SCORE_OUT" | grep -v '^PASS:')"
    if [ "$SCORE_RC" -ne 0 ]; then
      # 인프라 실패를 "전원 탈락"으로 위장하지 않는다 — 리뷰 H3.
      notify_auto "daily_brief" "🛑 확신도 엔진 오류(exit ${SCORE_RC}) — 자동 등록 건너뜀. data/brief.log 확인"
    elif [ -n "$PASS" ]; then
      # watch-add --tags는 --source처럼 호출 1건에 값 하나다(tg_bridge.py의 전역
      # 플래그 시맨틱 — _parse_watch_add_argv 참고) — 태그가 다른 심볼을 한 콜에
      # 섞을 수 없으므로 태그별로 묶어 나눠 호출한다. 예:
      # "AAPL:EVENT NVDA:EVENT TQQQ" → EVENT 그룹 1콜 + 무태그 그룹 1콜.
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
        R="$(timeout 60 .venv/bin/python server/scripts/tg_bridge.py watch-add --source auto $TAG_FLAG $GROUP 2>>"$LOG" || echo "자동 /watch 실패(${key}) — data/brief.log 확인")"
        RESULT="${RESULT}${R}
"
      done
      DISPLAY_PASS="$(printf '%s\n' "$PASS" | tr ' ' '\n' | awk -F: '{print $1}' | tr '\n' ' ')"
      notify_auto "daily_brief" "🤖 확신도 엔진 통과 → 자동 /watch: ${DISPLAY_PASS}
${SCORE_LINES:0:2800}
${RESULT:0:600}
(원치 않으면 /unwatch <심볼>)"
    else
      notify_auto "daily_brief" "🤖 브리핑 후보 ${CANDS} — 확신도 엔진 통과 0건, 자동 등록 없음
${SCORE_LINES:0:2800}"
    fi
    echo "[$(date '+%F %T')] auto-watch 후보: $CANDS → rc=$SCORE_RC 통과: ${PASS:-없음}" >> "$LOG"
  fi
fi
