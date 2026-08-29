# quant/trade/

## 한 줄 정의

**거래 평면** — 신호 생성부터 주문 집행까지, 실제 돈이 움직이는 핫패스.
틀리면 잃는 것: **돈**. 그래서 이 평면 전체가 결정론적 코드만 허용한다 —
네트워크·DB·LLM 호출 금지(09:15에 MySQL이 딸꾹질했다고 매매가 멈추면 안 된다).
전략은 `strategy/`, 리스크는 `risk/`, 국면 판단은 `regime/`으로 더 세분화돼
있다(각자 README 참고).

## 주요 파일 지도

- `loop.py` — **핵심 파이프라인**: `strategy.on_cycle → risk.approve →
  broker.place_order → sinks.on_fill`. 이 저장소에서 가장 큰 파일(100KB+) —
  백테스트와 라이브가 `run_cycle`을 공유한다(ADR-4).
- `universe.py` — 전략이 다룰 심볼 목록(유니버스)을 결정하는 프로바이더.
- `structure.py` — 시장 구조 층: 지지/저항·전고/전저·이동평균·빗각(추세 기울기)·
  Williams %R.
- `reconcile.py` — 브로커 대사(reconciliation) — "엔진이 안다고 믿는 것"과
  "브로커에 실제로 있는 것"을 맞춘다.
- `approval.py` — 신규 진입 승인 게이트(텔레그램 버튼 승인 요청의 로컬 상태 영속화).
- `control.py` — 수동 킬 스위치(halt/resume/flatten 상태를 디스크에 영속화).
- `cash_audit.py` — 원장(trades.jsonl)과 실제 포트폴리오 현금을 대조(순수 계산,
  네트워크 없음).
- `fmt.py` — 전략 사유 문자열용 가격 표시 헬퍼.
- `indicators/` — 보조지표 순수 함수(`breadth.py`: 시장 전체 risk-off 게이트,
  `trend_gate.py`: QuantConnect 상위 전략 기반 추세 필터).
- `strategy/` — 전략 구현체. → [`strategy/README.md`](strategy/README.md).
- `risk/` — 사이징 + 하드레일. → [`risk/README.md`](risk/README.md).
- `regime/` — 국면(방어/중립/공격) 판단, 시장별(US/KR) 분리(ADR-0009).

## 핵심 불변식

- **`quant/trade/`는 `collect`·`analyze`·`adapters`·`apps`를 임포트하지 않는다**
  (`tests/test_architecture.py`의 `FORBIDDEN`). HTTP·DB 라이브러리(`httpx`,
  `requests`, `websockets`, `pymysql`, `redis`, `duckdb` 등)도 금지
  (`FORBIDDEN_EXTERNAL["quant.trade"]`) — **redis가 특히 그렇다**: 휘발성이라
  재시작을 견디지 않는데, 포지션·주문 상태·control 플래그는 견뎌야 한다.
- 어댑터는 `quant.core.ports`의 Protocol을 구현하고, `quant/trade/`는 그 Protocol
  (`ctx.data`, `ctx.clock`, `ctx.broker`)만 통해 바깥과 접촉한다.
- `on_cycle`은 `Signal`(목표 비중)만 반환하고, 실제 주문 수량/승인은
  `risk.manager.RiskManagerImpl.approve()`가 결정한다 — 사이징 로직이 전략에
  흩어지지 않는다.
- KNOWN_DEBT(아키텍처 위반 허용 목록)는 2026-08-24 기준 0건 — 새 위반은 고치는
  것이지 목록에 추가하는 게 아니다.

## 데이터 흐름

**상류**: `quant/core/*`(모델/Protocol), `quant/adapters/*`(DataFeed/Broker
구현체가 여기로 주입됨), `config/settings.yaml`(전략 파라미터, 핫 리로드).
**하류**: `data/state/trades.jsonl`(거래 원장) → `quant/control/ledger.py`가
읽어 스코어보드를 만든다. `quant/control/`은 반대로 `settings.yaml`을 써서
다음 리로드에 이 평면이 읽게 한다(직접 호출 없음).

## 손대기 전에

- `uv run pytest -q tests/test_architecture.py -v` — 임포트 경계 + 금지 라이브러리.
- `uv run python -m quant.apps.cli backtest --strategy donchian --days 90` —
  거래 핫패스 스모크(가장 빠르게 회귀를 잡는 커맨드).
- `loop.py`를 건드렸다면 `tests/test_loop*.py`(있는 범위) + 관련 전략 테스트를
  전부 돌린다 — 모든 전략이 이 파일을 공유한다.
