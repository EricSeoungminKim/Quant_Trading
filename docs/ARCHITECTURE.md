# Architecture Overview

Ports & Adapters(헥사고날) 구조. 핵심 규칙: 의존성은 항상 안쪽(domain)을 향한다.

```
        ┌─────────────────────────────────────────┐
        │  domain/  (모델·Protocol)                 │  ← 외부 의존성 0
        │  strategies/ (donchian·orb_scan·          │  ← domain에만 의존
        │               intraday_scan)  risk/       │
        │  portfolio/  regime/(시장별 국면)          │
        └───────────────┬─────────────────────────┘
                        │ Protocol 경계
   ┌──────────┬─────────┼──────────┬───────────┐
   │ brokers/ │  data/  │ notify/  │ execution/ │  ← 어댑터 (교체 가능)
   │ kiwoom(WS│ universe│ telegram │ paper      │
   │  =시세)  │ fx/hist │          │ (live=Toss)│
   │ toss(주문)│ service │          │            │
   └──────────┴─────────┴──────────┴───────────┘
        조립: app/assembly.py (composition root)
        루프: app/loop.py — 국면·유니버스 롤, MTM 차단기, 하트비트/성적표
        원장: app/ledger.py — 체결 영속화 → 전략별 스코어보드
        채점: app/watch_scorer.py — 리포트 후보 확신도 엔진(리포팅 레이어)
```

- 전략·리스크 코어는 순수 Python — 단위 테스트와 백테스트가 브로커 없이 돈다.
- 브로커(Kiwoom/Toss)는 같은 `Broker` Protocol의 구현체. 어느 브로커로 보낼지는 config.
- Protocol 경계 정의는 `quant/core/ports.py`.

의사결정의 배경과 근거는 개별 ADR(Architecture Decision Record)에 있다:

## ADR 인덱스

- [0001 — 언어: Python (Go 기각)](adr/0001-python-over-go.md)
- [0002 — 웹 프레임워크 없음 (FastAPI/Django 기각)](adr/0002-no-web-framework.md)
- [0003 — Ports & Adapters (헥사고날)](adr/0003-ports-and-adapters.md)
- [0004 — 백테스트 = 라이브 코드 경로 공유](adr/0004-shared-backtest-live-path.md)
- [0005 — 브로커 전략: Toss 먼저, Kiwoom 병행 준비](adr/0005-broker-strategy-toss-then-kiwoom.md)
- [0006 — Private Banker 레이어 (banker/)](adr/0006-private-banker-layer.md)
- [0007 — 전작에서 이식하는 것 / 버리는 것](adr/0007-ported-vs-discarded.md)
- [0008 — DataFeed는 "조회 실패"와 "데이터 없음"을 구분한다](adr/0008-datafeed-failure-vs-emptiness.md)
- [0009 — 일일 국면 판단 (시장별 리스크 배수)](adr/0009-daily-regime-assessment.md)

새 아키텍처 결정을 내릴 때는 `docs/adr/000N-slug.md` 형식으로 새 ADR을 추가하고
위 인덱스에 링크를 더한다.
