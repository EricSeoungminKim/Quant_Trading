#!/usr/bin/env bash
# 학습형 선정자 ml_scorer — KR 08:22 / US 20:12 KST (ai_trader.sh 바로 뒤,
# 2026-08-28 신규).
#
# 과거 selection⋈forward_return(D+1)로 학습한 릿지 회귀가 그날 아침/개장전
# 리포트가 selections 원장에 남긴 서류(watch_scorer·ai_trader 가 본 것과 동일한
# 속성 벡터)를 채점해 judgments 원장(producer="ml_scorer")에 판단만 남긴다.
# 주문·워치리스트에는 닿지 않는다 — 성적은 outcomes(16:00)→리더보드(16:20
# 장마감 리포트)가 같은 input_hash 로 watch_scorer·ai_trader 와 나란히 매긴다.
#
# 표본(독립 거래일)이 min_train_days(기본 30) 미만이면 CLI(quant.apps.cli
# ml-scorer)가 학습·예측을 아예 하지 않고 "표본 부족(거래일 N/30) — 판단
# 없음" 한 줄만 stdout 에 낸다 — 2026-08-28 실측(거래일 10일뿐)으로는 지금
# 매일 이 경로다. **그 한 줄은 로그에만 남기고 텔레그램은 침묵한다**
# (ai_trader.sh 의 "조용한 것이 기본값" 관례와 동일) — 판단이 실제로 원장에
# 기록된 날만 카드를 보낸다.
set -u
cd "$(dirname "$0")/../.."

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
# 알림은 notify_auto (역할별 게이트 — server/scripts/lib/notify.sh): 픽·편입은
# 알아야 하지만 급하지 않다. **장중이면 data/notify_queue.jsonl 로 미뤄져 마감
# HTML 리포트로 나가고**, 장외면 지금처럼 즉시 발송된다.
. "$(dirname "$0")/lib/notify.sh"
NOTIFY_LANE="briefs"  # 텔레그램 포럼 토픽 레인 — docs/runbooks/telegram-rooms.md

if [ "$(date +%z)" != "+0900" ]; then
  echo "[$(date '+%F %T')] 호스트 TZ 가 KST 가 아님($(date +%z)) — 중단" >&2
  exit 1
fi

MARKET="${1:?사용법: ml_scorer.sh KR|US}"

# LLM 이 없다 — MySQL 조회 + numpy 릿지 학습뿐이라 120초면 넉넉하다(ai_trader.sh
# 의 420초 LLM 예산과 대비된다). stderr 는 로그에 남긴다(원인 진단용).
OUT="$(timeout 120 .venv/bin/python -m quant.apps.cli ml-scorer --market "$MARKET" 2>>data/ml_scorer.log)"
RC=$?

if [ "$RC" -ne 0 ]; then
  echo "[$(date '+%F %T')] $MARKET 실패 exit=$RC — 판단 미기록"
  exit 0
fi

# "표본 부족"/"MySQL 연결 없음"/"오늘 후보 없음" 은 전부 "판단 없음" 한 줄로
# 끝난다(quant/apps/cli.py cmd_ml_scorer 계약) — 이 셸은 그 표식 하나로
# 로그만 남길지 카드를 보낼지를 가른다.
if [ -z "$OUT" ] || printf '%s\n' "$OUT" | grep -q '판단 없음'; then
  echo "[$(date '+%F %T')] $MARKET 판단 없음 — 침묵: ${OUT:-무출력}"
  exit 0
fi

echo "[$(date '+%F %T')] $MARKET 판단 기록됨:"
echo "$OUT"

notify_auto "ml_scorer" "${OUT}" || true
