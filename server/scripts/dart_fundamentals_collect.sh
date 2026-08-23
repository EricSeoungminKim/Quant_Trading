#!/usr/bin/env bash
# 매주 일요일 15:50 KST — DART 재무제표(부채비율/ROE/BPS) 수집.
#
# 왜 이 스크립트가 존재하나 (2026-08-19):
# `quant/collect/sources/dart_financials.py`는 장타(가치·배당) 팩터 전략의 전제
# 데이터(부채비율/ROE/BPS)를 계산하는 fetch_and_persist를 갖고 있었지만 아무도
# 부르지 않았다. `cli dart-fundamentals`가 그 진입점이고, 이 스크립트가 실제로
# 주기 실행되게 배선한다.
#
# 왜 주 1회인가: DART 재무제표는 분기 실적 발표 주기로만 바뀐다(연 4회) — 매일
# 부르면 사실상 같은 응답을 반복 조회해 API 할당량만 소모한다. naver_fundamentals_
# collect.sh(매일, PER/ROE/시가총액 — 장중 시세와 함께 바뀜)와 주기가 다른 이유다.
#
# 왜 일요일인가: KR 정규장이 없어 거래 엔진과 자원 경쟁이 없고, 재무제표는 날짜에
# 민감하지 않아(발표된 사업보고서 값은 요일과 무관) 아무 요일이나 무방하다.
# 기존 크론(일 03:00 로그정리, 04:00 백업 복원 리허설)과 15:50은 겹치지 않는다.
#
# 대상: watchlist.yaml의 KR 종목(cli 기본 동작 — SYMBOLS 미지정 시). 로컬처럼
# watchlist.yaml이 없는 환경에서는 SYMBOLS 환경변수로 직접 넘길 수 있다(예:
# SYMBOLS="005930 000660" ./server/scripts/dart_fundamentals_collect.sh).
#
# 실패해도 exit 0(다른 수집 스크립트와 같은 원칙, dart_collect.sh 참고) — 종목별
# 실패는 cli dart-fundamentals가 원장에 "오류" 목록으로 이미 남기고 나머지 종목은
# 계속 처리한다(dart_financials.fetch_and_persist 계약). 결과는 로그에만 남긴다.
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
LOG="data/dart_fundamentals.log"

mkdir -p data
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

ARGS=(--root .)
if [ -n "${SYMBOLS:-}" ]; then
  ARGS+=(--symbols "$SYMBOLS")
fi

log "DART 펀더멘털 수집 시작"
OUT="$("$PY" -m quant.apps.cli dart-fundamentals "${ARGS[@]}" 2>&1)"
RC=$?
printf '%s\n' "$OUT" >> "$LOG"
log "DART 펀더멘털 수집 종료 rc=$RC"
exit 0
