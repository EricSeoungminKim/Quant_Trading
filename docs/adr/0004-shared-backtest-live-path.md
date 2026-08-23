# ADR-0004: 백테스트 = 라이브 코드 경로 공유

- Status: Accepted
- Date: 2026-07-28

## Context

백테스트와 라이브가 서로 다른 코드 경로를 쓰면 백테스트에서 검증된 로직이 라이브에서
다르게 동작할 위험이 있다 (look-ahead, 로직 드리프트 등).

## Decision

전작에서 검증된 패턴을 유지한다: `run_cycle`(MarketData → Signal → Risk → Order →
Fill)을 백테스트 리플레이와 라이브 루프가 공유한다. 데이터 핸들러만 교체된다.

look-ahead 방지: `history()`는 리플레이 시점 이전 완성봉만 반환한다.

## Consequences

- 백테스트 결과가 곧 라이브 로직의 검증이 된다.
- `DataFeed.history()` 구현체(stub, live)는 모두 이 규칙을 따라야 한다.
