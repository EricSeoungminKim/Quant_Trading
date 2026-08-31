#!/usr/bin/env bash
# 장마감 결과 리포트 — outcomes(16:00) 뒤, 그날 채워진 outcome 을 요약해 텔레그램으로.
# 사용법: server/scripts/close_report.sh
# 크론: 16:20 KST (outcomes 16:00 직후 — 그날 채워진 값을 읽는다).
set -u
cd "$(dirname "$0")/../.."

LOG="data/close_report.log"
mkdir -p data

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
# 알림은 전부 notify_defer (역할별 게이트 — server/scripts/lib/notify.sh):
# 요약·정보성이라 텔레그램으로는 **절대 나가지 않는다**. data/notify_queue.jsonl
# 에 쌓여 마감 HTML 리포트로만 간다.
. "$(dirname "$0")/lib/notify.sh"
# 메모리 워터마크(2026-08-31, 2GB 박스 관측) — server/scripts/lib/memlog.sh 참고.
. "$(dirname "$0")/lib/memlog.sh"
memlog_wrap "close_report"

# TZ 가드 — 크론 시각(16:20)은 호스트가 KST라는 전제다.
if [ "$(date +%z)" != "+0900" ]; then
  notify_defer "close_report" "⚠️ close_report: 호스트 TZ가 KST가 아님($(date +%z)) — 크론이 마감 직후가 아닐 수 있음"
fi

# timeout 200 — ops_watch.sh의 narrate 호출과 같은 창(로컬 Claude CLI 서술기
# 기본 180s가 이 EC2에서 최악 실측 있었다 — deepdive 크론 주석 참고). close-report
# 는 결정론 요약을 narrate 호출 **전에** flush 해서 찍으므로(cmd_close_report),
# 이 timeout 을 넘겨 SIGTERM 이 나도 $OUT 에는 이미 flush 된 요약이 남는다 —
# 아래 "$OUT 이 비었을 때만 실패로 본다"는 분기가 그 부분 출력을 성공으로 취급한다.
OUT="$(timeout 200 .venv/bin/python -m quant.apps.cli close-report 2>>"$LOG")"
if [ -n "$OUT" ]; then
  notify_defer "close_report" "${OUT:0:3900}"
else
  notify_defer "close_report" "close-report 생성 실패 — ${LOG} 확인"
fi
