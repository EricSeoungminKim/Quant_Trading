#!/usr/bin/env bash
# 승격 토론(Bull/Bear Debate) — KR 08:15 / US 21:53 KST (2026-09-02 신규,
# 회사형 AI 에이전트 레이어 레인 1).
#
# own_brief.sh(KR 08:12 / US 21:50)가 watch-score 확신도 게이트를 통과시켜
# 자동 편입한 종목만 다룬다 — 이 스크립트는 own_brief.sh 직후에 돌아 그날
# 편입분을 data/watchlist.yaml(source=auto, added_at=오늘)에서 읽는다.
#
# Bull(찬성)/Bear(반대)/Judge(심판) 3역할 토론으로 "유지/보류"를 판정하지만
# **관심종목을 바꾸지 않는다** — data/ledger/debate.jsonl 에 기록만 하고
# 텔레그램으로 알린다. "보류" 판정 종목의 실제 성과로 이 에이전트 자체를
# 한 달 뒤 채점하는 게 목적이다(quant/analyze/promotion_debate.py 모듈
# docstring). 전략 코드는 프롬프트에 넣지 않는다 — watch_scorer 의 채점 근거만.
#
# LLM: OpenRouter 무료 레인(quant.adapters.narrate.make_json_narrator, ai_trader
# 와 같은 JSON 계약). OPENROUTER_API_KEY 없으면 결근(무출력) — 안전망은
# narrate.py 의 기존 무자격증명 계약을 그대로 쓴다.
#
# 실패 처리: quant/trade/ 를 임포트하지 않는 리포팅 레인이라 이 스크립트가
# 죽어도 엔진은 무관하다. 오늘 자동 편입이 없거나 Toss 클라이언트 구성 실패,
# LLM 결근이면 전부 조용히 exit 0(무출력) — cmd_promotion_debate 가 이미
# 그렇게 설계돼 있다.
#
# 테스트: DRY_RUN=1 ./server/scripts/promotion_debate.sh KR
#         (LLM/Toss 는 실제로 호출된다 — 자격증명이 없으면 결근으로 안전하게
#          떨어진다. 실제 텔레그램 발송만 DRY_RUN 이 막는다 — ops_judge.sh 와
#          같은 계약.)
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
LOG="data/promotion_debate.log"
mkdir -p data data/ledger

MARKET="${1:?사용법: promotion_debate.sh KR|US}"
case "$MARKET" in
  KR|US) ;;
  *) echo "usage: $0 {KR|US}" >&2; exit 2 ;;
esac

log() { echo "[$(date '+%F %T')] [$MARKET] $*" >> "$LOG"; }

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
# 알림은 notify_auto — 승격 토론 판정은 알아야 하지만 급하지 않다(own_brief.sh
# 와 같은 게이트). 장중이면 마감 HTML 로 미뤄지고, 장외면 즉시 발송된다.
. "$(dirname "$0")/lib/notify.sh"
NOTIFY_LANE="briefs"  # 텔레그램 포럼 토픽 레인 — docs/runbooks/telegram-rooms.md

if [ "$(date +%z)" != "+0900" ]; then
  log "호스트 TZ 가 KST 가 아님($(date +%z)) — 중단"
  exit 1
fi

OUT="$(timeout 180 "$PY" -m quant.apps.cli promotion-debate --market "$MARKET" 2>>"$LOG")"
RC=$?

if [ "$RC" -ne 0 ]; then
  log "판단 명령 실패(rc=$RC) — 결근 처리"
  exit 0
fi

if [ -z "$OUT" ]; then
  log "오늘 편입분 없음/LLM 결근 — 무출력"
  exit 0
fi

log "판정 완료 — 알림 발송"
notify_auto "promotion_debate" "$OUT"
echo "$OUT"
