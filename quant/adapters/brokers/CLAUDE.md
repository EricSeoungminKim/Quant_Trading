# CLAUDE.md — quant/adapters/brokers/

## 여기 있는 것

브로커 어댑터. 각 브로커는 raw HTTP/WS 클라이언트(`client.py`)와, 그것을
`quant.core.models`로 매핑하는 `quant.core.ports.Broker` Protocol 구현체
(`broker.py`)로 나뉜다.

- `toss/` — Toss Securities Open API. `client.py`(raw HTTP, rate limiter, 토큰
  캐시), `broker.py`(`TossBroker`), `datafeed.py`. **Phase 1 라이브 브로커** (ADR-0005).
- `kiwoom/` — Kiwoom REST API + 웹소켓. **데이터 전용 — 주문은 영원히 Toss**
  (사용자 결정 2026-08-08). 2026-08-10 실전 서버 검증 완료:
  - `websocket.py` + `datafeed.py` — 실시간 체결 피드. `kiwoom.realtime.enabled:
    true`로 시세 최우선 라우트(stale 30초 초과 시 Toss REST 자동 폴백).
  - `client.py` — 토큰(캐시는 base_url별 분리 — 모의/실전 토큰 호환 안 됨, 실측),
    `investor_flow_daily`(ka10059 종목별 기관-외국인 수급, watch-score가 사용).
    quote/candles/place_order는 여전히 `NotImplementedError` — 시세는 웹소켓,
    주문은 Toss가 담당하므로 의도된 공백이다.

## 절대 여기서 하지 말 것

- **벤더 예외를 코어로 전파하지 않는다.** 네트워크/API 예외(`TossAPIError`, `httpx`
  예외 등)는 반드시 어댑터 안에서 잡는다. 다만 그 다음 처리는 Protocol에 따라 다르다:
  - **`Broker`(주문/잔고/현금)** — 로깅 후 `None`/빈 dict/0.0을 반환한다. 대체할
    브로커가 없으므로 실패를 알려봐야 갈 곳이 없다. `toss/broker.py`가 표준 패턴이다.
  - **`DataFeed`(시세/봉)** — `quant.core.ports.DataSourceError`를 던진다.
    실패를 빈 프레임/`None`으로 위장하면 `MarketDataService`가 정상 응답으로
    오인해 다른 소스로 넘어가지 않고, 전략은 "돌파가 없었다"와 "시세를 못
    받았다"를 구분할 수 없게 된다. 벤더 예외가 새는 게 아니라 도메인 예외로
    변환하는 것이므로 위 원칙과 충돌하지 않는다 — `MarketDataService`가 흡수해
    코어까지 가지 않는다. 조회는 성공했는데 데이터가 없는 경우는 실패가 아니므로
    빈 값을 반환한다 (`toss/datafeed.py` 참고).
- **Kiwoom의 미검증 스펙 위에 기능을 넓히지 않는다.** 실키로 검증되지 않은
  응답 스키마를 가정한 파싱 로직을 추가하지 않는다 (`client.py` 상단 docstring 참고).
- **`MODE` 가드를 우회하지 않는다.** 주문/holdings 변경 엔드포인트는
  `os.environ.get("MODE") == "live"`가 아니면 HTTP 요청 전에 거부해야 한다
  (`toss/broker.py: place_order` 참고) — paper 모드에서 실주문이 나가면 안 된다.

## 새 브로커를 추가하는 법 (recipe)

1. `quant/adapters/brokers/<name>/` 디렉토리 생성.
2. `client.py`: raw HTTP/WS 래퍼. 도메인 모델을 모른다 — dict/DataFrame만 다룬다.
   인증, rate limit, 재시도는 여기서 처리.
3. `broker.py`: `quant.core.ports.Broker` Protocol 구현
   (`place_order`, `positions`, `cash`). `client.py` 예외를 전부 잡아서 로깅 +
   `None`/기본값 반환으로 변환한다 — `toss/broker.py`를 그대로 템플릿으로 쓴다.
4. `config/settings.yaml`의 `execution.broker`에 브로커 이름 추가하고, 조립
   코드(`quant/apps/cli.py` 또는 `app/`)에서 config 값에 따라 인스턴스를 선택하게
   배선한다.
5. `.env.example`에 필요한 시크릿 키 이름을 추가 (값은 채우지 않는다).
