#!/usr/bin/env bash
# 킬스위치 드릴 — /halt·/resume 이 "버튼이 있다"가 아니라 "눌렀을 때 실제로
# 꺼진다"는 것을 사람이 눈으로 확인하는 절차를 자동화한다.
#
# **이 스크립트는 /halt·/resume 을 절대 스스로 보내지 않는다.** 텔레그램 앱 →
# tg-bridge 프로세스 → data/state/control.json 쓰기 → 엔진이 다음 사이클에
# 그 파일을 다시 읽는 경로 전체를 검증하는 게 요점이다 — 스크립트가 파일을
# 직접 써서 흉내 내면 그 경로의 절반(브리지가 실제로 명령을 받는지)을 건너뛰고
# "확인했다"는 착각만 남는다. 이 스크립트는 오직 **읽고 기다릴 뿐**이다.
#
# 사용법: ./server/scripts/halt_drill.sh
#   1. 시작 전 control.json 상태를 보여준다.
#   2. "텔레그램에서 지금 /halt [사유] 를 보내세요" 안내 → halted:true 가
#      반영될 때까지 폴링하며 걸린 시간을 찍는다.
#   3. 이어서 "/resume 을 보내세요" 안내 → halted:false 반영까지 폴링.
#   4. 두 구간 모두 반영되면 exit 0. 타임아웃(기본 300초, HALT_DRILL_TIMEOUT_
#      SECONDS 로 조정)이면 그 구간에서 exit 1(halt 대기 실패) / 2(resume 대기
#      실패) — 어느 쪽이 막혔는지 알아야 브리지를 볼지 엔진을 볼지 정할 수 있다.
#
# 명령은 브리지가 명령을 읽는 채팅 어디서든 보내면 된다(오너 본인 채팅이면
# 어디든 허용 — docs/runbooks/telegram-rooms.md §2) — 포럼 토픽을 묶어뒀다면
# 🎛 제어실에서 보내는 것을 권장한다.
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
TIMEOUT="${HALT_DRILL_TIMEOUT_SECONDS:-300}"
POLL_INTERVAL="${HALT_DRILL_POLL_SECONDS:-2}"

# control.json 을 읽어 "halted|halt_reason|halted_by" 한 줄로 낸다. 파일이 없거나
# 깨졌으면 TradingControl 의 기본값(halted=False)을 그대로 쓴다 — 엔진이 같은
# 상황에서 보는 것과 정확히 같은 판정이어야 드릴이 "실제로 무엇을 확인했는지"가
# 거짓말을 하지 않는다.
_state() {
  "$PY" -c '
from quant.trade.control import TradingControl

c = TradingControl()
halted = c.is_halted()
reason = c.halt_reason() if halted else ""
by = c.halted_by() if halted else ""
print(f"{halted}|{reason}|{by}")
'
}

# $1 = "True" 또는 "False" — control.json 의 halted 값이 그렇게 될 때까지 기다린다.
_wait_for() {
  local want="$1" start now elapsed line halted reason by
  start="$(date +%s)"
  while true; do
    line="$(_state)"
    IFS='|' read -r halted reason by <<< "$line"
    now="$(date +%s)"
    elapsed=$((now - start))
    if [ "$halted" = "$want" ]; then
      echo "  → 반영됨 (${elapsed}초 경과)$([ -n "$reason" ] && echo " — 사유: ${reason}")$([ -n "$by" ] && echo " (${by})")"
      return 0
    fi
    if [ "$elapsed" -ge "$TIMEOUT" ]; then
      echo "  → 타임아웃(${TIMEOUT}초) — 반영되지 않았다."
      echo "     확인할 것: tg-bridge.service 가 살아있는가(systemctl status tg-bridge),"
      echo "     명령을 보낸 채팅이 오너 본인 채팅인가(docs/runbooks/telegram-rooms.md §2),"
      echo "     엔진(quant-engine.service)이 control.json 을 읽는 경로가 맞는가."
      return 1
    fi
    sleep "$POLL_INTERVAL"
  done
}

echo "== 킬스위치 드릴 (server/scripts/halt_drill.sh) =="
echo "이 스크립트는 /halt·/resume 을 절대 스스로 보내지 않습니다 — 아래 안내에"
echo "따라 소유자가 직접 텔레그램에서 명령을 보내세요."
echo

echo "-- 시작 전 상태 (data/state/control.json) --"
line="$(_state)"
IFS='|' read -r halted reason by <<< "$line"
echo "halted=${halted} reason='${reason}' by='${by}'"
if [ "$halted" = "True" ]; then
  echo
  echo "경고: 이미 halted 상태입니다. 이대로 진행하면 1) /halt 대기가 즉시"
  echo "통과로 뜹니다 — 먼저 텔레그램에서 /resume 을 보내 정상 상태로 되돌린 뒤"
  echo "다시 실행하는 것을 권장합니다(Ctrl+C 로 지금 종료 가능)."
fi
echo

echo "1) 텔레그램에서 지금 /halt 드릴테스트 를 보내세요."
echo "   (최대 ${TIMEOUT}초 대기, ${POLL_INTERVAL}초마다 control.json 확인)"
if ! _wait_for "True"; then
  echo
  echo "== 실패 — /halt 가 반영되지 않았다 (제어 경로: 텔레그램→브리지→control.json) =="
  exit 1
fi
echo

echo "2) 이어서 텔레그램에서 /resume 을 보내세요."
echo "   (최대 ${TIMEOUT}초 대기)"
if ! _wait_for "False"; then
  echo
  echo "== 부분 실패 — /halt 는 반영됐지만 /resume 이 반영되지 않았다 =="
  exit 2
fi
echo

echo "== 통과 — /halt·/resume 경로(텔레그램→브리지→control.json→엔진)가 실제로 동작한다 =="
exit 0
