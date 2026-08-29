#!/usr/bin/env bash
# llm_trader — 엔진 밖 판단 레인 (2026-08-30 신규). KR 정규장 중 10분마다 크론이
# 부른다. 이 스크립트는 아무 주문도 내지 않는다 — headless `claude -p` 로 판단만
# 받아 data/state/llm_trader_inbox.jsonl 에 append 하고, 그 인박스를 엔진(별도
# 워커가 배선 중인 소비 전략)이 읽어 실제로 사고판다. quant/ 를 직접 임포트하지
# 않는다 — server/scripts/llm_trader.py 도 마찬가지(인박스 파일 하나가 엔진과의
# 유일한 쓰기 접점, ADR-0002/거래 핫패스 LLM 금지와 같은 원칙으로 판단
# 프로세스를 엔진과 분리한다). peek(시세 조회)만 예외로 llm_trader.py 가 우리
# CLI 를 서브프로세스로 부른다 — 이유는 아래 "도구 허용" 절 참고.
#
# 비용/부하 메모: 호출당 클로드 프로세스가 순간적으로 ~300MB 를 쓴다(관찰치 —
# EC2 는 총 1.8GB). 2026-08-30 소유자 지시로 주기를 30분→10분으로 당겨 Max
# 구독 사용량을 하루 **38회**(9:05~15:15, 10분 간격) 소모한다(기존 13회/일 대비
# 증가). DeepSeek 등 다른 키로 판단 호출부를 바꾸고 싶으면 아래 "2. 판단 호출"
# 절만 교체하면 된다 — 인박스 계약(llm_trader.py 상단 주석)은 그대로 유지해야
# 엔진 쪽이 안 깨진다.
#
# 도구 허용 (2026-08-30 실측, claude CLI 2.1.233): 원래 지시는 "WebSearch/
# WebFetch + quant.apps.cli peek 명령 패턴만" 허용하는 것이었다. 그런데
# --allowedTools/--disallowedTools/--settings permissions.{allow,deny} 네
# 조합을 EC2 에서 직접 실측한 결과 — Bash 를 막으면(이름이든 `Bash(패턴)`이든)
# 패턴 예외 없이 Bash 도구 자체가 통째로 사라지고, Bash 를 막지 않으면
# `--allowedTools`에 뭘 적든 임의 명령이 그대로 실행됐다(`id` 등). **이 CLI
# 버전은 "Bash 중 이 명령 패턴만" 부분 허용을 지원하지 않는다** — 전부
# 허용 아니면 전부 차단뿐이다. "무제한 Bash 는 금지"가 우선이므로 모델에게는
# Bash 를 아예 안 준다. 그 대신 peek 조회는 이 스크립트(llm_trader.py context)
# 가 후보 심볼에 대해 미리 호출해 컨텍스트 텍스트에 실어 보낸다 — 데이터
# API 활용이라는 목적은 달성하되, 모델이 임의 셸 명령을 실행할 길은 없다.
# 남은 도구는 WebSearch/WebFetch 뿐이다.
#
# 테스트: DRY_RUN=1 ./server/scripts/llm_trader.sh   (claude 호출 대신 고정 응답 주입)
set -u
cd "$(dirname "$0")/../.."

PY=.venv/bin/python
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
LOG="data/llm_trader.log"
LOCK="data/llm_trader.lock"
mkdir -p data data/state

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

# TZ 가드 — 정규장 판정이 전부 KST 전제다.
if [ "$(date +%z)" != "+0900" ]; then
  log "호스트 TZ가 KST가 아님($(date +%z)) — 중단"
  exit 1
fi

# 중복 실행 방지. flock 이 없으면(로컬 macOS 등 테스트 환경) 잠금 없이
# 진행한다 — 프로덕션(EC2, Ubuntu)엔 util-linux flock 이 기본 설치돼 있다.
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  if ! flock -n 9; then
    log "이미 실행 중 — 건너뜀"
    exit 0
  fi
fi

# 가드: KR 정규장(09:00~15:20 KST) 중에만 동작, 아니면 즉시 종료. 시각은
# LLM_TRADER_NOW_HHMM/LLM_TRADER_NOW_DOW 로 주입할 수 있다(own_brief.sh/
# notify.sh 와 같은 관례 — 테스트가 실제 벽시계에 의존하지 않게).
HHMM="${LLM_TRADER_NOW_HHMM:-$(date +%H%M)}"
DOW="${LLM_TRADER_NOW_DOW:-$(date +%u)}"
if [ "$DOW" -lt 1 ] || [ "$DOW" -gt 5 ] || [ "1$HHMM" -lt "10900" ] || [ "1$HHMM" -gt "11520" ]; then
  exit 0
fi

# --- 1. 컨텍스트 조립 (판단은 안 함 — 순수 조회, peek 프리페치 포함) ---
CONTEXT="$("$PY" server/scripts/llm_trader.py context 2>>"$LOG")"

PROMPT_HEADER="역할: 너는 승부욕 있는 독립 트레이더다. 목표는 한 달 안에 실제 수익률을
내는 것 — 기회가 보이면 과감하게, 근거가 무너지면 빠르게 손절한다. 단타·
스윙·장기 모두 가능하며 매 거래에 방향성(horizon)을 선언한다. 매도 타이밍도
전적으로 네 판단이다(엔진은 -5% 하드레일만 지켜준다). 확신 없는 거래는
하지 않되, 소심함으로 기회를 흘려보내는 것도 실패다.

계좌: 1,000만원 모의계좌로 한국 주식만 매매한다.

정보: 아래 아침 리포트 요약, 네 포지션, 참고 시세 데이터(우리 데이터 API로
조회된 것 — 리포트 상위 후보 + 네 포지션 한정)가 전부 제공된다. 그 목록에
없는 종목도 웹검색으로 직접 발굴해 거래해도 된다 — 리포트 후보에 갇힐
필요 없다. 필요하면 웹검색으로 최신 뉴스·섹터 흐름을 조사하라.

출력 계약: JSON 배열만 출력하라. 그 외 어떤 말도 덧붙이지 마라.
[{\"action\":\"buy\"|\"sell\",\"symbol\":\"<6자리 종목코드>\",\"weight\":0.1~0.34(buy만, sell은 null),\"horizon\":\"단타\"|\"스윙\"|\"장기\",\"reason\":\"<한 줄 근거>\"}]
후보가 없으면 [] 만 출력하라.

$CONTEXT"

# --- 2. 판단 호출 ---
if [ "${DRY_RUN:-0}" = "1" ]; then
  # 주의: 이 기본값을 ${LLM_TRADER_DRY_OUTPUT:-...} 안에 직접 못 넣는다 — JSON의
  # 리터럴 "}"가 bash 파라미터 확장의 닫는 중괄호로 잘못 읽혀 조기 종료된다.
  _DEFAULT_DRY_OUTPUT='[{"action":"buy","symbol":"005930","weight":0.15,"horizon":"스윙","reason":"DRY_RUN 테스트"}]'
  OUT="${LLM_TRADER_DRY_OUTPUT:-$_DEFAULT_DRY_OUTPUT}"
  log "DRY_RUN — claude 호출 생략, 고정 응답 주입"
else
  # 인자 순서 주의: 프롬프트를 -p 바로 뒤(포지셔널)에 두고 --disallowedTools 를
  # 맨 뒤에 둔다. --disallowedTools/--allowedTools 는 가변 인자(variadic)라
  # 프롬프트를 그 뒤에 붙이면 프롬프트 문장이 통째로 "허용/차단할 도구 이름"
  # 목록에 먹혀버린다(2026-08-30 EC2 실측 — "Permission deny rule '위' matches
  # no known tool" 식으로 깨짐). --allowedTools "WebSearch,WebFetch" 로 열어주고
  # --disallowedTools 로 나머지(Bash 포함, 위 "도구 허용" 절 참고)를 막는다.
  OUT="$(printf '%s' "$PROMPT_HEADER" | timeout 240 nice -n 10 "$CLAUDE_BIN" -p \
    "위 지침대로 판단하고 JSON 배열만 출력하라." \
    --model opus \
    --allowedTools "WebSearch,WebFetch" \
    --disallowedTools "Bash,Read,Write,Edit,Glob,Grep,NotebookEdit,Task,Agent,TodoWrite" \
    2>>"$LOG")"
  RC=$?
  if [ "$RC" -ne 0 ]; then
    log "claude 호출 실패 exit=$RC — 무거래 처리"
    exit 0
  fi
fi

log "프롬프트 요약: $(printf '%s' "$CONTEXT" | tr '\n' ' ' | cut -c1-500)"
log "응답 원문: $(printf '%s' "$OUT" | tr '\n' ' ' | cut -c1-2000)"

# --- 3. 검증·기록 (파싱 실패=무거래+로그) ---
RESULT="$(printf '%s' "$OUT" | "$PY" server/scripts/llm_trader.py record 2>>"$LOG")"
log "기록 결과: $(printf '%s' "$RESULT" | tr '\n' ' ')"
echo "$RESULT"
