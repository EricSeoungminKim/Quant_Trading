#!/usr/bin/env bash
# 로컬 → EC2 배포. 거래 엔진과 **같은 박스**에 나란히 올린다.
#
#   QT_SSH_HOST=ubuntu@100.87.129.113 ./server/deploy.sh
#
# 이 스크립트는 quant_trading_kiwoom(실거래 엔진)의 파일·크론·systemd 유닛을
# 절대 건드리지 않는다. 리포트는 별도 디렉토리·별도 유닛으로 격리된다.
#
# .env.local 은 **전송하지 않는다.** 시크릿은 사람이 scp 로 직접 넣는다:
#   scp .env.local ubuntu@<host>:/home/ubuntu/quant_trading_kiwoom/.env.local
set -euo pipefail

HOST="${QT_SSH_HOST:-}"
[ -n "$HOST" ] || { echo "QT_SSH_HOST 를 지정한다 (예: ubuntu@100.87.129.113)" >&2; exit 2; }

REMOTE_DIR=/home/ubuntu/quant_trading_kiwoom
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

echo "▶ 로컬 상태 확인"
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "  커밋되지 않은 변경이 있다. 커밋 후 배포한다." >&2
  git status --short >&2
  exit 1
fi
echo "  브랜치 $BRANCH / $(git rev-parse --short HEAD)"

echo "▶ 원격 디렉토리 준비"
ssh "$HOST" "mkdir -p $REMOTE_DIR/data $REMOTE_DIR/out"

echo "▶ 소스 동기화 (rsync — 시크릿·산출물 제외)"
rsync -az --delete \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
  --exclude '.env.local' --exclude 'data/' --exclude 'out/' \
  --exclude '.pytest_cache' \
  ./ "$HOST:$REMOTE_DIR/"

echo "▶ 원격 환경 구성"
ssh "$HOST" "bash -s" <<REMOTE
set -euo pipefail
cd "$REMOTE_DIR"
mkdir -p data out

if [ ! -f .env.local ]; then
  echo "  ⚠ .env.local 이 없다 — FRED/EIA/토스 키를 직접 scp 해야 한다"
  echo "     scp .env.local $HOST:$REMOTE_DIR/.env.local"
fi

# uv 로 venv 를 만든다 — 이 EC2 에는 python3.12-venv 패키지가 없어
# 'python3 -m venv' 가 ensurepip 부재로 실패한다(2026-08-13 실측). uv 는
# 자체 부트스트랩이라 apt 설치(sudo 비밀번호)가 필요 없다. 거래 저장소도
# 같은 이유로 venv 바이너리를 직접 가리키는 관례를 쓴다.
UV="\$HOME/.local/bin/uv"
[ -x "\$UV" ] || UV="\$(command -v uv || true)"
[ -n "\$UV" ] || { echo "  uv 를 찾을 수 없다 — 설치 후 재시도한다" >&2; exit 1; }
[ -d .venv ] || { echo "  venv 생성 (uv)"; "\$UV" venv .venv; }
"\$UV" sync

echo "  systemd 유닛 설치"
sudo cp server/systemd/market-report@.service /etc/systemd/system/
sudo cp server/systemd/market-report-kr.timer /etc/systemd/system/
sudo cp server/systemd/market-report-us.timer /etc/systemd/system/
sudo cp server/systemd/market-report-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now market-report-kr.timer market-report-us.timer
sudo systemctl enable --now market-report-web.service

echo "  타이머 상태"
systemctl list-timers 'market-report*' --no-pager | head -5
echo "  거래 엔진 무결성 (건드리지 않았다)"
systemctl is-active quant-engine.service tg-bridge.service
REMOTE

echo "▶ 완료. 수동 1회 실행: ssh $HOST 'sudo systemctl start market-report@KR.service'"
