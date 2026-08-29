#!/usr/bin/env bash
# 로컬 맥 원버튼 ML 파이프라인 (2026-08-30 신설). Cursor 터미널에서
# `make ml` 한 번으로: EC2 동기화 → 표본 게이트 판정 → (충족 시) 학습 → 리포트.
# 클로드에게 부탁하지 않고 소유자가 매일 직접 돌리는 용도 — 24시간 상시 가동
# 불필요, 서버 하나 없이 이 스크립트가 전부다. 자세한 사용법은
# local/ml/README.md.
#
# 환경변수:
#   QT_SSH_HOST   EC2 접속 문자열 (기본 ubuntu@100.87.129.113)
#   QT_REMOTE_DIR EC2 저장소 경로 (기본 quant_trading_kiwoom)
#   SKIP_SYNC=1   EC2 동기화를 건너뛰고 이미 당겨온 로컬 캐시로만 돈다(오프라인 재실행)
#   DRY_RUN=1     rsync/ssh/scp 를 실행하지 않고 명령만 출력한다
#   PUSH=1        학습 결과 요약 json 을 EC2 data/ml_inbox/ 로 scp 한다
set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root (local/ml/run.sh 기준 두 단계 위)

QT_SSH_HOST="${QT_SSH_HOST:-ubuntu@100.87.129.113}"
QT_REMOTE_DIR="${QT_REMOTE_DIR:-quant_trading_kiwoom}"
SKIP_SYNC="${SKIP_SYNC:-0}"
DRY_RUN="${DRY_RUN:-0}"
PUSH="${PUSH:-0}"

CACHE_DIR="data/ml"
mkdir -p "$CACHE_DIR"

# DRY_RUN=1 이면 실행하지 않고 명령만 echo 한다(rsync/ssh/scp 전용 래퍼) —
# 시크릿이 섞이는 MySQL 덤프 단계는 별도로 아래에서 직접 분기한다.
_run() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

if [ "$SKIP_SYNC" != "1" ]; then
  echo "[ml] EC2 동기화: $QT_SSH_HOST:$QT_REMOTE_DIR"

  # 명시적 include 목록 — .env* 는 절대 대상에 없다(시크릿 rsync 금지, 디렉터리
  # 통째 동기화가 아니라 파일 하나하나를 지정하는 이유이기도 하다).
  _run rsync -az --update \
    "$QT_SSH_HOST:$QT_REMOTE_DIR/data/ledger/selections.jsonl" \
    "$CACHE_DIR/selections.jsonl" \
    || { echo "[ml] rsync 실패(selections.jsonl) — 동기화 안 된 채 학습하지 않는다" >&2; exit 1; }

  _run rsync -az --update \
    "$QT_SSH_HOST:$QT_REMOTE_DIR/data/state/trades.jsonl" \
    "$CACHE_DIR/trades.jsonl" \
    || { echo "[ml] rsync 실패(trades.jsonl) — 중단" >&2; exit 1; }

  _run rsync -az --update \
    "$QT_SSH_HOST:$QT_REMOTE_DIR/data/history/" \
    "$CACHE_DIR/history/" \
    || { echo "[ml] rsync 실패(history parquet) — 중단" >&2; exit 1; }

  # 실제 학습 라벨(selection⋈forward_return D+1)은 EC2 MySQL 에만 있다 —
  # data/ledger/selections.jsonl 의 outcome_* 는 실제로는 채워지지 않는다
  # (2026-08-30 실측 — quant/research/ml_train.py 모듈 docstring 참고). 원격
  # 파이썬 표준입력에 local/ml/remote_dump.py 를 흘려 MySQL 조인 결과만 JSON
  # 으로 받는다 — 비밀번호는 원격 .env.local 안에서만 쓰이고 로컬 커맨드라인에
  # 노출되지 않는다.
  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] ssh $QT_SSH_HOST \"cd $QT_REMOTE_DIR && set -a && . .env.local && set +a && .venv/bin/python3 -\" < local/ml/remote_dump.py > $CACHE_DIR/train_labeled.json"
  else
    if ! ssh "$QT_SSH_HOST" \
      "cd $QT_REMOTE_DIR && set -a && . .env.local 2>/dev/null && set +a && .venv/bin/python3 -" \
      < local/ml/remote_dump.py > "$CACHE_DIR/train_labeled.json.tmp"
    then
      echo "[ml] MySQL 라벨 동기화 실패 — 동기화 안 된 채 학습하지 않는다" >&2
      rm -f "$CACHE_DIR/train_labeled.json.tmp"
      exit 1
    fi
    mv "$CACHE_DIR/train_labeled.json.tmp" "$CACHE_DIR/train_labeled.json"
  fi
else
  echo "[ml] SKIP_SYNC=1 — 로컬 캐시로만 진행: $CACHE_DIR"
fi

if [ ! -f "$CACHE_DIR/train_labeled.json" ]; then
  echo "[ml] $CACHE_DIR/train_labeled.json 없음 — 먼저 동기화가 필요하다(DRY_RUN/SKIP_SYNC 없이 재실행)" >&2
  exit 1
fi

RUN_DATE="$(date +%F)"
OUT_DIR="local/ml/out/$RUN_DATE"
mkdir -p "$OUT_DIR"

# 학습이 실제로 돌 때만(임계 도달) sklearn 이 필요하다 — 매번 동기화해 두면
# 그날 지연이 없다. 이미 설치돼 있으면 uv 가 즉시 끝낸다.
uv sync --group research --quiet

echo "[ml] 표본 게이트 판정 + (충족 시) 학습 실행"
uv run python -m quant.research.ml_train \
  --labeled-json "$CACHE_DIR/train_labeled.json" \
  --out-dir "$OUT_DIR" \
  | tee "$OUT_DIR/run.log"

if [ -f "$OUT_DIR/report.md" ]; then
  echo "[ml] 완료. 리포트: $OUT_DIR/report.md"
else
  echo "[ml] 완료 — 학습 생략(표본 수집 중, 위 출력 참고)."
fi

if [ "$PUSH" = "1" ] && [ -f "$OUT_DIR/summary.json" ]; then
  echo "[ml] PUSH=1 — 요약을 EC2 data/ml_inbox/ 로 전달"
  _run ssh "$QT_SSH_HOST" "mkdir -p $QT_REMOTE_DIR/data/ml_inbox"
  _run scp "$OUT_DIR/summary.json" "$QT_SSH_HOST:$QT_REMOTE_DIR/data/ml_inbox/summary_${RUN_DATE}.json"
fi
