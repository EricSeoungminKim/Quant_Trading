#!/usr/bin/env bash
# **Mac(받는 쪽)에서 실행한다.** EC2 의 번들을 당겨오고 받은 것을 대조한다.
#
#   ./server/scripts/backup_pull.sh [목적지디렉토리]      # 기본 ~/quant-backups
#
# 왜 EC2 가 밀지 않고 여기서 당기나: **EC2 에 오프사이트 자격증명을 두지 않는다.**
# 박스가 털렸을 때 백업까지 지울 수 있는 키가 같은 박스에 있으면 백업이 아니다.
# 같은 이유로 S3 도 나중에 이 스크립트 쪽(또는 제3의 러너)에 붙인다.
#
# 오프사이트 목적지가 Mac 인 것의 한계: **Mac 이 꺼져 있으면 그날 사본이 없다.**
# 그래서 마지막 성공 시각을 목적지에 남기고(`LAST_PULL`), 감시(`cli health`)가
# 그 나이를 본다 — 조용히 몇 주 안 당겨오는 상태를 막는다.
set -u
# cd 실패를 그냥 넘기면 이후 상대경로(.venv)가 엉뚱한 곳을 가리키는데도 계속 돈다.
cd "$(dirname "$0")/../.." || { echo "[pull] 저장소 루트로 이동 실패" >&2; exit 1; }

HOST="${QT_SSH_HOST:-ubuntu@100.87.129.113}"
DEST="${1:-$HOME/quant-backups}"
REMOTE="quant_trading_kiwoom/data/backups"

# **`uv run` 을 쓰지 않는다** — server/CLAUDE.md 의 불변식이다. 원래 이 파일은
# "받는 쪽은 Mac 이라 크론 환경이 아니다"며 예외를 뒀는데, **launchd 가 바로 그
# 데몬 환경이다.** 의존을 하나 줄이는 것이지 아래 사고의 원인 수정이 아니다 —
# 원인은 TCC 였고 그건 plist 쪽에서 고쳤다(`com.quant.backup-pull.plist` 주석).
PY="./.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[pull] .venv 가 없다 — 저장소에서 'uv sync' 를 먼저 돌릴 것" >&2
  exit 1
fi

mkdir -p "$DEST"

echo "[pull] $HOST:$REMOTE → $DEST"
# --ignore-existing: 번들은 불변이다. 이미 받은 걸 다시 받아 덮어쓸 이유가 없고,
# 덮어쓰기는 원격이 망가졌을 때 성한 사본을 지운다.
rsync -a --ignore-existing --prune-empty-dirs \
      --include='*/' --include='quant-*.tar.gz' --exclude='*' \
      "$HOST:$REMOTE/" "$DEST/" || { echo "[pull] rsync 실패" >&2; exit 1; }

# --- 받은 것을 대조한다 ---
# 전송 중 잘림이 이 경로의 실제 실패 모드다. 원격에서 정상이었다는 건 여기서
# 정상이라는 뜻이 아니다.
FAILED=0
COUNT=0
for b in "$DEST"/quant-*.tar.gz; do
  [ -f "$b" ] || continue
  COUNT=$((COUNT + 1))
  if ! $PY -m quant.apps.cli backup --verify "$b" >/dev/null 2>&1; then
    echo "[pull] 대조 실패: $b"
    $PY -m quant.apps.cli backup --verify "$b" 2>&1 | sed 's/^/        /'
    FAILED=$((FAILED + 1))
  fi
done

if [ "$FAILED" -eq 0 ] && [ "$COUNT" -gt 0 ]; then
  STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '%s\n' "$STAMP" > "$DEST/LAST_PULL"
  # **EC2 로 되쓴다.** 감시(`cli health`)는 EC2 에서 돌므로, 여기서만 기록하면
  # "오프사이트 사본이 있나"를 영원히 모른다 — 그러면 `backup` 항목이 매번
  # unknown 으로 남고, 조용히 몇 주 안 당겨오는 상태를 못 잡는다.
  if ! printf '%s\n' "$STAMP" | ssh "$HOST" "cat > $REMOTE/LAST_PULL"; then
    echo "[pull] 경고: EC2 에 LAST_PULL 을 되쓰지 못했다 — 감시가 오프사이트 여부를 모른다" >&2
  fi
  echo "[pull] 번들 ${COUNT}개 전부 대조 통과 · LAST_PULL=$STAMP"
  exit 0
fi
if [ "$COUNT" -eq 0 ]; then
  echo "[pull] 받은 번들이 없다 — 원격에서 backup.sh 가 도는지 확인할 것" >&2
  exit 1
fi
echo "[pull] ${FAILED}/${COUNT} 개가 대조 실패 — LAST_PULL 을 갱신하지 않는다" >&2
exit 1
