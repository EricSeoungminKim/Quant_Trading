#!/usr/bin/env bash
# 매주 토요일 05:00 KST — KR 1분봉 연구 레이크(EC2 ~/qb_backfill) 자동 증분 백필.
#
# 왜 필요한가 (2026-09-06): quant-backtest(연구 저장소)의 KR 1분봉은 이 저장소
# 사람이 그때그때 EC2에 SSH로 들어가 손으로 돌린 1회성 백필(2026-09-03
# `scripts/kr_after_backfill.sh` 신설, 2026-09-05/06 ETF 2종 추가)로만 자라 왔다
# — ~96종목 13개월치가 전부이고, **사람이 기억하지 않으면 멈춘다.** 토요일은
# KR·US 정규장이 둘 다 없어 키움 쿼터가 완전히 비는 유일한 창이라 이 시각을 골랐다
# (동시에 EC2 리소스도 엔진과 경쟁하지 않는다).
#
# **CLI 엔트리는 없다** — `quant-backtest/qb/fetch_kiwoom.py`(ka10080, 1분봉)는
# 라이브러리 함수 `fetch(symbols, interval, max_pages, root)`만 노출하고
# `if __name__ == "__main__"` 블록이 없다(2026-09-06 조사 확인, `qb/fetch_kiwoom_flow.py`
# ka10059 쪽에는 있어 착각하기 쉽다). 실제 96종목 백필도 이 함수를 EC2에서 직접
# 호출했다(quant-backtest `results/letf/SUMMARY_kr.md` "1. 데이터" 절:
# `qb.fetch_kiwoom.fetch`, `interval="1m"`, `max_pages=150`) — 이 스크립트는 그
# 호출을 재현해 정기화한 것뿐이다.
#
# 대상 심볼 = watchlist.yaml(그날 관심종목) ∪ KR 레버리지/인버스 ETF 고정셋
# ∪ 대형주 유니버스 캐시(`data/state/kr_largecap_universe.json`, ~300종목,
# `quant/collect/kr_largecap_daily.py` — 회사 리포트가 참조하는 그 목록). 전부
# 6자리 숫자 코드만 남긴다(watchlist에는 US 티커도 섞여 있다 — ka10080은 KR
# 종목만 받는다).
#
# 증분 규칙 (fetch()는 월별 gap-only 재조회를 직접 지원하지 않는다 — 매번 지금
# 시점부터 과거로 페이지를 넘겨 서버가 주는 데까지 받는 방식이라, 이 스크립트가
# 심볼별로 "얼마나 깊이 넘길지"를 흉내낸다):
#   - 레이크에 그 심볼의 1분봉 파티션이 아예 없거나 마지막 파티션이 2개월 넘게
#     낡았다(신규 진입 또는 장기 결측 복구) → max_pages=150(96종목 백필 실측값)
#   - 최근 2개월 안에 파티션이 있다(정상 주간 케이던스) → max_pages=20(추정 —
#     ka10080 한 페이지≈900행 관측치 기준 한 달≈8페이지, 2개월 버퍼)
# 어느 쪽이든 `_write_lake`가 기존 달과 병합·dedup하므로 재실행은 항상 안전
# (멱등)하다 — 과거 달이 덮어써져 사라지지 않는다.
#
# 안전장치:
#   - `~/qb_backfill` 체크아웃/venv가 없으면 즉시 중단 + 1회만 알림(복구되면
#     다음 결측 시 다시 알리도록 마커를 지운다).
#   - KR 정규장 요일(주말만 거른다 — `StaticSessionCalendar`는 휴장일을 모른다,
#     `quant/core/session.py` 자체 문서화)이면 중단 — 이 크론이 평일에 걸리는
#     상황은 곧 "엔진의 키움 실시간 라우트가 살아 있는 동안 같은 쿼터를 또
#     쓴다"는 뜻이라 반드시 막아야 한다. 스케줄이 토요일 고정이라 정상 동작
#     중엔 이 분기를 절대 타지 않는다 — 크론 오편집·수동 실행 방어선.
#
# 토큰 캐시: `QB_KIWOOM_CACHE`를 `~/qb_backfill` 전용 경로로 고정한다 —
# `qb/fetch_kiwoom.py` 모듈 docstring의 실측 경고(엔진 자신의 토큰 캐시
# 디렉터리를 재사용하면 "발급하면 캐시가 무효화된다"는 착각을 낳은 사고가
# 있었다)를 그대로 따른다.
#
# 사용법: DRY_RUN=1 ./server/scripts/kr_minute_backfill.sh
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python   # 이 저장소(엔진) 자체 venv — 요일 판정 + 심볼 목록 추출용
QB_DIR="${QB_BACKFILL_DIR:-$HOME/qb_backfill}"
QB_PY="$QB_DIR/.venv/bin/python"
LAKE_ROOT="$QB_DIR/lake"   # kr_after_backfill.sh(연구 저장소)가 rsync 로 읽는 것과 같은 경로
LOG="data/kr_minute_backfill.log"
MISSING_MARKER="data/state/kr_minute_backfill_missing.flag"

mkdir -p data data/state
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
. "$(dirname "$0")/lib/notify.sh"
NOTIFY_LANE="ops"  # 텔레그램 포럼 토픽 레인 — docs/runbooks/telegram-rooms.md
. "$(dirname "$0")/lib/memlog.sh"
memlog_wrap "kr_minute_backfill"

# --- 가드 1: ~/qb_backfill 체크아웃/venv 없음 ---
if [ ! -x "$QB_PY" ]; then
  log "중단: $QB_PY 없음 — qb_backfill 체크아웃/venv 확인 필요"
  if [ ! -f "$MISSING_MARKER" ]; then
    notify_now "🚨 KR 1분봉 주간 백필 중단 — ${QB_DIR}/.venv/bin/python 없음. qb_backfill 체크아웃을 확인하라 (server/scripts/kr_minute_backfill.sh). 복구 전까지 재알림 없음."
    touch "$MISSING_MARKER"
  fi
  exit 1
fi
rm -f "$MISSING_MARKER"

# --- 가드 2: KR 정규장 요일이면 중단(위 안전장치 절 참고) ---
IS_TRADING_DAY="$("$PY" - <<'PYEOF' 2>>"$LOG"
from datetime import datetime, timezone
from quant.core.session import StaticSessionCalendar
now = datetime.now(timezone.utc)
sess = StaticSessionCalendar().session("KR", now)
print("1" if sess is not None else "0")
PYEOF
)"
if [ "$IS_TRADING_DAY" = "1" ]; then
  notify_now "🚨 KR 1분봉 주간 백필이 KR 정규장 요일에 트리거됐다 — 엔진 키움 실시간 라우트와 쿼터 충돌 위험, 중단(server/scripts/kr_minute_backfill.sh). server/crontab.txt 확인."
  log "중단: KR 정규장 요일 트리거"
  exit 1
fi

# --- 대상 심볼: watchlist ∪ KR 레버리지/인버스 ETF ∪ 대형주 유니버스 ---
export QT_ETF_SYMBOLS="069500 122630 252670 229200 233740 251340 114800"
SYMBOLS="$("$PY" - <<'PYEOF' 2>>"$LOG"
import json
import os
import re

import yaml

code_re = re.compile(r"^\d{6}$")
out = []

# watchlist.yaml — backfill_1m.sh 와 동일 파서(문자열/딕셔너리 항목 둘 다 지원)
try:
    with open("data/watchlist.yaml", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
except FileNotFoundError:
    raw = {}
for e in (raw.get("symbols") or []):
    sym = e.get("symbol") if isinstance(e, dict) else e
    if sym and code_re.match(str(sym)):
        out.append(str(sym))

# KR 레버리지/인버스 ETF 고정셋
out += [s for s in os.environ.get("QT_ETF_SYMBOLS", "").split() if code_re.match(s)]

# 대형주 유니버스 캐시 — quant/collect/kr_largecap_daily.py가 쓰는 그 파일
try:
    with open("data/state/kr_largecap_universe.json", encoding="utf-8") as f:
        payload = json.load(f)
    for e in (payload.get("symbols") or []):
        sym = e.get("symbol") if isinstance(e, dict) else e
        if sym and code_re.match(str(sym)):
            out.append(str(sym))
except (FileNotFoundError, ValueError):
    pass

print(" ".join(dict.fromkeys(out)))  # 순서 유지, 중복 제거
PYEOF
)"

N_SYMBOLS="$(echo "$SYMBOLS" | wc -w | tr -d ' ')"

if [ -z "$SYMBOLS" ]; then
  log "대상 심볼 0개 — watchlist/대형주 캐시 모두 비었거나 파싱 실패"
  notify_defer "kr_minute_backfill" "KR 1분봉 주간 백필: 대상 심볼 0개(watchlist·대형주 캐시 모두 비었음) — 아무 것도 하지 않음. ${LOG} 확인"
  exit 0
fi

log "백필 시작: ${N_SYMBOLS}개 심볼 → ${LAKE_ROOT}"

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[DRY_RUN] qb_backfill=$QB_DIR lake_root=$LAKE_ROOT"
  echo "[DRY_RUN] 대상 심볼(${N_SYMBOLS}개): $SYMBOLS"
  exit 0
fi

export QB_KIWOOM_CACHE="${QB_KIWOOM_CACHE:-$QB_DIR/.kiwoom_cache}"

FETCH_OUT="$(cd "$QB_DIR" && QT_SYMBOLS="$SYMBOLS" QT_LAKE_ROOT="$LAKE_ROOT" \
  timeout 14400 "$QB_PY" - <<'PYEOF' 2>&1
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from qb.fetch_kiwoom import fetch

LAKE_ROOT = Path(os.environ["QT_LAKE_ROOT"])
SYMBOLS = os.environ["QT_SYMBOLS"].split()
FULL_MAX_PAGES = int(os.environ.get("QT_FULL_MAX_PAGES", "150"))
TOPUP_MAX_PAGES = int(os.environ.get("QT_TOPUP_MAX_PAGES", "20"))
GAP_THRESHOLD_MONTHS = int(os.environ.get("QT_GAP_MONTHS", "2"))


def month_parts(symbol: str) -> list:
    d = LAKE_ROOT / symbol
    if not d.exists():
        return []
    return sorted(p.relative_to(d) for p in d.glob("*/*.parquet"))


def last_ym(parts: list):
    if not parts:
        return None
    p = parts[-1]
    return (int(p.parts[0]), int(p.stem))


def months_between(a, b) -> int:
    return (b[0] - a[0]) * 12 + (b[1] - a[1])


now = datetime.now(timezone.utc)
cur_ym = (now.year, now.month)

before = {s: month_parts(s) for s in SYMBOLS}
full_syms, topup_syms = [], []
for s in SYMBOLS:
    ly = last_ym(before[s])
    if ly is None or months_between(ly, cur_ym) > GAP_THRESHOLD_MONTHS:
        full_syms.append(s)
    else:
        topup_syms.append(s)

all_probes, all_errors = [], []
if full_syms:
    print(f"[신규/장기결측 {len(full_syms)}종목] max_pages={FULL_MAX_PAGES}: {' '.join(full_syms)}")
    probes, errors = fetch(full_syms, interval="1m", max_pages=FULL_MAX_PAGES, root=LAKE_ROOT)
    all_probes += probes
    all_errors += errors
if topup_syms:
    print(f"[기존 {len(topup_syms)}종목 갱신] max_pages={TOPUP_MAX_PAGES}: {' '.join(topup_syms)}")
    probes, errors = fetch(topup_syms, interval="1m", max_pages=TOPUP_MAX_PAGES, root=LAKE_ROOT)
    all_probes += probes
    all_errors += errors

before_total = sum(len(v) for v in before.values())
after_total = sum(len(month_parts(s)) for s in SYMBOLS)

fail_note = ""
if all_errors:
    fail_note = " (실패: " + ", ".join(e.split(":")[0] for e in all_errors) + ")"

summary = (
    f"KR 1분봉 주간 백필: 대상 {len(SYMBOLS)}종목(신규/복구 {len(full_syms)}·갱신 "
    f"{len(topup_syms)}) — 성공 {len(all_probes)}·실패 {len(all_errors)}{fail_note}, "
    f"신규 파티션 {after_total - before_total}개월치 추가. lake={LAKE_ROOT}"
)
print("SUMMARY " + summary)
for e in all_errors:
    print("ERR", e)
sys.exit(1 if all_errors else 0)
PYEOF
)"
FETCH_RC=$?
printf '%s\n' "$FETCH_OUT" >> "$LOG"

SUMMARY="$(printf '%s\n' "$FETCH_OUT" | grep '^SUMMARY ' | tail -1 | sed 's/^SUMMARY //')"
log "백필 종료: rc=${FETCH_RC}"

notify_defer "kr_minute_backfill" "${SUMMARY:-KR 1분봉 주간 백필 결과 파싱 실패(rc=${FETCH_RC}) — ${LOG} 확인}"

exit "$FETCH_RC"
