#!/usr/bin/env bash
# 로컬 맥 — EC2 ~/qb_backfill(연구 저장소 체크아웃)의 KR 1분봉 레이크를 당겨온다.
# 서버 쪽 자동화(server/scripts/kr_minute_backfill.sh, 토 05:00 KST)가 채운 걸 이
# 스크립트로 받는다. local/ml/run.sh와 같은 정신(소유자가 터미널에서 직접
# 돌리는 원버튼) — 클로드가 대신 돌리지 않는다.
#
# 목적지가 `data/lake_kr`가 아니라 `data/lake`인 이유(2026-09-06 조사, 착오
# 정정): quant-backtest는 US(Alpaca) 1분봉과 KR(Kiwoom) 1분봉을 같은 루트에
# 심볼 디렉터리로만 구분해 쓴다(`qb/lake.py: DEFAULT_LAKE = data/lake`,
# `qb/fetch_kiwoom.py`/`qb/fetch_alpaca.py` 둘 다 이 기본값). 실제 96종목
# 백필의 rsync 대상도 `data/lake`다(`quant-backtest/scripts/kr_after_backfill.sh`
# 참고) — `data/lake_kr`는 README.md의 낡은 절(엔진 자체 `data/history/`를
# 읽기 전용 복제하던 예전 경로, ka10080이 이 맥에서 인증 거부되기 전 방식)이
# 남긴 별개의 디렉터리이지 이 파이프라인의 목적지가 아니다.
#
# 환경변수:
#   QT_SSH_HOST    EC2 접속 문자열 (기본 ubuntu@100.87.129.113, kr_after_backfill.sh와 동일)
#   QB_REPO_DIR    로컬 quant-backtest 체크아웃 경로 (기본 ../quant-backtest, 이 저장소의 형제 디렉터리)
#   QB_REMOTE_LAKE EC2 쪽 레이크 경로 (기본 /home/ubuntu/qb_backfill/lake)
#   DRY_RUN=1      rsync를 실행하지 않고 명령만 출력한다
#
# 사용법: ./local/lake/pull_kr_minute.sh   또는  make lake-pull-kr
set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root (local/lake/pull_kr_minute.sh 기준 두 단계 위)

QT_SSH_HOST="${QT_SSH_HOST:-ubuntu@100.87.129.113}"
QB_REPO_DIR="${QB_REPO_DIR:-../quant-backtest}"
QB_REMOTE_LAKE="${QB_REMOTE_LAKE:-/home/ubuntu/qb_backfill/lake}"

if [ ! -d "$QB_REPO_DIR" ]; then
  echo "[lake-pull-kr] $QB_REPO_DIR 없음 — quant-backtest 체크아웃 경로를 QB_REPO_DIR로 지정하라" >&2
  exit 1
fi

LOCAL_LAKE="$QB_REPO_DIR/data/lake"
mkdir -p "$LOCAL_LAKE"

_run() {
  if [ "${DRY_RUN:-0}" = "1" ]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

echo "[lake-pull-kr] $QT_SSH_HOST:$QB_REMOTE_LAKE/ → $LOCAL_LAKE/"
_run rsync -a --stats "$QT_SSH_HOST:$QB_REMOTE_LAKE/" "$LOCAL_LAKE/"

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[dry-run] 완료 — 실제 동기화 없음"
  exit 0
fi

N_KR="$(ls "$LOCAL_LAKE" 2>/dev/null | grep -cE '^[0-9]{6}$' || true)"
echo "[lake-pull-kr] 완료 — 레이크 내 KR(6자리 코드) 심볼 수: ${N_KR}"

# 매니페스트 갱신은 여기서 실행하지 않는다(요청 사항) — quant-backtest에서
# 사람이 직접 돌린다. 전체 rebuild_manifest()는 쓰지 않는다: US(Alpaca) 심볼
# 항목까지 source 라벨을 "kiwoom:ka10080"으로 덮어써 버리기 때문이다
# (kr_after_backfill.sh가 이미 그 이유로 전체 재빌드 대신 심볼별
# update_manifest만 쓴다). 아래는 지금 레이크에 있는 KR 심볼 전체에 대해
# 같은 방식으로 갱신하는 커맨드 — 복사해서 quant-backtest 안에서 직접 실행할 것.
cat <<EOF

[다음 단계 — 지금 실행되지 않음] 매니페스트 갱신은 quant-backtest 저장소에서 사람이 직접:

  cd $QB_REPO_DIR
  uv run python -c "
import re
from pathlib import Path
from qb.lake import update_manifest
lake = Path('data/lake')
n = 0
for d in sorted(lake.iterdir()):
    if d.is_dir() and re.fullmatch(r'[0-9]{6}', d.name):
        update_manifest(lake, d.name, '1m', 'kiwoom:ka10080')
        n += 1
print(f'매니페스트 갱신: {n}개 KR 심볼')
"
EOF
