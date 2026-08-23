# ADR-0005: 브로커 전략 — Toss 먼저, Kiwoom 병행 준비

- Status: Accepted
- Date: 2026-07-28

## Context

실계좌·돈·검증된 클라이언트가 전부 Toss에 있다. Kiwoom은 2025년 REST API가 출시된
지 얼마 되지 않았고, 해외주식(TQQQ) REST API 지원 범위는 실제 키 발급 후 검증이
필요하다 (출시 초기엔 국내주식 중심이었을 가능성 — 미검증 가정을 코드에 심지 않는다).

## Decision

Phase 1 라이브 주문은 Toss 어댑터로 간다. Kiwoom은 REST+websocket 어댑터
스켈레톤만 만들어 둔다. KR 시장·실시간 WS 피드는 Kiwoom이 담당한다.

전략 코드는 어느 브로커인지 모른다 — `domain.interfaces.Broker` Protocol만 본다.

## Consequences

- `quant/adapters/brokers/kiwoom/`은 실키 발급 전까지 `NotImplementedError`를 내는
  스켈레톤 상태로 유지된다.
- Kiwoom 엔드포인트 스펙이 실키로 검증되기 전까지는 추정 스펙 위에서 코드를 넓히지
  않는다.
