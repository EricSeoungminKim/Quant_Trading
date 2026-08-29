# quant/adapters/

## 한 줄 정의

**네트워크·디스크 I/O는 오직 여기서만** — `quant.core.ports`의 Protocol
(`Clock`, `DataFeed`, `Broker`, `RiskManager`, `Notifier`, `EventSink` 등)을
구현하는 어댑터 계층. 4평면(수집/분석/거래/제어) 어디에도 속하지 않지만,
거래 평면이 브로커/시세를 만지는 유일한 경로이므로 틀리면 **주문이 안 나가거나
잘못 나간다**. `brokers/`·`data/`는 자체 [`CLAUDE.md`](brokers/CLAUDE.md)/
[`CLAUDE.md`](data/CLAUDE.md)가 있다.

## 주요 파일 지도

- `brokers/` — 브로커 어댑터. `toss/`(Toss Securities Open API, **Phase 1 라이브
  브로커**, ADR-0005), `kiwoom/`(REST+웹소켓, **데이터 전용 — 주문은 영원히 Toss**,
  2026-08-10 실전 서버 검증 완료). 각 브로커는 raw 클라이언트(`client.py`) +
  Protocol 구현(`broker.py`)로 나뉜다.
- `data/` — `clock.py`(`SimClock`/`WallClock`), `stub.py`(백테스트 전용 합성
  1분봉, seed=42), `resample.py`(1분봉→N분봉), `universe.py`(그날의 거래
  유니버스, `FileWatchlistUniverse`), `fx.py`(환율 일일 캐시), `service.py`
  (`MarketDataService`: kiwoom_rt > toss > history 우선순위 라우팅), `session.py`
  (Toss 시장 캘린더), `history.py`/`ingest/`(로컬 Parquet 백필).
- `execution/` — `paper.py`(paper 브로커, 현재가 즉시 체결, Broker Protocol 구현).
- `notify/` — `telegram.py`(Notifier 구현체), `telegram_approval.py`(인라인 버튼
  진입 승인 봇).
- `macro/` — `fred.py`(FRED 매크로 시계열 수집 어댑터).
- `persistence/` — `sink.py`(EventSink: JSONL append / 콘솔 / 다중 sink 브로드캐스트).
- `schema/` — MySQL 마이그레이션 SQL(`001_initial.sql` ~ `005_forward_return_rebuild.sql`).
- `db.py` — 분석 저장소(MySQL) 연결 + 마이그레이션 실행.
- `env.py` — `.env.local` 로딩(값은 절대 로그에 남기지 않음).
- `http.py` — 공용 HTTP 클라이언트.
- `kv.py` — 휘발성 키-값 저장소(`core.ports.KeyValue` 구현).
- `narrate.py` — 서술기 어댑터(결정론적 판정을 산문으로, Phase 5.4).
- `olap.py` — Parquet 위 분석 질의(DuckDB).
- `regime_indicators.py` — 국면 지표 어댑터(`quant/trade/regime/interfaces.py` 구현).
- `smart_flow_log.py` — 키움 웹소켓 "세력 신호" 프레임을 원장에 남기는 어댑터.
- `tick_log.py` — 엔진이 사이클마다 읽는 시세를 초 단위로 디스크에 남기는 어댑터.

## 핵심 불변식

- **`quant/adapters/`는 `quant/trade/`를 임포트하지 않는다** — 어댑터는 코어의
  Protocol을 구현할 뿐, 거래 로직을 알지 않는다(`tests/test_architecture.py`).
- 벤더 예외는 어댑터 안에서 삼킨다. 다만 그 다음 처리는 Protocol에 따라 다르다:
  - **`Broker`**(주문/잔고/현금) — 로깅 후 `None`/빈 dict/0.0 반환(대체할 브로커가
    없으므로 실패를 알려도 갈 곳이 없다).
  - **`DataFeed`**(시세/봉) — `DataSourceError`를 던진다(빈 값으로 위장하면
    `MarketDataService`가 정상 응답으로 오인해 다른 소스로 폴백하지 못한다).
- `StubDataFeed`(`data/stub.py`)는 백테스트/스모크 전용 합성 데이터 — paper/live
  경로에서 절대 쓰지 않는다.
- `history()`는 완성봉만 반환한다(look-ahead 금지) — 모든 `DataFeed` 구현체가
  지켜야 하는 계약.
- Toss `place_order`류 엔드포인트는 `MODE=live`가 아니면 HTTP 요청 전에 거부한다
  — paper 모드에서 실주문이 나가면 안 된다.

## 데이터 흐름

**상류**: 외부 브로커/시세 API(Toss, Kiwoom), FRED, MySQL/Parquet 로컬 저장소.
**하류**: `quant/core/ports.py`의 Protocol을 통해 `quant/trade/loop.py`(엔진)와
`quant/apps/assembly.py`(composition root)로 주입된다.

## 손대기 전에

- `uv run pytest -q tests/test_architecture.py -v` — `adapters → trade` 임포트가
  없는지.
- 새 브로커/데이터 소스를 추가했다면 `brokers/CLAUDE.md`/`data/CLAUDE.md`의
  recipe(Protocol 구현 → `config/settings.yaml` 배선 → `.env.example`에 키 이름
  추가)를 그대로 따른다.
- Kiwoom 관련 변경은 "미검증 스펙 위에 기능을 넓히지 않는다" 원칙 확인
  (`brokers/CLAUDE.md`).
