#!/usr/bin/env bash
# 매일 07:20 KST — DART 공시 수집 (KR 리포트 07:50 발행 전).
#
# 왜 이 스크립트가 존재하나 (2026-08-16, 서브프로젝트 H-2):
# 공시는 뉴스보다 정확한 팩트 이벤트다 — 배당·잠정실적·유증 등은 기사보다
# 원문 공시가 먼저 나오고 해석의 여지가 없다. 네이버 공시 페이지는 robots.txt
# 전면 비허용이라(스펙 docs/superpowers/specs/2026-08-16-news-scale-design.md
# 실측) 스크레이핑 경로가 없고, 공식 DART OpenAPI 가 유일한 접근로다.
#
# report_cli dart-collect 는 항상 exit 0(실패해도 리포트 파이프라인을 막지
# 않는다) — 이 스크립트도 같은 원칙으로 결과를 로그에만 남긴다.
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
LOG="data/dart.log"

mkdir -p data
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

log "DART 공시 수집 시작"
OUT="$("$PY" -m quant.apps.report_cli dart-collect --market KR --root . 2>&1)"
RC=$?
printf '%s\n' "$OUT" >> "$LOG"
log "DART 공시 수집 종료 rc=$RC"
exit 0
