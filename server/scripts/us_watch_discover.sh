#!/usr/bin/env bash
# 평일 21:50 KST (US 개장 40분 전) — US 관심종목 자동 편입.
#
# 왜 daily_brief.sh와 별도인가:
# - 소스가 다르다. 08:40 브리핑은 **한국 회사 리포트**(한국 시황·증권사 리포트)를
#   읽으므로 US 종목 후보가 사실상 나오지 않는다. 실측(2026-08-10~11): 자동 등록된
#   종목 전부가 KR이었다. US는 리포트가 아니라 **Toss 거래대금 랭킹 발굴**이 유일한
#   자동 편입 경로다.
# - 타이밍이 다르다. KR은 개장(09:00) 전 08:40이 맞고, US는 개장(22:30 KST, 서머타임에
#   따라 23:30) 전이어야 그날 세션에 반영된다.
#
# LLM을 쓰지 않는다 — 후보 발굴(랭킹 API)도 채점(확신도 엔진)도 결정론적이다.
# 그래서 daily_brief.sh의 프롬프트 주입 방어 장치가 여기엔 필요 없다.
#
# 엔진 반영: 이 스크립트가 관심종목 파일에 쓰면 엔진이 **다음 유니버스 롤**에
# 흡수한다. 롤 경계는 KST 자정과 08:57 두 곳이므로, 21:50에 등록된 US 종목은
# 자정 롤에서 흡수된다 — US 세션(22:30~05:00)의 **자정 이후 구간부터** 거래 대상이
# 된다. 개장 직후부터 잡으려면 유니버스 롤 경계를 하나 더 늘려야 한다(미구현).
#
# 테스트: DRY_RUN=1 ./server/scripts/us_watch_discover.sh
set -u
cd "$(dirname "$0")/../.."

LOG="data/us_discover.log"
mkdir -p data

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"

tg() {
  curl -s -m 10 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    -d "chat_id=${TG_CHAT}" --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

if [ "$(date +%z)" != "+0900" ]; then
  tg "⚠️ us_watch_discover: 호스트 TZ가 KST가 아님($(date +%z)) — 건너뜀"
  exit 0
fi

# --- 발굴 + 채점 (결정론적 엔진) ---
# --symbols 없이 --discover-us만으로 실행 — 후보 출처가 랭킹뿐이다.
SCORE_OUT="$(timeout 180 .venv/bin/python -m quant.apps.cli watch-score --discover-us 2>>"$LOG")"
SCORE_RC=$?
PASS="$(printf '%s\n' "$SCORE_OUT" | grep -E '^PASS:' | tail -1 | sed 's/^PASS:[[:space:]]*//')"
SCORE_LINES="$(printf '%s\n' "$SCORE_OUT" | grep -v '^PASS:' | head -c 2800)"

echo "[$(date '+%F %T')] rc=$SCORE_RC 통과: ${PASS:-없음}" >> "$LOG"

if [ "$SCORE_RC" -ne 0 ]; then
  tg "⚠️ 미국 관심종목 자동 발굴 실패 (엔진 오류) — 오늘 미국 세션은 기존 목록으로 진행합니다"
  exit 1
fi

if [ -z "$PASS" ] || [ "$PASS" = "없음" ]; then
  echo "[$(date '+%F %T')] 통과 종목 없음 — 등록 생략" >> "$LOG"
  exit 0
fi

# --- 등록 (하드캡 + 화이트리스트) ---
# 화이트리스트: US 티커는 영문 1~5자(+선택적 .A/.B 클래스). 숫자 6자리(KR)는 여기서
# 나오면 안 된다 — 나오면 발굴 시장 필터가 깨진 것이므로 통째로 거른다.
ADDED=""
COUNT=0
if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[DRY_RUN] 등록 대상: $PASS"
  echo "$SCORE_LINES"
  exit 0
fi

for SYM in $PASS; do
  [ "$COUNT" -ge 5 ] && break   # 하드캡 5 — 랭킹 발굴이 폭주해도 관심종목을 덮지 않는다
  case "$SYM" in
    [A-Za-z][A-Za-z0-9]*) : ;;
    *) echo "[$(date '+%F %T')] 화이트리스트 탈락: $SYM" >> "$LOG"; continue ;;
  esac
  if [ "${#SYM}" -gt 6 ]; then
    echo "[$(date '+%F %T')] 티커 길이 초과: $SYM" >> "$LOG"; continue
  fi
  # --source를 심볼보다 앞에 둔다(daily_brief.sh와 동일 형태). 파서는 이제 위치에
  # 무관하지만, 두 호출자의 형태를 맞춰두는 편이 다음 사람에게 덜 헷갈린다.
  if .venv/bin/python server/scripts/tg_bridge.py watch-add --source auto "$SYM" >>"$LOG" 2>&1; then
    ADDED="$ADDED $SYM"
    COUNT=$((COUNT + 1))
  fi
done

if [ -n "$ADDED" ]; then
  tg "🇺🇸 미국 관심종목 자동 편입

📈 추가된 종목:$ADDED

거래대금 상위에서 발굴해 확신도 엔진을 통과한 종목입니다.
자정 유니버스 갱신 후 매매 대상이 됩니다.

$SCORE_LINES"
else
  echo "[$(date '+%F %T')] 통과했으나 등록 0건" >> "$LOG"
fi
