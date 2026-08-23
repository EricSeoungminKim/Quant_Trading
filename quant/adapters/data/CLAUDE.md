# CLAUDE.md — quant/adapters/data/

## 여기 있는 것 (그리고 루트 `/data/`와 헷갈리지 말 것)

`quant/adapters/data/`는 **소스 코드**다 (git 추적):

- `clock.py` — `Clock` Protocol 구현: `SimClock`(백테스트, `set()`으로만 시간 이동),
  `WallClock`(실거래, 실제 시스템 시간). US/KR 세션 시간대는 `zoneinfo`로 처리.
- `stub.py` — `StubDataFeed`: **백테스트/스모크 전용** 결정론적 합성 1분봉
  (seed=42). paper/live에서 절대 쓰지 않는다.
- `resample.py` — 1분봉 → N분봉 리샘플 (`closed="left", label="left"`, 미완성봉 drop).
- `universe.py` — 그날의 거래 유니버스 (`FileWatchlistUniverse`: 텔레그램 `/watch`가
  쓰는 data/watchlist.yaml을 세션 경계에서만 읽음, `CompositeUniverse`, ranking).
- `fx.py` — 환율 (`DailyFxProvider` 일일 캐시), `service.py` — 소스 라우팅
  (`MarketDataService`: kiwoom_rt > toss > history 우선순위 + 시세 캐시 + health),
  `session.py` — Toss 시장 캘린더, `history.py`/`ingest/` — 로컬 Parquet 백필.

루트의 `/data/`(리포 최상단, `.gitignore`에 있음)는 완전히 다른 것 — 런타임 상태
(캐시, 로그, `report.log`)이며 **git에 커밋하지 않는다**. 혼동하지 않는다.

## 절대 여기서 하지 말 것

- `StubDataFeed`를 실데이터인 것처럼 paper/live 경로에 연결하지 않는다 — 합성
  데이터를 실거래 판단에 쓰면 안 된다.
- `history()` 구현이 형성 중인(미완성) 봉을 반환하게 하지 않는다 — look-ahead
  금지는 `quant/core/ports.py`의 계약이며, 모든 `DataFeed` 구현체가 지켜야 한다
  (`resample_1m`이 마지막 bin을 항상 버리는 이유, `StubDataFeed.history()`가
  `self._now`와 같은 타임스탬프의 마지막 행을 제외하는 이유).

## 실 데이터 어댑터를 추가하는 법

실시간/과거 데이터를 제공하는 새 소스(예: 특정 브로커의 캔들 API)를 추가하려면:

1. `quant.core.ports.DataFeed` Protocol(`quote()`, `history()`)을 구현하는
   클래스를 작성한다. 브로커에 종속적이면 `quant/adapters/brokers/<name>/datafeed.py`
   (예: `toss/datafeed.py`)에 두고, 브로커에 독립적이면 `quant/adapters/data/`에 둔다.
2. `history()`는 완성봉만 반환하고 `interval`(`"1m"|"5m"|"15m"|"1d"`)에 맞춰
   리샘플한다 — 1분봉만 원본으로 갖고 있다면 `resample.py`의 `resample_1m`을
   재사용한다.
3. 네트워크 예외는 어댑터 내부에서 처리 — 실패 시 빈 `DataFrame`/`None` 반환
   (raw 예외를 전략/코어로 전파하지 않는다).
