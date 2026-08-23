# ADR-0001: 언어 — Python (Go 기각)

- Status: Accepted
- Date: 2026-07-28

## Context

15분봉 단타는 HFT가 아니다. 봉 완성 주기가 900초인데 Go가 주는 이득은 ms 단위 —
수익 곡선에 영향 없음. Kiwoom websocket 수신도 asyncio로 충분하다.

백테스팅을 노트북+그래프로 하고 싶다는 요구가 있고, pandas/matplotlib 생태계는
Python 독점이다. 한 언어로 통일하고 싶다는 요구도 있는데, 이 두 조건을 동시에
만족하는 언어는 Python뿐이다.

전작(stock-algo-trade)의 검증된 코드가 전부 Python이라, 이식 비용도 최소화된다.

## Decision

Python 3.12 + asyncio 단일 프로세스로 간다. Go는 채택하지 않는다.

## Consequences

- 백테스트/전략/리스크 코어가 pandas 기반 노트북과 같은 언어를 공유한다.
- 전작 코드(Donchian 로직, Toss 클라이언트, Telegram notifier 등)를 거의 그대로 이식할 수 있다.
- Go 재검토 조건: 진짜 틱 단위 HFT로 전환할 때. 그때도 실행 게이트웨이만 Go로 빼는
  하이브리드가 우선이지 전체 재작성이 아니다.
