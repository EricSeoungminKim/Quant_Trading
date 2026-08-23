#!/usr/bin/env bash
# 복원 리허설 — **안 해본 백업은 백업이 아니다.** Phase 5.1 의 완료 조건.
#
#   ./server/scripts/backup_restore_check.sh [번들경로]     # 생략하면 가장 최근 것
#
# 대조(`backup --verify`)는 "번들이 자기 매니페스트와 일치한다"만 증명한다. 그건
# 번들이 **쓸 수 있는지**를 말해주지 않는다. 여기서 실제로 풀어서:
#
#   1. 아티팩트가 파싱되나 (jsonl 한 줄씩 JSON 으로, portfolio.json 이 dict 로)
#   2. 라이브보다 줄이 많지 않나 — 원장은 append-only 이므로 백업 > 라이브면
#      **라이브가 잘렸다**는 뜻이고, 그건 백업 문제가 아니라 더 큰 문제다
#   3. MySQL 덤프가 빈 DB 에 실제로 적재되나 + 주요 테이블 행 수가 라이브와 맞나
#
# 3번은 스크래치 DB(`<DB>_restore_rehearsal`)를 만들고 끝나면 지운다. 권한이 없으면
# **"확인 불가"로 보고한다** — "정상"으로 넘기지 않는다.
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
BUNDLE="${1:-}"
if [ -z "$BUNDLE" ]; then
  BUNDLE="$(ls -1t data/backups/quant-*.tar.gz 2>/dev/null | head -1)"
fi
if [ -z "$BUNDLE" ] || [ ! -f "$BUNDLE" ]; then
  echo "리허설할 번들이 없다 (data/backups/quant-*.tar.gz)" >&2
  exit 2
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILED=0
# **"확인 못 했다"를 "통과"에 합산하지 않는다.** 종료코드 세 개로 나눈다:
#   0 = 전부 확인하고 통과   1 = 문제 있음   2 = 부분 통과(모르는 항목이 있다)
# 첫 리허설에서 실제로 이걸 틀렸다 — MySQL 이 "확인 불가"인데 "리허설 통과"를
# 찍고 exit 0 을 냈다. 이 phase 가 막으려는 결함을 검증 도구가 저지른 것이다.
UNKNOWN=0
say() { printf '%s\n' "$*"; }

say "=== 복원 리허설: $BUNDLE ==="

# --- 1. 매니페스트 대조 ---
if "$PY" -m quant.apps.cli backup --verify "$BUNDLE" >/dev/null 2>&1; then
  say "[1/3] 매니페스트 대조: OK"
else
  say "[1/3] 매니페스트 대조: 실패"
  "$PY" -m quant.apps.cli backup --verify "$BUNDLE" 2>&1 | sed 's/^/      /'
  FAILED=1
fi

# --- 2. 풀어서 실제로 읽어본다 ---
tar -xzf "$BUNDLE" -C "$WORK" || { say "[2/3] 풀기 실패"; exit 1; }

if "$PY" - "$WORK" <<'PYEOF'
import json, sys
from pathlib import Path

work = Path(sys.argv[1])
live = Path("data")
problems, checked = [], 0

for path in sorted(work.rglob("*.jsonl")):
    rel = path.relative_to(work)
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError as e:
            problems.append(f"{rel}:{n} JSON 파싱 실패: {e}")
            break
    checked += 1

    # append-only 이므로 라이브가 같거나 더 길어야 한다. 백업이 더 길면 라이브가
    # 잘린 것이다 — 백업 문제가 아니라 더 큰 문제이므로 조용히 넘기지 않는다.
    live_file = live / rel
    if live_file.exists():
        n_backup = sum(1 for x in path.read_text(encoding="utf-8").splitlines() if x.strip())
        n_live = sum(1 for x in live_file.read_text(encoding="utf-8").splitlines() if x.strip())
        if n_live < n_backup:
            problems.append(f"{rel}: 라이브가 백업보다 짧다 ({n_live} < {n_backup}) — 라이브가 잘렸다")

for path in sorted(work.rglob("*.json")):
    if path.name == "MANIFEST.json":
        continue
    try:
        json.loads(path.read_text(encoding="utf-8"))
        checked += 1
    except json.JSONDecodeError as e:
        problems.append(f"{path.relative_to(work)} JSON 파싱 실패: {e}")

print(f"      파일 {checked}개 파싱 확인")
for p in problems:
    print(f"      {p}")
sys.exit(1 if problems else 0)
PYEOF
then
  say "[2/3] 아티팩트 복원·파싱: OK"
else
  say "[2/3] 아티팩트 복원·파싱: 실패"
  FAILED=1
fi

# --- 3. MySQL 덤프를 스크래치 DB 에 실제로 적재 ---
_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
DB="$(_env MYSQL_DATABASE)"
DUMP="$(ls -1 "$WORK"/mysql/*.sql.gz 2>/dev/null | head -1)"

if [ -z "$DB" ]; then
  # MySQL 이 없는 박스(개발용 Mac)일 수 있다. 그래도 "통과"라고 부르지 않는다 —
  # 이 리허설은 DB 복원을 시험하지 못했다.
  say "[3/3] MySQL: MYSQL_DATABASE 미설정 — **확인 불가**(DB 복원은 시험되지 않았다)"
  UNKNOWN=1
elif [ -z "$DUMP" ]; then
  # DB 가 설정된 박스인데 번들에 덤프가 없다 = 백업이 DB 를 빼먹었다. 이건 실패다.
  say "[3/3] MySQL: DB 는 설정됐는데 번들에 덤프가 없다 — 백업이 DB 를 빼먹었다"
  FAILED=1
else
  SCRATCH="${DB}_restore_rehearsal"
  MH="$(_env MYSQL_HOST)"; MH="${MH:-127.0.0.1}"
  MU="$(_env MYSQL_USER)"
  export MYSQL_PWD="$(_env MYSQL_PASSWORD)"
  my() { mysql --host="$MH" --user="$MU" --default-character-set=utf8mb4 "$@"; }

  if ! my -e "DROP DATABASE IF EXISTS \`$SCRATCH\`; CREATE DATABASE \`$SCRATCH\` CHARACTER SET utf8mb4;" 2>/dev/null; then
    say "[3/3] MySQL: 스크래치 DB 를 만들 권한이 없다 — **확인 불가**"
    say "      복원 가능 여부를 모르는 상태다. CREATE 권한을 주거나 사람이 수동 리허설할 것:"
    say "      gunzip -c <덤프> | mysql -u <user> <스크래치DB>"
    UNKNOWN=1
  else
    if gunzip -c "$DUMP" | my "$SCRATCH" 2>/dev/null; then
      MISMATCH=0
      for t in article trade selection; do
        L="$(my -N -B -e "SELECT COUNT(*) FROM \`$t\`" "$DB" 2>/dev/null || echo "?")"
        R="$(my -N -B -e "SELECT COUNT(*) FROM \`$t\`" "$SCRATCH" 2>/dev/null || echo "?")"
        # 라이브가 더 많은 건 정상이다(백업 이후 적재됐다). 복원본이 더 많으면 이상.
        if [ "$L" = "?" ] || [ "$R" = "?" ]; then
          say "      $t: 확인 불가 (라이브=$L 복원=$R)"; UNKNOWN=1
        elif [ "$R" -gt "$L" ]; then
          say "      $t: 복원본이 라이브보다 많다 (라이브=$L 복원=$R) — 라이브가 줄었다"; MISMATCH=1
        else
          say "      $t: 라이브=$L 복원=$R"
        fi
      done
      [ "$MISMATCH" -eq 0 ] && say "[3/3] MySQL 복원: OK" || { say "[3/3] MySQL 복원: 이상"; FAILED=1; }
    else
      say "[3/3] MySQL 복원: 덤프 적재 실패 — **이 백업으로는 DB 를 되살릴 수 없다**"
      FAILED=1
    fi
    my -e "DROP DATABASE IF EXISTS \`$SCRATCH\`;" 2>/dev/null || \
      say "      경고: 스크래치 DB($SCRATCH) 를 지우지 못했다 — 수동 정리 필요"
  fi
  unset MYSQL_PWD
fi

if [ "$FAILED" -ne 0 ]; then
  say "=== 결과: 문제 있음 — 위 항목 확인 (이 백업으로 복원할 수 있다고 믿지 말 것) ==="
  exit 1
fi
if [ "$UNKNOWN" -ne 0 ]; then
  say "=== 결과: 부분 통과 — 확인 못 한 항목이 있다. '리허설했다'고 기록하지 말 것 ==="
  exit 2
fi
say "=== 결과: 리허설 통과 (전 항목 확인) ==="
exit 0
