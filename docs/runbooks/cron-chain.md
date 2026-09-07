# 크론 체인 계약 — 전수 시뮬레이션 (2026-09-07)

이 문서는 `server/crontab.txt`가 실제로 어떤 순서·자원·실패 모드로 도는지
샌드박스(Mac, `data/`·EC2 전부 미접촉)에서 전수 실행해 확인한 결과다.
실행 방법·발견한 구멍·자원 프로파일 순으로 적는다. 회귀 테스트는
`tests/e2e/test_cron_chain.py`(`QT_E2E=1`).

## 0. "00:00 국면(regime) 리프레시"는 크론이 아니다

`server/crontab.txt`에 자정 항목은 없다. 국면 리프레시는 상시 구동 중인
`quant-engine.service`(`quant.apps.cli paper`) 내부에서, KST 거래일이 바뀔 때
(`quant/trade/loop.py::run_paper_loop`, `regime_day != today_kst`) 매 루프
사이클마다 검사해 1회 실행된다 — 별도 프로세스로 뜨지 않으므로 이 문서의
"스크립트 표"에는 없다. 유니버스 롤(08:27/14:53/22:10)도 같은 프로세스 안의
버킷 경계(`_universe_roll_bucket`)다.

## 1. 실행 방법 (샌드박스)

```
sandbox_root/
  server, quant, .venv, config, pyproject.toml, uv.lock   ← 실 저장소 심볼릭 링크
  data/, out/, home/                                       ← 이 테스트 전용 빈 트리(또는 스냅샷 사본)
  bin/curl        ← 텔레그램 발신 가로채기(요청 로그 + {"ok":true})
  bin/systemctl, journalctl, sudo, stat  ← systemd/GNU stat 셈(watchdog.sh용)
```

핵심 규칙: 스크립트가 전부 `cd "$(dirname "$0")/../.."`로 저장소 루트를 찾으므로
**반드시 샌드박스 안의 심볼릭 링크 경로로 호출해야 한다** — 진짜 저장소 경로로
부르면 `dirname`이 진짜 저장소로 풀려 샌드박스가 무의미해진다(실측으로 걸린
함정, 아래 "테스트 작성 시 함정" 참고).

환경변수: `NOTIFY_ENV_FILE=/dev/null`(`.env.local` 부재와 동일 효과) +
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`(가짜 값, notify.sh 발송 경로를 실제로
타게 함) + `TELEGRAM_API_BASE`(안 쓰여도 무해하게) + `NOTIFY_*_LEDGER`/`_QUEUE`/
`_RATE_DIR`를 전부 샌드박스 경로로. `OPS_NARRATOR=none`, `OPENROUTER_API_KEY=""`.

## 2. 크론 체인 순서 (KR, 실측)

| 시각 | 스크립트 | RC | Wall | Peak(footprint) | 파일변경 | TG 발송 |
|---|---|---|---|---|---|---|
| (상시) | quant-engine.service 내부 국면 리프레시 | — | — | — | — | — |
| 05:00 | `report_cli deepdive --market KR` | 0 | 35.2s | 205MB(maxrss) | 4 | 0 |
| 06:40 | `report_accuracy.sh`(→`report_review_daily.sh` 체이닝) | 0 | 7.7s | 195MB | 5 | 1 |
| 07:35 | `run_report.sh KR`(`REPORT_MAX_WAIT` 대기 후 발행) | — | 대기시간 제외 측정 안 함 | — | — | — |
| 08:05 | `tg_bridge.py watch-reset KR` | 0 | 0.4s | 141MB | 0* | 0 |
| 08:12 | `own_brief.sh KR` | 1** | 0.7s | 30MB | 3 | 2 |
| 08:15 | `promotion_debate.sh KR` | 0 | 0.9s | 119MB | 1 | 0 |
| 09:30 | `flow_scan.sh KR` | 0 | 0.8s | 119MB | 1 | 0 |
| 09:35 | `tg_digest.sh KR` | 0 | 0.9s | 185MB | 3 | 1 |
| 15:35 | `session_pnl.sh KR` | 0 | 1.1s | 115MB | 2 | 0(큐만) |
| 16:05 | `trade_review.sh KR` | 0 | 4.7–7.1s | 169MB | 3–4 | 1(수정 전엔 재실행 시 2) |
| 16:10 | `scoreboard_weekly.sh` | 0 | 2.0s | 116MB | 1 | 0(큐만) |
| 16:20 | `publish_portfolio.sh` | 0 | 0.35s | 2.6MB | 1 | 0(배포 키 없어 조기 스킵) |
| 16:50 | `capital_review.sh` | 0 | 0.8s | 116MB | 1 | 0(강등 없음) |
| 16:55 | `daily_wrap.sh KR` | 0 | 1.3s | 121MB | 1 | 0(`.env.local` 없어 sendDocument 스킵) |
| 16:58 | `pnl_attribution.sh KR` | 0 | 0.4s | 115MB | 1 | 0 |
| 17:30 | `report_cli deepdive --market US` | 0 | 2.9s | 174MB | 4 | 0 |
| 매시 05분 | `ops_watch.sh` | 1(alert) | 2.2–2.4s | 252MB | 4 | 1(재실행해도 1 — clock 항목만 매번 재발송, 아래 §3-1) |
| */5분 | `watchdog.sh` | 0 | 0.4–1.7s | 1.6–10MB | 0–3 | 0(정상) |
| 매분 | `usage_sample.sh` | — | — | — | — | — |

\* watch-reset: 스냅샷에 `data/watchlist.yaml`이 없어 "이미 비어 있음"으로 조기 종료 —
데이터 공백(§4). \*\* own_brief: 실제 벽시계(장외)로 돌려 "데드라인 초과"로 정상
스킵(RC=1은 own_brief.sh의 알려진 종료 계약이 아니라 아래 §3-2에서 확인한 별도 스크립트
호출 체인 안의 종료코드 — 로그상 편입 자체는 의도대로 생략됐다). 데드라인 이전
시각(08:10)으로 강제한 재실행에서는 watch-score까지 정상 진행하고 Toss 자격증명
부재로 rc=2 결근(§4, 의도된 안전장치).

Peak 수치는 macOS `/usr/bin/time -l`의 `peak memory footprint`/`maximum resident
set size`다 — Linux RSS와 정확히 같은 지표는 아니지만(macOS footprint가 보통
더 크게 잡힌다) 크기 자릿수 비교에는 충분하다. 셸 래퍼 자체는 1.6–10MB로
무시할 만하고, 무게는 전부 그 안에서 뜨는 `.venv/bin/python` 서브프로세스
(pandas/yfinance/httpx import 오버헤드)에서 나온다 — 데이터 크기와 무관하게
기본 120MB 안팎이 "바닥"이고, yfinance 네트워크가 걸리는 것(deepdive)만
160–205MB까지 올라간다.

## 3. 발견한 구멍 (우선순위순)

### 1. [높음] `notify.sh`의 `_notify_send`가 오래된 bash에서 매번 조용히 실패한다 — **로컬(Mac) 실행에 실재하는 버그**

`server/scripts/lib/notify.sh` (`_notify_send`, thread_args 블록 근처):

```sh
local thread_args=()
if [ -n "$thread_id" ]; then
  thread_args=(-d "message_thread_id=${thread_id}")
fi
...
resp="$(curl ... "${thread_args[@]}" ...)"
```

`set -u` 아래서 **빈 배열**을 `"${arr[@]}"`로 펼치면 bash < 4.4에서
"unbound variable"로 죽는다(4.4+에서 고쳐진 버그). macOS의 `/bin/bash`·
`/usr/bin/bash`는 GPLv3 회피로 **3.2.57에 멈춰 있다**. `NOTIFY_LANE`이 설정된
상태(지금 저장소의 거의 모든 최근 스크립트가 설정한다)로 아직 그 레인이
`tg_lanes.json`에 안 묶여 있으면(`thread_id`가 빈 문자열인 흔한 경우)
`thread_args`가 빈 배열로 남아 이 버그를 100% 밟는다.

**증거**: `tests/e2e/test_cron_chain.py`의 `_stub_report_cli_trade_review`
경로를 macOS 기본 PATH 순서(`/usr/bin` 먼저)로 두고 재현 — `_notify_send`가
curl을 한 번도 부르지 못하고 곧장 `_notify_record_failure`로 빠졌다
(`notify.sh: line 350: thread_args[@]: unbound variable`).

**영향**: EC2(Ubuntu, bash 5.x)는 안 걸린다 — 프로덕션은 안전하다. 하지만
오너가 "로컬(Mac)에서도 paper 루프를 돌린다"(로컬 실행 메모)면, 로컬에서
장외 즉시발송(`notify_now`/장외 `notify_auto`) 경로를 타는 모든 알림이 macOS
기본 셸로 실행될 때마다 **소리 없이 실패**하고 `data/ledger/notify_failures.jsonl`
에만 쌓인다 — 사람이 그 원장을 보지 않으면 "왜 로컬에서는 텔레그램이 안 오지"를
영원히 모른다.

**제안 수정**(`notify.sh` — 다른 워커 담당 영역이라 직접 고치지 않았다):
`thread_args=()` 대신 `thread_args=(); [ -n "$thread_id" ] && thread_args+=(-d "message_thread_id=${thread_id}")`
로 바꿔도 빈 배열 확장 문제 자체는 남는다 — 근본 수정은 `"${thread_args[@]}"`
확장 자체를 `set +u`로 감싸거나(`set +u; ... "${thread_args[@]}" ...; set -u`),
`(( ${#thread_args[@]} ))`로 분기해 인자를 안 펼치는 두 갈래 curl 호출로
쪼개거나, 셔뱅을 `#!/usr/bin/env bash`에서 명시적 최신 bash 경로로 못박는
것이다. 이 저장소 전체가 macOS 로컬 실행을 지원 대상으로 삼는다면 CI/테스트에
"오래된 bash로 notify.sh 로드" 가드를 추가하는 편이 재발을 막는다.

### 2. [높음] `ops_watch.sh`의 "clock" 발견이 24시간 반복알림 억제를 매번 무력화한다

`quant/control/health.py::clock_findings` (약 282줄):

```python
out.append(Finding("clock", ALERT,
    f"엔진 하트비트 시각이 관측 시각과 {int(abs(age).total_seconds())}초 어긋난다"))
```

`dedupe_repeat_alerts`(같은 파일)는 `check|level|detail` 해시로 24시간 억제를
하는데, 이 `detail` 문자열에 **매번 달라지는 경과초**가 박혀 있어 지문이
매 실행마다 새로 만들어진다 — 즉 이 항목은 절대 억제되지 않는다.

**증거**: 같은 조건(엔진 하트비트가 계속 낡은 상태)에서 `ops_watch.sh`를
연속 두 번 돌렸다. 1회차 알림은 findings 7건(타이머 5·Redis·피드)을 담았고,
2회차는 그 6건이 **모두 사라지고**(정상적으로 억제됨) "clock" 하나만, 그것도
경과초가 654초→723초로 바뀐 채 **다시 발송**됐다. `dedupe_repeat_alerts`
자체는 올바르게 작동한다 — 이 발견 하나만 설계를 무력화하는 문구를 쓴다.

**영향**: 엔진 하트비트가 실제로 낡은 채 방치되는 사고(정확히 watchdog.sh가
잡아야 할 그 상황)가 나면, 매시 정각 05분마다 **다른 문구의 "새" 알림**이
영원히 나간다 — 이 스크립트의 헤더 주석이 명시적으로 경계하는 바로 그
"소음이라 사람이 끈다"를 이 항목 하나가 만든다.

**제안 수정**(`quant/control/health.py` — `quant/**`라 이번 작업 범위 밖,
보고만 함): 경과초를 `detail`에서 빼고 별도 필드로 옮기거나, 분/시간
단위로 버킷화(예: `f"...{int(abs(age).total_seconds()//60)}분대 어긋난다"`)해
지문을 안정시킨다 — 다른 유사 지표(예: intraday_history의 `age.days`)는 이미
일(day) 단위라 이 문제가 없다.

### 3. [중간, 수정함] `trade_review.sh`가 같은 날 재실행 시 텔레그램을 중복 발송했다

**증거**: 샌드박스에서 `trade_review.sh KR`을 같은 날짜로 연속 두 번 실행 —
수정 전에는 `data/ledger/notify_sent.jsonl`이 44→45로 늘어야 할 자리에 44→45→46
(2회차도 또 +1)까지 늘었다(동일 카드 문구 재확인). `report_cli trade-review`는
순수 재계산이라 "오늘 이미 통보했다"를 모르고, 스크립트도 그걸 확인하지 않았다.
크론 재시도, 사람의 수동 재실행(디버깅), 겹침 중 무엇이든 이 경로를 두 번
타면 오너에게 같은 리뷰 카드가 두 번 간다.

**수정**: `server/scripts/trade_review.sh`에 `data/state/trade_review_sent_<MARKET>.txt`
마커(오늘 날짜 기록, watchdog.sh의 상태파일 관례를 따름)를 추가 — 같은 날
재호출이면 `notify_auto` 자체를 건너뛴다. 발송/큐잉이 실패하면 마커를
기록하지 않아 다음 실행에서 재시도된다. 날짜가 바뀌면 자연히 다시 보낸다.
회귀 테스트: `tests/e2e/test_cron_chain.py::test_trade_review_does_not_double_send_on_rerun`,
`::test_trade_review_resends_on_new_day`.

**같은 패턴이 의심되는 다른 스크립트**(이번엔 못 고침 — 증거 부족·시간 부족):
`session_pnl.sh`·`daily_wrap.sh`·`pnl_attribution.sh`·`scoreboard_weekly.sh`도
"오늘 이미 처리했다"를 스스로 기억하지 않고 매 호출마다 재계산·재발송(또는
재큐잉)한다. 이번 샌드박스에서는 전부 원장이 비어 있어(§4) 실제 중복 발송을
관측하지 못했지만, 코드 구조상 같은 위험을 안고 있다 — `capital_review.sh`만
"강등이 실제로 일어났을 때만" 보내므로 구조적으로 안전하다.

### 4. [중간] `daily_wrap.sh`의 sendDocument가 `notify.sh` 게이트 전체를 우회한다

`daily_wrap.sh`(121번째 줄)는 `curl ... https://api.telegram.org/bot${TG_TOKEN}/sendDocument`를
**하드코딩**하고, 토큰도 `notify.sh`의 `_notify_token`(환경변수 우선)이 아니라
자체 `_env()`로 **`.env.local` 파일만** 읽는다(`run_report.sh`/`run_close_report.sh`도
같은 패턴). 결과:
- `TELEGRAM_API_BASE`로 리다이렉트 안 됨(하드코딩 URL) — 이번 샌드박스는 curl을
  PATH로 통째로 가로채 우회했지만, notify.sh만 신뢰하는 향후 테스트/운영 도구는
  이 스크립트를 놓친다.
- 레인 라우팅(`NOTIFY_LANE`)·발송 실패 원장(`notify_failures.jsonl`)·발송 성공
  원장(`notify_sent.jsonl`)·레이트리밋에 전혀 안 걸린다 — "무엇을 보냈는지"
  감사가 `daily_wrap.sh`의 sendDocument만 구멍이 난다.
- 환경변수로 토큰을 주입할 수 없어(테스트/새 배포 방식과 불일치) `.env.local`이
  있어야만 검증 가능하다.

이번 작업에서는 고치지 않음(다른 워커의 notify.sh 영역과 인접 — sendDocument를
notify.sh에 편입하는 리팩터는 그쪽 검토 대상으로 보고).

### 5. [낮음] `notify_sent.jsonl`/`notify_failures.jsonl`은 append에 flock이 없다

`_notify_enqueue`(notify_queue.jsonl)는 2026-09-06에 `<큐>.lock` 전용 락파일로
flock을 건 반면(주석에 그 이유가 상세히 적혀 있다), `_notify_record_sent`/
`_notify_record_failure`는 여전히 `>> "$f"` 맨몸 append다. 텍스트가 사전에
1000/200자로 잘리므로 PIPE_BUF(4096, Linux) 안에 대개 들어가 실사고 확률은
낮지만, 크론이 몰리는 시각(문서 상단 주석이 이미 그런 사례 셋을 든다)에
두 스크립트가 같은 초에 발송하면 두 원장 중 하나가 이론상 인터리브될 수
있다 — 같은 파일 그룹인데 보호 수준이 다르다.

### 6. [정보] `own_brief.sh`의 편입 데드라인은 시계 주입 지점이 없다

`notify.sh`는 `NOTIFY_NOW_HHMM`/`NOTIFY_NOW_DOW`로 테스트가 벽시계를 대체할 수
있게 해뒀다(`tests/test_notify_gate.py`). `own_brief.sh`의 데드라인 검사
(`date +%H%M` 직접 호출, 92~139줄)는 그런 주입 지점이 없다 — 이번 시뮬레이션도
실제 벽시계(장외)로 도니 "데드라인 초과"로만 확인했고, 데드라인 이전 분기는
가짜 `date` 바이너리를 PATH에 심어서야 재현할 수 있었다(정상 동작 확인함:
watch-score까지 진행 → Toss 자격증명 없어 rc=2로 안전하게 결근). 버그는
아니지만, notify.sh와의 일관성을 위해 같은 패턴을 넣으면 향후 회귀 테스트가
쉬워진다.

### 7. [정보, 수정 안 함] `usage_sample.sh`는 macOS에서 원천적으로 못 돈다

`/proc/loadavg`·`/proc/meminfo`·`/proc/stat` 전부 Linux 전용이라 이 스크립트는
샌드박스(macOS)에서 실행 자체가 무의미하다 — 이번 세션 중 다른 워커가 이미
`cpu_pct()`의 `bc` 의존성을 awk로 고치는 작업을 진행 중이었다(작업 파일 충돌
방지를 위해 손대지 않았다). EC2(Linux)에서는 정상 실행될 것으로 판단하나,
이 샌드박스로는 실측 검증이 불가능했다 — 자원 프로파일(§5)은 리포트/알림
스크립트들의 실측치로 대신한다.

### 8. [정보, 이 작업 범위 밖] `tests/test_notify_gate.py`가 로컬 저장소 원장을 오염시킨다

이번 검증(`uv run pytest tests/test_notify_gate.py`) 도중 발견: 이 테스트
스위트는 `NOTIFY_QUEUE`·`NOTIFY_FAILURE_LEDGER`·`NOTIFY_RATE_DIR`은 tmp_path로
격리하지만 **`NOTIFY_SENT_LEDGER`는 격리하지 않는다** — 발송 성공 케이스가
전부 실제 저장소의 `data/ledger/notify_sent.jsonl`(그리고 다른 실패 테스트
경로 일부가 `notify_failures.jsonl`)에 실제로 append된다. 확인해보니 이
로컬(Mac) 체크아웃의 두 파일은 **이미 100% 이런 테스트 픽스처 텍스트로만
채워져 있었다**(1566줄 전부 `"source":"bash"`, 633줄 전부 `"source": "engine"`
— 실제 운영 데이터가 전혀 없다). EC2 실 데이터는 아니라 위험하지 않지만,
로컬에서 이 두 파일로 뭔가(예: `cli health`의 발송 실패 카운트)를 검증하려는
사람은 완전히 오염된 신호를 보게 된다. `notify.sh` 담당 워커에게 보고 —
고치려면 그 픽스처에 `NOTIFY_SENT_LEDGER` tmp_path 격리 한 줄을 추가하면 된다.

## 4. 데이터 공백 (스냅샷에 없어서 못 본 것)

EC2 스냅샷(`data/state`, `data/ledger`, `data/public`, `config`, `out/2026/09/07`)에는
`data/watchlist.yaml`이 없다 — own_brief/flow_scan/tg_digest가 실제로 종목을
편입하는 경로(watch-score 확신도 게이트 통과 → watch-add)는 이번 시뮬레이션에서
**끝까지 확인하지 못했다**. 대신 watch-score 자체는 Toss 실시세 클라이언트가
필요해(`TOSS_CLIENT_ID`/`SECRET` 없음) "실시세 없이 채점하지 않는다"는 설계된
안전장치로 rc=2 결근하는 것까지는 확인했다 — 이건 버그가 아니라 의도된
페일세이프다.

## 5. 자원 프로파일 — 피크 vs 평균 추정

**측정 한계**: 이 표는 macOS 샌드박스 실측(위 §2)을 바탕으로 한 **추정**이다.
EC2는 Linux(다른 malloc/페이지캐시 동작)이고, `usage_sample.sh` 자체가
macOS에서 못 돌아(§3-7) 실측 `data/ops/usage.jsonl`과 직접 대조하지 못했다.

- **바닥(항상 상주)**: `quant-engine.service`(paper 루프) + `tg-bridge.service`.
  오너가 제시한 실측 상주치: 엔진 ~190MB + 브리지 ~125MB = **~315MB**
  (2GB 박스의 ~17%).
- **크론 스파이크 1건**: 이번 실측한 모든 리포트/알림 스크립트의 서브프로세스
  피크는 **115~120MB가 바닥, 네트워크(yfinance) 관련은 160~205MB**. 순수 셸
  래퍼(watchdog 등) 자체는 무시할 만하다(<10MB).
- **최악 겹침 창 추정**: `server/crontab.txt` 상단 주석이 이미 스스로 진단한
  세 몰림 지점(과거에 벌려 놓은 것들)과 별개로, 06:xx대(리포트 정확도 06:40 +
  daily_feedback 06:25 + trade_review 06:17(US) + macro_collect 06:15 + backfill
  06:10대)처럼 **LLM/네트워크를 쓰는 리포트 스크립트 3~4개가 같은 10분 창에
  몰리는 구간**이 KR 16:xx·US 06:xx 마감 후에 반복된다. 이 창에서 겹치는 것이
  최대 2개라 가정해도(크론틱이 1분 단위라 완전 동시 시작은 드물지만, 앞
  스크립트가 늦게 끝나면 겹친다):
  `315MB(상시) + 2 × ~200MB(리포트 서브프로세스) ≈ 715MB` — 1.8GB의 ~40%.
  3개가 겹치면(예: 06:15 macro_collect + 06:17 trade_review US + 06:25
  daily_feedback US가 앞 작업 지연으로 밀려 겹치는 경우) **~915MB**, 1.8GB의
  ~51%. 스왑 없이는 여유가 있지만, systemd/OS 자체 오버헤드(측정 안 함)를
  더하면 절반을 넘는 구간이 하루 중 두 번(KR·US 마감 후) 있다고 보는 게
  안전하다.
- **평균**: 크론이 없는 시각은 바닥(~315MB, 1.8GB의 ~17%)에 수렴한다 — 크론
  스크립트는 대부분 1~35초 안에 끝나 하루 대부분의 표본에서 관측되지 않는다
  (`* * * * *` usage_sample.sh가 1분 해상도라 초 단위 스파이크의 상당수를
  놓칠 가능성도 있다 — 이 자체가 관측 한계다).

## 6. 회귀 테스트

`tests/e2e/test_cron_chain.py` — 기본 실행에서 빠진다(`pytest.mark.e2e` +
`pyproject.toml`의 `addopts`에서 `-m 'not live and not e2e'`). 돌리려면:

```
QT_E2E=1 uv run pytest -m e2e tests/e2e/test_cron_chain.py -v
```

포함 항목: watchdog(정상 시 침묵) · ops_watch(종료코드 계약 0/1/2) ·
capital_review/pnl_attribution(빈 원장에서 침묵) · daily_wrap(토큰 없어도
안 죽음) · trade_review 중복발송 회귀(§3-3의 수정을 지킴, 날짜 바뀌면 재발송
확인). 텔레그램은 가짜 `curl`로, systemd는 가짜 `systemctl`/`journalctl`/
`sudo`로 가로챈다 — 실제 네트워크·EC2·이 저장소의 실제 `data/`는 절대
건드리지 않는다.

**테스트 작성 시 함정 2가지**(둘 다 이 파일 작성 중 실측으로 걸림, 다음에
비슷한 셸 e2e 테스트를 쓸 사람을 위해 남긴다):
1. 스크립트를 실제 저장소 절대경로로 부르면 `cd "$(dirname "$0")/../.."`가
   진짜 저장소로 풀려 샌드박스가 무의미해진다 — 반드시 샌드박스 안의
   심볼릭 링크 경로로 호출.
2. macOS 기본 PATH(`/usr/bin` 먼저)로 두면 `env bash`가 3.2.57을 집어
   §3-1의 버그를 밟는다 — 테스트 PATH는 `/opt/homebrew/bin`(bash 5)을 먼저
   둔다.
