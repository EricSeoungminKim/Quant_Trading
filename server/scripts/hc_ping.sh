#!/usr/bin/env bash
# 데드맨 스위치 핑 (healthchecks.io) — cron이 10분마다 실행.
#
# 설계: 엔진(quant/trade)에 네트워크 호출을 넣지 않는다(평면 불변식). 엔진은
# 지금처럼 heartbeat.json만 쓰고, 이 스크립트가 **밖에서** 그 파일의 신선도를
# 보고 핑을 쏜다. 그래서 세 층이 한꺼번에 감시된다:
#   - 엔진이 죽으면      → heartbeat 정체 → /fail 핑 → 즉시 알림
#   - cron/EC2가 죽으면  → 핑 자체가 끊김 → healthchecks 유예(grace) 후 알림
#   - 텔레그램이 죽어도  → healthchecks는 이메일 등 별도 채널로 알림
#
# HEALTHCHECKS_PING_URL 이 .env.local 에 없으면 아무것도 안 한다(에러 아님).
set -u
cd "$(dirname "$0")/../.."

URL=$(grep -m1 '^HEALTHCHECKS_PING_URL=' .env.local 2>/dev/null | cut -d= -f2-)
[ -z "${URL:-}" ] && exit 0

HB=data/state/heartbeat.json
now=$(date +%s)
mtime=$(stat -c %Y "$HB" 2>/dev/null || echo 0)
age=$((now - mtime))

if [ "$age" -lt 900 ]; then
  curl -fsS -m 10 --retry 3 -o /dev/null "$URL"
else
  # 엔진 하트비트 정체 — /fail 로 즉시 다운 처리(유예 안 기다림), 사유 동봉
  curl -fsS -m 10 --retry 3 -o /dev/null --data-raw "heartbeat stale ${age}s" "$URL/fail"
fi
