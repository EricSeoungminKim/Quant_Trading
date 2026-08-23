# ADR-0008 — DataFeed는 "조회 실패"와 "데이터 없음"을 구분한다

## Status

Accepted (2026-08-06)

## Context

기존 어댑터 규약은 "네트워크 예외는 어댑터 안에서 잡고 `None`/빈 값을 반환한다"였다.
`Broker`에는 맞는 규칙이다 — 대체 브로커가 없으니 실패를 알려도 갈 곳이 없다.

그런데 `MarketDataService`(ADR-0003의 부패 방지 계층)는 **소스가 예외를 던질 때만**
다음 우선순위 소스로 넘어간다. `TossDataFeed`가 모든 예외를 삼키고 빈 프레임을
반환하고 있었으므로, 서비스는 그것을 정상 응답으로 기록하고(`provenance="toss"`,
`degraded=False`) 폴백을 시도하지 않았다. 즉 **폴백 계층이 설계상 존재했지만 실제로는
발동할 수 없었다.**

더 나쁜 결과는 전략 쪽이다. 빈 프레임을 받은 `DonchianStrategy._check_entry`는
`len(bars) < lookback_bars + 1`로 조용히 `return None` 한다 — "돌파가 없었다"와
"시세를 못 받았다"가 완전히 같은 모양이 된다. 장애가 무증상으로 흐른다.

## Decision

Protocol별로 실패 처리 규약을 나눈다.

- `Broker` — 실패 시 `None`/빈 dict/0.0 반환 (기존 유지).
- `DataFeed` — 실패 시 `domain.interfaces.DataSourceError`를 던진다.
  벤더 예외(httpx, `TossAPIError`)를 그대로 흘리는 것이 아니라 도메인 예외로
  변환하는 것이므로 "벤더 예외를 코어로 전파하지 않는다"는 원칙과 충돌하지 않는다.
  `MarketDataService`가 이를 잡아 폴백하고, 모든 소스가 실패하면 그때 `None`/빈
  프레임을 반환한다 — 코어는 여전히 예외를 보지 않는다.

구분 기준: **조회 자체가 실패했는가**. 조회는 성공했는데 해당 심볼 데이터가 없으면
그것은 실패가 아니므로 빈 값을 반환한다. 갱신에 실패했지만 유효한 캐시가 있으면
stale 데이터를 반환한다 — 실제 데이터가 빈 값보다 낫고, 이것도 실패가 아니다.

## Consequences

- 폴백이 실제로 동작한다. 1차 소스 실패 시 경고 로그 + `health().degraded=True` +
  `provenance()`가 실제 서빙 소스를 보고한다.
- 새 `DataFeed` 구현체를 추가할 때 이 규약을 지켜야 한다. 실패를 빈 값으로 위장하면
  폴백이 조용히 죽는다.
- `MarketDataService`를 거치지 않고 `DataFeed` 구현체를 직접 쓰는 코드는 이제
  `DataSourceError`를 처리해야 한다. 조립은 서비스를 통하도록 되어 있으므로
  (`app/assembly.py`) 정상 경로에는 영향이 없다.
