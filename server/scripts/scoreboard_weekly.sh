#!/usr/bin/env bash
# 매주 금요일 16:10 KST — 최근 7일 + 누적 스코어보드 + **거래 부검**을 텔레그램으로.
# KR 마감(15:30) 후, 사용자가 주말에 자본 배분을 판단할 수 있는 시점.
#
# 부검(forensics)을 같이 보내는 이유(2026-08-22): 스코어보드는 "졌다"까지만
# 말한다. 2026-08-21에 그 다음 질문("진입이 문제냐 청산이 문제냐")에 답한 건
# 손으로 쓴 1회용 스크립트였다 — 1분봉에 체결을 얹어 MFE/MAE를 재니 이익
# 구간(MFE 중앙 +113bp)에 들어갔다가 전부 반납(실현 -47bp)하고 있었고, 진입
# 위치는 승패를 가르지 못했다(rho=+0.00). 그 분석이 1회용이면 다음에도 손으로
# 다시 해야 한다. 매주 같은 자리에서 같은 방식으로 나오게 붙인다.
set -u
cd "$(dirname "$0")/../.."

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
# 알림은 전부 notify_defer (역할별 게이트 — server/scripts/lib/notify.sh):
# 요약·정보성이라 텔레그램으로는 **절대 나가지 않는다**. data/notify_queue.jsonl
# 에 쌓여 마감 HTML 리포트로만 간다.
. "$(dirname "$0")/lib/notify.sh"

WEEK="$(timeout 60 .venv/bin/python -m quant.apps.cli scoreboard --days 7 2>/dev/null)"
ALL="$(timeout 60 .venv/bin/python -m quant.apps.cli scoreboard 2>/dev/null)"
notify_defer "scoreboard_weekly" "${WEEK:-주간 스코어보드 생성 실패}

${ALL:-누적 스코어보드 생성 실패}"

# 부검은 1분봉을 종결마다 읽어 스코어보드보다 오래 걸린다(실측 106건 ~10초).
# 실패해도 스코어보드 발송은 이미 끝났으므로 영향이 없다 — 별도 메시지로 보낸다.
FORENSICS="$(timeout 180 .venv/bin/python -m quant.apps.cli forensics 2>/dev/null)"
notify_defer "scoreboard_weekly" "${FORENSICS:-거래 부검 생성 실패 — data/scoreboard.log 확인}"

# 자본 곡선 성과(2026-08-24, gs-quant 대조 도입) — 거래 단위(bps)가 아니라
# 자본 단위(변동성·샤프·MDD). 곡선 점이 5개 미만이면 "표본 부족"이 그대로 간다.
PERF="$(timeout 60 .venv/bin/python -m quant.apps.cli performance 2>/dev/null)"
notify_defer "scoreboard_weekly" "${PERF:-자본 곡선 성과 생성 실패}"
