# quant/trade/strategy/

## 한 줄 정의

전략 구현체 모음 — 각각 `quant.core.ports.Strategy` Protocol(`id`, `symbols`,
`on_cycle(ctx) -> list[Signal]`)을 만족하는 클래스 1개 = 파일 1개. 거래 평면
소속이라 틀리면 **돈을 잃는다**. 에이전트용 상세 규칙(등록 절차 등)은
[`CLAUDE.md`](CLAUDE.md) 참고 — 여기 README는 "무슨 전략이 있는가"를 사람이
빠르게 훑기 위한 것.

## 주요 파일 지도 (2026-08-29 기준, `STRATEGY_REGISTRY` 등록 순)

- `donchian.py` — Donchian 15분 채널 브레이크아웃(TQQQ/SQQQ). 전작에서 라이브
  검증된 파라미터 계승, 롱 온리. (`donchian_pure` = `shell.py`로 감싼 순수함수
  계약 파일럿, 아직 settings.yaml 미배선)
- `orb.py` — Opening Range Breakout, Zarattini & Aziz 논문 규격 그대로(비활성,
  `orb_scan`의 기준점).
- `orb_scan.py` — ORB 스캐너, 관심종목 유니버스 소비 다종목 롱 온리 변형.
- `intraday_scan.py` — 장중 세션 신고가 돌파(개장+30분~마감-60분), `orb_scan`이
  놓치는 개장 이후 움직임을 잡는다.
- `mean_reversion.py` — 평균회귀 반등, 과매도 구간 롱 진입, 오버나이트 허용.
- `cross_momentum.py` — 횡단면(상대) 모멘텀 로테이션, 관심종목 중 최근 강세 상위 롱.
- `confluence.py` — MACD·볼린저·RSI·이동평균·박스 인식 합류(confluence) 투표.
- `news_momentum.py` — 뉴스 모멘텀 개장매수, 리포트 EVENT 태그 소비(60KB, 최대
  전략 파일).
- `news_scalp.py` — 단타 갈래(A), 개장 진입·당일 청산 고정.
- `frgn_accumulate.py` — 외국인 적립 갈래(B), 수급 추세 태그 기반 고정액 매수.
- `scalp_1m.py` — 1분봉 스캘프, 조기 진입 + 확실한 이익실현(95KB, 저장소 최대
  단일 파일). (`scalp_1m_pure` = `shell.py`로 감싼 순수함수 계약 파일럿)
- `close_bet.py` — 종가배팅, 마감 무렵 강세 종목을 종가에 사서 다음날 시초 갭에 판다.
- `gap_fade.py` — 갭하락 되돌림, 순수 전용 신규 전략(레거시 쌍둥이 없음).
- `pullback_impulse.py` — 눌림목 임펄스 스캘프(5분봉), 순수 계약 전용 신규 전략.
- `rsi2_dip.py` — RSI(2) 눌림매수(Connors), 추세 위 단기 과매도 눌림.
- `intraday_momentum.py` — Zarattini·Aziz·Barbon(2024) "Beat the Market" 기반
  일중 모멘텀.
- `overnight_drift.py` — 오버나이트 드리프트, 마감 직전 매수→다음날 개장 직후 매도.
- `mr_vwap_quiet.py` — 저거래량 VWAP 평균회귀 스캘핑("조용한" 종목의 일시적
  하방 이탈 매수).
- `vol_breakout.py` — 변동성 돌파(Larry Williams), 전일 레인지 비율만큼 시가에
  더한 값을 돌파 기준으로.
- `shell.py` — `PureStrategy`(core.strategy_api)를 기존 `Strategy` Protocol로
  감싸는 어댑터(엔진 분리 설계 Phase A).
- `__init__.py` — `STRATEGY_REGISTRY` dict + `config/settings.yaml` 기반 팩토리.

## 핵심 불변식

- **전략 1개 = 파일 1개.** config 별칭으로 같은 클래스를 다중 인스턴스화하지
  않는다(전작 `donchian_apex_vm17...` 패턴 재발 금지, ADR-0007).
- `on_cycle`은 `Signal`(목표 비중, 수량 아님)만 반환 — 실제 주문 수량/승인은
  `risk.manager.RiskManagerImpl.approve()`가 결정.
- `ctx.broker`는 포지션 조회에만 쓴다 — 전략이 직접 주문을 내지 않는다.
- 파라미터는 생성자 인자(`params: dict`)로만 주입 — `quant.apps.config`(yaml
  로더) 임포트 금지.
- 포지션당 부가 상태(entry/stop/target)는 `Position.meta`에 저장 — `Portfolio`
  영속화에 얹혀 재시작에도 살아남는다.
- 공통 시맨틱(2026-08-10): 유효한 진입 신호를 '이미 보유/이미 진입'으로 막지
  않는다 — 같은 완성봉 1회 가드(`_last_entry_bar`)와 리스크 레일이 폭주를 막는다.

## 데이터 흐름

**상류**: `ctx.data`(DataFeed, 완성봉만), `ctx.clock`, `quant/control/`이 쓴
`config/settings.yaml`(파라미터 핫 리로드). **하류**: `Signal` 리스트 →
`quant/trade/risk/manager.py`(사이징) → `quant/trade/loop.py`(주문 집행).

## 손대기 전에

- `uv run python -m quant.apps.cli backtest --strategy <name> --days 90` — 전략별
  백테스트 스모크.
- 새 전략을 추가했다면 `tests/`에 단위 테스트 추가(`tests/test_donchian.py` 패턴
  참고, stub `DataFeed`/`Context`로 `on_cycle` 직접 호출) + `STRATEGY_REGISTRY`
  등록 + `config/settings.yaml`의 `strategies:` 블록 확인.
- `uv run pytest -q tests/test_architecture.py -v` — `FORBIDDEN_EXTERNAL["quant.trade.strategy"]`
  (`yaml`, `dotenv` 금지)까지 포함해 확인.
