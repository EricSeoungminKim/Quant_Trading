#!/usr/bin/env bash
# 판단하는 워치독 (2026-08-19) — ops_watch.sh(규칙 기반) 위에 LLM 교차검증을 얹는다.
#
# 왜: ops_watch.sh 의 규칙(quant.control.health)은 "파일이 있나/신선한가"만 본다.
# 2026-08-19 하루에만 그 규칙이 하나도 못 잡은 결함이 5건 나왔다 — 전부 에러를 내지
# 않고 그럴듯한 숫자·문구를 냈기 때문이다(전략별 장부 배선 누락으로 현금 게이트
# 무력화 → 현금 -1,047만원, 리포트가 KOSPI 부호를 반대로 표시, 적립 매수가 의도의
# 2배 체결 등). 공통점: 서로 다른 데이터 소스를 대조해야만 틀렸다는 게 드러난다.
# 이 스크립트는 그 대조를 LLM(quant.control.ops_judge, 도구 호출 에이전트)에게
# 시킨다. **규칙 기반 감시를 대체하지 않는다** — 먼저 `cli health`를 그대로 부르고,
# 그 결과를 판단 에이전트의 도구 중 하나로 그대로 넘긴다.
#
# ## 판정은 세 갈래다
#   ok    → **아무 말도 하지 않는다.** (ops_watch.sh 와 같은 원칙 — 매번 "정상"
#           알림이 오면 사람이 끄고, 끈 알림은 없는 알림이다.)
#   alert(이상) / review(확인 필요) → 알린다. review 는 "모른다"의 안전망이다
#           (LLM 자격증명 없음, 호출 실패, 형식 어김, 근거 없는 alert 는 전부
#           review 로 낮아진다 — quant.control.ops_judge.run_judgment 문서 참고).
#
# ## 사용법
#   ./server/scripts/ops_judge.sh <라벨>
#   라벨은 프롬프트에 그대로 노출되는 컨텍스트 문자열이다(크론이 kr-midday/
#   us-midsession 을 넘긴다). 생략하면 manual.
#
# 테스트: DRY_RUN=1 ./server/scripts/ops_judge.sh kr-midday
#         (LLM 은 실제로 호출된다 — OPENROUTER_API_KEY 가 없으면 review 로 안전하게
#          떨어진다. 실제 텔레그램 발송만 DRY_RUN 이 막는다.)
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
LOG="data/ops_judge.log"
STATE="data/state/ops_judge.state"   # 마지막으로 알린 판정의 해시 (같은 판정 중복 알림 방지)

LABEL="${1:-manual}"
# 벽시계 예산(초) — 실제 강제 중단은 아래 `timeout`이 진다(cmd_ops_judge 는
# 시작 전 게이트만 건다). 1.8GB 박스에서 OpenRouter 무료 레인 왕복(재시도+폴백
# 모델 포함, close_report.sh/deepdive 크론과 같은 근거)을 감안해 240초 기본값.
TIME_BUDGET="${OPS_JUDGE_TIME_BUDGET:-240}"

mkdir -p data data/state
log() { echo "[$(date '+%F %T')] [$LABEL] $*" >> "$LOG"; }

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
# 알림은 전부 notify_defer (역할별 게이트 — server/scripts/lib/notify.sh):
# 요약·정보성이라 텔레그램으로는 **절대 나가지 않는다**. data/notify_queue.jsonl
# 에 쌓여 마감 HTML 리포트로만 간다.
. "$(dirname "$0")/lib/notify.sh"

# TZ 가드 — run_report.sh/run_close_report.sh 와 같은 방어. 세션 경계·편입
# 데드라인이 KST 전제라 타임존이 어긋나면 판단 자체가 무의미하다.
if [ "$(date +%z)" != "+0900" ]; then
  log "호스트 TZ 가 KST 가 아님($(date +%z)) — 중단"
  notify_defer "ops_judge" "🚨 판단 워치독(${LABEL}): 호스트 타임존이 KST 가 아니다 — 점검 중단"
  exit 1
fi

# --- 1. 규칙 기반 결과 먼저 (ops_watch.sh 와 같은 인자) ---
# **대체가 아니다** — 판단 에이전트의 도구 중 하나로 그대로 넘긴다
# (quant.control.ops_judge.TOOLS_SPEC: get_rule_based_findings).
RULE_JSON="$("$PY" -m quant.apps.cli health \
  --expect-timer news-collect-kr.timer \
  --expect-timer news-collect-us.timer \
  --expect-timer market-report-kr.timer \
  --expect-timer market-report-us.timer \
  --expect-timer warehouse-ingest.timer \
  --expect-timer market-close-report-kr.timer \
  --required-source macro \
  --required-source calendar \
  --required-source toss_rankings \
  --required-secret FRED_API_KEY \
  --required-secret TOSS_CLIENT_ID \
  --required-secret TOSS_CLIENT_SECRET \
  --required-secret TELEGRAM_BOT_TOKEN 2>>"$LOG")"
printf '%s\n' "$RULE_JSON" >> "$LOG"

# --- 2. 판단 (LLM 교차검증) ---
# 벽시계 상한은 여기 `timeout`이 진다. +30초 여유는 파이썬 기동·파일 I/O 몫이다.
BODY="$(printf '%s' "$RULE_JSON" | timeout $((TIME_BUDGET + 30)) nice -n 10 "$PY" -m quant.apps.cli ops-judge \
  --rule-based-json - --label "$LABEL" --time-budget "$TIME_BUDGET" 2>>"$LOG")"
RC=$?
printf '%s\n' "$BODY" >> "$LOG"

if [ -z "$BODY" ]; then
  # 판단 명령 자체가 출력 없이 죽었다. **이것도 침묵하면 안 된다** — 이 감시가
  # 조용히 죽는 경로 중 셸 레벨에서 잡을 수 있는 것(타임아웃 SIGTERM, 파이썬
  # 크래시 등).
  notify_defer "ops_judge" "🚨 판단 워치독(${LABEL}): 점검 명령이 출력 없이 실패했다 (rc=${RC}, timeout=${TIME_BUDGET}s) — data/ops_judge.log 확인"
  exit 1
fi

LEVEL="$(printf '%s' "$BODY" | "$PY" -c '
import json, sys
try:
    print(json.load(sys.stdin).get("level", "review"))
except Exception:
    print("review")
' 2>/dev/null)"
[ -z "$LEVEL" ] && LEVEL="review"

if [ "$LEVEL" = "ok" ]; then
  log "정상 — 알리지 않음"
  rm -f "$STATE"          # 회복했으면 다음 이상은 새 사건이다
  [ "${DRY_RUN:-0}" = "1" ] && echo "[DRY_RUN] level=ok — 알림 없음"
  exit 0
fi

# --- 3. 중복 방지 --- 같은 판정(레벨+근거)이 반복되면 한 번만 알린다.
HASH="$(printf '%s' "$BODY" | "$PY" -c '
import hashlib, json, sys
try:
    body = json.load(sys.stdin)
except json.JSONDecodeError:
    print("unparsable"); raise SystemExit
key = json.dumps([body.get("level"), sorted(body.get("reasons") or [])], ensure_ascii=False)
print(hashlib.sha256(key.encode()).hexdigest()[:16])
' 2>/dev/null)"
if [ -f "$STATE" ] && [ "$(cat "$STATE")" = "$HASH" ] && [ "${DRY_RUN:-0}" != "1" ]; then
  log "같은 판정(${HASH}) — 알림 생략"
  exit "$RC"
fi

# --- 4. 서식화 ---
SUMMARY="$(printf '%s' "$BODY" | "$PY" -c '
import json, sys
b = json.load(sys.stdin)
print(b.get("summary") or "(산문 없음)")
print()
for r in (b.get("reasons") or []):
    print(f"- {r}")
print()
print("사용한 도구: " + (", ".join(b.get("tools_used") or []) or "없음"))
print("narrator: " + str(b.get("narrator")))
' 2>/dev/null)"
if [ -z "$SUMMARY" ]; then
  # 바닥의 바닥 — 서식화까지 실패하면 JSON 원문(ops_watch.sh 와 같은 관례).
  SUMMARY="(형식화 실패 — JSON 원문)
$(printf '%s' "$BODY" | head -c 2000)"
fi

HEAD="$([ "$LEVEL" = "alert" ] && echo '🚨 판단 워치독: 이상' || echo '🧭 판단 워치독: 확인 필요')"
notify_defer "ops_judge" "${HEAD} (${LABEL})

${SUMMARY}

전체: data/ops_judge.log"

[ "${DRY_RUN:-0}" != "1" ] && printf '%s' "$HASH" > "$STATE"
log "알림 전송 (level=${LEVEL}, 해시 ${HASH})"
[ "$LEVEL" = "alert" ] && exit 1
exit 2
