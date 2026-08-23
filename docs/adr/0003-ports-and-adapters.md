# ADR-0003: Ports & Adapters (헥사고날)

- Status: Accepted
- Date: 2026-07-28

## Context

전략·리스크 코어가 브로커 구현 세부사항에 묶이면 브로커 교체(Toss↔Kiwoom)나
paper↔live 전환마다 코드를 고쳐야 한다. AI(Claude) 에이전트가 작업하기 쉬운 구조도
필요하다 — 디렉토리 이름이 곧 책임을 말해야 한다.

## Decision

```
        ┌────────────────────────────────────┐
        │  domain/  (모델·이벤트·Protocol)     │  ← 외부 의존성 0
        │  strategies/  risk/  portfolio/     │  ← domain에만 의존
        └───────────────┬────────────────────┘
                        │ Protocol 경계
   ┌──────────┬─────────┼──────────┬───────────┐
   │ brokers/ │  data/  │ notify/  │ execution/ │  ← 어댑터 (교체 가능)
   │ kiwoom   │  live   │ telegram │ paper/live │
   │ toss     │  stub   │          │            │
   └──────────┴─────────┴──────────┴───────────┘
```

전략·리스크 코어는 순수 Python — 단위 테스트와 백테스트가 브로커 없이 돈다.
Kiwoom/Toss는 같은 `Broker` Protocol의 구현체. TQQQ 주문을 어느 브로커로 보낼지는
config 한 줄로 정한다.

## Consequences

- 브로커 교체·모드 전환이 config 변경만으로 가능하다.
- 디렉토리 이름 = 책임, 파일 하나 = 개념 하나 — AI가 읽기 쉬운 구조.
- Protocol 경계는 `quant/core/ports.py`에 명시된다.
