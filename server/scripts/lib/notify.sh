#!/usr/bin/env bash
# 역할별 텔레그램 게이트 — 장중에는 매매만, 나머지는 마감 리포트로 (2026-08-28 소유자 지시).
#
# 소유자 원문 요지: "텔레그램 메시지가 너무 복잡하다. **매매 관련된 것만 장중에**
# 보내고, 나머지(변경점·실적·문제·지분변경)는 장 끝나고 HTML 파일 하나로 받고
# 싶다. 간단 명료하게. 말이 너무 많으면 읽기 싫어진다."
#
# **엔진(quant/adapters/notify/)의 체결·신호 알림은 이 게이트를 타지 않는다** —
# 그게 장중에 받고 싶어하는 유일한 것이다. 이 파일이 바꾸는 건 server/scripts/
# 아래 크론 스크립트들의 알림이다. 그전에는 20개 넘는 스크립트가 각자 `tg()` 를
# 복제해 텔레그램을 직접 쳤고, 그래서 "무엇이 장중에 나가는가"를 아무도 한곳에서
# 답할 수 없었다. 이제 그 답은 이 파일 하나다.
#
# ## 세 개의 문
#
#   notify_now   "<text>"            즉시 발송. **지금 조치 안 하면 손해**인 것만.
#                                    (엔진 다운/행, 운영 이상 판정, 백업 실패, 인증 실패)
#   notify_auto  "<source>" "<text>"  장중이면 미루고, 장외면 즉시 발송.
#                                    (편입·픽·강등·거버너 결정 — 알아야 하지만 급하진 않다)
#   notify_defer "<source>" "<text>"  **절대 발송하지 않는다.** 큐에만 쌓는다.
#                                    (백필 결과·요약·정보성 — 마감 HTML 로만 간다)
#
# 미뤄진 것은 `data/notify_queue.jsonl` 에 append 되고, 마감 HTML 리포트가 이걸
# 읽어 "문제 발견 및 개선" 절에 넣는다. 줄 형식(계약):
#
#   {"ts":"2026-08-28T10:03:11+0900","source":"backfill_1m","text":"...","level":"defer"}
#
#   ts     발송 시도 시각. KST 로컬 + 오프셋(ISO 8601). 리포트가 그대로 찍는다.
#   source 스크립트 basename(확장자 없음). 어디서 왔는지 — 절 안의 그룹 키.
#   text   원문 그대로. **문구는 이 게이트가 손대지 않는다.**
#   level  어느 문이 미뤘는지 — "defer"(항상 미룸) | "auto"(장중이라 미룸).
#          "auto" 는 장외였다면 이미 나갔을 메시지라 리포트에서 위로 올릴 만하다.
#
# ## 장중 판정
#
# KR 정규장 평일 09:00~15:30 KST, US 정규장 22:30~06:00 KST(서머타임 폭을 덮는
# 넉넉한 창 — 이 게이트는 주문이 아니라 알림 타이밍을 가르므로 보수적이어도 된다).
# 시각은 `NOTIFY_NOW_HHMM`(4자리 HHMM) / `NOTIFY_NOW_DOW`(1=월~7=일, `date +%u`)
# 로 주입할 수 있다 — 테스트가 실제 벽시계에 의존하지 않게.
#
# ## 안전 계약
#
# - 토큰/챗ID 가 없으면 셋 다 **조용히 성공**한다(no-op, exit 0). 로컬·테스트 실행이
#   이것 때문에 죽으면 안 된다.
# - 큐 파일 쓰기 실패도 스크립트를 죽이지 않는다.
# - `DRY_RUN=1` 이면 발송 대신 찍는다(기존 스크립트들의 관례를 그대로 승계).
# - 이 파일은 **멱등하게 source 가능**하다(아래 가드).
#
# 사용법: . "$(dirname "$0")/lib/notify.sh"

if [ -z "${_NOTIFY_SH_LOADED:-}" ]; then
_NOTIFY_SH_LOADED=1

# 저장소 루트 — 호출 스크립트의 cwd 에 의존하지 않는다(server/scripts/lib → ../../..).
_NOTIFY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

# .env.local 에서 키 하나만 읽는다 — export 하지 않는다(하위 프로세스가 시크릿을
# env 로 물려받지 않는 기존 계약: own_brief.sh/daily_brief.sh 주석 참고).
_notify_env() {
  grep "^$1=" "${NOTIFY_ENV_FILE:-$_NOTIFY_ROOT/.env.local}" 2>/dev/null | head -1 | cut -d= -f2-
}

# 토큰 해석 순서: 환경변수 → 호출 스크립트가 이미 읽어둔 TG_TOKEN/TG_CHAT → .env.local.
# (watchdog.sh 처럼 .env.local 을 통째로 source 하는 스크립트와, own_brief.sh 처럼
#  두 키만 지역 변수로 뽑는 스크립트가 둘 다 있어서 셋 다 받는다.)
_notify_token() {
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then printf '%s' "$TELEGRAM_BOT_TOKEN"; return 0; fi
  if [ -n "${TG_TOKEN:-}" ]; then printf '%s' "$TG_TOKEN"; return 0; fi
  _notify_env TELEGRAM_BOT_TOKEN
}

_notify_chat() {
  if [ -n "${TELEGRAM_CHAT_ID:-}" ]; then printf '%s' "$TELEGRAM_CHAT_ID"; return 0; fi
  if [ -n "${TG_CHAT:-}" ]; then printf '%s' "$TG_CHAT"; return 0; fi
  _notify_env TELEGRAM_CHAT_ID
}

# 장중인가 — 순수 셸. 0=장중(미룸), 1=장외(발송).
#
# 자릿수 함정 주의: `[ 0900 -le 1000 ]` 은 bash 산술이 "0900" 을 8진수로 읽어 에러다.
# own_brief.sh 데드라인 비교와 같은 관례로 앞에 "1" 을 붙여 피한다(그 결함은
# 2026-08-13 에 실제로 아침 편입을 통째로 건너뛰게 했다 — tests/test_own_brief_deadline.py).
_in_market_hours() {
  local hhmm dow
  hhmm="${NOTIFY_NOW_HHMM:-$(date +%H%M)}"
  dow="${NOTIFY_NOW_DOW:-$(date +%u)}"

  # KR 정규장 — 평일 09:00~15:30 KST
  if [ "$dow" -ge 1 ] && [ "$dow" -le 5 ] \
     && [ "1$hhmm" -ge "10900" ] && [ "1$hhmm" -le "11530" ]; then
    return 0
  fi
  # US 정규장 앞머리 — 월~금 22:30~23:59 KST
  if [ "$dow" -ge 1 ] && [ "$dow" -le 5 ] && [ "1$hhmm" -ge "12230" ]; then
    return 0
  fi
  # US 정규장 뒷머리 — 그 세션이 넘어온 화~토 00:00~06:00 KST
  # (일요일 새벽은 금요일 밤 세션이 아니다 — 주말은 장외다.)
  if [ "$dow" -ge 2 ] && [ "$dow" -le 6 ] && [ "1$hhmm" -le "10600" ]; then
    return 0
  fi
  return 1
}

# JSON 문자열 이스케이프 — 역슬래시/따옴표/탭/CR/개행. 순수 셸(sed+awk)로 한다:
# 큐 적재가 파이썬 인터프리터 기동에 의존하면 .venv 가 깨진 날 알림이 통째로
# 사라진다.
_notify_json_escape() {
  local tab cr
  tab="$(printf '\t')"
  cr="$(printf '\r')"
  printf '%s' "$1" \
    | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e "s/$tab/\\\\t/g" -e "s/$cr/\\\\r/g" \
    | awk 'BEGIN{ORS=""} NR>1{print "\\n"} {print}'
}

# 큐에 한 줄 append. **실패해도 절대 스크립트를 죽이지 않는다.**
_notify_enqueue() {  # $1=source $2=text $3=level
  local f line
  f="${NOTIFY_QUEUE:-$_NOTIFY_ROOT/data/notify_queue.jsonl}"
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  line="$(printf '{"ts":"%s","source":"%s","text":"%s","level":"%s"}' \
    "$(date +%Y-%m-%dT%H:%M:%S%z)" \
    "$(_notify_json_escape "$1")" \
    "$(_notify_json_escape "$2")" \
    "$(_notify_json_escape "$3")")"
  # 크론이 겹치면 3900자 메시지 두 개가 섞여 JSON 이 깨진다(append 원자성은
  # PIPE_BUF 까지만). flock 이 있으면 쓴다 — 없으면(맥) 그냥 append.
  if command -v flock >/dev/null 2>&1; then
    ( flock 200; printf '%s\n' "$line" >&200 ) 200>>"$f" 2>/dev/null || true
  else
    printf '%s\n' "$line" >> "$f" 2>/dev/null || true
  fi
  return 0
}

# 실제 발송. 0=보냄(또는 토큰 없어 no-op), 1=발송 실패.
#
# **실패를 삼키지 않는다** — ops_watch.sh 가 `if tg ...; then mark; fi` 로 첫 알림
# 유실을 잡던 계약을 그대로 승계한다(구 `|| true` 시절 알림이 유실돼도 상태가
# 기록돼 영원히 재시도하지 않는 결함이 났다).
#
# parse_mode=HTML(2026-09-04, L1 서식) 로 먼저 보낸다 — 크론 리포트들이
# quant.core.tgfmt 스타일 `<b>`/`<code>` 태그를 담은 텍스트를 넘기기 시작해서다.
# 텔레그램이 태그를 거부하면(응답에 "ok":false) parse_mode 없이 **그 한 통만**
# 평문으로 즉시 재시도한다 — 서식 버그로 알림 자체가 유실되면 안 된다.
_notify_send() {  # $1=text
  local token chat resp
  token="$(_notify_token)"
  chat="$(_notify_chat)"
  if [ -z "$token" ] || [ -z "$chat" ]; then
    return 0   # 조용한 no-op — 로컬/테스트 실행이 이것 때문에 죽지 않는다
  fi
  if [ "${DRY_RUN:-0}" = "1" ]; then
    printf '[DRY_RUN][TG]\n%s\n' "$1"
    return 0
  fi
  resp="$(curl -s -m 15 "${TELEGRAM_API_BASE:-https://api.telegram.org}/bot${token}/sendMessage" \
    -d "chat_id=${chat}" -d "parse_mode=HTML" --data-urlencode "text=$1" 2>/dev/null)"
  case "$resp" in *'"ok":true'*) return 0 ;; esac
  resp="$(curl -s -m 15 "${TELEGRAM_API_BASE:-https://api.telegram.org}/bot${token}/sendMessage" \
    -d "chat_id=${chat}" --data-urlencode "text=$1" 2>/dev/null)"
  case "$resp" in *'"ok":true'*) return 0 ;; esac
  return 1
}

# ── 공개 API ──────────────────────────────────────────────────────────────

# 즉시 발송 — 긴급·안전 관련만.
notify_now() {  # $1=text
  _notify_send "$1"
}

# 절대 발송하지 않는다 — 마감 HTML 리포트로만 간다.
notify_defer() {  # $1=source $2=text
  _notify_enqueue "$1" "$2" "defer"
}

# 장중이면 미루고, 장외면 즉시 발송한다.
notify_auto() {  # $1=source $2=text
  if _in_market_hours; then
    _notify_enqueue "$1" "$2" "auto"
  else
    _notify_send "$2"
  fi
}

fi
