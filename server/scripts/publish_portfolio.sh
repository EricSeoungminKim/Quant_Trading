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
# 크론: publish_performance.sh 직후(KR 16:25 / US 06:25).
# 조용한 것이 기본값 — 실패해도 exit 0(리포팅 레인이 엔진을 죽이면 안 된다).
set -u
cd "$(dirname "$0")/../.."

LOG="data/portfolio_publish.log"
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

SRC="data/public/performance.json"
WORK="$HOME/.cache/quant-portfolio"
KEY="$HOME/.ssh/id_portfolio"
REPO="git@github.com:EricSeoungminKim/quant-portfolio.git"

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

mkdir -p public/data
cp "$OLDPWD/$SRC" public/data/performance.json

if git diff --quiet -- public/data/performance.json; then
  log "변경 없음 — push 생략"
  exit 0
fi

git -c user.name="quant-engine" -c user.email="noreply@localhost" \
  commit -q -m "data: 성과 갱신 $(TZ=Asia/Seoul date '+%F %H:%M') KST" -- public/data/performance.json \
  >>"$LOG" 2>&1 || { log "commit 실패"; exit 0; }
git push origin HEAD:main >>"$LOG" 2>&1 && log "push 성공" || log "push 실패(배포 키 권한 확인)"
exit 0
