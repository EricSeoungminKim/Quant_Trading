#!/usr/bin/env bash
# 매시 05분 — 운영 이상 감시. Phase 5.2 / 5.4.
#
# 왜: 이번 주 치명적 결함 5개를 **전부 사람이 발견했다.** 그중 넷(토큰 만료, 데드라인
# 오판, 타임존, 시크릿 패턴)은 자동 감시가 잡을 수 있는 종류였다.
#
# watchdog.sh 와 겹치지 않는다: 그쪽은 5분마다 **엔진의 침묵**(다운/행)을 본다.
# 이쪽은 한 시간마다 **엔진이 잘 도는데 틀린 것**을 본다 — 낡은 봉, 죽은 피드,
# 원장 불일치, 로그에 남은 시크릿, 설치본 드리프트, 백업 부재.
#
# ## 판정은 세 갈래다
#
#   ok(0) → **아무 말도 하지 않는다.** 매시간 "정상" 알림이 오면 사람이 끄고,
#           끈 알림은 없는 알림이다.
#   alert(1) / unknown(2) → 알린다. **`unknown` 을 정상으로 합산하지 않는다** —
#           "모른다"는 이 저장소가 반복해서 다친 자리다(Redis 죽으면 빈 목록,
#           coverage 실패면 None, 캐시 실패면 0).
#
# ## LLM 은 서술만 한다 (ADR-0002, Phase 5.4)
#
# 감지는 전부 `quant.control.health` 의 결정론적 순수 함수다. LLM 은 그 JSON 을
# 사람이 읽을 한국어로 바꾸는 데만 쓰인다 — **경보가 LLM 에 의존하지 않는다.**
# 서술이 실패하면 결정론적 형식으로 그대로 보낸다.
#
# 서술기(narrator)는 갈아끼울 수 있다(`OPS_NARRATOR`):
#   claude(기본) — 로컬 Claude Code CLI. **도구를 전부 차단한다**(daily_brief.sh 와
#                  같은 계약): 텍스트→텍스트뿐이라 주문 경로에 닿을 방법이 없다.
#   openrouter   — 경계만 뚫어둔 자리. 아직 미구현이고, 부르면 none 으로 떨어진다.
#   none         — 결정론적 형식. 항상 동작하는 바닥.
#
# 테스트: DRY_RUN=1 ./server/scripts/ops_watch.sh
#         DRY_RUN=1 OPS_NARRATOR=none ./server/scripts/ops_watch.sh
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
LOG="data/ops_watch.log"
STATE="data/state/ops_watch.state"   # 마지막으로 알린 발견 집합의 해시 (중복 방지)
# 라벨용일 뿐이다 — 실제 선택과 실행파일 경로 기본값은 make_narrator() 가 갖는다.
# (CLAUDE_BIN 은 어댑터가 직접 env 에서 읽으므로 여기서 세팅하지 않는다.)
NARRATOR="${OPS_NARRATOR:-claude}"

mkdir -p data data/state
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
tg() {
  # 성공 여부를 반환한다 — 실패를 삼키면(구 `|| true`) 첫 알림이 유실돼도 상태가
  # 기록돼 영원히 재시도하지 않는 결함이 났다. 텔레그램 응답의 "ok":true 로 판정.
  if [ "${DRY_RUN:-0}" = "1" ]; then printf '[DRY_RUN][TG]\n%s\n' "$1"; return 0; fi
  RESP="$(curl -s -m 15 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    -d "chat_id=${TG_CHAT}" --data-urlencode "text=$1" 2>/dev/null)"
  case "$RESP" in *'"ok":true'*) return 0 ;; esac
  return 1
}

# --- 1. 감지 (결정론적) ---
# 있어야 하는 타이머를 명시한다 — 유닛이 조용히 사라지는 것을 잡는다.
# 필수 소스·시크릿을 명시한다. **비워두면 감시가 결측을 못 본다** — 2026-08-14 에
# API 키를 못 읽어 소스 5개가 결측인 채로 리포트가 발행됐고, 리포트는 그 사실을
# engine.json 에 기록했는데 아무도 읽지 않았다. after_hours 는 시간대에 따라 정상적으로
# 빠지므로 넣지 않는다(거짓 경보가 오는 감시는 꺼진다).
REPORT="$("$PY" -m quant.apps.cli health \
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
RC=$?
printf '%s\n' "$REPORT" >> "$LOG"

if [ "$RC" -eq 0 ]; then
  log "정상 — 알리지 않음"
  rm -f "$STATE"          # 회복했으면 다음 이상은 새 사건이다
  [ "${DRY_RUN:-0}" = "1" ] && echo "[DRY_RUN] verdict=ok — 알림 없음"
  exit 0
fi

if [ -z "$REPORT" ]; then
  # health 자체가 죽었다. 이것도 침묵하면 안 된다.
  tg "🚨 운영 감시: 점검 명령이 출력 없이 실패했다 (rc=${RC}) — data/ops_watch.log 확인"
  exit 1
fi

# --- 2. 중복 방지 ---
# 같은 이상으로 매시간 알림이 오면 사람이 끈다. 발견 집합이 바뀔 때만 알린다.
HASH="$(printf '%s' "$REPORT" | "$PY" -c '
import hashlib, json, sys
try:
    body = json.load(sys.stdin)
except json.JSONDecodeError:
    print("unparsable"); raise SystemExit
key = json.dumps(sorted(f["detail"] for f in body.get("findings", [])), ensure_ascii=False)
print(hashlib.sha256(key.encode()).hexdigest()[:16])
')"
# mtime 하트비트: 같은 발견 집합이라도 12시간 넘게 지속 중이면 다시 알린다 —
# "아직도 안 고쳐졌다"는 그 자체로 새 정보다.
if [ -f "$STATE" ] && [ "$(cat "$STATE")" = "$HASH" ] \
   && [ -z "$(find "$STATE" -mmin +720 2>/dev/null)" ] && [ "${DRY_RUN:-0}" != "1" ]; then
  log "같은 발견 집합($HASH) — 알림 생략"
  exit "$RC"
fi

# --- 3. 서술 (갈아끼울 수 있는 경계) ---
PROMPT="다음은 개인 자동매매 시스템의 운영 점검 결과 JSON이다. 한국어로 6줄 이내로
요약해라. 규칙: (1) level이 alert인 것을 먼저, unknown은 그 뒤에. (2) unknown은
'이상'이 아니라 '확인 못 했다'로 표현해라. (3) 추측하지 말고 JSON에 있는 사실만
말해라. (4) 조치 제안은 한 줄만. (5) 인사말·서론 없이 바로 본문.

$REPORT"

narrate_none() {
  # JSON 을 argv 로 넘긴다(스크립트가 stdin 을 쓰므로). 셸 인용 안에서 f-string 을
  # escape 하려다 SyntaxError 를 냈고, 그 결과 **빈 알림이 나갔다** — 첫 리허설에서
  # 실제로 그랬다. 그래서 여기는 format() 만 쓰고, 아래에 바닥을 하나 더 깐다.
  "$PY" - "$REPORT" <<'PYEOF'
import json, sys
b = json.loads(sys.argv[1])
mark = {"alert": "🚨", "unknown": "❔"}
print("판정: {} (이상 {} / 확인불가 {})".format(b["verdict"], b["n_alert"], b["n_unknown"]))
for f in b.get("findings", []):
    print("{} [{}] {}".format(mark.get(f["level"], "·"), f["check"], f["detail"]))
PYEOF
}

# 서술기 선택은 **셸에 없다.** `adapters.narrate.make_narrator()` 한 곳에만 있고
# (claude / openrouter / none), 여기서는 그 결과가 있나 없나만 본다. 스위치를 양쪽에
# 두면 두 곳이 갈리고, 갈라진 쪽이 조용한 쪽이 된다.
BODY="$(printf '%s' "$PROMPT" | timeout 200 nice -n 10 "$PY" -m quant.apps.cli narrate 2>>"$LOG")"

# 서술이 비었거나 실패하면 결정론적 형식으로 보낸다.
# **경보는 절대 LLM 에 의존하지 않는다.**
if [ -z "${BODY:-}" ]; then
  BODY="$(narrate_none)"
  [ -n "$BODY" ] && [ "$NARRATOR" != "none" ] && BODY="(서술기 실패 — 원본 형식)
$BODY"
fi

# 바닥의 바닥. 형식화까지 실패하면 **JSON 원문**을 보낸다 — 읽기 불편한 알림은
# 알림이지만, 빈 알림은 알림이 아니다(첫 리허설에서 빈 본문이 나갔다).
if [ -z "${BODY:-}" ]; then
  BODY="(형식화 실패 — JSON 원문)
$(printf '%s' "$REPORT" | head -c 2500)"
fi

HEAD="$([ "$RC" -eq 1 ] && echo '🚨 운영 감시: 이상' || echo '❔ 운영 감시: 확인 못 한 항목')"
if tg "${HEAD}

${BODY}

전체: data/ops_watch.log"; then
  # **발송에 성공했을 때만** 상태를 기록한다. 예전엔 무조건 기록해서, 첫 알림이
  # 마침 네트워크 문제로 유실되면 "이미 알렸다"로 남아 그 문제에 대해 영원히
  # 아무 알림도 못 받았다 — 문제가 사라졌다 재발할 때만 새 사건으로 잡혔다.
  [ "${DRY_RUN:-0}" != "1" ] && printf '%s' "$HASH" > "$STATE"
  log "알림 전송 (verdict rc=$RC, 해시 $HASH, 서술기 $NARRATOR)"
else
  log "알림 전송 실패 — 상태 미기록(다음 주기에 재시도) (verdict rc=$RC, 해시 $HASH)"
fi
exit "$RC"
