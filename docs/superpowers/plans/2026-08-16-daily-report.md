# 매일 리포트 + 개장일 집계 창 (서브프로젝트 G) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.
> 스펙: `docs/superpowers/specs/2026-08-16-daily-report-design.md`.

**Goal:** 리포트를 주말·공휴일 포함 매일 생성·텔레그램 발송하고, 개장일 아침 리포트가
"마지막 개장일 이후" 일별 산출물(engine JSON)을 종목별 최신일 우선으로 집계한다.

**스펙과 달라진 실측 1건:** 스펙이 전제한 KR 앵커 일봉 `data/history/069500/1d` 는
EC2에 **존재하지 않는다** (069500 은 `data/history/069500/{YYYY}/` 분봉뿐, 마지막
2026-08-11 — 백필 크론이 없다). 따라서 KR 일봉 백필(yfinance `069500.KS`)을 신설한다.
US 앵커 `data/history/QQQ/1d` 는 신선함을 확인했다(마지막 봉 08-14).

**Architecture:** 순수 판정/병합 함수는 `quant/analyze/`(opendays·병합), 배선은
`quant/apps/report_cli.py`, 스케줄은 systemd 타이머+crontab. 거래 평면 무접촉.

## Global Constraints
- 거래 평면(`quant/trade/`) 무접촉 — 엔진은 세션 게이트가 이미 공휴일을 거른다.
- 없는 값 0 위장 금지: 앵커 부재 시 `None` 반환, 집계는 건너뛰고 로그. 안전한 방향
  실패 = 창이 넓어지는 것(집계 과다)이지 좁아지는 게 아니다. 창 상한 7일.
- `KNOWN_DEBT` 추가 금지, 테스트 약화 금지, `|safe` 금지, 커밋 메시지는 한국어 '왜'.
- 시크릿 값 화면 출력 금지. EC2 배포는 장 마감 후(오늘 토요일 — 휴장).
- 완료 주장 전: `uv run pytest` + `report_cli --help` 스모크.

---

### Task 1: 개장일 판정 순수 함수 (`opendays`)

**Files:** Create `quant/analyze/opendays.py` / Test `tests/test_opendays.py`

**Interfaces (Produces):**
- `anchor_dir_for(market: str, root: Path) -> Path` — KR→`root/"data/history/069500/1d"`, US→`root/"data/history/QQQ/1d"`. 그 외 ValueError.
- `last_open_day(anchor_dir: Path, today: date) -> date | None` — 앵커 일봉 parquet
  파티션(`*/*.parquet`, 정렬 후 마지막 2개만 읽음)에서 **`today` 미만**의 마지막 봉
  날짜. 디렉토리/파일 없음·빈 프레임·파싱 실패 → `None` (예외 전파 금지).
  봉 타임스탬프는 tz-aware(UTC 또는 KST) — `.date()` 로 환산하되 QQQ 봉은
  `04:00 UTC` = 그 거래일이므로 **UTC 기준 date** 를 쓴다 (실측: 08-14 금요일 봉 =
  `2026-08-14 04:00+00`). KR(069500.KS via yfinance 1d)도 자정 타임스탬프라 동일.
- `window_dates(last_open: date | None, today: date, cap: int = 7) -> list[date]` —
  `[last_open+1 .. today-1]` 오름차순. `last_open is None` → `[]` (집계 불가 = 기존
  동작 유지). 창이 cap 초과면 **최근 cap일만** 남기고 잘랐다는 사실은 호출부가 로그.

- [x] Step 1: 실패 테스트 — (a) 월 파티션 2개에서 마지막 봉 08-14 → `last_open_day(…, date(2026,8,18)) == date(2026,8,14)`; (b) `today=08-14` 당일이면 **그 전** 봉(08-13); (c) 디렉토리 없음 → None; (d) 빈 parquet 무시; (e) `window_dates(8-14, 8-18) == [8-15, 8-16, 8-17]`; (f) `window_dates(None, …) == []`; (g) cap 동작. 픽스처는 tmp_path 에 pandas 로 소형 parquet 생성(QQQ 실측과 같은 04:00 UTC 인덱스).
- [x] Step 2: 실패 확인 (`uv run pytest tests/test_opendays.py -v` → import 실패)
- [x] Step 3: 구현 (pandas 사용 — analyze 평면 허용)
- [x] Step 4: 통과 확인 + `uv run pytest -q tests/test_architecture.py`
- [x] Step 5: 커밋 `feat(analyze): 개장일 판정을 달력이 아니라 앵커 일봉 데이터로 (G)`

### Task 2: KR 일봉 백필 (yfinance `069500.KS`) + 크론

**Files:** Modify `quant/collect/quotes/yf_source.py`, `server/crontab.txt` / Create `server/scripts/backfill_kr_daily.sh` / Test `tests/test_yf_source.py`(기존 파일 있으면 거기)

**Interfaces:**
- yf_source `fetch()`: 야후 티커 매핑 — `symbol` 이 6자리 숫자면 `f"{symbol}.KS"` 로
  질의하되 **반환·저장 심볼은 원래 6자리 그대로**. (`069500` → 저장 경로
  `data/history/069500/1d/` — Task 1 의 KR 앵커 경로와 일치. 기존 Toss 분봉
  `data/history/069500/{YYYY}/` 와는 층이 달라 충돌 없음.)
- `backfill_kr_daily.sh`: `backfill_us_daily.sh` 를 본떠 SYMBOL=069500,
  INTERVAL=1d, source=yf, LOOKBACK_DAYS=40. 신선도 되짚기: 마지막 봉이 6일(달력)
  초과로 낡으면 텔레그램 경고(공휴일 연휴 최대 3~4일을 넘는 값). DRY_RUN 지원.
- crontab: `30 16 * * *` 매일 (KR 마감 15:30 후; 휴장일엔 새 봉이 없을 뿐 무해,
  yfinance 는 키·IP 제약 없음). 주석에 '왜 매일인가' 명시.

- [x] Step 1: 실패 테스트 — yf.download 를 mock 해 `fetch("069500", …)` 가 `069500.KS` 로 질의하는지 + `fetch("QQQ", …)` 는 그대로인지
- [x] Step 2: 실패 확인 → Step 3: 구현(매핑 3줄) → Step 4: 통과 + 실호출 1회 스모크(로컬: `uv run python -m quant.apps.cli fetch --symbol 069500 --source yf --interval 1d --start 2026-08-01` — 봉 날짜가 KR 개장일과 일치하는지 육안 확인, 08-15(광복절)·주말 봉이 **없어야** 정상)
- [x] Step 5: 셸 스크립트 + crontab 줄 작성 (스크립트는 `bash -n` 문법 검사)
- [x] Step 6: 커밋 `feat(collect): KR 앵커 일봉 백필 — 개장일 판정의 데이터 원천 (G)`

### Task 3: 개장일 집계 — 이전 일별 engine JSON 병합

**Files:** Create `quant/analyze/carryover.py` / Modify `quant/apps/report_cli.py`, `quant/analyze/templates/report.html.j2` / Test `tests/test_carryover.py`, 기존 리포트 테스트

**Interfaces:**
- Consumes: Task 1 의 `anchor_dir_for`/`last_open_day`/`window_dates`.
- **실측 보정**: engine.json 의 후보 축은 `candidates` 키가 아니라
  `symbols`(list[dict], 식별자 `symbol`) + `auto_watch`(문자열
  `"AUTO_WATCH: SYM:TAG+TAG …"` — own_brief→watch-score 가 이걸 먹는다,
  `market_brief.auto_watch_tokens`/`engine_tokens` 참고).
- `merge_carryover(payload: dict, prior: list[tuple[date, dict]]) -> dict` (순수) —
  `prior` 는 (날짜, 그날 engine JSON payload) **오름차순**. 반환 payload 에서:
  (a) `symbols`: 오늘 payload 에 이미 있는 `symbol` 은 그대로(오늘 우선), 없는
  심볼은 가장 최근 prior 날짜 것을 `carried_from: "YYYY-MM-DD"` 키를 붙여 뒤에
  추가. (b) `auto_watch`: prior 들의 토큰 중 오늘 토큰에 없는 심볼만 뒤에 덧붙임
  (오늘 토큰 순서 우선 — brief 캡(MAX_CANDIDATES)이 오늘 것을 먼저 먹게).
  원본 dict 는 변경하지 않는다(copy). 두 키 다 없으면 그대로 반환.
  주의: `engine_tokens` 는 EVENT 날짜를 오늘 `session_date` 로 찍는다 — 캐리
  뉴스가 "오늘 신선"으로 채점되는 건 **의도된 동작**이다(휴장 기간 재료를 첫
  개장일에 신선한 것으로 취급) — 코드 주석으로 명시.
- report_cli `_emit`: `_record_selections(payload_fresh)` 는 **병합 전** payload 로
  호출(캐리 항목이 오늘 selections 원장으로 재기록되면 판단 표본이 중복 오염).
  그 뒤 `window_dates` 의 각 날짜에 대해 `out/YYYY/MM/DD/{market}_engine.json` 을
  `_load_artifact` 로 읽어(없으면 그 날짜만 건너뛰고 로그) `merge_carryover` →
  병합본을 `write_machine`/렌더에 사용. 앵커 부재(`last_open_day=None`)면 병합
  없이 기존 동작 + stderr 로그 1줄.
- 템플릿: 후보 카드에서 `carried_from` 이 있으면 이름 옆 작은 배지
  `<span class="badge-carry">MM-DD 발</span>` (기존 badge 스타일 재사용, `|safe` 금지).

- [x] Step 1: 실패 테스트 — (a) 오늘 심볼 우선; (b) 이전 여러 날 중 최신일 채택; (c) `carried_from` 부여; (d) 원본 불변; (e) 빈 prior → 동일 payload; (f) report_cli 배선 테스트: tmp out/ 에 전일 engine.json 심어 두고 빌드 경로가 병합하는지(기존 리포트 테스트의 픽스처 패턴 재사용); (g) selections 원장에 캐리 심볼이 **안** 들어가는지
- [x] Step 2: 실패 확인 → Step 3: 구현 → Step 4: `uv run pytest -q -k "carryover or report"` + 전체
- [x] Step 5: 커밋 `feat(report): 개장일 아침 집계 — 휴장 기간 후보를 최신일 우선으로 병합 (G)`

### Task 4: 원장 기록 게이트 — 휴장일 중복 오염 방지

**Files:** Modify `quant/apps/report_cli.py` / Test 기존 `tests/test_report_cli*.py` 계열

**왜:** 매일 실행되면 토/일/월 크롤이 **금요일의 수급·시세를 새 날짜로 재기록**한다
— flows.jsonl 기간합(1일~1년 뷰)과 selections 판단 표본이 중복 오염된다.

**Interfaces:**
- `_should_record_ledger(market: str, root: Path, today: date) -> bool` (report_cli
  내부) — `last_open = last_open_day(anchor_dir_for(market, root), today)`;
  `last_open is None` → **True** (앵커 없으면 기존 동작 유지 — 기록이 판정에
  인질잡히지 않는다); 아니면 `today <= last_open + 1일`.
  - 화(정상): last_open=월 → 화 ≤ 월+1 ✓ 기록. **토**: last_open=금 → ✓ 기록
    (금요일 마감 데이터의 첫 기록 — 기존 월-금 스케줄은 이걸 놓쳤다).
    일·휴장월: ✗ 건너뜀(같은 금요일 데이터의 재기록).
- `_emit` 에서 `_record_flows`·`_record_selections`·`_log_overlap` 호출을 이 게이트로
  감싼다. 건너뛸 때 stderr 한 줄("원장 기록 건너뜀 — 마지막 개장일 데이터 기록됨").

- [x] Step 1: 실패 테스트 — (a) 일요일(last_open=금) 기록 안 함; (b) 토요일 기록함; (c) 정상 화요일 기록함; (d) 앵커 없음 → 기록함
- [x] Step 2: 실패 확인 → Step 3: 구현 → Step 4: `uv run pytest -q -k report` + 전체
- [x] Step 5: 커밋 `fix(report): 휴장일 원장 재기록 차단 — 수급·판단 표본 중복 오염 방지 (G)`

### Task 5: 매일 스케줄 + 텔레그램 요약

**Files:** Modify `server/systemd/market-report-kr.timer`, `server/systemd/market-report-us.timer`, `server/crontab.txt`, `server/scripts/run_report.sh`, `quant/apps/report_cli.py` / Test `tests/test_report_cli*.py`

**Interfaces:**
- 타이머: `OnCalendar=Mon..Fri 07:50 Asia/Seoul` → `OnCalendar=*-*-* 07:50 Asia/Seoul`
  (US 는 19:50). 주석에 '매일인 이유(G)' 한 줄.
- crontab: deepdive 두 줄(`0 5 * * 1-5`, `30 17 * * 1-5`)과 close_report
  (`20 16 * * 1-5`)를 `* * *` 매일로. `outcomes`(16:00)·own_brief 등 나머지는 불변
  (스펙 비목표). 각 줄 주석 갱신.
- `report_cli summary --market {KR|US}`: 오늘 `out/…/{market}_engine.json` 을 읽어
  **stdout 3줄 이내** 결정론 요약: `후보 N개`(auto_watch 토큰 수) + `상위:
  이름(점수)×3`(`symbols` 를 `baseline_score100` 내림차순, None 은 제외 — 0 위장
  금지) . 파일 없거나 파싱 실패면 exit 0 + 빈 출력(알림은 부가 기능 — 실패가
  발송을 막지 않는다).
- `run_report.sh` `notify ok`: `SUMMARY="$($PY -m quant.apps.report_cli summary --market $MARKET 2>/dev/null || true)"` 를 본문에 덧붙임.

- [x] Step 1: 실패 테스트 — summary: (a) engine.json 픽스처로 상위 3 출력; (b) 파일 없음 → 빈 출력 exit 0; (c) 후보 0개 → "후보 0개"
- [x] Step 2: 실패 확인 → Step 3: 구현 → Step 4: 타이머/크론/셸 수정(`bash -n`) → Step 5: `uv run pytest -q -k report` + 전체
- [x] Step 6: 커밋 `feat(ops): 리포트·deepdive·close-report 매일 실행 + 발행 알림에 결정론 요약 (G)`

### Task 6: EC2 배포 + E2E (컨트롤러 직접)

- [x] 배포(장 휴장 확인됨 — 토요일), KR 일봉 초기 백필(`--start 2024-01-01`),
  `last_open_day(KR)` 이 2026-08-14(금) 인지 EC2 에서 확인
- [x] 타이머 재로드(`systemctl daemon-reload` + timer 재시작), crontab 재설치
- [x] `DRY_RUN`/수동 1회로 summary 포함 알림 확인 (실제 발송 1회는 사용자에게 보임 — 사전 고지)
- [x] 첫 실전 검증 일정 기록: 일 07:50(첫 주말 자동 리포트) → 월 07:50(광복절 대체휴일 — 매일 원칙) → 화 07:50(집계 창이 금요일 이후 전체를 덮는지)
- [x] `docs/vault/변경기록.md` + 백로그 체크박스 + 커밋

## Self-Review
- 스펙 커버리지: 크론 매일(T5) / 발송+요약(T5) / 개장일 판정 순수 함수(T1, 단
  KR 앵커 데이터 신설 T2 — 실측으로 스펙 보정) / 집계 병합(T3) / 엔진 무변경 ✓.
  스펙에 없지만 매일 실행이 **유발**하는 결함(원장 중복 오염)을 T4 로 선제 차단.
- 타입 일치: `last_open_day → date|None` 을 T3(window_dates)·T4(게이트)가 같은
  시그니처로 소비. `carried_from` 은 T3 산출 → 템플릿 소비.
- 08-17 검증 시나리오가 T6 에 날짜별로 박혀 있다.
