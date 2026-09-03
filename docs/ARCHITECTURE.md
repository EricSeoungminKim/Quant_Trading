# Architecture Overview

**4개 평면(plane) 구조**(2026-09-03, Phase 1~7 재설계 완료). 나누는 기준은
기능이 아니라 *틀렸을 때 잃는 것*이다. 경계는 `tests/test_architecture.py`가
임포트 그래프를 파싱해 강제한다 — `KNOWN_DEBT`는 비어 있고(2026-08-24 부채
완납), 계속 비어 있어야 한다.

```
┌───────────────────────────── 정보 평면 (틀리면: 데이터가 빈다/선정이 나빠진다) ─┐
│  quant/collect/   스크래핑·피드·텔레그램·FRED·DART        (LLM/네트워크 허용) │
│  quant/analyze/   채점·뷰·산문       →  워치리스트 "파일"만 편집한다        │
│  quant/report/    자체 리포트 조립+렌더 (KR 08:00 / US 20:00)               │
└──────────────────────────────────────┬────────────────────────────────────┘
                            watchlist.yaml (파일이지 임포트가 아니다)
┌──────────────────────────────────────▼────────────────────────────────────┐
│  quant/trade/     전략 · 리스크 · 국면 · 루프        결정론적 코드만        │
│                   네트워크 없음, DB 없음, LLM 없음 — 테스트로 강제          │
└──────────────────────────────────────┬────────────────────────────────────┘
                        Protocol 경계 (quant/core/ports.py)
┌──────────────────────────────────────▼────────────────────────────────────┐
│  quant/adapters/  모든 I/O — 키움 WS/REST · Toss REST · 텔레그램            │
│  quant/apps/      조립 + CLI — 모든 평면이 만나는 유일한 곳                 │
│  quant/control/   원장 · 스코어보드 · 거버너 · 포렌식 · 실험 · outcomes    │
└─────────────────────────────────────────────────────────────────────────────┘

quant/core/      순수 도메인(models·ports·oms·portfolio) — 외부 의존성 0, 나머지
                 전부가 임포트할 수 있는 바닥.
quant/backtest/  라이브와 같은 run_cycle을 리플레이하는 엔진(ADR-4) + 통계 검증.
                 최상위 backtest/(사람이 돌리는 노트북)와 짝을 이루지만 다른 것.
quant/research/  파라미터 최적화·walk-forward·ML 학습 — quant/backtest/ 엔진 위에서 돈다.
```

## 평면별 규칙(요약 — 정본은 루트 `CLAUDE.md` "아키텍처 불변식")

| 평면 | 디렉토리 | 틀리면 | 허용 |
|---|---|---|---|
| 수집 | `quant/collect/` | 데이터가 빈다 | 스크래핑, LLM, 실패, 재시도 |
| 분석 | `quant/analyze/`, `quant/report/` | 선정이 나빠진다 | LLM, 느린 배치 |
| **거래** | `quant/trade/` | **돈을 잃는다** | 결정론적 코드만 |
| 제어 | `quant/control/` | 다음 세션이 나빠진다 | 자동 조정, 실험, 롤백 |

가장 중요한 규칙: **`quant/collect/`·`quant/analyze/`는 `quant/trade/`를
임포트하지 않는다.** 스크래핑한 뉴스가 주문으로 이어지는 경로를 코드 수준에서
끊는다 — 뉴스는 유니버스(워치리스트 파일)만 편집하고, 진입은 전략이 가격으로
판단한다. `quant/trade/`도 반대 방향으로 `collect`/`analyze`/`adapters`/`apps`를
모른다(어댑터 장애가 매매를 멈추면 안 된다). `quant/core/`는 `quant` 안에서
자기 자신만 안다. 전체 금지 쌍과 예외 목록은 `tests/test_architecture.py`의
`FORBIDDEN`/`KNOWN_DEBT`가 실행 가능한 정의다 — 이 문서보다 그쪽이 최신이다.

어댑터는 `quant/core/ports.py`의 Protocol(`Clock`, `DataFeed`, `Broker`,
`RiskManager`, `Notifier`, `EventSink`, `Strategy` 등)을 구현한다. 어댑터의
네트워크 예외는 어댑터 안에서 삼킨다 — raw 예외를 코어로 올리지 않는다.

## 데이터의 흐름 (임포트가 아니라 파일 인계)

평면 경계를 넘는 정보는 **함수 호출이 아니라 파일**로 건넌다 — 그래야 위 규칙이
코드로 강제된다. 세 갈래:

1. **유니버스**: `quant/report/`가 세션마다 `out/YYYY/MM/DD/{KR,US}_engine.json`을
   쓴다 → `server/scripts/own_brief.sh`가 그 JSON을 읽어 확신도 엔진
   (`quant/analyze/watch_scorer.py` `watch-score`)에 태우고, LLM 없이 임계
   통과분만 `data/watchlist.yaml`에 자동 등록한다 → `quant/trade/loop.py`의
   유니버스 롤(KST 자정·08:27·22:10)이 그 파일을 읽어 전략 심볼 목록을 갱신한다.
2. **성과**: 체결마다 `quant/trade/loop.py`가 `data/state/trades.jsonl`(append-only
   원장, `quant/control/ledger.py`)에 쓴다 → 주간 크론 `run scoreboard`가 승률·
   payoff·Wilson CI를 집계하고 `server/scripts/publish_performance.sh`가
   `performance.json`으로 내보내 `server/scripts/publish_portfolio.sh`가 공개
   사이트(`quant-portfolio` 저장소)에 발행한다 — **숫자가 자본 배분을 결정한다**.
3. **리서치 판정**: `data/ledger/selections.jsonl`(리포트 후보 선정 기록)에
   `quant/control/outcomes.py`(`cli outcomes`, 16:00 KST 크론)가 D+1/5/20 전방
   수익률을 채워 후보 채점 로직을 사후 검증한다. 전략 승격은 별도 경로 —
   `backtest-gate` CLI가 GO/NO_GO 판정 JSON을 쓰고, `quant/control/promotion.py`
   (`promote` CLI)가 그 증거·경과일·미활성 여부를 fail-closed로 검사한 뒤
   `config/settings.yaml`의 해당 전략 블록만(다른 블록은 바이트 동일 유지)
   `enabled`/`backtest_pass`/`evidence`로 갱신한다. `quant/control/`이 쓰고
   `quant/trade/`가 다음 리로드에 읽는다 — 거버너가 엔진을 직접 건드리지 않는
   이유와 같다.

## ADR 인덱스

의사결정의 배경과 근거는 개별 ADR(Architecture Decision Record)에 있다:

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

## 더 볼 곳

- 평면별 세부 불변식: `quant/core/CLAUDE.md`, `quant/trade/strategy/CLAUDE.md`,
  `quant/adapters/brokers/CLAUDE.md`, `quant/adapters/data/CLAUDE.md`,
  `quant/backtest/README.md`, `quant/report/README.md`, `server/CLAUDE.md`.
- 손으로 쓴 운영 지식(그래프가 모르는 "왜"): `docs/vault/00-START-HERE.md` 이하.
- 처음 이 저장소를 맡았다면 `docs/vault/착수보고서.md`부터.
