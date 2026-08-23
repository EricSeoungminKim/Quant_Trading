#!/usr/bin/env bash
# 매일 15:45 KST — 네이버 거래상위 펀더멘털(시가총액/PER/ROE) 수집 (KR 정규장
# 마감 15:30 이후).
#
# 왜 이 스크립트가 존재하나 (2026-08-19):
# `quant/collect/sources/naver_quant.py`는 장타(가치·배당) 팩터 전략의 전제
# 데이터(PER/ROE/시가총액)를 이미 오랫동안 파싱만 하고 버려왔다 — fetch_and_persist
# 를 부르는 곳이 코드베이스에 없었다. `cli naver-fundamentals`가 그 진입점이고,
# 이 스크립트가 실제로 주기 실행되게 배선한다.
#
# 마감 후인 이유: 장중 값(PER/ROE/시가총액)은 계속 바뀌지만, append_ledger가
# (date, code) 기준으로 그날의 **첫 관측값**만 남기므로 장중에 여러 번 돌려도
# 소용없다 — 하루 한 번, 그날 최종 스냅샷에 가까운 값으로 고정한다.
#
# DART 재무제표(fundamentals_dart, 분기 단위로만 바뀜)와 주기가 달라 스크립트를
# 분리했다 — dart_fundamentals_collect.sh 는 주 1회면 충분하다.
#
# 실패해도 exit 0(다른 수집 스크립트와 같은 원칙, dart_collect.sh 참고) — 결과는
# 로그에만 남긴다. 이 배치를 막는 다운스트림 파이프라인이 없어 알림도 필요 없다.
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
LOG="data/naver_fundamentals.log"

mkdir -p data
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

log "네이버 펀더멘털 수집 시작"
OUT="$("$PY" -m quant.apps.cli naver-fundamentals --root . 2>&1)"
RC=$?
printf '%s\n' "$OUT" >> "$LOG"
log "네이버 펀더멘털 수집 종료 rc=$RC"
exit 0
