#!/usr/bin/env bash
# 공개 포트폴리오 사이트 데이터 갱신 — 성과 JSON을 공개 저장소로 push.
#
# 왜 별도 저장소인가: 본 저장소는 전략 로직·운영 상세가 들어 있어 Vercel 에
# 접근 권한을 주는 것 자체가 위험하다. 공개 저장소에는 프론트엔드와 이 JSON
# 하나만 있고, 여기 push 하면 Vercel 이 자동 재배포한다.
#
# 왜 배포 키인가: EC2 가 공개 저장소에만 쓸 수 있어야 한다(본 저장소 자격증명
# 재사용 금지 — 사고 시 폭발 반경을 공개 저장소로 한정).
#
# 발행 게이트(2026-09-06, 오너 요구사항: "공개 사이트는 유실되거나 틀린 데이터를
# 절대 보여주면 안 된다") — push 하기 전에 `quant.apps.cli validate-performance`
# 로 $SRC 를 검증한다. 지금 이미 공개 저장소에 올라가 있는 버전(덮어쓰기 전에
# 미리 떠 둔다)을 `--previous`로 넘겨 체결 수 역행(데이터 유실)까지 본다.
# 오류가 하나라도 있으면 push를 **하지 않고** ops 레인으로 알린다.
#
# 크론: publish_performance.sh 직후(KR 16:25 / US 06:25).
# 조용한 것이 기본값 — 검증 실패를 포함해 실패해도 exit 0(리포팅 레인이 엔진을
# 죽이면 안 된다). systemd/cron 은 `.venv/bin/python` 을 직접 부른다 — `uv run`
# 아니다(server/CLAUDE.md 로컬 불변식).
set -u
cd "$(dirname "$0")/../.."
ROOT="$(pwd)"

# **절대 경로**로 잡는다 — 아래에서 클론 디렉토리로 cd 하므로 상대 경로 로그·
# 소스는 그 순간 사라진다(2026-09-02 실측: "data/portfolio_publish.log: No such
# file or directory" 로 로그가 통째로 날아갔다).
LOG="$ROOT/data/portfolio_publish.log"
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

# 알림은 notify_now(역할별 게이트 — server/scripts/lib/notify.sh) — 검증 실패로
# push 를 막는 건 조용히 넘어갈 일이 아니다(공개 사이트가 낡은 채로 멈춘다).
. "$(dirname "$0")/lib/notify.sh"
NOTIFY_LANE="ops"  # 텔레그램 포럼 토픽 레인 — docs/runbooks/telegram-rooms.md

SRC="$ROOT/data/public/performance.json"
WORK="$HOME/.cache/quant-portfolio"
KEY="$HOME/.ssh/id_portfolio"
REPO="git@github.com:EricSeoungminKim/quant-portfolio.git"
VENV_PY="$ROOT/.venv/bin/python"

[ -f "$SRC" ] || { log "성과 JSON 없음 — 스킵"; exit 0; }
[ -f "$KEY" ] || { log "배포 키 없음($KEY) — 스킵(소유자가 GitHub Deploy key 등록 필요)"; exit 0; }

export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

if [ ! -d "$WORK/.git" ]; then
  rm -rf "$WORK"
  git clone --depth 1 "$REPO" "$WORK" >>"$LOG" 2>&1 || { log "clone 실패 — 스킵"; exit 0; }
fi

cd "$WORK" || exit 0
git fetch --depth 1 origin main >>"$LOG" 2>&1 || { log "fetch 실패"; exit 0; }
git reset --hard origin/main >>"$LOG" 2>&1 || exit 0

# 덮어쓰기 전에 "지금 이미 발행된" 버전을 --previous 로 남긴다 — 검증기가
# 체결 수 역행을 이걸로 잡는다. 이번이 첫 발행이면(파일 없음) 그냥 없는 채로
# 넘긴다(validate-performance 가 그 검사만 건너뛴다).
PREV_JSON="$(mktemp)"
trap 'rm -f "$PREV_JSON"' EXIT
[ -f public/data/performance.json ] && cp public/data/performance.json "$PREV_JSON"

# settings.yaml을 기본 상대경로("config/settings.yaml")로 읽으므로 cwd가
# ROOT여야 한다 — 이 시점 cwd는 위에서 $WORK로 옮겨져 있어 서브셸로 감싼다
# (바깥 스크립트의 cwd는 그대로 $WORK 에 남는다).
VALIDATE_OUT="$(cd "$ROOT" && "$VENV_PY" -m quant.apps.cli validate-performance --payload "$SRC" --previous "$PREV_JSON" 2>>"$LOG")"
VALIDATE_RC=$?
printf '%s\n' "$VALIDATE_OUT" >>"$LOG"

if [ "$VALIDATE_RC" -ne 0 ]; then
  log "성과 JSON 검증 실패 — push 중단"
  FIRST3="$(printf '%s\n' "$VALIDATE_OUT" | grep '^error|' | head -3 | sed -E 's/^error\|([^|]*)\|(.*)$/• \1: \2/')"
  notify_now "🚨 공개 성과 JSON 검증 실패 — push 중단
${FIRST3}"
  exit 0
fi

# 성공 로그 — 체결 수(전략별 total.trips 합)와 generated_at. 이 파싱은 quant
# 패키지 임포트가 필요 없는 순수 표준 라이브러리 json 이라 $WORK(사이트 저장소,
# 파이썬 venv 없음)에서도 안전하다.
TRADE_INFO="$("$VENV_PY" - "$SRC" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    d = json.load(f)
trips = sum(
    (s.get("total") or {}).get("trips", 0)
    for s in d.get("strategies", []) if isinstance(s, dict)
)
print(f"{trips}\t{d.get('generated_at')}")
PY
)"
TRIPS="${TRADE_INFO%%$'\t'*}"
GENERATED_AT="${TRADE_INFO#*$'\t'}"

mkdir -p public/data
cp "$SRC" public/data/performance.json

if git diff --quiet -- public/data/performance.json; then
  log "변경 없음 — push 생략 (체결 ${TRIPS}건, generated_at ${GENERATED_AT})"
  exit 0
fi

git -c user.name="quant-engine" -c user.email="noreply@localhost" \
  commit -q -m "data: 성과 갱신 $(TZ=Asia/Seoul date '+%F %H:%M') KST" -- public/data/performance.json \
  >>"$LOG" 2>&1 || { log "commit 실패"; exit 0; }
git push origin HEAD:main >>"$LOG" 2>&1 \
  && log "push 성공 (체결 ${TRIPS}건, generated_at ${GENERATED_AT})" \
  || log "push 실패(배포 키 권한 확인)"
exit 0
