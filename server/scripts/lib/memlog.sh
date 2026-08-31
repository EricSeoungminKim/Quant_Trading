#!/usr/bin/env bash
# 메모리 워터마크 로그 — 무거운 크론이 시작/종료 시점에 `free -m` 요약 1줄을
# data/memlog.jsonl 에 append 한다 (2026-08-31, 2GB 박스 최적화).
#
# 왜 필요한가: 크론 몰림(server/crontab.txt 상단 주석)을 시각으로 벌려 놨어도
# "실제로 메모리가 얼마나 쓰였나"는 관측치가 없으면 다음 최적화가 감으로 하는
# 추측이 된다. 무거운 크론 6개(백필 4종 + close_report + daily_wrap)만 대상 —
# run_report.sh 경유 systemd 리포트 빌드는 이미 자체 로그(data/report.log)가
# 있고 이 파일이 손댈 영역이 아니다.
#
# 줄 형식(계약): {"ts":"...","script":"backfill_1m","phase":"start","mem_used_mb":123,"swap_used_mb":0}
#
# ## 안전 계약 (notify.sh 와 같은 원칙)
# - `free` 가 없으면(로컬 맥) 조용히 아무것도 안 쓴다. 크론 스크립트를 절대
#   막지 않는다 — 이 로그는 부가 관측이지 본작업이 아니다.
# - 큐 파일 쓰기 실패도 스크립트를 죽이지 않는다.
# - **호출은 한 줄**: `memlog_wrap "<script>"` 를 스크립트 상단에서 한 번만
#   부르면 시작 기록을 즉시 남기고, 그 스크립트가 어떤 exit 경로(성공/실패/
#   DRY_RUN 조기 종료)로 끝나든 EXIT 트랩이 종료 기록을 남긴다 — 백필
#   스크립트마다 3~4개인 exit 지점 전부에 수동으로 심을 필요가 없다.
# - 이 파일은 **멱등하게 source 가능**하다(아래 가드).
#
# 사용법: . "$(dirname "$0")/lib/memlog.sh"; memlog_wrap "$(basename "$0" .sh)"

if [ -z "${_MEMLOG_SH_LOADED:-}" ]; then
_MEMLOG_SH_LOADED=1

# 저장소 루트 — 호출 스크립트의 cwd 에 의존하지 않는다(notify.sh 와 동일 관례).
_MEMLOG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

_memlog_write() {  # $1=script $2=phase(start|end)
  command -v free >/dev/null 2>&1 || return 0
  local snap mem swap f line
  snap="$(free -m 2>/dev/null)" || return 0
  mem="$(printf '%s\n' "$snap" | awk '/^Mem:/{print $3}')"
  swap="$(printf '%s\n' "$snap" | awk '/^Swap:/{print $3}')"
  [ -n "$mem" ] || return 0
  case "$swap" in ''|*[!0-9]*) swap=0 ;; esac
  f="${MEMLOG_FILE:-$_MEMLOG_ROOT/data/memlog.jsonl}"
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  line="$(printf '{"ts":"%s","script":"%s","phase":"%s","mem_used_mb":%s,"swap_used_mb":%s}' \
    "$(date +%Y-%m-%dT%H:%M:%S%z)" "$1" "$2" "$mem" "$swap")"
  # 크론이 겹치면 append 원자성이 깨질 수 있다(notify.sh 큐와 같은 이유) — flock
  # 이 있으면 쓴다, 없으면(맥) 그냥 append.
  if command -v flock >/dev/null 2>&1; then
    ( flock 202; printf '%s\n' "$line" >&202 ) 202>>"$f" 2>/dev/null || true
  else
    printf '%s\n' "$line" >> "$f" 2>/dev/null || true
  fi
  return 0
}

# 공개 API — 스크립트 상단에서 한 번만 부른다.
memlog_wrap() {  # $1=script(basename, 확장자 없이)
  local name="$1"
  _memlog_write "$name" "start"
  trap "_memlog_write '$name' end" EXIT
}

fi
