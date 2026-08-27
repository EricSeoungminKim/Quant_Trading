#!/usr/bin/env bash
# 자본 자동 강등 — KR 마감 정산 뒤 16:50 KST (2026-08-28 배선, 소유자 북극성:
# "지는 곳에서 자본을 빼면 포트폴리오는 매일 나아진다").
#
# quant/control/allocator.py 는 순수 로직(4층 방어: 증거·하한·냉각·한 방향)이고,
# `cmd_capital_review`(quant/apps/cli.py)가 원장을 읽어 config/auto_params.yaml
# 오버레이에 반영한다 — governor.sh 와 같은 골격(TZ 가드, _env, 텔레그램).
#
# **처음에는 --dry-run 으로 돈다.** 관찰 기간 동안 로그/텔레그램으로 "무엇을
# 강등했을지"를 지켜보고, 사람이 그 판단을 신뢰하게 되면 이 스크립트에서
# --dry-run 을 뗀다. 그 전환은 사람이 한다 — 자동으로 승격되지 않는다.
#
# **강등이 실제로 일어났을 때만** 텔레그램을 보낸다 — 무변경은 침묵(다른
# 크론과 같은 관례). 스킵(냉각/하한)만 있었던 날도 capital_decisions.jsonl에는
# 남지만 텔레그램 스팸은 아니다.
set -u
cd "$(dirname "$0")/../.."

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"

if [ "$(date +%z)" != "+0900" ]; then
  echo "[$(date '+%F %T')] 호스트 TZ 가 KST 가 아님($(date +%z)) — 중단" >&2
  exit 1
fi

OUT="$(timeout 60 .venv/bin/python -m quant.apps.cli capital-review --dry-run 2>>data/capital_review.log)"
RC=$?

if [ "$RC" -ne 0 ]; then
  echo "[$(date '+%F %T')] 실패 exit=$RC"
  exit "$RC"
fi

if [ -z "$OUT" ]; then
  echo "[$(date '+%F %T')] 강등 없음 — 조용히 대기"
  exit 0
fi

echo "[$(date '+%F %T')] 강등 발생:"
echo "$OUT"

if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ]; then
  curl -s -m 10 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    -d "chat_id=${TG_CHAT}" --data-urlencode "text=${OUT}" >/dev/null 2>&1 || true
fi
