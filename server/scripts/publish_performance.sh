#!/usr/bin/env bash
# 공개 포트폴리오 웹사이트용 성과 JSON 생성 — data/public/performance.json.
# 사용법: server/scripts/publish_performance.sh
# 크론:  KR 20 16 * * 1-5 (마감 후) / US 20 6 * * 2-6 (마감 후)
#
# 입력은 거래 원장(data/state/trades.jsonl) 하나뿐 — 종목/포지션/계좌 잔고
# 절대값은 출력에 없다(quant/control/performance.py 계약). 계산은
# `quant.apps.cli publish-performance`(제어 평면, 결정론적) — LLM/네트워크
# 호출 없음.
#
# **git push는 하지 않는다** — 공개 저장소 배선이 끝나기 전까지는 로컬(EC2)
# data/public/에만 쓴다. 그 배선이 끝나면 이 스크립트에 push 단계를 추가한다.
set -u
cd "$(dirname "$0")/../.."

LOG="data/publish_performance.log"
mkdir -p data data/public

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

# TZ 가드 — 크론 시각(KR 16:20 / US 06:20)은 호스트가 KST 라는 전제다.
# daily_wrap.sh 와 같은 계약: 어긋나도 **중단하지 않는다** — 성과 JSON은
# 하루 늦게라도 만드는 편이 아예 안 만드는 것보다 낫다.
if [ "$(date +%z)" != "+0900" ]; then
  log "호스트 TZ 가 KST 가 아님($(date +%z)) — 크론 시각 전제가 깨졌을 수 있음(계속 진행)"
fi

OUT_PATH="data/public/performance.json"
if timeout 60 .venv/bin/python -m quant.apps.cli publish-performance --out "$OUT_PATH" >> "$LOG" 2>&1; then
  log "성과 JSON 생성 성공 — $OUT_PATH"
else
  # 실패는 로그만 — 리포팅 실패가 거래 엔진과 무관한 소음 알림을 만들면 안 된다
  # (daily_wrap.sh 와 같은 원칙). 잡 생존은 cli health 하트비트가 별도로 본다.
  log "성과 JSON 생성 실패 exit=$? — 이전 파일 유지"
fi
exit 0
