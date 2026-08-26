#!/usr/bin/env bash
# 매일 05:40 KST — 1분봉 표본 적재(scalp_1m 백테스트용).
#
# 왜 이 시각인가 (docs/superpowers/specs/2026-08-18-scalp-1m-design.md "데이터" 절):
# US 마감(EST 06:00/EDT 05:00) 직후·KR 개장(09:00) 전이다 — 그날 US 세션 전체를
# 놓치지 않으면서 KR 개장 전 크론 러시(08:05 watch-reset부터 시작하는 체인)와
# 겹치지 않는다.
#
# 왜 이 스크립트가 필요한가: Toss 1분봉은 **4거래일 롤링만** 제공한다(QUICKREF.md
# 실측, `docs/data-availability.md`) — scalp_1m은 이 근본적 데이터 부족 때문에
# 백테스트 표본이 아예 없다(paper 번인이 유일한 검증 경로). 매일 당겨서 append하면
# 며칠~몇 주 뒤에는 최소한의 표본이 쌓인다. 하루라도 건너뛰면 4일 롤링 창을 넘겨
# 그 구간이 영구히 사라진다 — "매일" 크론이어야 하는 이유가 backfill_kr_daily.sh
# 와는 다르다(그쪽은 휴장일 no-op이 무해하지만, 이쪽은 실제로 데이터가 사라진다).
#
# 저장 경로/멱등성: `quant.apps.cli fetch --source toss --interval 1m`이 그대로
# 쓰는 `quant.collect.quotes.backfill.backfill()`을 통해서만 쓴다(어댑터의 기존
# 저장 함수 재사용 — 새 포맷을 만들지 않는다). data/history/{symbol}/{YYYY}/{MM}.parquet
# (1분봉 레이아웃, 기존 하위호환)에 월별 파티션으로 쌓이고, backfill()이 이미
# 타임스탬프 dedup + 마지막 봉 이후 gap만 재조회하는 멱등 재개를 보장한다(모듈
# docstring 참고) — 이 스크립트가 별도로 dedup할 필요가 없다.
#
# 대상: 관심종목(watchlist) 전 종목 + 국면/전략 앵커(QQQ·069500·TQQQ·SOXL).
# 관심종목은 매일 바뀌므로 그날그날 data/watchlist.yaml을 읽는다 — 스크립트 상단에
# 고정 목록을 박아두지 않는다.
#
# 테스트: DRY_RUN=1 ./server/scripts/backfill_1m.sh
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
LOG="data/fetch_1m.log"
LOOKBACK_DAYS=4  # Toss 1분봉 롤링 한도(4거래일) — 그 이상 되짚어봐야 서버가 주지 않는다.

mkdir -p data
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
tg() {
  if [ "${DRY_RUN:-0}" = "1" ]; then
    printf '[DRY_RUN][TG]\n%s\n\n' "$1"
    return 0
  fi
  curl -s -m 10 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    -d "chat_id=${TG_CHAT}" --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

START="$(date -u -d "${LOOKBACK_DAYS} days ago" +%Y-%m-%d 2>/dev/null \
         || date -u -v-${LOOKBACK_DAYS}d +%Y-%m-%d)"   # GNU / BSD 양쪽

# 앵커 — 국면/전략이 매매 판단에 참조하는 고정 심볼(watchlist 편집과 무관하게 항상 적재).
ANCHORS="QQQ 069500 TQQQ SOXL"

# 관심종목 — 그날의 watchlist.yaml에서 심볼만 뽑는다(브리지가 쓰는 두 형식 모두
# 지원: 문자열 리스트 / {symbol,...} 객체 리스트 — quant.trade.universe와 동일 관용).
WATCH_SYMBOLS="$("$PY" - <<'PYEOF' 2>>"$LOG"
import yaml
try:
    with open("data/watchlist.yaml", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
except FileNotFoundError:
    raw = {}
entries = raw.get("symbols") or []
out = []
for e in entries:
    sym = e.get("symbol") if isinstance(e, dict) else e
    if sym:
        out.append(str(sym))
print(" ".join(out))
PYEOF
)"

# 최근 체결 심볼(2026-08-26, 조직도 역할 5) — 일일 피드백·부검이 재생할 봉은
# "그날 거래한 종목"인데, 아침에 워치리스트에서 빠진 종목(보유만 유지 등)은
# 위 두 목록에 없어 봉이 안 모였다(실측: 000500 진입이 '1분봉 없어 판정 제외').
# Toss 1m은 4거래일 롤링이라 지금 안 당기면 **영구 유실**이다 — 거래 원장에서
# 롤링 창 안의 체결 심볼을 뽑아 합류시킨다. 원장 없음/파싱 실패는 빈 목록.
TRADED_SYMBOLS="$("$PY" - <<'PYEOF2' 2>>"$LOG"
import json
from datetime import datetime, timedelta, timezone
cutoff = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
out = []
try:
    with open("data/state/trades.jsonl", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if (r.get("ts") or "") >= cutoff and r.get("symbol"):
                out.append(str(r["symbol"]))
except FileNotFoundError:
    pass
print(" ".join(dict.fromkeys(out)))
PYEOF2
)"

SYMBOLS="$(printf '%s\n' $ANCHORS $WATCH_SYMBOLS $TRADED_SYMBOLS | awk '!seen[$0]++')"  # 중복 제거, 순서 유지

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[DRY_RUN] symbols: $SYMBOLS"
  echo "[DRY_RUN] start=$START (source=toss, interval=1m)"
fi

log "1분봉 백필 시작: $(echo $SYMBOLS | wc -w)종목 from $START"

FAILS=""
TOTAL_BARS=0
for SYM in $SYMBOLS; do
  if [ "${DRY_RUN:-0}" = "1" ]; then
    echo "[DRY_RUN] fetch $SYM 1m from $START (source=toss)"
    continue
  fi
  OUT="$(timeout 120 "$PY" -m quant.apps.cli fetch \
    --symbol "$SYM" --interval 1m --source toss --start "$START" 2>&1)"
  RC=$?
  printf '%s\n' "$OUT" >> "$LOG"
  if [ "$RC" -ne 0 ]; then
    FAILS="$FAILS $SYM"
    log "실패: $SYM rc=$RC"
    continue
  fi
  BARS="$(printf '%s\n' "$OUT" | grep -oE 'total_bars: [0-9]+' | grep -oE '[0-9]+' || echo 0)"
  TOTAL_BARS=$((TOTAL_BARS + BARS))
done

if [ "${DRY_RUN:-0}" = "1" ]; then
  exit 0
fi

log "1분봉 백필 종료: 신규 봉 합계 ${TOTAL_BARS}개, 실패 종목:${FAILS:- 없음}"

# 정상일 때는 조용하다(다른 백필 크론과 동일 관례) — 실패가 있었을 때만 알린다.
if [ -n "$FAILS" ]; then
  tg "🚨 1분봉 백필 일부 실패 — 종목:${FAILS}
scalp_1m 표본 적재용(Toss 1m은 4거래일 롤링이라 하루라도 밀리면 그 구간이 영구히
사라진다). data/fetch_1m.log 확인"
  exit 1
fi
exit 0
