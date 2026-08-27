#!/usr/bin/env bash
# 토 06:50 KST — 파라미터 자동 반영 거버너 (2026-08-28 배선).
#
# param-propose(06:40) 직후, 그 주 제안이 원장에 쌓인 시점에 돈다. `governor.py`
# 는 이미 완성돼 있었지만(6층 방어 + ALLOWED/FORBIDDEN) 이걸 부르는 프로덕션
# 코드가 없어서 제안이 나와도 아무도 심사·반영하지 않았다 — 이 크론이 그 마지막
# 칸을 채운다.
#
# **처음에는 --dry-run 으로 돈다.** 관찰 기간(수 주) 동안 텔레그램/로그로
# "무엇을 반영했을지"를 지켜보고, 사람이 그 판단을 신뢰하게 되면 이 스크립트에서
# --dry-run 을 떼서 실반영으로 전환한다. 그 전환은 사람이 한다 — 자동으로
# 승격되지 않는다.
#
# 결정이 있을 때만(수락 1건 이상 또는 롤백) 텔레그램을 보낸다 — 다른 크론과
# 같은 관례(조용한 것이 기본값).
set -u
cd "$(dirname "$0")/../.."

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"

if [ "$(date +%z)" != "+0900" ]; then
  echo "[$(date '+%F %T')] 호스트 TZ 가 KST 가 아님($(date +%z)) — 중단" >&2
  exit 1
fi

OUT="$(timeout 60 .venv/bin/python -m quant.apps.cli governor-apply --dry-run 2>>data/governor.log)"
RC=$?

if [ "$RC" -ne 0 ]; then
  echo "[$(date '+%F %T')] 실패 exit=$RC"
  exit "$RC"
fi

if [ -z "$OUT" ]; then
  echo "[$(date '+%F %T')] 결정 없음(수락 0건) — 조용히 대기"
  exit 0
fi

echo "[$(date '+%F %T')] 결정 발생:"
echo "$OUT"

if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ]; then
  curl -s -m 10 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    -d "chat_id=${TG_CHAT}" --data-urlencode "text=${OUT}" >/dev/null 2>&1 || true
fi
