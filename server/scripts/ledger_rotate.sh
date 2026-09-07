#!/usr/bin/env bash
# 원장 크기 순환(2026-09-07, V1/V4 감사: smart_flow.jsonl 이 8일 만에 308MB — 37MB/일, 읽는 곳 없음).
# 대상 파일이 상한을 넘으면 `data/ledger/archive/<이름>-<타임스탬프>.jsonl.gz` 로 옮기고 새 파일에서
# 이어 쓴다(작성자들은 flush 마다 append-only 로 열어 쓰므로 rename 사이에 잃는 행이 없다).
# 백업(03:30)이 data/ledger 전체를 담으므로 archive 도 번들에 들어간다. 매일 03:10 크론.
#   사용: ledger_rotate.sh            # 기본 목록·상한
#         MAX_MB=50 ledger_rotate.sh  # 상한 조정
set -u
cd "$(dirname "$0")/../.."
MAX_MB="${MAX_MB:-100}"
ARCHIVE_DIR="data/ledger/archive"
mkdir -p "$ARCHIVE_DIR"
# 크기 순환 대상 — 읽는 곳이 없는 대용량 append 원장만(smart_flow: 키움 WS 세력 신호 L1 틱, 소비자
# 0). notifications.jsonl 은 cli._read_notifications(배달 점검)가, macro_rates.jsonl 은 macro 모듈이
# 전량 읽으므로 넣지 않는다. trades/selections/report_claims 같은 판정 원장도 절대 넣지 않는다.
for f in data/ledger/smart_flow.jsonl; do
  [ -f "$f" ] || continue
  size_mb=$(( $(stat -c %s "$f") / 1048576 ))
  if [ "$size_mb" -ge "$MAX_MB" ]; then
    stamp=$(date +%Y%m%d-%H%M%S)
    base=$(basename "$f" .jsonl)
    mv "$f" "$ARCHIVE_DIR/${base}-${stamp}.jsonl"
    gzip -q "$ARCHIVE_DIR/${base}-${stamp}.jsonl"
    echo "[$(date '+%F %T')] rotated $f (${size_mb}MB) -> $ARCHIVE_DIR/${base}-${stamp}.jsonl.gz"
  fi
done
# 아카이브 보존: 90일 초과분 삭제(백업 번들에 이미 14일치가 있다)
find "$ARCHIVE_DIR" -name "*.jsonl.gz" -mtime +90 -delete 2>/dev/null || true
