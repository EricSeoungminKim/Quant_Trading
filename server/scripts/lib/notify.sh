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
# 세 함수 모두 반환값이 **실제 결과**다(0=성공, 그 외=실패) — notify_defer도
# 큐 쓰기 자체가 실패하면 0이 아니다(2026-09-04 수정). session_pnl.sh/
# manual_recs.sh처럼 발송 결과를 로그에 남기고 싶은 호출부는 이 값을 그대로
# 쓰면 된다. notify_auto가 이번에 큐에 넣을지 즉시 보낼지 미리 알고 싶으면
# (성공 시 "전송 성공"과 "큐 적재"를 구분해 로그로 남기고 싶을 때)
# `notify_auto_would_defer`(공개 API, 아래)를 부르면 된다.
#
# 미뤄진 것은 `data/notify_queue.jsonl` 에 append 되고, 마감 HTML 리포트가 이걸
# 읽어 "문제 발견 및 개선" 절에 넣는다. 줄 형식(계약):
#
#   {"ts":"2026-08-28T10:03:11+0900","source":"backfill_1m","text":"...","level":"defer","lane":"ops"}
#
#   ts     발송 시도 시각. KST 로컬 + 오프셋(ISO 8601). 리포트가 그대로 찍는다.
#   source 스크립트 basename(확장자 없음). 어디서 왔는지 — 절 안의 그룹 키.
#   text   원문 그대로. **문구는 이 게이트가 손대지 않는다.**
#   level  어느 문이 미뤘는지 — "defer"(항상 미룸) | "auto"(장중이라 미룸).
#          "auto" 는 장외였다면 이미 나갔을 메시지라 리포트에서 위로 올릴 만하다.
#   lane   호출 시점의 `NOTIFY_LANE`(2026-09-05, 아래 "레인 라우팅" 절) 그대로.
#          빈 문자열이면 레인 미지정(기존 호출부) — 큐가 나중에 flush될 때
#          "어느 레인 것이었는지"를 잃지 않기 위한 것뿐, 이 파일이 flush
#          시점의 라우팅까지 책임지지는 않는다(그건 flush를 하는 스크립트,
#          예: daily_wrap.sh가 자기 `NOTIFY_LANE`으로 보낸다).
#
# ## 레인 라우팅 (포럼 토픽, 2026-09-05)
#
# 오너가 텔레그램 슈퍼그룹에 포럼 토픽 5개(제어실/매매/브리핑/채널 인텔/운영)를
# 만들고 브리지의 `/here <레인>`으로 바인딩하면(`docs/runbooks/telegram-rooms.md`),
# 그 매핑이 `data/state/tg_lanes.json`에 쌓인다. 각 크론 스크립트가 자기 위치에서
# `NOTIFY_LANE="<레인>"`을 설정해두면(위 세 함수가 이 값을 읽는다),
# `notify_now`/`notify_auto`가 실제 발송하는 순간 그 레인의 토픽으로
# `message_thread_id`를 붙여 보낸다. **레인이 아직 안 묶였으면(또는
# `NOTIFY_LANE` 자체를 안 정했으면) 기존과 완전히 동일하게 레거시 단일 채팅으로
# 간다** — 마이그레이션 전에는 아무것도 깨지지 않는다. 판정 규칙은
# `quant/core/tglanes.py`와 정확히 같아야 한다(이 파일은 그 파이썬 모듈을
# 임포트하지 않고 `python3 -c`로 JSON만 다시 읽는다 — 크론 환경에 `quant`
# 패키지가 항상 임포트 가능하다고 보장할 수 없어서다).
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

# 큐에 한 줄 append. 반환값은 **실제 쓰기 성공 여부**를 반영한다(2026-09-04
# 수정 — 이전엔 항상 0을 반환해 notify_defer/notify_auto 호출부가 "큐 적재
# 성공/실패"를 구분할 방법이 없었다: session_pnl.sh/manual_recs.sh가 발송
# 결과를 로그에 남기려 해도 그 값이 항상 성공만 가리켰다). 실패해도 **호출
# 스크립트를 죽이지는 않는다** — 이 파일을 쓰는 크론 중 `set -e`를 켠 곳이
# 없고(`set -u`뿐), 반환값을 무시하는 기존 호출부(`notify_auto ... || true`)도
# 그대로 안전하다.
#
# `lane`(2026-09-05, 텔레그램 포럼 토픽 레인 — 아래 "레인 라우팅" 절)은 호출
# 시점의 `NOTIFY_LANE` 값 그대로 줄에 남긴다(빈 문자열이면 레인 미지정) — 큐가
# 나중에 마감 리포트로 flush될 때 "이 알림이 어느 레인 것이었는지"가 남아야
# 한다.
_notify_enqueue() {  # $1=source $2=text $3=level
  local f line rc
  f="${NOTIFY_QUEUE:-$_NOTIFY_ROOT/data/notify_queue.jsonl}"
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  line="$(printf '{"ts":"%s","source":"%s","text":"%s","level":"%s","lane":"%s"}' \
    "$(date +%Y-%m-%dT%H:%M:%S%z)" \
    "$(_notify_json_escape "$1")" \
    "$(_notify_json_escape "$2")" \
    "$(_notify_json_escape "$3")" \
    "$(_notify_json_escape "${NOTIFY_LANE:-}")")"
  # 크론이 겹치면 3900자 메시지 두 개가 섞여 JSON 이 깨진다(append 원자성은
  # PIPE_BUF 까지만). flock 이 있으면 쓴다 — 없으면(맥) 그냥 append.
  if command -v flock >/dev/null 2>&1; then
    ( flock 200; printf '%s\n' "$line" >&200 ) 200>>"$f" 2>/dev/null
    rc=$?
  else
    printf '%s\n' "$line" >> "$f" 2>/dev/null
    rc=$?
  fi
  return "$rc"
}

# ── 레인 라우팅(포럼 토픽, 2026-09-05) ──────────────────────────────────────
#
# `quant/core/tglanes.py`가 정의한 판정을 셸에서도 복제한다(같은 로직, 같은
# 파일 — `data/state/tg_lanes.json`). 파이썬 패키지를 임포트하지 않고
# `python3 -c`로 JSON만 파싱한다 — 크론 환경에서 `quant`가 항상 임포트 가능하다고
# 보장할 수 없어서다(`.venv/bin/python`을 쓰지 않는 셸도 있다).
#
# 레인 헤더 문구는 `quant.core.tglanes.LANES`와 반드시 같아야 한다 —
# `tests/test_notify_gate.py`가 둘을 대조한다.
_notify_lane_header() {  # $1=lane
  case "$1" in
    control) printf '🎛 제어실' ;;
    trades)  printf '📈 매매' ;;
    briefs)  printf '📰 브리핑' ;;
    intel)   printf '📡 채널 인텔' ;;
    ops)     printf '🚨 운영' ;;
    *) printf '%s' "$1" ;;
  esac
}

# <lane> → "chat_id|thread_id|bound" 한 줄(구분자는 `|` — bash `read`가 IFS를
# 공백류(tab/space/newline)로 두면 빈 필드를 삼켜버려 값이 밀린다: 실측으로
# `IFS=$'\t' read` 가 "\t\t1"을 필드 3개가 아니라 1개("1")로 접어버렸다. `|`는
# chat_id/thread_id 숫자값에 나올 수 없으니 안전하다). 매핑이 없거나 그 레인이
# 아직 바인딩 안 됐으면 chat_id/thread_id 는 빈 문자열 — 호출부가 레거시 chat
# 으로 폴백한다. `bound`는 "이 레인은 아니어도 매핑 파일 자체에 바인딩이 하나라도
# 있는가"(1/0) — 레거시로 떨어지는 메시지에 헤더를 붙일지 판단하는 데 쓴다
# (`quant.core.tglanes.is_bound`와 같은 규칙).
_notify_lane_target() {  # $1=lane
  local lane="$1" file
  file="${NOTIFY_LANES_FILE:-$_NOTIFY_ROOT/data/state/tg_lanes.json}"
  python3 -c '
import json, sys
lane, path = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except (OSError, ValueError):
    data = {}
chat_id = data.get("chat_id")
threads = data.get("threads") or {}
thread_id = threads.get(lane)
bound = "1" if (chat_id is not None and threads) else "0"
if chat_id is not None and thread_id is not None:
    print(f"{chat_id}|{thread_id}|{bound}")
else:
    print(f"||{bound}")
' "$lane" "$file" 2>/dev/null
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
#
# `NOTIFY_LANE`(2026-09-05)이 설정돼 있으면 `_notify_lane_target`으로 그 레인의
# `(chat_id, thread_id)`를 찾는다 — 아직 안 묶였으면 기존과 동일하게 레거시
# `chat_id`로 폴백하되, 매핑 파일에 **다른** 레인이라도 하나 바인딩돼 있으면
# (`bound=1`) 그 레거시 채팅이 여러 레인이 섞이는 방이 되므로 한 줄 헤더를
# 붙인다. `NOTIFY_LANE`이 비어 있으면(기존 호출부) 완전히 예전과 동일하다.
_notify_send() {  # $1=text
  local token chat resp lane target t_chat t_thread t_bound chat_id thread_id text
  token="$(_notify_token)"
  chat="$(_notify_chat)"
  if [ -z "$token" ] || [ -z "$chat" ]; then
    return 0   # 조용한 no-op — 로컬/테스트 실행이 이것 때문에 죽지 않는다
  fi
  text="$1"
  lane="${NOTIFY_LANE:-}"
  chat_id="$chat"
  thread_id=""
  if [ -n "$lane" ]; then
    target="$(_notify_lane_target "$lane")"
    IFS='|' read -r t_chat t_thread t_bound <<< "$target"
    if [ -n "$t_chat" ] && [ -n "$t_thread" ]; then
      chat_id="$t_chat"
      thread_id="$t_thread"
    elif [ "$t_bound" = "1" ]; then
      text="$(_notify_lane_header "$lane")"$'\n'"$text"
    fi
  fi
  if [ "${DRY_RUN:-0}" = "1" ]; then
    printf '[DRY_RUN][TG]\n%s\n' "$text"
    return 0
  fi
  local thread_args=()
  if [ -n "$thread_id" ]; then
    thread_args=(-d "message_thread_id=${thread_id}")
  fi
  resp="$(curl -s -m 15 "${TELEGRAM_API_BASE:-https://api.telegram.org}/bot${token}/sendMessage" \
    -d "chat_id=${chat_id}" "${thread_args[@]}" -d "parse_mode=HTML" --data-urlencode "text=$text" 2>/dev/null)"
  case "$resp" in *'"ok":true'*) return 0 ;; esac
  resp="$(curl -s -m 15 "${TELEGRAM_API_BASE:-https://api.telegram.org}/bot${token}/sendMessage" \
    -d "chat_id=${chat_id}" "${thread_args[@]}" --data-urlencode "text=$text" 2>/dev/null)"
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

# notify_auto가 **지금** 호출되면 큐에 넣을지(장중) 즉시 보낼지(장외)를 미리
# 알려준다 — notify_auto 자체의 동작·반환값 계약(0/1)은 바꾸지 않는 순수 조회다.
# 호출부가 "전송 성공"과 "큐 적재"를 구분해 로그에 남기고 싶을 때 쓴다
# (session_pnl.sh/manual_recs.sh, 2026-09-04) — notify_auto를 부르기 직전/직후
# 어느 쪽에서 호출해도 같은 순간의 벽시계를 보므로 결과가 갈릴 일은 사실상 없다.
notify_auto_would_defer() {
  _in_market_hours
}

fi
