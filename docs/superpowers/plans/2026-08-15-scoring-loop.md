# 채점 루프 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Phase 7 리더보드가 굶고 있는 입력 두 가지(전 종목 기준가, 정보 있는 베이스라인)를 공급하고 장마감 결과 텔레그램을 추가한다.

**Architecture:** 기존 경로 확장만 — `fetch_symbol_quotes` 에 OHLCV additive 키, `report_cli build` 시세 대상 확대, `_trend_score`(검증된 순수 함수) 재사용으로 `baseline_score100`, `selection_judgment` 점수 출처 교체 + producer_version "2", 새 `close-report` CLI + 셸 래퍼. trade 평면 무접촉.

**Tech Stack:** 기존 그대로 (yfinance 배치, pandas, narrate 포트, session_pnl.sh 발송 패턴).

**스펙:** `docs/superpowers/specs/2026-08-15-scoring-loop-design.md` — 먼저 읽을 것.

## Global Constraints

- **trade 평면 무접촉.** `tests/test_architecture.py` 통과, `KNOWN_DEBT` 추가 금지.
- **실패를 0으로 위장하지 않는다.** OHLCV 부족 심볼은 `baseline_score100` 키 부재(0점 아님). 시세 결측은 개수 로그.
- **기존 소비자 무변경.** `fetch_symbol_quotes` 확장은 additive 키만 — `close/prev/change_pct/history` 스키마 불변. `machine_payload` 신규 키는 있을 때만 생성.
- **producer_version 규율.** 점수 산식이 바뀌므로 judgment 는 version "2" — v1 표본과 섞이지 않는다(`quant/adapters/schema/002_judgment.sql` 주석이 근거).
- **발송은 스케줄 푸시 갈래만.** `tg_bridge.py`(대화형 봇, 다른 토큰) 무접촉. LLM 서술은 선택 — 죽어도 발송된다.
- 테스트 약화 금지. 커밋 메시지 한국어 "왜". 완료 주장 전 `uv run pytest -q` 전체.

---

## 파일 구조

| 파일 | 역할 | 신규/수정 |
|---|---|---|
| `quant/collect/sources/market.py` | `fetch_symbol_quotes` 에 `ohlcv` additive 키 | 수정 |
| `quant/apps/report_cli.py` | 시세 대상 전 종목 확대 + 매핑 탈락 카운트 + baseline 계산 배선 | 수정 |
| `quant/analyze/baseline.py` | OHLCV→`_trend_score` 재사용 + 당일 미완성 봉 방어 (순수) | 신규 |
| `quant/analyze/render.py` | `machine_payload` 심볼에 `baseline_score100`(있을 때만) | 수정 |
| `quant/control/judgment.py` | score 출처 `baseline_score100` 우선 | 수정 |
| `quant/apps/cli.py` | `--scorer-version` 기본 "2" + `close-report` 서브커맨드 | 수정 |
| `quant/control/close_report.py` | 장마감 요약 조립 (순수) | 신규 |
| `server/scripts/close_report.sh` | 발송 래퍼 (session_pnl.sh 패턴) | 신규 |
| `server/crontab.txt` | `20 16 * * 1-5` close-report | 수정 |
| 테스트 5개 | 각 단위 | 신규/수정 |

---

### Task 1: `fetch_symbol_quotes` OHLCV 확장 + 조용한 탈락 카운트

**Files:**
- Modify: `quant/collect/sources/market.py:159-196` (`fetch_symbol_quotes`)
- Modify: `quant/apps/report_cli.py:188-197` (시세 수집부 — 탈락 카운트 로그)
- Test: `tests/test_market_quotes.py` (기존 관련 테스트 위치를 `grep -rn "fetch_symbol_quotes" tests/` 로 확인해 그 파일에 추가)

**Interfaces:**
- Produces: 심볼당 dict 에 `"ohlcv": pd.DataFrame`(columns `open/high/low/close/volume`, DatetimeIndex 오름차순, 최대 1y) 키 추가 — **야후가 그 심볼의 OHLCV 를 못 주면 키 자체가 없다.** 기존 키(`close/prev/change_pct/history`)와 소비자는 불변. Task 3 이 `ohlcv` 를 소비.
- report_cli 시세 수집부는 `요청 N / 매핑실패 N / 조회실패 N` 을 stderr 로 출력(기존 `_log_overlap` 스타일).

- [x] **Step 1: 실패하는 테스트** — 기존 테스트 파일의 yf mock 관례를 먼저 읽고 따르되, 의도는:

```python
def test_fetch_symbol_quotes_carries_ohlcv(monkeypatch):
    # yf.download mock 이 OHLCV 멀티컬럼 프레임을 주면 ohlcv 키가 붙는다
    q = fetch_symbol_quotes(["TQQQ"])
    df = q["TQQQ"]["ohlcv"]
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.is_monotonic_increasing
    # 기존 키 불변
    assert {"close", "prev", "change_pct", "history"} <= set(q["TQQQ"].keys())


def test_missing_ohlcv_means_no_key(monkeypatch):
    # 종가는 있는데 volume 이 전부 NaN 인 심볼 → ohlcv 키 부재 (0 프레임으로 위장 금지)
    assert "ohlcv" not in q["BAD"]
```

- [x] **Step 2: 실패 확인** — `uv run pytest -q -k fetch_symbol` → FAIL
- [x] **Step 3: 구현** — `yf.download` 호출에서 `["Close"]` 축약을 풀고 OHLCV 전체를 받아(같은 1회 배치), 심볼별로 소문자 컬럼 DataFrame 을 조립. NaN-전부 행 드랍 후 30행 미만이면 키 생략. 기존 close/history 계산은 그 프레임의 close 에서 그대로.
- [x] **Step 4: report_cli 탈락 카운트** — `codes` → `yahoo_syms` 변환부(192행 근처)에서 `매핑실패 = [c for c in codes if c not in market_map]` 개수, 조회 후 `조회실패 = 요청 - 응답` 개수를 stderr 한 줄로.
- [x] **Step 5: 통과 확인** — `uv run pytest -q -k "fetch_symbol or report"` → PASS
- [x] **Step 6: 커밋** — `feat(collect): 시세 배치에 OHLCV 동승 — 같은 1회 호출, 조용한 탈락은 개수로 보인다`

---

### Task 2: 전 종목 기준가

**Files:**
- Modify: `quant/apps/report_cli.py:169-197` — `codes = [code for code, _ in rank(cont)]` 의 상위 10 제한을 풀고 **cont 전체**(+ 앵커 중복 제거)로
- Test: 기존 report_cli 빌드 테스트 파일에 추가

**Interfaces:**
- Consumes: Task 1 의 카운트 로그(그대로 동작해야 함).
- Produces: `sym_quotes` 가 매핑 가능한 전 종목을 담는다 → `machine_payload` 가 그 심볼들에 `close` 를 붙인다(기존 로직 그대로) → `selections` 원장의 close 채워진 행이 늘어난다.

- [x] **Step 1: 실패하는 테스트** — cont 픽스처에 12개 심볼을 넣고, 시세 요청 목록이 10개로 잘리지 않고 12개 전부인지 assert (yf mock).
- [x] **Step 2: 실패 확인** → FAIL (10개로 잘림)
- [x] **Step 3: 구현** — `rank(cont)` 는 **노출용**(HTML 상위 10)으로만 유지하고, 시세용 `codes` 는 `list(cont.keys())` 로. 카드 상세(`stock_detail`, limit 6)는 무변경.
- [x] **Step 4: 통과 확인** + 회귀 `uv run pytest -q -k report`
- [x] **Step 5: 커밋** — `feat(report): 기준가를 노출 상위 10 이 아니라 유니버스 전부에 — 채점 교집합 5→50+ 목표`

---

### Task 3: `baseline.py` — 검증된 채점기의 적용 범위 확장 (순수)

**Files:**
- Create: `quant/analyze/baseline.py`
- Test: `tests/test_baseline.py`

**Interfaces:**
- Consumes: Task 1 의 `ohlcv` DataFrame, `quant/analyze/watch_scorer.py` 의 `_trend_score(daily) -> (score, reasons, breakdown)` (순수 — 시그니처는 그 파일 281행이 진실).
- Produces: `baseline_score(ohlcv: pd.DataFrame, today: date | None = None) -> int | None` — None 은 "채점 불가"(행 부족·컬럼 결측), 0~100 은 점수. **당일 미완성 봉 제거를 여기서 한다** (`watch_scorer.score_symbol` 428-432행과 같은 방어 — 그 코드를 읽고 같은 규칙으로).

- [x] **Step 1: 실패하는 테스트**

```python
# tests/test_baseline.py
"""베이스라인 = watch_scorer TREND 프로필의 적용 범위 확장.
핵심 계약: 채점 불가는 None (0 이 아니다 — 0 은 '최하위 평가'가 되어 IC 를 오염)."""
import pandas as pd
from datetime import date
from quant.analyze.baseline import baseline_score


def _df(n=60, up=True):
    idx = pd.bdate_range(end="2026-08-14", periods=n)
    base = pd.Series(range(n), index=idx, dtype=float)
    close = 100 + (base if up else -base) * 0.5
    return pd.DataFrame({"open": close - 0.2, "high": close + 0.5,
                         "low": close - 0.5, "close": close,
                         "volume": [1_000_000] * n}, index=idx)


def test_uptrend_scores_above_downtrend():
    up, down = baseline_score(_df(up=True)), baseline_score(_df(up=False))
    assert up is not None and down is not None and up > down  # 순위를 만든다


def test_insufficient_rows_is_none_not_zero():
    assert baseline_score(_df(n=10)) is None


def test_incomplete_today_bar_is_dropped():
    df = _df()
    # 오늘 날짜의 미완성 봉을 붙여도 점수가 어제 기준과 같아야 한다
    today = pd.Timestamp(date.today())
    partial = df.iloc[[-1]].rename(index={df.index[-1]: today})
    with_partial = pd.concat([df, partial])
    assert baseline_score(with_partial, today=date.today()) == baseline_score(df)
```

- [x] **Step 2: 실패 확인** → FAIL
- [x] **Step 3: 구현** — `_trend_score` 를 임포트해 감싼다: 컬럼 검증 → 당일 봉 제거 → 행수 확인(`_MIN_ROWS` 를 watch_scorer 에서 임포트) → `int(_trend_score(df)[0])`. 예외는 None (조용한 0 금지 — docstring 에 이유).
- [x] **Step 4: 통과 확인** + `uv run pytest -q tests/test_architecture.py` (analyze 내부 임포트라 위반 없음)
- [x] **Step 5: 커밋** — `feat(analyze): baseline_score — 검증된 TREND 채점기를 유니버스 전체로, 불가는 None`

---

### Task 4: payload 탑재 + judgment v2

**Files:**
- Modify: `quant/analyze/render.py` (`machine_payload` — `baseline_score100` 있을 때만), `quant/apps/report_cli.py` (build 경로에서 ohlcv→baseline_score 계산해 전달), `quant/control/judgment.py:108` (score 출처), `quant/apps/cli.py:1224` (`--scorer-version` 기본 "2")
- Test: 기존 `tests/report/test_render.py`·judgment 테스트 파일에 추가

**Interfaces:**
- Consumes: Task 3 `baseline_score`.
- Produces: payload 심볼 `baseline_score100: int`(있을 때만); `selection_judgment` 는 `attrs.get("baseline_score100")` 우선, 없으면 `trending_score100`(하위 호환 — 옛 원장 행 재처리 시); 새 판단의 producer_version 은 "2".

- [x] **Step 1: 실패하는 테스트** — ① payload: sym_quotes 에 ohlcv 있는 심볼만 `baseline_score100` 이 붙는다 ② judgment: attrs 에 둘 다 있으면 baseline 이 이긴다, baseline 만 없으면 trending 폴백, 둘 다 없으면 score None ③ cli 기본 버전 "2".
- [x] **Step 2: 실패 확인** → FAIL
- [x] **Step 3: 구현** — render 는 Task 6(A)의 sector 와 같은 "있을 때만 키" 관례. judgment 는 한 줄 우선순위. cli 는 기본값 문자열 교체 + 도움말에 왜("점수 산식 교체 — v1 과 표본 분리").
- [x] **Step 4: 통과 확인** + 회귀 `-k "render or judgment or outcomes"`
- [x] **Step 5: 커밋** — `feat(control): 베이스라인 점수를 judgment v2 로 — 상수 50 이 만들던 무순위를 끝낸다`

---

### Task 5: close-report — 장마감 요약 + 발송

**Files:**
- Create: `quant/control/close_report.py`, `server/scripts/close_report.sh`
- Modify: `quant/apps/cli.py` (서브커맨드), `server/crontab.txt` (`20 16 * * 1-5`)
- Test: `tests/test_close_report.py`

**Interfaces:**
- Consumes: `selections.load()` 행(오늘 채워진 `outcome_d{1,5,20}_bps`), `quant/control/ledger.py` 스코어보드 함수(실제 이름은 파일을 읽고), `leaderboard.promotion_verdict` 의 `Verdict`(frozen dataclass — `.reason` 한국어 완성문), `narrate.make_narrator`(선택).
- Produces: `build_close_report(rows, verdicts, scoreboard) -> str` (순수 — 결정론 요약문, 표본 수 병기), CLI `close-report` 가 stdout 으로 출력 + `record_run("close-report", ...)`. 셸 래퍼는 `session_pnl.sh` 를 그대로 본뜬 6줄(TZ 가드 + timeout + `tg "${OUT:0:3900}"`).

- [x] **Step 1: 실패하는 테스트** — 픽스처 행(오늘 만기 2건: +120bps/−80bps)으로 ① 상위/하위와 표본 수가 요약문에 있다 ② 만기 0건이면 "오늘 만기 지평 없음" (침묵이 아니라 명시) ③ verdict.reason 이 포함된다.
- [x] **Step 2: 실패 확인** → FAIL
- [x] **Step 3: 구현** — 순수 조립 함수 + CLI 배선(`cmd_outcomes` 인접 관례). narrate 는 결정론 요약을 프롬프트로 "2문장 코멘트"만 요청, None 이면 생략.
- [x] **Step 4: 셸 래퍼 + 크론** — `session_pnl.sh` 를 읽고 같은 구조로. 크론 주석: "outcomes(16:00) 뒤 — 그날 채워진 값을 읽는다".
- [x] **Step 5: 통과 확인** — 단위 + `uv run python -m quant.apps.cli close-report --help`
- [x] **Step 6: 커밋** — `feat(control): 장마감 결과 리포트 — 결정론 요약 + 선택적 서술, 표본 수 항상 병기`

---

### Task 6: 문서 + 전체 검증 + 로컬 E2E

- [x] **Step 1: 전체 검증** — `uv run pytest -q` / `tests/test_architecture.py -v` / `report_cli --help` / `cli backtest --strategy donchian --days 90` — 전부 통과(1895 passed / 11 skipped / 1 xfailed, architecture 8 passed).
- [x] **Step 2: 로컬 E2E** — `report_cli build --market KR --root .` 재실행 후 **직접 셈**: ① selections 원장 오늘 행 close 채워진 개수 10/71(14%) → 93/93(100%, 목표 50+ 초과) ② payload baseline_score100 92/93 존재, min 0 / max 100 / 중앙값 65 / 고유값 9종(상수 아님) ③ `cli close-report` 출력 육안 확인 — 만기 0건("오늘 만기 지평 없음"으로 정직하게 침묵). 결과 수치 변경기록에 기록.
- [x] **Step 3: 문서** — `docs/vault/변경기록.md` 맨 위 항목(무엇/왜/실측 수치), `docs/plans/개선-백로그-2026-08-15.md` §4 Phase 7 입력 항목에 E 참조, 이 계획 체크박스 [x].
- [x] **Step 4: 커밋** — `chore(docs): 채점 루프(E) 완결 — Phase 7 입력 공급 실측`

---

## Self-Review

- 스펙 커버리지: E-1(T1·T2) / E-2(T1·T3·T4) / E-3(T5) / E-4(시간) / 실패 모드(각 태스크 None·카운트 규칙 + T5 record_run) — 전부 매핑.
- 플레이스홀더: "실제 코드를 읽고 맞출 것" 지시들은 기존 코드가 단일 진실이라는 지시(A 계획과 같은 규약). 코드 블록 필요한 곳엔 있음.
- 타입 일관성: `ohlcv` DataFrame 컬럼(T1 정의→T3 소비), `baseline_score -> int | None`(T3→T4), `Verdict.reason`(탐사로 확인된 실제 필드→T5). producer_version "2" 문자열 T4 한 곳.
