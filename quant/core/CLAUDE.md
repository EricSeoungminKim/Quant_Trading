# CLAUDE.md — quant/core/

## 여기 있는 것

- `models.py` — 순수 데이터클래스 (`Bar`, `Quote`, `Position`, `Signal`, `Order`,
  `OrderState`, `OrderStatus`, `Fill`, `Market`, `Side`, `SignalAction`,
  `Instrument`, `ApprovalRequest`). stdlib only.
- `terms.py` — FRED 릴리즈명 → 한국 시장 통용 표기 매핑 + 순수 조회 함수
  (`event_term`/`event_desc`/`event_full`/`event_freq`). `quant/collect/`(캘린더
  수집)와 `quant/analyze/`(리포트 렌더링) 양쪽이 쓴다.
- `oms.py` — 주문 상태기계 (접수 → 부분체결 → 체결/거부/취소/만료). 순수 함수만.
  **왜 core 인가**: 거래 평면과 브로커 어댑터가 **같은** 전이 규칙을 써야 하는데
  `quant.adapters` 는 `quant.trade` 를 임포트할 수 없다. 의존 방향의 바닥에 둬야
  양쪽이 쓸 수 있다. 어댑터는 `report_fill`(예외를 삼키는 변형)을 쓰고, 순수
  계층은 엄격한 `filled_from`/`on_*` 를 쓴다.
- `ports.py` — 코어(전략/리스크/루프)와 어댑터 사이의 Protocol 경계
  (`Clock`, `DataFeed`, `Broker`, `RiskManager`, `Notifier`, `Narrator`,
  `EventSink`, `OrderSink`, `KeyValue`, `Strategy`) + `Context`.

## 절대 여기 임포트하지 말 것

- `httpx`, `websockets` 등 네트워크 라이브러리 — 외부 의존성은 0이어야 한다.
- `quant.adapters.brokers.*`, `quant.adapters.notify.*`, `quant.adapters.data.*` 등
  어떤 어댑터도 — 의존 방향이 역전된다.
- `quant.apps.config` (YAML/env 로딩) — config 파싱은 `quant/apps/` 소관.

## 로컬 불변식 (interfaces.py 문서화 규칙 그대로)

- 전략은 `DataFeed`와 `Clock`만 읽는다. 브로커/실행을 직접 만지지 않는다.
- `DataFeed.history()`는 호출 시점 기준 **완성봉만** 반환한다 (look-ahead 금지).
  현재 형성 중인 봉을 포함시키지 않는다.
- 어댑터의 네트워크 예외는 어댑터 내부에서 처리한다 — 코어로 raw 예외를 전파하지
  않는다. `quant/core/`의 Protocol 시그니처는 예외가 아니라 `None`/빈 반환값으로
  실패를 표현한다 (`RiskManager.approve` 거부 시 `None`).
  **예외: `Broker.place_order` 는 `OrderState` 를 돌려준다**(Phase 6.3) — 거부/미제출/
  결론없음/부분체결을 `None` 하나로 뭉개면 사후에 "왜 안 샀지"에 답할 수 없고
  미체결 잔량이 사라진다.
- `Bar`는 완성된 OHLCV만 표현한다 — forming bar를 이 타입으로 만들지 않는다.
- 가격은 종목의 표시 통화(USD/KRW) 그대로 저장한다. 통화 환산은 `portfolio/`나
  `risk/`에서만 한다 — `quant/core/`은 환산 로직을 갖지 않는다.

## 새 Protocol/모델을 추가할 때

새 어댑터 종류가 필요해지면(예: 새로운 브로커 기능) 여기 Protocol을 먼저 확장하고,
어댑터가 그것을 구현하게 한다 — 반대 순서(어댑터에 메서드를 먼저 만들고 나중에
Protocol화)로 가지 않는다. Protocol에 docstring으로 계약(무엇을 반환하는지, 실패는
어떻게 표현하는지)을 명시한다.
