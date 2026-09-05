#!/usr/bin/env bash
# 화-토 07:00 KST — QQQ 일봉 백필 + **신선도 되짚기**.
#
# 왜 이 스크립트가 존재하나 (2026-08-13):
# 봉 백필이 스케줄된 적이 없었다. 크론도 타이머도 없어서 `data/history/QQQ/1d/`의
# 마지막 봉 날짜는 그냥 **마지막 수동 실행 시각**이었다 — 실측 07-31, 그날은 08-13.
#
# 그게 백테스트만의 문제가 아니었다. `quant/trade/regime/provider.py`가 이 파일을
# **직접** 읽어 라이브 US 국면(방어 0.5x / 중립 1.0x / 공격 1.3x)을 계산한다.
# 13일 낡은 종가가 "현재가"로 사이징에 곱해지고 있었고, 상태는 스스로를 정상이라고
# 보고했다. 코드 쪽 가드는 884f1eb에서 넣었다(낡으면 지표 제외 + degraded).
# 이 스크립트는 그 가드가 애초에 발동할 일이 없게 만드는 쪽이다.
#
# **왜 QQQ만인가.** 지금 라이브 사이징이 파일에서 읽는 봉은 이것 하나다.
# TQQQ/SQQQ 15분봉은 백테스트/하네스용이고, EC2에는 아예 없다 — 그걸 여기서
# 받기 시작하면 착수보고서가 미결로 남긴 "하네스를 어디서 돌릴지"를 조용히
# 답해버린다. 그 결정이 나면 그때 항목을 늘린다.
#
# **소스는 yfinance다. alpaca가 아니다.** 이걸 실측으로 정했다(2026-08-13):
# alpaca 무료 구독은 과거 SIP는 주지만 **최근 구간을 403으로 막는다**. 그래서
# alpaca로 이 크론을 돌리면 rc=0 을 내면서 봉은 영구히 낡는다(실측: 08-01~08-13
# 9거래일이 전부 빈 채로 "0 bar(s) written"). IEX 폴백은 하지 않는다 —
# alpaca_source.py의 판단대로 IEX 거래량은 통합의 약 1.2%라 스케일이 섞인다.
#
# yfinance는 일봉을 상장 이후 전체로, 인증 없이 준다(yf_source.py의 실측 한도표).
# 소스를 바꿔도 데이터가 갈리지 않는다는 걸 확인했다:
#   겹치는 22봉(2026-07-01~07-31) 종가 비교, 최대 절대 괴리 **0.0bp**
#   타임스탬프도 양쪽 모두 04:00:00+00:00 — 같은 거래일이 두 번 들어가지 않는다
# 그래서 기존 alpaca 파티션에 그대로 이어 붙여도 된다.
#
# toss는 후보가 아니다 — 1분봉만 지원한다(cli.py가 명시적으로 거부한다).
#
# 테스트: DRY_RUN=1 ./server/scripts/backfill_us_daily.sh
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
LOG="data/fetch_us_daily.log"
SYMBOL="QQQ"
INTERVAL="1d"
# 40일 되돌아본다. backfill()은 완성된 과거 월 파티션을 건너뛰고 마지막 봉 이후만
# 받으므로 비용은 거의 없다. 월 경계에서 앞 달이 덜 찬 경우까지 스스로 메운다 —
# 크론이 며칠 빠져도 다음 실행이 복구한다.
LOOKBACK_DAYS=40

mkdir -p data
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

_env() { grep "^$1=" .env.local 2>/dev/null | head -1 | cut -d= -f2-; }
TG_TOKEN="$(_env TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(_env TELEGRAM_CHAT_ID)"
# 알림은 전부 notify_defer (역할별 게이트 — server/scripts/lib/notify.sh):
# 요약·정보성이라 텔레그램으로는 **절대 나가지 않는다**. data/notify_queue.jsonl
# 에 쌓여 마감 HTML 리포트로만 간다.
. "$(dirname "$0")/lib/notify.sh"
NOTIFY_LANE="ops"  # 텔레그램 포럼 토픽 레인 — docs/runbooks/telegram-rooms.md
# 메모리 워터마크(2026-08-31, 2GB 박스 관측) — server/scripts/lib/memlog.sh 참고.
. "$(dirname "$0")/lib/memlog.sh"
memlog_wrap "backfill_us_daily"

START="$(date -u -d "${LOOKBACK_DAYS} days ago" +%Y-%m-%d 2>/dev/null \
         || date -u -v-${LOOKBACK_DAYS}d +%Y-%m-%d)"   # GNU / BSD 양쪽

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[DRY_RUN] fetch $SYMBOL $INTERVAL from $START (source=yfinance)"
fi

log "백필 시작: $SYMBOL $INTERVAL from $START"
FETCH_OUT="$(timeout 300 "$PY" -m quant.apps.cli fetch \
  --symbol "$SYMBOL" --interval "$INTERVAL" --source yfinance --start "$START" 2>&1)"
FETCH_RC=$?
printf '%s\n' "$FETCH_OUT" >> "$LOG"

# SPY 일봉 (2026-08-29 추가) — 소비자는 아침 리포트의 S&P 상승 확률
# (quant/report/collect/index_outlook.py, "유사 조건일 기저율" 계산의 대리 심볼).
# "왜 QQQ만인가" 원칙(위)의 예외를 하나 늘리는 것이므로 소비자를 명시한다.
# 되짚기 판정은 QQQ 전용으로 둔다 — SPY가 낡으면 확률의 표본 구간이 며칠
# 짧아질 뿐이고(method 문구에 연 수가 그대로 드러난다) 국면 사이징과 무관하다.
log "백필 시작: SPY $INTERVAL from $START"
SPY_OUT="$(timeout 300 "$PY" -m quant.apps.cli fetch \
  --symbol SPY --interval "$INTERVAL" --source yfinance --start "$START" 2>&1)"
[ $? -ne 0 ] && log "SPY 백필 종료코드 비정상 — 확률 표본이 낡을 수 있다"
printf '%s\n' "$SPY_OUT" >> "$LOG"

# --- 되짚기: 받았다고 믿지 않는다 ---
#
# exit 0 이 "봉이 최신이 됐다"를 뜻하지 않는다. 벤더가 빈 응답을 주면 fetch는
# 성공하고 파일은 그대로다 — 이 저장소가 반복해서 다친 모양("모른다"를 "이상
# 없음"으로)이라, 결과를 파일에서 다시 읽어 확인한다.
#
# 임계값은 국면 가드와 **같은 상수**를 쓴다. 여기 숫자를 따로 적으면 언젠가
# 갈라지고, 갈라진 쪽이 조용한 쪽이 된다.
VERDICT="$(timeout 60 "$PY" - <<'PYEOF' 2>>"$LOG"
from datetime import datetime, timezone
import pandas as pd
from quant.adapters.olap import coverage
from quant.trade.regime.provider import STALE_DAILY_BARS_AFTER

cov = coverage("QQQ", "1d")
if cov is None or cov.last_ts is None:
    # DuckDB 미설치이거나 파일이 없다 — "정상"이 아니라 "확인 불가"다.
    print("UNKNOWN 커버리지를 읽지 못했다 (duckdb 미설치 또는 파티션 없음)")
else:
    last = pd.Timestamp(cov.last_ts)
    last = last.tz_localize("UTC") if last.tzinfo is None else last.tz_convert("UTC")
    age = pd.Timestamp(datetime.now(timezone.utc)) - last
    state = "OK" if age <= STALE_DAILY_BARS_AFTER else "STALE"
    print(f"{state} 마지막 봉 {last.date()}, {age.days}일 경과 "
          f"(임계 {STALE_DAILY_BARS_AFTER.days}일), 봉 {cov.n_bars}개")
PYEOF
)"
log "되짚기: ${VERDICT:-<판정 실패>}"

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[DRY_RUN] fetch rc=$FETCH_RC"
  echo "[DRY_RUN] 되짚기: ${VERDICT:-<판정 실패>}"
  exit 0
fi

# 정상일 때는 조용하다 — 매일 오는 "정상" 알림은 사람이 끄고, 끈 알림은 없는 알림이다.
case "${VERDICT%% *}" in
  OK)
    [ "$FETCH_RC" -ne 0 ] && notify_defer "backfill_us_daily" "⚠️ QQQ 일봉 백필 종료코드 ${FETCH_RC} — 다만 봉은 최신이다(${VERDICT#* }). data/fetch_us_daily.log 확인"
    exit 0
    ;;
  STALE)
    notify_defer "backfill_us_daily" "🚨 QQQ 일봉이 백필 후에도 낡았다 — ${VERDICT#* }
US 국면(regime)이 이 파일을 읽어 사이징에 곱한다. 낡으면 코드 가드가 지표를
제외하고 중립(1.0x)으로 떨어뜨리므로 손실 방향은 아니지만, 국면 판단이 통째로
사라진 상태다.
yfinance 응답·네트워크 확인: tail -40 data/fetch_us_daily.log"
    exit 1
    ;;
  *)
    notify_defer "backfill_us_daily" "🚨 QQQ 일봉 백필 검증 불가 — ${VERDICT:-판정 실패} (fetch rc=${FETCH_RC})
'봉이 최신인지 모른다'는 상태다. data/fetch_us_daily.log 확인"
    exit 1
    ;;
esac
