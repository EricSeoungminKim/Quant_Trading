#!/usr/bin/env bash
# 신입사원 AI 트레이더(수습) — KR 08:20 / US 20:10 KST (2026-08-26 소유자 지시).
#
# 그날 아침/개장전 리포트가 selections 원장에 남긴 서류(기존 직원 watch_scorer
# 가 본 것과 동일한 속성 벡터)를 읽고, 3역할 LLM 토론으로 판단만 기록한다.
# 주문·워치리스트에 닿지 않는다 — 성적은 outcomes(16:00)→리더보드(16:20
# 장마감 리포트)가 매긴다. 승격은 리더보드 promote 판정 + 사람 결정.
#
# **조용한 것이 기본값이다** (experiments_daily.sh 와 같은 관례): 픽이 없거나
# LLM 결근이면 stdout 이 비고, 텔레그램도 안 나간다. 잡 자체의 생존은
# cli health 하트비트가 따로 본다.
set -u
cd "$(dirname "$0")/../.."

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"

if [ "$(date +%z)" != "+0900" ]; then
  echo "[$(date '+%F %T')] 호스트 TZ 가 KST 가 아님($(date +%z)) — 중단" >&2
  exit 1
fi

MARKET="${1:?사용법: ai_trader.sh KR|US}"

# LLM 3회 호출 — 무료 레인이 느릴 수 있어 넉넉히. 실패해도 판단이 안 남을 뿐
# 다른 시스템에 영향 없다(결근).
OUT="$(timeout 420 .venv/bin/python -m quant.apps.cli ai-trader --market "$MARKET" 2>/dev/null)"
RC=$?

if [ "$RC" -ne 0 ]; then
  echo "[$(date '+%F %T')] $MARKET 실패 exit=$RC (결근 처리 — 판단 미기록)"
  exit 0   # 수습의 결근은 경보가 아니다 — 리더보드가 판단 있는 날만 센다
fi

if [ -z "$OUT" ]; then
  echo "[$(date '+%F %T')] $MARKET 픽 없음/결근 — 침묵"
  exit 0
fi

echo "[$(date '+%F %T')] $MARKET 픽 발생:"
echo "$OUT"

if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ]; then
  curl -s -m 10 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    -d "chat_id=${TG_CHAT}" --data-urlencode "text=${OUT}" >/dev/null 2>&1 || true
fi
