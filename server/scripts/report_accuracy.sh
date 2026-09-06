#!/usr/bin/env bash
# 리포트 정확도 스코어카드 — 데일리 마켓 리포트가 낸 방향콜/종목 후보 청구가
# 실제로 맞았는지 매일 밤 채점해 텔레그램에 (2026-09-06 소유자 지시 priority-1).
# 사용법: server/scripts/report_accuracy.sh
#
# `report accuracy`(기본 --since 30일 전 --until 오늘)는 만기된 청구만 채점하고
# 항상 스코어카드를 낸다(daily_feedback.sh 처럼 "오늘 없으면 조용히" 가 아니다
# — n<20 인 지평은 표 안에서 "판단 불가"로 스스로 표시하므로 빈 출력이 없다).
# 원장 append(`data/ledger/report_accuracy.jsonl`)는 CLI 쪽에서 매 실행마다
# 한 행씩 쌓는다(멱등 아님 — 스코어카드 이력 자체가 값이다, report_claims.jsonl
# 만 청구 원장으로 멱등).
#
# 06:40 KST — 그날 KR 마감(15:30)·US 마감(05:00 EDT/06:00 EST)이 이미 지났고,
# 다음날 KR 리포트(08:00)보다 앞선다.
set -u
cd "$(dirname "$0")/../.."

LOG="data/report_accuracy.log"
mkdir -p data

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
. "$(dirname "$0")/lib/notify.sh"
NOTIFY_LANE="briefs"  # 텔레그램 포럼 토픽 레인 — docs/runbooks/telegram-rooms.md

OUT="$(timeout 300 .venv/bin/python -m quant.apps.report_cli accuracy 2>>"$LOG")"
RC=$?

if [ "$RC" -ne 0 ]; then
  notify_auto "report_accuracy" "⚠️ 리포트 정확도 채점 실패 (exit ${RC}) — ${LOG} 확인"
  echo "[$(date '+%F %T')] 실패 exit=$RC"
else
  echo "$OUT"
  notify_auto "report_accuracy" "${OUT:0:3900}"
fi

# 회고 카드 자동화(2026-09-06, Phase 6 재발 방지 루프) — 정확도 스코어카드
# 직후, 성숙한 세션(전날 KR 오전/마감·전날 US 오전)의 날짜별 회고 카드를
# 만들어 텔레그램 브리핑 레인에 한 줄씩 보낸다. **이 파일의 성패와 무관하게**
# 항상 시도한다(2026-09-07 수정 — 예전엔 위 accuracy 채점이 실패하면 여기
# 도달하기 전에 `exit "$RC"`로 먼저 빠져나가 이 주석의 약속을 코드가 어기고
# 있었다: 채점기가 넘어진 날 회고 카드까지 통째로 못 도는 사고). 독립 로그
# (data/report_review.log)로 실패를 격리한다 — 위 정확도 스코어카드 발송은
# 이미 끝난 뒤라 아래에서 뭐가 터져도 위 알림엔 영향이 없다(`|| true`).
./server/scripts/report_review_daily.sh >> data/report_review.log 2>&1 || true

exit "$RC"
