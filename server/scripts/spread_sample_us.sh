#!/usr/bin/env bash
# 호가창 스프레드 실측 수집 — US 정규장 전용, 레버리지 페어 앵커 강제 포함
# (2026-09-06 live-readiness §4). spread_sample.sh US와 크론 슬롯을 나눠
# 쓰지 않는다 — 이 스크립트가 그 자리를 대신한다(server/crontab.txt).
#
# 왜 spread_sample.sh US 만으로는 부족한가: 그 스크립트의 기본 대상(cmd_spread_sample)
# 은 워치리스트 + 전략의 top-level `symbols:` 목록이다. letf_pair_qqq는
# `symbols: [TQQQ, SQQQ]`라 우연히 잡히지만, letf_pair_sox는 `symbols: []`이고
# SOXL/SOXS는 `params.long_symbol`/`params.short_symbol`에만 있어(config/settings.yaml)
# 그 목록에 안 잡힌다. 이 네 심볼(TQQQ/SQQQ/SOXL/SOXS)은 슬리피지 실측
# (`cli slippage-report`)의 핵심 대상이라 전략 설정 구조와 무관하게 항상
# 명시적으로 강제한다 — `--extra-symbols`(quant.apps.cli cmd_spread_sample).
#
# 나머지는 spread_sample.sh와 동일한 계약: 측정 전용(거래 평면에 안 닿는다),
# 조용함(원장·로그에만 남고 텔레그램 없음), 5 TPS 셀프스로틀(Toss MARKET_DATA
# 상한을 엔진과 나눠 쓴다).
#
# 테스트: DRY_RUN=1 ./server/scripts/spread_sample_us.sh
set -u
cd "$(dirname "$0")/../.."

if [ "$(date +%z)" != "+0900" ]; then
  echo "[$(date '+%F %T')] 호스트 TZ 가 KST 가 아님($(date +%z)) — 중단" >&2
  exit 1
fi

PY=.venv/bin/python
LOG="data/spread.log"
mkdir -p data data/ledger
log() { echo "[$(date '+%F %T')] [US] $*" >> "$LOG"; }

# 레버리지 페어 앵커(letf_pair_qqq/letf_pair_sox) — config/settings.yaml 상단
# 표 참고. 워치리스트/전략 symbols: 목록과 무관하게 항상 포함한다.
ANCHORS=(TQQQ SQQQ SOXL SOXS)

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[DRY_RUN] spread-sample --market US --rounds 1 --extra-symbols ${ANCHORS[*]}"
  "$PY" -m quant.apps.cli spread-sample --market US --rounds 1 --extra-symbols "${ANCHORS[@]}"
  exit 0
fi

OUT="$(timeout 300 "$PY" -m quant.apps.cli spread-sample --market US --rounds 1 --extra-symbols "${ANCHORS[@]}" 2>>"$LOG")"
RC=$?
printf '%s\n' "$OUT" >> "$LOG"
log "종료 rc=$RC"
exit 0   # 측정 실패는 경보가 아니다 — 다음 10분에 다시 잰다
