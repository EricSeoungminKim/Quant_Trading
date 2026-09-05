#!/usr/bin/env bash
# 데드맨 스위치 — cron */5분. 엔진의 침묵과 정상을 구분한다.
#
# 왜 필요한가: 하트비트 텔레그램은 엔진이 살아 있을 때만 온다. **엔진이 죽거나
# 멈추면 침묵하는데, 침묵은 "조용한 정상"과 구분되지 않는다.** 크래시는 systemd
# Restart=always가 잡지만, 행(hang — 프로세스는 살아 있는데 루프가 멈춤)은 아무도
# 못 잡는다. 그래서 외부(cron)에서 두 가지를 본다:
#   1) 서비스가 active인가 (inactive면 systemd 복구가 실패했다는 뜻 — 사람 호출)
#   2) 하트비트 파일(data/state/heartbeat.json — 엔진이 매 사이클 기록)이 신선한가
#      루프는 장외에도 5초마다 돌므로 파일 나이 > 임계 = 행. 재시작 + 알림.
#
# 알림 중복 방지: 같은 장애로 5분마다 알림이 오면 소음이라 사람이 끈다 — 그게 최악.
# 장애당 1회 알림, 회복 시 상태 해제(+회복 알림 1회).
set -u
cd "$(dirname "$0")/../.."

HEARTBEAT="data/state/heartbeat.json"
STATE="data/state/watchdog.state"     # 마지막으로 알린 장애 종류 (dedup)
STALE_SECONDS=300                     # 루프 5초 주기 대비 60배 여유 — 오탐 방지
mkdir -p data/state

set -a; [ -f .env.local ] && . ./.env.local; set +a
# 알림은 전부 notify_now (역할별 게이트 — server/scripts/lib/notify.sh). 엔진이
# 죽거나 멈춘 것은 "지금 조치 안 하면 손해"의 원형이라 장중에도 즉시 나간다.
# 회복 알림도 그 장애의 짝이므로 같이 즉시 — 미뤄서 보내면 의미가 없다.
. "$(dirname "$0")/lib/notify.sh"
NOTIFY_LANE="ops"  # 텔레그램 포럼 토픽 레인 — docs/runbooks/telegram-rooms.md
alerted() { [ -f "$STATE" ] && grep -qx "$1" "$STATE"; }
mark()    { echo "$1" > "$STATE"; }
clear_mark() {
  if [ -f "$STATE" ]; then
    rm -f "$STATE"
    notify_now "✅ 워치독: 엔진 회복 확인" || true   # 발송 실패가 크론 exit 코드를 바꾸지 않게
  fi
}

# --- 1. 서비스 생존 ---
if ! systemctl is-active --quiet quant-engine; then
  if ! alerted "engine-down"; then
    mark "engine-down"
    notify_now "🚨 워치독: quant-engine 서비스 다운 — systemd 자동복구도 실패한 상태. 수동 확인 필요:
ssh 후 'journalctl -u quant-engine -n 50' / 'sudo systemctl restart quant-engine'"
  fi
  exit 0
fi

if ! systemctl is-active --quiet tg-bridge; then
  if ! alerted "bridge-down"; then
    mark "bridge-down"
    notify_now "⚠️ 워치독: tg-bridge 다운 — /watch·/halt 명령 불가 상태 (거래 엔진은 정상)"
  fi
  exit 0
fi

# --- 2. 하트비트 신선도 (행 감지) ---
# 파일이 아직 없으면(하트비트 미탑재 구버전) 이 검사는 건너뛴다 — 없는 기능으로
# 오탐 알림을 만들지 않는다.
if [ -f "$HEARTBEAT" ]; then
  age=$(( $(date +%s) - $(stat -c %Y "$HEARTBEAT" 2>/dev/null || echo 0) ))
  if [ "$age" -gt "$STALE_SECONDS" ]; then
    if ! alerted "engine-hung"; then
      mark "engine-hung"
      notify_now "🚨 워치독: 엔진 행(hang) 의심 — 하트비트 ${age}초 정체 (임계 ${STALE_SECONDS}초). 자동 재시작 시도."
    fi
    sudo systemctl restart quant-engine
    exit 0
  fi
fi

# --- 3. 실시간 시세 인증 실패 (조용히 죽은 피드 감지) ---
# 왜: 엔진은 실시간 피드가 죽어도 Toss REST로 폴백해 **정상적으로 계속 돈다** —
# 하트비트도 신선하고 서비스도 active다. 즉 위 1·2번 검사로는 절대 안 잡힌다.
# 실제로 2026-08-11~13에 키움 웹소켓이 20시간 넘게 30초마다 인증 실패했는데
# 아무 알림도 울리지 않았고, 로그 벽만 쌓였다.
#
# 이제 피드는 토큰을 재접속마다 새로 발급받으므로 만료는 스스로 회복한다. 그래도
# AUTH_ALERT_MARKER가 뜬다면 만료가 아니라 자격증명/계정/호스트 문제라는 뜻이라
# 사람이 봐야 한다. 재시작은 하지 않는다 — 자격증명 문제는 재시작으로 안 낫고,
# 장중에 거래 엔진을 흔드는 쪽이 더 위험하다.
AUTH_MARKER="Kiwoom WS 인증 연속 실패"
if journalctl -u quant-engine --since "10 min ago" --no-pager 2>/dev/null \
     | grep -q "$AUTH_MARKER"; then
  if ! alerted "realtime-auth"; then
    mark "realtime-auth"
    notify_now "🚨 워치독: 키움 실시간 시세 인증 연속 실패 — 웹소켓 피드가 죽어 있다.
거래는 Toss REST 폴백으로 계속되지만 시세가 느려진다(체결 지연/슬리피지 확대).
토큰 재발급으로도 안 되는 상황이라 자격증명·계정·호스트 확인 필요:
journalctl -u quant-engine | grep '인증 연속 실패' | tail -5"
  fi
  exit 0
fi

clear_mark
