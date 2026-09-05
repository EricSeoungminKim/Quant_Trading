#!/usr/bin/env bash
# [임시 크론] 월-금 09:05 KST — 키움 웹소켓 실시간이 장중에 진짜 오는지 자동 검증.
#
# 배경: 모의키 토큰 발급은 확인됐지만(2026-08-10 새벽), 웹소켓은 장외라 서버가
# 연결을 끊어 검증 불가였다. 장중(09:05, KR 개장 5분 뒤 — 체결이 확실히 흐르는
# 시각)에 모의/실전 서버를 각각 15초씩 찔러 틱 수신 여부를 텔레그램으로 보고한다.
#
# 검증 완료 후 절차: last_tick_at이 찍히는 서버가 확인되면
#   1) config/settings.yaml: kiwoom.realtime.enabled: true (+ 필요시 WS URL 결정)
#   2) server/crontab.txt에서 이 크론 제거
# 이 스크립트는 주문 기능이 전혀 없다 — kiwoom-probe는 읽기 전용 조회뿐.
set -u
cd "$(dirname "$0")/../.."

set -a; [ -f .env.local ] && . ./.env.local; set +a
# notify_now (역할별 게이트 — server/scripts/lib/notify.sh). 이 임시 크론은 09:05,
# 즉 **장중 한복판**에 돈다. 그래도 즉시 보내는 이유: 여기서 보고하는 건 실시간
# 시세 인증·수신 여부라, 틀렸으면 그날 장 내내 시세가 느린 채로 매매한다.
# (검증이 끝나면 이 크론 자체가 제거된다 — 파일 상단 절차 참고.)
. "$(dirname "$0")/lib/notify.sh"
NOTIFY_LANE="ops"  # 텔레그램 포럼 토픽 레인 — docs/runbooks/telegram-rooms.md

probe() {  # $1=라벨 $2=appkey $3=secret $4=rest_base $5=ws_url
  if [ -z "$2" ] || [ -z "$3" ]; then
    echo "[$1] 키 미설정 — 건너뜀"
    return
  fi
  out=$(KIWOOM_APP_KEY="$2" KIWOOM_SECRET_KEY="$3" KIWOOM_BASE_URL="$4" KIWOOM_WS_URL="$5" \
        timeout 90 .venv/bin/python -m quant.apps.cli kiwoom-probe --symbol 005930 --seconds 15 2>&1 \
        | grep -E "토큰|health:|last quote:" | head -4)
  echo "[$1]
$out"
}

R_MOCK="$(probe "모의 mockapi" "${KIWOOM_MOCK_APP_KEY:-}" "${KIWOOM_MOCK_SECRET_KEY:-}" \
  "https://mockapi.kiwoom.com" "wss://mockapi.kiwoom.com:10000/api/dostk/websocket")"
R_REAL="$(probe "실전 api" "${KIWOOM_APP_KEY:-}" "${KIWOOM_SECRET_KEY:-}" \
  "https://api.kiwoom.com" "wss://api.kiwoom.com:10000/api/dostk/websocket")"

notify_now "🔌 키움 웹소켓 장중 검증 (005930, 15초씩)
${R_MOCK}
${R_REAL}
— last quote에 값이 있으면 성공. 성공한 서버 기준으로 kiwoom.realtime.enabled 켜면 됨."
echo "[$(date '+%F %T')] kiwoom_ws_check 완료" >&2
