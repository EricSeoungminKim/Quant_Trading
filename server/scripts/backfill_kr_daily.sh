#!/usr/bin/env bash
# 매일 16:30 KST — 069500(KODEX200) 일봉 백필 + **신선도 되짚기**.
#
# 왜 이 스크립트가 존재하나 (2026-08-16, 서브프로젝트 G):
# 개장일 판정(`quant/analyze/opendays.py`)이 069500 일봉의 "마지막 봉 날짜"를
# 앵커로 쓴다. EC2 실측으로 확인됐다 — `data/history/069500/1d`는 애초에
# **존재하지 않았다**(069500은 Toss 분봉만 `data/history/069500/{YYYY}/`에
# 쌓이고 있었다, 마지막 2026-08-11). 백필 크론이 없으면 개장일 판정 자체가
# 영구히 None(판정 불가)이 되어 리포트 집계 창이 계속 닫힌 채로 남는다.
#
# backfill_us_daily.sh(QQQ)를 그대로 본떴다 — 소스·되짚기 철학이 같다.
#
# **왜 yfinance인가.** 069500.KS로 상장 이후 일봉 전체를 인증·IP 제약 없이
# 준다(yf_source.py 실측 한도표, "1d" interval은 리밋 없음). Toss는 1분봉만
# 지원해 후보가 아니다(cli.py가 명시적으로 거부).
#
# **왜 매일(월~금이 아니라)인가.** 이 파일 자체가 "오늘이 개장일인가"의 판정
# 근거다 — 휴장일에 돌려도 yfinance가 새 봉을 안 주니 그냥 무해한 no-op이고
# (idempotent), 반대로 월~금만 돌리면 대체휴일·임시공휴일로 평일이 휴장인
# 주간에 판정 근거가 갱신되지 않는 사각이 생긴다. 매일 도는 게 더 싸고 더
# 안전하다.
#
# 테스트: DRY_RUN=1 ./server/scripts/backfill_kr_daily.sh
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
LOG="data/fetch_kr_daily.log"
SYMBOL="069500"
INTERVAL="1d"
# 40일 되돌아본다. backfill()은 완성된 과거 월 파티션을 건너뛰고 마지막 봉 이후만
# 받으므로 비용은 거의 없다. 월 경계에서 앞 달이 덜 찬 경우까지 스스로 메운다 —
# 크론이 며칠 빠져도 다음 실행이 복구한다.
LOOKBACK_DAYS=40

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

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[DRY_RUN] fetch $SYMBOL $INTERVAL from $START (source=yfinance, 069500.KS 로 질의)"
fi

log "백필 시작: $SYMBOL $INTERVAL from $START"
FETCH_OUT="$(timeout 300 "$PY" -m quant.apps.cli fetch \
  --symbol "$SYMBOL" --interval "$INTERVAL" --source yfinance --start "$START" 2>&1)"
FETCH_RC=$?
printf '%s\n' "$FETCH_OUT" >> "$LOG"

# --- 되짚기: 받았다고 믿지 않는다 ---
#
# exit 0 이 "봉이 최신이 됐다"를 뜻하지 않는다. 벤더가 빈 응답을 주면 fetch는
# 성공하고 파일은 그대로다 — 결과를 파일에서 다시 읽어 확인한다.
#
# 임계값 6일: backfill_us_daily.sh가 국면 가드와 공유하는 것과 같은 상수
# `STALE_DAILY_BARS_AFTER`(timedelta(days=6))를 그대로 쓴다 — KR 전용 상수를
# 새로 만들면 "공휴일 연휴 최대 3~4일"이라는 같은 판단 기준이 두 곳에서
# 따로 관리되다 언젠가 갈라진다. 6일이면 3~4일짜리 연휴는 오탐 없이 넘긴다.
VERDICT="$(timeout 60 "$PY" - <<'PYEOF' 2>>"$LOG"
from datetime import datetime, timezone
import pandas as pd
from quant.adapters.olap import coverage
from quant.trade.regime.provider import STALE_DAILY_BARS_AFTER

cov = coverage("069500", "1d")
if cov is None or cov.last_ts is None:
    # DuckDB 미설치이거나 파일이 없다 — "정상"이 아니라 "확인 불가"다.
    print("UNKNOWN 커버리지를 읽지 못했다 (duckdb 미설치 또는 파티션 없음)")
else:
    last = pd.Timestamp(cov.last_ts)
    last = last.tz_localize("UTC") if last.tzinfo is None else last.tz_convert("UTC")
    age = pd.Timestamp(datetime.now(timezone.utc)) - last
    state = "OK" if age <= STALE_DAILY_BARS_AFTER else "STALE"
    print(f"{state} 마지막 봉 {last.date()}, {age.days}일 경과 "
          f"(임계 {STALE_DAILY_BARS_AFTER.days}일), 봉 {cov.n_bars}개")
PYEOF
)"
log "되짚기: ${VERDICT:-<판정 실패>}"

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[DRY_RUN] fetch rc=$FETCH_RC"
  echo "[DRY_RUN] 되짚기: ${VERDICT:-<판정 실패>}"
  exit 0
fi

# 정상일 때는 조용하다 — 매일 오는 "정상" 알림은 사람이 끄고, 끈 알림은 없는 알림이다.
case "${VERDICT%% *}" in
  OK)
    [ "$FETCH_RC" -ne 0 ] && tg "⚠️ 069500 일봉 백필 종료코드 ${FETCH_RC} — 다만 봉은 최신이다(${VERDICT#* }). data/fetch_kr_daily.log 확인"
    exit 0
    ;;
  STALE)
    tg "🚨 069500 일봉이 백필 후에도 낡았다 — ${VERDICT#* }
개장일 판정(opendays)이 이 파일을 앵커로 쓴다. 낡으면 last_open_day가 갱신되지
않아 리포트 집계 창이 계속 닫힌 채로 남는다.
yfinance 응답·네트워크 확인: tail -40 data/fetch_kr_daily.log"
    exit 1
    ;;
  *)
    tg "🚨 069500 일봉 백필 검증 불가 — ${VERDICT:-판정 실패} (fetch rc=${FETCH_RC})
'봉이 최신인지 모른다'는 상태다. data/fetch_kr_daily.log 확인"
    exit 1
    ;;
esac
