# quant/backtest/

## 한 줄 정의

**백테스트 엔진 코드** — 라이브와 동일한 `run_cycle`을 리플레이하는 엔진 본체 +
통계 검증 도구. `backtest/`(리포 최상단 노트북 폴더, 사람이 돌리는 실험 공간)와
짝을 이루지만 다른 것 — 여기는 **코드**, 거기는 **실행/실험**. 틀리면: 백테스트
성과가 실제와 다르게 나와(look-ahead, 통계적 착시) 나쁜 전략을 라이브에 올리게 된다.

## 주요 파일 지도

- `engine.py` — 라이브와 동일한 `run_cycle`을 공유하는 리플레이 백테스트 엔진
  (ADR-4, 37KB). 전략/리스크 로직을 백테스트 전용으로 따로 구현하지 않는다.
- `roundtrips.py`(2026-09-03) — 체결 로그 → 라운드트립 FIFO 매수-수수료 배분의
  단일 정의. `engine._round_trip_pnl`과 `analytics._round_trip_detail`이 각자
  구현하던 같은 루프를 여기로 합쳤다.
- `walkforward.py` — 롤링 OOS(walk-forward) 안정성 하네스 — 전략 교체 시대의
  상시 검증 도구.
- `purged_cv.py` — Purged & embargoed 시계열 교차검증 — 라벨 누수를 막는 분할
  (2026-08-28).
- `statistics.py` — 다중검정 보정 통계 — "샤프가 좋다"를 "우연이 아니다"로
  바꾸는 층(2026-08-28).
- `strategy_report.py` — 전략 성적 보고서 — `quant-expert` 스킬 §4 형식을
  **코드가 강제**(2026-08-28).
- `fitness.py` — 적합도 함수 — 자동 개선(Phase 8)이 최적화할 대상.
- `catalyst_study.py` — 촉매 연구: DART 공시 유형·급등 선행신호·뉴스 키워드의
  실측 상관(서브프로젝트 M).
- `bullish_accuracy.py` — 호재 마커 사전 정확도 실측(서브프로젝트 P Part 2).
- `intraday_verify.py` — 당일 단타 스코어러 과거 재현 검증 하네스(서브프로젝트
  K Part 2).
- `report_follow.py` / `report_replay.py` — 리포트 추종/재구성 백테스트(서브프로젝트
  L-6, N).

## 핵심 불변식

- **라이브와 같은 코드 경로.** `quant.trade.loop.run_cycle`을 그대로 리플레이한다
  — 전략/리스크 로직을 여기 전용으로 재구현하지 않는다. 로직이 갈라지는 순간
  백테스트 결과는 의미가 없어진다.
- **look-ahead 금지.** 데이터 핸들러의 `history()`는 리플레이 시점 이전에
  완성된 봉만 반환한다(`quant/core/ports.py` 계약을 그대로 물려받음).
- 성과 주장(샤프·승률 등)은 `statistics.py`의 다중검정 보정과 `purged_cv.py`의
  누수 방지를 거치지 않으면 "우연이 아니다"라고 말할 수 없다.

## 데이터 흐름

**상류**: `quant/trade/*`(전략/리스크, 라이브와 동일 코드), `quant/adapters/data/*`
(과거 데이터 소스 또는 `stub.py` 합성 데이터), `data/history/`(로컬 Parquet).
**하류**: `backtest/*.ipynb`(노트북에서 이 패키지를 import해 실험), `quant/apps/cli.py
backtest` 서브커맨드, `quant/research/*`(파라미터 최적화가 이 엔진 위에서 돈다).

## 손대기 전에

- `uv run python -m quant.apps.cli backtest --strategy donchian --days 90` —
  루트 CLAUDE.md 필수 검증 커맨드, 엔진 자체의 회귀를 가장 빠르게 잡는다.
- 성과 지표를 다루는 변경(fitness, statistics)은 `quant-expert` 스킬 발동이
  필수 — 백테스트 숫자 해석은 적대적 서브에이전트 교차검증 대상이다(루트
  CLAUDE.md).
- `uv run pytest`에서 `tests/test_backtest.py`, `tests/test_backtest_hermetic.py`,
  `tests/test_backtest_statistics.py`, `tests/test_backtest_walkforward.py`,
  `tests/test_purged_cv.py` 등 관련 테스트 확인.
