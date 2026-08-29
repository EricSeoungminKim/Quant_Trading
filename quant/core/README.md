# quant/core/

## 한 줄 정의

4개 평면(수집/분석/거래/제어) 그 아래를 받치는 **순수 도메인 계층** — 외부 의존성이
0인 stdlib(+pandas 타입힌트)만의 세계다. 틀리면 잃는 것: 여기는 `collect`·`analyze`·
`trade`·`control`·`adapters`가 전부 공유하므로, 버그 하나가 전체 시스템에 조용히
퍼진다. 에이전트 규칙은 [`CLAUDE.md`](CLAUDE.md) 참고.

## 주요 파일 지도

- `models.py` — 순수 도메인 데이터클래스(`Bar`, `Quote`, `Position`, `Signal`,
  `Order`, `OrderState`, `Fill`, `Market`, `Side` 등). stdlib only.
- `ports.py` — 코어와 어댑터 사이의 Protocol 경계(`Clock`, `DataFeed`, `Broker`,
  `RiskManager`, `Notifier`, `EventSink`, `Strategy` 등) + `Context`.
- `oms.py` — 주문 상태기계(접수→부분체결→체결/거부/취소/만료). 순수 함수만, I/O 없음.
- `clock.py` — `Clock` 구현: `SimClock`(백테스트), `WallClock`(실거래).
- `session.py` — 거래 세션(개장~마감) 조회.
- `report_clock.py` — 리포트 발행 시각과 세션 윈도우.
- `fx.py` — USD/KRW 환율 프로바이더(조회 함수만, brokers는 임포트하지 않음).
- `terms.py` — FRED 릴리즈명 → 한국 시장 통용 표기 매핑.
- `strategy_api.py` — 전략을 순수함수로 만드는 계약(엔진 분리 설계 Phase A).
- `timeseries.py` — 자본 곡선(equity curve) 성과 분석, 순수 stdlib.
- `log_redact.py` — 로그에서 시크릿을 지우는 필터.
- `portfolio/` — `portfolio.py`(포지션/현금 상태 + JSON 영속화, `PaperBroker`가
  사용), `ownership.py`(엔진이 직접 진입한 수량만 추적하는 소유 원장).

## 핵심 불변식

- **core는 `quant` 안에서 자기 자신만 안다** — `quant.adapters`/`quant.trade`/
  `quant.collect`/`quant.analyze`/`quant.control`/`quant.apps` 어느 것도 임포트하지
  않는다. `tests/test_architecture.py::test_core_imports_nothing_from_quant_outside_core`
  가 이를 강제한다.
- `httpx`, `websockets`, `yaml`, `pymysql`, `redis`, `duckdb` 등 외부 의존 금지
  (`FORBIDDEN_EXTERNAL["quant.core"]`).
- `DataFeed.history()`는 호출 시점 기준 **완성봉만** 반환한다(look-ahead 금지) —
  `Bar` 타입 자체가 완성 OHLCV만 표현한다.
- 어댑터의 네트워크 예외는 어댑터 안에서 처리한다 — core의 Protocol은 예외가 아니라
  `None`/빈 반환값으로 실패를 표현한다(`Broker.place_order`만 예외적으로 `OrderState`
  를 돌려준다).
- 가격은 종목의 표시 통화 그대로 저장한다 — 통화 환산은 `portfolio/`나 `risk/`에서만.

## 데이터 흐름

**상류**: 없음 — core는 정의(타입/Protocol/순수 상태기계)만 제공하고 아무것도
읽지 않는다. **하류**: `quant/adapters/*`가 여기 Protocol을 구현하고, `quant/trade/*`
가 여기 모델(`Signal`, `Order`, `Context`)로 전략/리스크를 짜며, `quant/control/*`·
`quant/analyze/*`도 `models.py`/`terms.py`/`timeseries.py`를 공용 어휘로 쓴다.

## 손대기 전에

- `uv run pytest -q tests/test_architecture.py -v` — 의존 방향(core가 바깥을
  임포트하지 않는지)과 금지 외부 라이브러리를 확인.
- 관련 단위 테스트: `tests/test_oms.py`, `tests/test_clock.py`, `tests/test_session.py`,
  `tests/test_fx.py`, `tests/test_timeseries.py`.
- 새 Protocol/모델이 필요하면 여기를 먼저 확장하고 어댑터가 구현하게 한다 —
  반대 순서(어댑터 먼저)로 가지 않는다(`CLAUDE.md` 참고).
- `uv run pytest`(전체 스위트)로 마무리 — core는 전 영역이 의존하므로 회귀가
  가장 넓게 퍼진다.
