#!/usr/bin/env bash
# KR 개별종목 일봉 히스토리 백필 — 장타(팩터·모멘텀) 전략 백테스트의 데이터 관문.
#
# 왜 이 스크립트가 필요한가 (2026-08-19):
# `data/history/`에는 QQQ/TQQQ/SQQQ(약 10년)와 069500(28거래일)뿐이었다 —
# KR 개별종목 일봉은 0종목이라 6개월 모멘텀/가치 팩터 백테스트가 아예 불가능했다.
# Toss 일봉은 소급이 깊다(docs/data-availability.md 실측: 2010-02-11까지, 사실상
# 전체 역사) — 1분봉(4거래일 롤링)과 달리 일봉은 이 문제가 없다.
#
# **adjusted=true(수정주가)를 항상 명시적으로 보낸다**
# (quant/collect/quotes/toss_source.py: TossCandleSource._fetch). 액면분할·병합이
# 반영되지 않은 가격으로 모멘텀을 계산하면 가짜 급등락이 생긴다.
#
# 저장 경로/멱등성: backfill_1m.sh와 동일하게 `quant.apps.cli fetch`가 그대로
# 쓰는 `quant.collect.quotes.backfill.backfill()`을 통해서만 쓴다 — 새 포맷을
# 만들지 않는다. 1분봉이 아닌 native interval(1d)이므로
# data/history/{symbol}/1d/{YYYY}/{MM}.parquet 레이아웃(기존 QQQ/069500 일봉과
# 동일)에 월별 파티션으로 쌓인다. backfill()이 이미 완성된 과거 월 파티션을
# 스킵하고 마지막 봉 이후 gap만 재조회하는 멱등 재개를 보장한다 — 이 스크립트가
# 별도로 dedup/증분 판단을 할 필요가 없다.
#
# 대상: 관심종목(data/watchlist.yaml)의 KR 종목(6자리 숫자, quant.core.models.
# market_of_symbol과 동일 규칙) + 시장 앵커(069500). 로컬처럼 watchlist.yaml이
# 없는 환경/테스트에서는 SYMBOLS 환경변수로 직접 넘길 수 있다(예:
# SYMBOLS="005930 000660" ./server/scripts/backfill_kr_stock_daily.sh).
#
# 깊이: 기본 2년(6개월 모멘텀 + 워밍업 + walk-forward 창). 첫 인자로 조절
# 가능: ./server/scripts/backfill_kr_stock_daily.sh 5  (5년 백필).
#
# 레이트리밋: 실제 페이지네이션 내 호출 간격/429 Retry-After 재시도는
# TossClient._request(quant/adapters/brokers/toss/client.py)가 이미 담당한다
# (MARKET_DATA_CHART 그룹, 5 TPS). 다만 심볼마다 `cli fetch`가 별도 프로세스로
# 뜨므로 그 안의 rate limiter 상태는 심볼 경계에서 리셋된다 — 프로세스 경계에도
# 여유를 두기 위해 심볼 사이에 SLEEP_BETWEEN(기본 1초)을 둔다.
#
# 실패 집계: 심볼별 성공/실패를 명시적으로 세고, 실패 사유(stdout/stderr 마지막
# 줄)를 요약 로그에 남긴다 — 빈 값으로 실패를 위장하지 않는다.
#
# 로컬 한계: Toss Open API는 등록된 IP만 허용한다(403 otherwise) — 로컬 Mac에서는
# 실제 백필을 검증할 수 없다(구조적 결측, 메모 참고). 단위 테스트는
# tests/test_toss_source.py가 페이크 클라이언트로 페이징/adjusted/native_interval을
# 검증한다.
#
# 테스트: DRY_RUN=1 SYMBOLS="005930 000660" ./server/scripts/backfill_kr_stock_daily.sh
#
# 크론 등록은 제안만 — 이 파일은 server/crontab.txt에 아직 없다. 실행 시각은
# 사용자가 정한다(후보: KR 장 마감 15:30 이후, 또는 US 마감 후 창 05:00~08:30 KST).
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
LOG="data/fetch_kr_stock_daily.log"
DEPTH_YEARS="${1:-2}"
SLEEP_BETWEEN="${SLEEP_BETWEEN:-1}"

mkdir -p data
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
# 알림은 전부 notify_defer (역할별 게이트 — server/scripts/lib/notify.sh):
# 요약·정보성이라 텔레그램으로는 **절대 나가지 않는다**. data/notify_queue.jsonl
# 에 쌓여 마감 HTML 리포트로만 간다.
. "$(dirname "$0")/lib/notify.sh"
NOTIFY_LANE="ops"  # 텔레그램 포럼 토픽 레인 — docs/runbooks/telegram-rooms.md
# 메모리 워터마크(2026-08-31, 2GB 박스 관측) — server/scripts/lib/memlog.sh 참고.
. "$(dirname "$0")/lib/memlog.sh"
memlog_wrap "backfill_kr_stock_daily"

START="$(date -u -d "${DEPTH_YEARS} years ago" +%Y-%m-%d 2>/dev/null \
         || date -u -v-${DEPTH_YEARS}y +%Y-%m-%d)"   # GNU / BSD 양쪽

# 앵커 — 유일하게 이미 일봉이 있는 KR 심볼(개장일 판정 앵커, backfill_kr_daily.sh
# 가 채운다). 여기서도 채워두면 KR 유니버스 팩터 백테스트의 시장 벤치마크로 쓸 수
# 있다.
ANCHORS="069500"

if [ -n "${SYMBOLS:-}" ]; then
  WATCH_SYMBOLS=""  # SYMBOLS 환경변수로 전체 목록을 직접 지정한 경우 watchlist는 무시
else
  # 관심종목 — KR 종목(6자리 숫자)만 걸러 뽑는다. quant.core.models.market_of_symbol과
  # 동일한 규칙(4곳에서 각자 구현하던 걸 하나로 모은 그 규칙)을 그대로 재사용한다.
  WATCH_SYMBOLS="$("$PY" - <<'PYEOF' 2>>"$LOG"
import yaml
from quant.core.models import market_of_symbol

try:
    with open("data/watchlist.yaml", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
except FileNotFoundError:
    raw = {}
entries = raw.get("symbols") or []
out = []
for e in entries:
    sym = e.get("symbol") if isinstance(e, dict) else e
    if sym and market_of_symbol(str(sym)) == "KR":
        out.append(str(sym))
print(" ".join(out))
PYEOF
)"
fi

SYMBOLS="${SYMBOLS:-}"
SYMBOLS="$(printf '%s\n' $ANCHORS $WATCH_SYMBOLS $SYMBOLS | awk '!seen[$0]++' | grep -v '^$')"  # 중복 제거, 순서 유지

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[DRY_RUN] symbols: $SYMBOLS"
  echo "[DRY_RUN] depth_years=$DEPTH_YEARS start=$START (source=toss, interval=1d, adjusted=true)"
fi

N_SYMBOLS="$(echo $SYMBOLS | wc -w | tr -d ' ')"
log "KR 개별종목 일봉 백필 시작: ${N_SYMBOLS}종목 from $START (depth=${DEPTH_YEARS}y, adjusted=true)"

TOTAL_BARS=0
OK_COUNT=0
FAIL_COUNT=0
FAIL_LINES=""
for SYM in $SYMBOLS; do
  if [ "${DRY_RUN:-0}" = "1" ]; then
    echo "[DRY_RUN] fetch $SYM 1d from $START (source=toss, adjusted=true)"
    continue
  fi
  OUT="$(timeout 300 "$PY" -m quant.apps.cli fetch \
    --symbol "$SYM" --interval 1d --source toss --start "$START" 2>&1)"
  RC=$?
  printf '%s\n' "$OUT" >> "$LOG"
  sleep "$SLEEP_BETWEEN"
  if [ "$RC" -ne 0 ]; then
    FAIL_COUNT=$((FAIL_COUNT + 1))
    REASON="$(printf '%s\n' "$OUT" | tail -3 | tr '\n' ' ' | cut -c1-200)"
    FAIL_LINES="${FAIL_LINES}
  - ${SYM}: rc=${RC} ${REASON}"
    log "실패: $SYM rc=$RC — ${REASON}"
    continue
  fi
  OK_COUNT=$((OK_COUNT + 1))
  BARS="$(printf '%s\n' "$OUT" | grep -oE 'total_bars: [0-9]+' | grep -oE '[0-9]+' || echo 0)"
  TOTAL_BARS=$((TOTAL_BARS + BARS))
done

if [ "${DRY_RUN:-0}" = "1" ]; then
  exit 0
fi

log "KR 개별종목 일봉 백필 종료: 성공 ${OK_COUNT}/${N_SYMBOLS}, 실패 ${FAIL_COUNT}, 봉 합계(누적) ${TOTAL_BARS}개"

# 정상일 때는 조용하다(다른 백필 크론과 동일 관례) — 실패가 있었을 때만 알린다.
if [ "$FAIL_COUNT" -gt 0 ]; then
  notify_defer "backfill_kr_stock_daily" "🚨 KR 개별종목 일봉 백필 일부 실패 (${FAIL_COUNT}/${N_SYMBOLS}종목)${FAIL_LINES}
data/fetch_kr_stock_daily.log 확인"
  exit 1
fi
exit 0
