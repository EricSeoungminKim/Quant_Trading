#!/usr/bin/env bash
# 매일 16:30 KST — 자동 판정 루프 (2026-08-24).
#
# 소유자가 다른 프로젝트로 자리를 비우는 동안, 이 저장소가 스스로 판정해서
# 먼저 알려주기 위한 잡이다. "바꿨다 → 데이터가 쌓인다 → **누가 물어봐야**
# 판정된다"였던 것을 "→ 자동으로 판정이 온다"로 바꾼다.
#
# **조용한 것이 기본값이다.** `cli experiments` 는 판정할 게 없으면 아무것도
# 출력하지 않고, 이 스크립트는 stdout 이 비면 텔레그램을 보내지 않는다.
# 매일 "아직 모릅니다"를 보내면 사람이 안 읽게 되고, 안 읽는 알림은 없는 것보다
# 나쁘다 — 진짜 경보(전략 사망)까지 같이 묻히기 때문이다.
#
# KR 마감(15:30) 후이고 마감 리포트(13:50)·스코어보드(금 16:10)와 겹치지 않는
# 시각이다. 매일 도는 이유: 판정은 표본이 차는 순간 나와야지, 주 단위로 미루면
# 최대 6일 늦는다.
set -u
cd "$(dirname "$0")/../.."

# 마지막으로 알린 판정 집합의 해시 (중복 방지 — ops_watch.sh 와 같은 관례).
# 사망 경보는 상황이 바뀔 때까지 매일 같은 내용이 나온다. 그걸 매일 보내면
# **사장님이 읽지 않게 되고**, 그때부터 이 루프는 없는 것보다 나쁘다(새 판정이
# 옛 경보 사이에 묻힌다). 내용이 바뀔 때만 보낸다.
STATE="data/state/experiments.state"

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
# 알림은 notify_defer (역할별 게이트 — server/scripts/lib/notify.sh): 요약·정보성
# 이라 텔레그램으로는 **절대 나가지 않는다**. data/notify_queue.jsonl 에 쌓여
# 마감 HTML 리포트로만 간다.
. "$(dirname "$0")/lib/notify.sh"
NOTIFY_LANE="briefs"  # 텔레그램 포럼 토픽 레인 — docs/runbooks/telegram-rooms.md

# TZ 가드 — 이 잡의 시각 판단(오늘 날짜, 변경일 경계)이 전부 KST 전제다.
if [ "$(date +%z)" != "+0900" ]; then
  echo "[$(date '+%F %T')] 호스트 TZ 가 KST 가 아님($(date +%z)) — 중단" >&2
  exit 1
fi

OUT="$(timeout 300 .venv/bin/python -m quant.apps.cli experiments 2>/dev/null)"
RC=$?

if [ "$RC" -ne 0 ]; then
  # 실패는 알린다 — 판정 루프가 조용히 죽으면 "판정할 게 없다"와 구분되지 않는다.
  # (이 잡 자체의 하트비트는 cli health 의 jobs 목록이 따로 본다.)
  notify_defer "experiments_daily" "⚠️ 자동 판정 잡 실패 (exit ${RC}) — data/experiments.log 확인"
  echo "[$(date '+%F %T')] 실패 exit=$RC"
  exit "$RC"
fi

if [ -z "$OUT" ]; then
  echo "[$(date '+%F %T')] 판정 없음 — 조용히 대기"
  exit 0
fi

echo "[$(date '+%F %T')] 판정 발생:"
echo "$OUT"

# 날짜 줄(맨 위 헤더)은 매일 바뀌므로 해시에서 뺀다 — 안 빼면 중복 방지가 전혀
# 작동하지 않는다(매일 다른 해시).
HASH="$(printf '%s' "$OUT" | grep -v '^🧪 자동 판정' | sha256sum | cut -c1-16)"

if [ -f "$STATE" ] && [ "$(cat "$STATE")" = "$HASH" ] && [ "${FORCE_SEND:-0}" != "1" ]; then
  echo "[$(date '+%F %T')] 같은 판정 집합($HASH) — 알림 생략"
  exit 0
fi

notify_defer "experiments_daily" "${OUT}"
mkdir -p "$(dirname "$STATE")"
printf '%s' "$HASH" > "$STATE"
echo "[$(date '+%F %T')] 알림 전송 (해시 $HASH)"
