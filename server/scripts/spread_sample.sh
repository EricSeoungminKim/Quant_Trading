#!/usr/bin/env bash
# 호가창 스프레드 실측 수집 — KR/US 정규장 중 10분 간격 (2026-08-28 신규).
#
# 왜: 스캘핑 전략의 비용 가정(`slippage_bps: 2.5`, "왕복 20bp")이 전부 추정치다.
# 비용이 엣지보다 크면 스캘핑은 전부 무의미하므로, 알파를 찾기 전에 이 숫자를
# 실측으로 바꾼다. 측정 전용 — 주문·워치리스트·거래 평면에 닿지 않는다.
#
# **조용하다.** ai_trader.sh 는 픽을 텔레그램으로 보내고 macro_collect.sh 는
# 실패 시 보내지만, 이 잡은 둘 다 하지 않는다. 10분마다 도는 측정 잡이 말을
# 하기 시작하면 알림이 무의미해진다 — 결과는 원장(data/ledger/spread.jsonl)과
# 로그(data/spread.log)에만 남고, 잡의 생존은 원장 신선도로 사람이 확인한다.
#
# 부하: 라운드당 심볼 1회 조회 + 호출 간 1초(CLI 기본 --interval-seconds). Toss
# MARKET_DATA 상한 10 TPS 를 엔진과 나눠 쓰므로 CLI 가 스스로 5 TPS 로 자른다
# (quant/apps/cli.py cmd_spread_sample docstring 참고).
#
# 테스트: DRY_RUN=1 ./server/scripts/spread_sample.sh KR
set -u
cd "$(dirname "$0")/../.."

if [ "$(date +%z)" != "+0900" ]; then
  echo "[$(date '+%F %T')] 호스트 TZ 가 KST 가 아님($(date +%z)) — 중단" >&2
  exit 1
fi

MARKET="${1:?사용법: spread_sample.sh KR|US}"
case "$MARKET" in
  KR|US) ;;
  *) echo "사용법: spread_sample.sh KR|US" >&2; exit 2 ;;
esac

PY=.venv/bin/python
LOG="data/spread.log"
mkdir -p data data/ledger
log() { echo "[$(date '+%F %T')] [$MARKET] $*" >> "$LOG"; }

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[DRY_RUN] spread-sample --market $MARKET --rounds 1"
  "$PY" -m quant.apps.cli spread-sample --market "$MARKET" --rounds 1
  exit 0
fi

OUT="$(timeout 300 "$PY" -m quant.apps.cli spread-sample --market "$MARKET" --rounds 1 2>>"$LOG")"
RC=$?
printf '%s\n' "$OUT" >> "$LOG"
log "종료 rc=$RC"
exit 0   # 측정 실패는 경보가 아니다 — 다음 10분에 다시 잰다
