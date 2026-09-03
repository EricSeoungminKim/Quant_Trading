#!/usr/bin/env bash
# KR 대형주(시총 ≥3,000억, 유니버스 상위 ~300종목) 일봉 백필 — 스윙 시그널
# (단기반전/거래량충격, quant/analyze/swing_signals.py) 전제 데이터.
#
# quant/collect/kr_largecap_daily.py 가 전부 한다(모듈 docstring 참고):
# 유니버스 캐시(data/state/kr_largecap_universe.json)가 없거나 30일 넘게
# 낡았으면 시총 재계산부터(느림 — Toss stock_info 심볼당 1회, STOCK 그룹
# 5 TPS) 하고, 아니면 캐시를 그대로 읽어 그 심볼들의 일봉만 증분 백필한다
# (빠름 — backfill()의 gap-only 재조회, 대부분의 날 이 경로).
#
# 이 스크립트는 `cli fetch` 를 심볼마다 별도 프로세스로 부르지 않는다
# (backfill_kr_stock_daily.sh 와 달리) — kr_largecap_daily.py 가 한 프로세스
# 안에서 ~300종목을 순회해 TossClient 레이트리미터 상태를 공유한다(그 스크립트
# 자신의 지적: 프로세스 경계마다 리미터가 리셋되는 낭비).
#
# **크론 예산 경고**: 첫 실행(캐시 없음)이나 30일마다의 재계산일은 시총 계산만
# 500초 넘게 걸릴 수 있다 — 15:36~manual_recs(15:50) 예산(840초)을 넘기면
# `timeout` 이 죽이지만, backfill()이 심볼 단위 멱등이라 다음날 실행이 이어서
# 받는다(kr_largecap_daily.py 모듈 docstring). manual_recs 자체는 이 스크립트의
# 결과와 무관하게(유니버스 없으면 그 두 프로듀서만 조용히 0건) 항상 정상 실행된다.
#
# 사용법: server/scripts/backfill_kr_largecap_daily.sh [depth_years]
# 크론: 15:36 KST 월~금 (정규장 마감 15:30 직후, manual_recs KR 15:50 전 —
# 2026-09-03 이 배선과 함께 15:40→15:50 으로 미뤘다, server/crontab.txt 참고).
#
# 테스트: DRY_RUN=1 ./server/scripts/backfill_kr_largecap_daily.sh
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
LOG="data/fetch_kr_largecap_daily.log"
DEPTH_YEARS="${1:-2}"

mkdir -p data
_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
. "$(dirname "$0")/lib/notify.sh"
. "$(dirname "$0")/lib/memlog.sh"
memlog_wrap "backfill_kr_largecap_daily"

DRY_RUN_FLAG=""
if [ "${DRY_RUN:-0}" = "1" ]; then
  DRY_RUN_FLAG="--dry-run"
fi

OUT="$(timeout 700 "$PY" -m quant.collect.kr_largecap_daily --depth-years "$DEPTH_YEARS" $DRY_RUN_FLAG 2>>"$LOG")"
RC=$?
printf '%s\n' "$OUT" >> "$LOG"

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "$OUT"
  exit 0
fi

# 정상일 때는 조용하다(다른 백필 크론과 동일 관례, notify_defer — 요약·정보성이라
# 텔레그램으로는 절대 나가지 않는다, 마감 HTML 로만) — 실패했을 때만 알린다.
if [ "$RC" -ne 0 ]; then
  notify_defer "backfill_kr_largecap_daily" "🚨 KR 대형주 일봉 백필 실패(rc=${RC}) — ${LOG} 확인
$(printf '%s\n' "$OUT" | tail -5)"
  exit 1
fi
exit 0
