#!/usr/bin/env bash
# 장 마감 하루 요약 HTML 한 장을 텔레그램 **파일**로 보낸다 (2026-08-28 소유자 지시).
# 사용법: server/scripts/daily_wrap.sh {KR|US}
# 크론:  KR 55 16 * * 1-5  /  US 55 6 * * 2-6  (마감 정산·피드백 뒤)
#
# ## 왜 sendMessage 가 아니라 sendDocument 인가
#
# "텔레그램 메시지가 너무 복잡하다 — 매매 관련만 장중에 보내고, 마감 뒤엔 하루
# 요약을 HTML 파일로 달라." 메시지로 보내면 4096자 제한에 맞춰 잘라야 하고,
# 잘린 요약은 결국 또 하나의 시끄러운 메시지가 된다. 파일은 열어야 보이므로
# **장중 알림 흐름을 오염시키지 않는다** — 그게 이 리포트의 역할 분리다.
#
# 저장소 최초의 sendDocument 사용처다. 형식: multipart/form-data 로
#   document=@<파일>  +  chat_id  +  caption(한 줄)
# curl 의 -F 는 파일명에 `@`/`;` 가 있으면 오해할 수 있으므로 경로를 그대로
# 넘기지 않고 `document=@<path>;type=text/html` 로 타입까지 못박는다.
#
# 테스트: DRY_RUN=1 ./server/scripts/daily_wrap.sh KR   (렌더만, 전송 없음)
set -u
cd "$(dirname "$0")/../.."

MARKET="${1:-}"
if [ "$MARKET" != "KR" ] && [ "$MARKET" != "US" ]; then
  echo "사용법: $0 {KR|US}" >&2
  exit 2
fi

LOG="data/daily_wrap.log"
mkdir -p data

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

# 메모리 워터마크(2026-08-31, 2GB 박스 관측) — server/scripts/lib/memlog.sh 참고.
. "$(dirname "$0")/lib/memlog.sh"
memlog_wrap "daily_wrap"

# 서술(narration) 게이트 — server/scripts/lib/notify.sh (2026-09-04, 소유자 지시:
# 문서 앞에 자연어 요약을 한 통 더 보낸다). OPS_NARRATOR 가 꺼져 있거나 서술이
# 실패하면 `--narration-only` 는 무출력이고, 여기서도 조용히 건너뛴다 — 문서+
# 캡션 계약은 그대로다.
. "$(dirname "$0")/lib/notify.sh"

# TZ 가드 — 크론 시각(KR 16:55 / US 06:55)은 호스트가 KST 라는 전제다.
# ai_trader.sh 와 같은 계약이되, 여기서는 **중단하지 않는다**: 요약 파일은
# 시각이 조금 어긋나도 만드는 편이 낫다(빠진 날은 나중에 복원할 수 없다).
if [ "$(date +%z)" != "+0900" ]; then
  log "호스트 TZ 가 KST 가 아님($(date +%z)) — 크론 시각 전제가 깨졌을 수 있음(계속 진행)"
fi

OUT="$(timeout 120 .venv/bin/python -m quant.apps.cli daily-wrap --market "$MARKET" 2>>"$LOG")"
RC=$?
if [ "$RC" -ne 0 ] || [ -z "$OUT" ]; then
  log "$MARKET 렌더 실패 exit=$RC — 전송 없음"
  exit 0   # 요약 실패는 경보가 아니다(잡 생존은 cli health 하트비트가 본다)
fi

FILE="$(printf '%s\n' "$OUT" | head -1)"
CAPTION="$(printf '%s\n' "$OUT" | grep -E '^CAPTION:' | head -1 | sed 's/^CAPTION:[[:space:]]*//')"
[ -z "$CAPTION" ] && CAPTION="${MARKET} 마감 요약"

if [ ! -f "$FILE" ]; then
  log "$MARKET 렌더는 성공했는데 파일이 없다: $FILE — 전송 없음"
  exit 0
fi

# 서술 미리보기 — 문서와 별도로 stdout 하나만 낸다(위 notify.sh 주석 참고).
# 렌더 실패는 문서 발송을 막지 않는다(narration은 부가 기능, 문서가 본체다).
NARRATION="$(timeout 60 .venv/bin/python -m quant.apps.cli daily-wrap --market "$MARKET" --narration-only 2>>"$LOG")"

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[DRY_RUN] file=$FILE"
  echo "[DRY_RUN] caption=$CAPTION"
  [ -n "$NARRATION" ] && echo "[DRY_RUN] narration=$NARRATION"
  exit 0
fi

if [ -z "$TG_TOKEN" ] || [ -z "$TG_CHAT" ]; then
  log "$MARKET 텔레그램 자격증명 없음 — 파일만 생성: $FILE"
  exit 0
fi

# 문서보다 먼저 보낸다(소유자 지시) — notify_now는 parse_mode=HTML로 즉시
# 발송하고 텔레그램이 태그를 거부하면 평문으로 재시도한다(lib/notify.sh 계약).
if [ -n "$NARRATION" ]; then
  notify_now "$NARRATION" || log "$MARKET 서술 메시지 발송 실패 — 문서 발송은 계속 진행"
fi

send() {
  # 성공 여부를 반환한다(ops_watch.sh 와 같은 계약) — 실패를 삼키면 재시도가
  # 사라진다. 텔레그램 응답의 "ok":true 로 판정.
  RESP="$(curl -s -m 60 "https://api.telegram.org/bot${TG_TOKEN}/sendDocument" \
    -F "chat_id=${TG_CHAT}" \
    -F "document=@${FILE};type=text/html" \
    -F "caption=${CAPTION}" 2>/dev/null)"
  case "$RESP" in *'"ok":true'*) return 0 ;; esac
  return 1
}

if send; then
  log "$MARKET 전송 성공 — $FILE"
  exit 0
fi

log "$MARKET 1차 전송 실패 — 20초 후 1회 재시도"
sleep 20
if send; then
  log "$MARKET 재시도 전송 성공 — $FILE"
else
  # 그래도 실패하면 로그만 — 실패 알림을 또 텔레그램으로 쏘면 그것이 곧
  # 소유자가 없애 달라고 한 소음이다. 파일은 디스크에 남아 있다.
  log "$MARKET 재시도도 실패 — 로그만 남긴다 (파일은 $FILE 에 있음)"
fi
exit 0
