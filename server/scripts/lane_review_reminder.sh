#!/usr/bin/env bash
# 관찰 레인 판정일 알림(2026-09-07 소유자 결정 1 적용) — config/settings.yaml 의
# `strategies.<id>.validation.review_by` 가 오늘 이하인 enabled 전략을 골라 제어실
# 레인에 한 줄 보낸다. 크론: 매일 08:00 KST(server/crontab.txt). 해당 없으면 무발송.
# 판정 자체는 사람이 한다(`run scoreboard` 에폭 절, 순 기대값 ≤ 0 또는 n < 20 → 비활성).
set -u
cd "$(dirname "$0")/../.."
_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
. "$(dirname "$0")/lib/notify.sh"
NOTIFY_LANE="control"
DUE="$(.venv/bin/python - <<'PY'
import datetime, yaml
today = datetime.date.today()
cfg = yaml.safe_load(open("config/settings.yaml", encoding="utf-8"))
due = []
for sid, c in (cfg.get("strategies") or {}).items():
    v = (c or {}).get("validation") or {}
    rb = v.get("review_by")
    if c.get("enabled") and rb and datetime.date.fromisoformat(str(rb)) <= today:
        due.append(f"{sid}({rb})")
print(" ".join(due))
PY
)"
[ -z "$DUE" ] && exit 0
notify_now "lane_review" "📋 관찰 레인 판정일 — $DUE
에폭 이후 스코어보드(run scoreboard)로 판정: 순 기대값 ≤ 0 또는 n < 20 이면 enabled: false. 판정 전엔 계속 관찰."
