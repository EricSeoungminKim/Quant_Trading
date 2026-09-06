#!/usr/bin/env bash
# 리포트 날짜별 회고 자동화 — 재발 방지 루프(2026-09-06 소유자 지시, 리포트·
# 텔레그램·사이트 실전화 계획 Phase 6). 1단계(results/report_review/의 61장,
# 사람이 손으로 감사)를 매일 자동으로 반복해 원장(data/ledger/report_review.jsonl)
# 에 쌓고, 텔레그램 브리핑 레인에 한 줄씩 보낸다. `report_accuracy.sh`가 이
# 스크립트를 끝에서 부른다(정확도 스코어카드 직후 — "after the 06:40 accuracy
# job") — 이 파일 자체를 크론에 별도로 등록하지 않는다(주간 모드는 예외, 아래).
#
# 사용법:
#   server/scripts/report_review_daily.sh          # 일일 — 최근 며칠(재시도
#                                                    #   창)의 세 성숙 세션을 훑는다.
#   server/scripts/report_review_daily.sh weekly    # 주간 — 금 16:20 크론 전용,
#                                                    #   그 주 원장 행을 집계.
#
# ## 왜 "최근 며칠"을 매번 훑나 (멱등 + 재시도, 별도 상태 없이)
#
# `review-daily` 서브커맨드(quant/apps/report_cli.py::cmd_review_daily)는 카드
# 파일이 이미 있으면 네트워크 호출 전에 즉시 스킵한다 — 그래서 매일
# `${LOOKBACK_DAYS:-3}`일을 다시 훑어도 이미 성공한 날짜는 사실상 공짜(파일
# 존재 확인 한 번)다. 반대로 어제 시세 조회가 야후 레이트리밋 등으로 실패해
# 카드를 못 썼다면(그 경우 CLI가 파일을 쓰지 않는다 — "결측을 0으로 위장하지
# 않는다"는 원칙의 연장, quant/control/report_review.py 모듈 docstring) 그
# 날짜가 이 창 안에 남아 있는 한 다음날 자동으로 재시도된다 — 재시도 여부를
# 추적하는 상태 파일이 따로 없다.
#
# ## 세 개의 성숙 세션 (고정)
#
# 이 스크립트가 도는 시각(D, KST 아침, report_accuracy.sh 뒤라 06:4x)엔 "어제"
# (D-1)의 KR 오전판·KR 마감판은 이미 그날 정규장이 끝났고(15:30 KST), D-1
# 저녁에 시작한 US 오전판 세션도 자정을 넘겨 D 새벽에 마감한다 — 셋 다 D
# 아침엔 실현가가 있다(report_accuracy.sh 상단 주석의 "2-6" 요일 근거와 같은
# 이유). 카드 자체는 "그날 자기 세션"(D+0 open→close)만 있으면 렌더 가능하고,
# D+1 종가(다음날 참고용 필드)는 없으면 카드 안에서 "결측"으로 그대로 나간다
# (report_review.build_card 가 이미 그렇게 설계돼 있다) — 이 스크립트가 그것
# 때문에 하루 더 기다리지 않는다.
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
LOG="data/report_review.log"
mkdir -p data

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
. "$(dirname "$0")/lib/notify.sh"
NOTIFY_LANE="briefs"  # 텔레그램 포럼 토픽 레인 — docs/runbooks/telegram-rooms.md

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

if [ "${1:-}" = "weekly" ]; then
  OUT="$(timeout 60 "$PY" -m quant.apps.report_cli review-daily --weekly 2>>"$LOG")"
  RC=$?
  if [ "$RC" -ne 0 ]; then
    log "주간 회고 실패(rc=$RC)"
    exit 0
  fi
  if [ -n "$OUT" ]; then
    log "주간 회고 — 발송"
    notify_auto "report_review_weekly" "$OUT"
  else
    log "주간 회고 — 집계할 행 없음(무출력)"
  fi
  exit 0
fi

# 일일 — 최근 며칠(재시도 창) × 세 성숙 세션(KR open/close, US open).
#
# `date -d`(GNU, EC2)를 우선 쓰고 실패하면 `date -v`(BSD, 로컬 Mac 개발/테스트)
# 로 넘어간다 — server/CLAUDE.md 불변식(EC2는 GNU 전제)을 깨지 않으면서 이
# 스크립트를 로컬에서도 손으로 검증할 수 있게 한다.
_days_ago() { date -d "-${1} day" +%F 2>/dev/null || date -v-"${1}"d +%F; }

LOOKBACK_DAYS="${LOOKBACK_DAYS:-3}"
for i in $(seq 1 "$LOOKBACK_DAYS"); do
  D="$(_days_ago "$i")"
  for spec in "KR:open" "KR:close" "US:open"; do
    MARKET="${spec%%:*}"
    SESSION="${spec##*:}"
    OUT="$(timeout 120 "$PY" -m quant.apps.report_cli review-daily \
      --market "$MARKET" --date "$D" --session "$SESSION" 2>>"$LOG")"
    RC=$?
    if [ "$RC" -ne 0 ]; then
      log "$MARKET $D $SESSION — 실패(rc=$RC)"
      continue
    fi
    if [ -n "$OUT" ]; then
      log "$MARKET $D $SESSION — 신규 카드, 발송"
      notify_auto "report_review_daily" "$OUT"
    fi
  done
done
