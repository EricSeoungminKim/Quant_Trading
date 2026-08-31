#!/usr/bin/env bash
# 로컬(Mac)에서 실행: 코드 변경을 push하고 서버에 반영한다.
# 사용법: QT_SSH_HOST=ubuntu@<ElasticIP> ./server/scripts/deploy.sh
#   NO_RESTART=1  풀·동기화만(엔진 무접촉 — 장중에도 안전)
#   FORCE=1       장중 강제(비상시에만 — 서버 data/deploy.log 에 남는다)
#
# ## 장중 하드 가드 (2026-08-31)
# 사람의 "지금 장중인가" 판단이 두 번 실패했다 — 8/28 14:54(KR 장중), 8/31
# 14:48(KR 마감 42분 전) 재시작. 포지션은 lot 영속으로 무사했지만 그건 운이다.
# 규율이 반복 실패하면 규율을 코드로 대체한다 — 이 스크립트가 유일한 배포
# 경로이고, 장중이면 재시작을 거부한다. (수동 ssh 재시작은 이제 규칙 위반이다.)
set -euo pipefail

if [ -z "${QT_SSH_HOST:-}" ]; then
  echo "[deploy] QT_SSH_HOST 환경변수가 설정되지 않았습니다." >&2
  echo "  사용법: QT_SSH_HOST=ubuntu@<ElasticIP> ./server/scripts/deploy.sh" >&2
  exit 1
fi

REPO_DIR="quant_trading_kiwoom"

# --- 장중 판정 (KST 고정 — 로컬 머신의 시간대와 무관) ---
now_kst=$(TZ=Asia/Seoul date '+%H%M' | sed 's/^0*//')   # 산술 비교용(8진수 방지)
dow=$(TZ=Asia/Seoul date '+%u')                          # 1=월 … 7=일
in_session=0
# KR 정규장: 평일 09:00~15:30 KST
if [ "$dow" -le 5 ] && [ "$now_kst" -ge 900 ] && [ "$now_kst" -le 1530 ]; then
  in_session=1
fi
# US 정규장(KST): 월~금 밤 22:30~ + 화~토 새벽 ~06:00.
# DST 여부로 마감이 05:00/06:00 을 오가므로 06:00 까지 보수적으로 장중 취급
# (오탐은 배포가 몇 시간 늦어질 뿐 — 안전한 방향).
if { [ "$dow" -le 5 ] && [ "$now_kst" -ge 2230 ]; } \
   || { [ "$dow" -ge 2 ] && [ "$dow" -le 6 ] && [ "$now_kst" -le 600 ]; }; then
  in_session=1
fi

if [ "$in_session" = "1" ] && [ "${NO_RESTART:-0}" != "1" ] && [ "${FORCE:-0}" != "1" ]; then
  echo "⛔ 배포 거부: 지금은 장중이다 (KST $(TZ=Asia/Seoul date '+%H:%M'), 요일 $dow)." >&2
  echo "   엔진 재시작 = 포지션 관리 순간 중단. 마감 후 재시도하거나:" >&2
  echo "   NO_RESTART=1 (엔진 무접촉 풀만) / FORCE=1 (진짜 비상 — 기록 남음)" >&2
  exit 2
fi

echo "[deploy] git push"
git push

echo "[deploy] 서버 반영: $QT_SSH_HOST"
# 비로그인 ssh 셸에는 uv가 PATH에 없다 (server/CLAUDE.md 불변식 — 2026-08-10
# 'uv: command not found'로 재시작이 조용히 스킵된 실측). 절대경로로 호출한다.
if [ "${NO_RESTART:-0}" = "1" ]; then
  ssh "$QT_SSH_HOST" "cd $REPO_DIR && git pull --ff-only && ~/.local/bin/uv sync && crontab server/crontab.txt"
  echo "[deploy] NO_RESTART=1 — 엔진 무접촉 완료."
  exit 0
fi
if [ "${FORCE:-0}" = "1" ]; then
  ssh "$QT_SSH_HOST" "cd $REPO_DIR && echo \"[deploy] FORCE 장중 배포 \$(date '+%F %T')\" >> data/deploy.log"
fi
ssh "$QT_SSH_HOST" "cd $REPO_DIR && git pull --ff-only && ~/.local/bin/uv sync \
  && crontab server/crontab.txt \
  && sudo systemctl restart quant-engine tg-bridge \
  && systemctl is-active quant-engine tg-bridge \
  && (journalctl -u quant-engine --since '40 sec ago' --no-pager | grep -m1 '엔진 조립 완료' \
      || { echo '⚠️ 조립 완료 로그 미확인'; exit 1; }) \
  && echo \"[deploy] 완료 \$(date '+%F %T')\" >> data/deploy.log"

echo "[deploy] 완료."
