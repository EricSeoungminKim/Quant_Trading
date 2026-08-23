# ADR-0009 — 일일 시장 국면(regime) 판단과 risk_multiplier

## Status

Proposed (2026-08-08) — `quant/trade/regime/`는 구현됐지만 `risk/`·`app/`로의
배선은 아직 없다(아래 "배선 방법" 참고). 통합이 끝나기 전까지는 이 ADR도
Proposed로 둔다.

## Context

전략(donchian)은 시장 국면과 무관하게 항상 같은 사이징으로 진입한다. 명백히
위험선호가 꺾인 날(금리 급등, 지수 급락, 변동성 급등)에도 평상시와 같은 비중을
싣는다. 반대로 명백한 상승 국면에서 비중을 더 싣을 여지도 없다.

사용자 요구는 "채권, 금리, 비트코인 가격 등을 고려하여 오늘은 내려가는 날인지
올라가는 날인지 파악하고 방어적/중립적/공격적 비중"이다. 이걸 매 사이클(10초
폴링) 계산하면 루트 CLAUDE.md의 "거래 핫패스에 LLM/네트워크 호출 금지" 불변식을
깬다 — 사이클 중 외부 API가 죽으면 그대로 거래가 멈춘다.

## Decision

`quant/trade/regime/` 패키지를 신설해 **하루 1회, 세션 시작 전에만** 국면을
계산하고 파일에 캐시한다. 거래 사이클은 그 캐시만 읽는다.

### 구성

- `models.py` — `RegimeState(label, risk_multiplier, reasons, computed_at, degraded)`.
  `label`은 `"defensive" | "neutral" | "aggressive"`.
- `indicators.py` — 지표별 순수 채점 함수. 전부 `+1(위험선호)/0(중립)/-1(위험회피)`
  정수 점수 + 사람이 읽는 사유 문자열을 반환하고, 판단에 필요한 데이터가 없으면
  `score=None`(집계에서 제외, 억지로 0을 넣지 않음)을 반환한다.
  - `qqq_trend_score` — QQQ 종가의 20일 이평 대비 위치(로컬 `data/history/QQQ/1d`).
  - `qqq_volatility_score` — 최근 5일 실현변동성 vs 60일(로컬 데이터, 동일 소스).
  - `bond_yield_score` — 국내 국채 10년 수익률 변화(bp). Toss
    `GET /api/v1/market-indicators/prices` 클라이언트 주입, 실패 허용.
  - `kospi_score` — 코스피 지수 등락률(%). 동일 API, 동일 클라이언트.
  - `bitcoin_score` — 비트코인 가격 등락률(%). **구현체 없음** — 어댑터
    Protocol(`interfaces.BitcoinPriceAdapter`)만 정의, 어떤 공개 API를 쓸지는
    [미결정]. `None`을 주는 기본 어댑터로 두면 이 지표는 항상 제외된다.
- `interfaces.py` — `MarketIndicatorClient`, `BitcoinPriceAdapter` Protocol.
  `domain/interfaces.py`의 Clock/DataFeed/Broker/... 계열과는 의도적으로 분리했다
  — 코어 실행 루프가 의존하는 포트가 아니라 이 패키지 전용의 좁은 입력이라서다.
- `provider.py` — `RegimeProvider`:
  - `refresh(force=False) -> RegimeState`: 네트워크 I/O를 하는 유일한 메서드.
    캐시 파일(`data/state/regime.json`)의 `computed_at`이 오늘(Asia/Seoul)이면
    재계산하지 않는다.
  - `risk_multiplier() -> float`: **네트워크 호출 없음.** 메모리 → 캐시 파일
    순으로만 읽는다. 아무 것도 없으면(최초 실행, refresh 전) 중립(1.0)을 반환한다
    — "판단 불가면 중립" 원칙을 핫패스 레벨에서도 강제한다.
  - `current_state() -> RegimeState | None`: 위와 동일하게 네트워크 없이 조회.

### 점수 규칙

지표별 점수를 단순 합산 → 설정된 임계(`aggressive_min_score`/`defensive_max_score`,
기본 +2/-2)로 3분류. 규칙·배수·임계는 전부 `config/settings.yaml`의 `regime:`
블록에서 조정 가능:

```yaml
regime:
  risk_multipliers: { defensive: 0.5, neutral: 1.0, aggressive: 1.3 }
  aggressive_min_score: 2
  defensive_max_score: -2
```

**이 점수 규칙이 수익을 낸다는 증거는 없다.** ML 없이 규칙 기반·해석 가능하게
설계했고, 목적은 명백한 하락 국면에서 노출을 줄이는 방어이지 알파 창출이 아니다
— 모든 지표 함수 docstring에 명시했다.

### 판단 불가 처리

- 지표 하나가 조회 실패 → 해당 지표만 집계에서 제외(`score=None`), 사유 문자열은
  남는다("OO 조회 실패 — 지표 제외"). 나머지 지표로 계속 판단한다.
- 지표 **전부** 실패(로컬 QQQ 데이터도 없고 원격 클라이언트도 없거나 전부 실패) →
  `label="neutral"`, `risk_multiplier=1.0`, `degraded=True`. `degraded`는 정상
  중립(점수 합이 중립 구간)과 강제 중립(판단 자체를 못 함)을 구분하는 플래그다 —
  호출부가 이 값을 보고 로그/알림을 걸 수 있다(조용한 기본값 금지).

## 배선 방법 (통합 지점, 이 워커 범위 밖)

1. `app/assembly.py`에서 세션 시작 시 1회 `RegimeProvider.refresh()`를 호출한다
   (예: paper/live 루프 진입 직전, 또는 매 세션 첫 사이클 전).
2. `RegimeProvider`를 `Context`나 별도 필드로 `risk/manager.py`의
   `RiskManagerImpl`에 주입하고, 사이징 계산 결과에
   `regime_provider.risk_multiplier()`를 곱한다. 이 메서드는 네트워크를 호출하지
   않으므로 사이클 핫패스에서 안전하게 반복 호출할 수 있다.
3. `degraded=True`가 뜨면(예: `refresh()` 직후 또는 매 세션 시작 시 상태 점검)
   Notifier로 알림을 보낼지는 오케스트레이터가 결정한다 — `regime/`는 Notifier를
   모른다(domain 방향 의존 규칙 위반 방지).
4. 비트코인 지표를 실제로 켜려면 `BitcoinPriceAdapter` 구현체를 하나 만들고
   `RegimeProvider(bitcoin_adapter=...)`로 주입한다. 어떤 API를 쓸지는 여전히
   [미결정].

## 한계 / [미검증]

- 국내 국채 금리·코스피는 미국 레버리지 ETF(TQQQ/SQQQ)의 직접적 동인이 아니다 —
  Toss API가 국내 지표만 제공해서 쓰는 거시 리스크심리 대리지표(proxy)다.
  상관관계가 약할 수 있다. [미검증]
- 임계값(band_pct, band_bp, high_ratio/low_ratio, aggressive_min_score 등)은
  전부 임의로 잡은 기본값이다 — 과거 데이터로 튜닝하지 않았다. [미검증]
- 이 규칙이 실제로 손실을 줄이는지, 아니면 단순히 노출을 줄여 상승장 수익도 함께
  깎는지 백테스트로 검증하지 않았다. [미검증]
- 비트코인 지표는 어댑터 미구현으로 항상 제외된다 — 소스 API [미결정].
- risk_multiplier가 daily_loss_limit_pct·max_position_pct 같은 기존 하드레일과
  곱연산으로 합성되는지, 아니면 별도 축인지는 `risk/manager.py` 통합 시점에
  결정해야 한다 — 이 ADR은 그 결정을 내리지 않는다.

## Consequences

- 거래 사이클에 새 네트워크 의존성이 생기지 않는다 — `refresh()`는 세션 시작
  전에만, `risk_multiplier()`는 파일/메모리만 읽는다.
- `data/state/regime.json`은 런타임 상태(git 미추적)다 — `quant/adapters/data/CLAUDE.md`의
  "루트 `/data/`는 소스가 아니다" 관례를 따른다.
- 새 지표를 추가하려면 `indicators.py`에 순수 함수 하나, `provider.py`의
  `_compute()`에 호출 한 줄을 추가하면 된다. 기존 지표·캐시 로직에 영향 없다.
