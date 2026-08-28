#!/usr/bin/env bash
# 매일 03:30 KST — 아티팩트 + MySQL 덤프를 번들로. Phase 5.1.
#
# 왜: **아티팩트가 진실인데 EC2 디스크 한 곳에만 있었다.** DB 엔진 선택보다 큰
# 위험이다. 그리고 누적 뉴스는 되찾을 수 없다 — RSS 를 9시간 뒤 재수집하면 주요
# 피드 겹침이 0 이었다(실측). 원장은 애초에 재생성이 없다.
#
# 이 스크립트는 **번들을 만들 뿐 전송하지 않는다.** 전송은 받는 쪽이 당긴다
# (`backup_pull.sh`, Mac 에서 실행) — EC2 에 오프사이트 자격증명을 두지 않으려는
# 것이다. EC2 가 털리면 백업까지 지울 수 있는 키가 같은 박스에 있으면 안 된다.
#
# 번들 안 매니페스트와의 대조·회귀(줄어듦) 검사는 `quant.apps.cli backup` 이 한다.
# 문제가 있으면 종료코드 1 이고, 여기서 알린다.
#
# 테스트: DRY_RUN=1 ./server/scripts/backup.sh
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
OUT="data/backups"
LOG="data/backup.log"
KEEP=14           # 2주. 13MB짜리라 디스크가 아니라 "언제까지 되돌릴 수 있나"가 기준이다.
TMP="$OUT/.tmp"

mkdir -p "$OUT" "$TMP"
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
# 알림은 전부 notify_now (역할별 게이트 — server/scripts/lib/notify.sh). 백업이
# 조용히 실패한 채로 며칠 지나면 복원할 때까지 아무도 모른다 — 그래서 즉시.
# (성공은 원래 침묵이다. 여기서 나가는 건 전부 실패·경고다.)
. "$(dirname "$0")/lib/notify.sh"

STAMP="$(date +%Y%m%d-%H%M%S)"
DUMP="$TMP/mysql-${STAMP}.sql.gz"
INCLUDE=()

# --- 1. MySQL 덤프 (있으면) ---
#
# MySQL 은 아티팩트에서 재적재하면 복구된다(control/warehouse.py). 그래도 담는
# 이유는 재적재가 몇 시간이고, 장애 중에 몇 시간은 길다는 것뿐이다. 그래서
# **덤프 실패가 아티팩트 백업을 막지 않는다** — 진짜로 못 되찾는 건 아티팩트다.
DB="$(_env MYSQL_DATABASE)"
if [ -n "$DB" ]; then
  # 비밀번호를 인자로 주지 않는다(ps 에 보인다). MYSQL_PWD 를 이 명령에만 건다.
  # `--no-tablespaces`: 덤프 사용자에게 PROCESS 권한이 없으면 mysqldump 가
  # "Access denied ... PROCESS privilege ... when trying to dump tablespaces" 를
  # 내뱉는다(2026-08-14 실측). 덤프 자체는 성공하지만 로그에 매번 에러가 남아
  # 진짜 에러를 가린다. 논리 백업에 tablespace 정보는 필요 없다 — 끈다.
  # (전역 PROCESS 권한을 주는 것보다 이쪽이 최소권한이다.)
  if MYSQL_PWD="$(_env MYSQL_PASSWORD)" mysqldump \
       --host="$(_env MYSQL_HOST || echo 127.0.0.1)" \
       --user="$(_env MYSQL_USER)" \
       --single-transaction --quick --default-character-set=utf8mb4 \
       --no-tablespaces \
       "$DB" 2>>"$LOG" | gzip > "$DUMP"; then
    # 파이프라인이라 mysqldump 실패가 gzip 성공에 묻힐 수 있다 — 크기로 되짚는다.
    if [ -s "$DUMP" ]; then
      INCLUDE+=(--include "$DUMP")
      log "MySQL 덤프 $(du -h "$DUMP" | cut -f1)"
    else
      log "MySQL 덤프가 0바이트 — 번들에서 제외"
      notify_now "⚠️ 백업: MySQL 덤프가 0바이트다. 아티팩트만 담는다 (재적재로 복구 가능하지만 확인 필요)"
      rm -f "$DUMP"
    fi
  else
    log "mysqldump 실패 — 번들에서 제외"
    notify_now "⚠️ 백업: mysqldump 실패. 아티팩트만 담는다 — data/backup.log 확인"
    rm -f "$DUMP"
  fi
else
  log "MYSQL_DATABASE 없음 — 아티팩트만 담는다"
fi

# --- 2. 번들 ---
OUT_JSON="$("$PY" -m quant.apps.cli backup --out "$OUT" "${INCLUDE[@]+"${INCLUDE[@]}"}" 2>&1)"
RC=$?
printf '%s\n' "$OUT_JSON" >> "$LOG"
rm -f "$DUMP"

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "$OUT_JSON"
  echo "[DRY_RUN] backup rc=$RC"
fi

if [ "$RC" -ne 0 ]; then
  notify_now "🚨 백업 실패 (rc=${RC})
$(printf '%s' "$OUT_JSON" | tail -c 900)

'줄이 줄었다'가 보이면 **원장 소스가 망가진 것**이다 — 그 상태를 백업하면 지난
백업까지 밀려나므로, 원인을 보기 전에 다시 돌리지 말 것."
  exit 1
fi

# --- 3. 보관 개수 ---
# 오래된 것부터 지운다. 지운 개수만 로그에 남긴다.
GONE="$(ls -1t "$OUT"/quant-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | wc -l | tr -d ' ')"
if [ "${GONE:-0}" -gt 0 ] && [ "${DRY_RUN:-0}" != "1" ]; then
  ls -1t "$OUT"/quant-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
fi
log "완료 · 보관 $(ls -1 "$OUT"/quant-*.tar.gz 2>/dev/null | wc -l | tr -d ' ')개 · 정리 ${GONE:-0}개"
exit 0
