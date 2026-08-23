"""시장 전체 하락(risk-off) 게이트 — 개별 종목이 아니라 **시장 앵커**(KR:
KODEX200 069500, US: QQQ)의 당일 낙폭으로 "오늘은 시장 전체가 밀리는 날인가"를
판정한다. QuantConnect 이식(trend_gate.py, 일봉·개별종목 단위)과는 축이 다르다
— 이건 "종목이 나쁘다"가 아니라 "오늘 시장이 나쁘다"를 본다.

## 도입 배경 (2026-08-18 관측 사실)

같은 날 KR 장에서 news_momentum이 09:00:11에 3종목(005180·005930·034020)을
동시 진입해 전부 -2% 손절, intraday_scan이 13:30에 096770을 당일 범위 92%
지점(세션 신고가)에서 진입해 그날 최대 손실(-2.37%)을 냈다. 유니버스 17종 중
15종이 하락한, 개별 종목이 아니라 **시장 전체가 되돌린 날**이었다 — 그런데
세 전략 모두 시장의 방향을 전혀 보지 않고 롱 진입을 계속했다.

## 측정 결과(2026-08-18 KR+US, EC2 실측, 원장 trades.jsonl 대조) — 결과가 약하다

앵커(KODEX200/QQQ) 당일 시가 대비 낙폭 임계 X를 -0.3%/-0.5%/-1.0%로 스윕해
그날 실제 진입 9건(신규 롱)에 반사실로 적용한 결과:

- **09:00:11 동시 3건(그날 손실의 절반 이상, 합계 -26,772원)은 어느 X로도
  막히지 않는다.** 이유가 구조적이다 — 진입 시각(09:00:11)에는 앵커의 당일
  첫 1분봉조차 아직 닫히지 않아(첫 완성봉은 09:01) `anchor_drawdown`이 아직
  **None**(데이터 없음)이다. 개장 직후 진입하는 전략(news_momentum/
  news_scalp의 entry_window_seconds 기본 120초)에는 이 게이트가 사실상
  무력하다 — 낙폭이 보이기도 전에 이미 진입이 끝난다.
- 09:19/09:58/10:01/10:28 진입(042700·096770) 시점엔 앵커가 오히려 시가
  대비 **+0.27~+0.60% 위**였다(그날 09~10시대는 반등 구간이었다) — X를 아무리
  타이트하게 잡아도 이 구간 진입은 막을 수 없다. 이 구간 손실 2건(-3,239원/
  -2,297원)도 승자 1건(+761원)도 게이트와 무관하다.
- 13:30:05(096770, 그날 최대 손실 -9,497원/-2.37%)만 세 X 모두에서 막힌다
  (그 시각 앵커는 -2.83%였다). US SQQQ 21:32(프리마켓, -0.06원 사실상
  무손익)는 X=-0.3%/-0.5%에서 막히고 X=-1.0%에서는 통과한다(앵커 -0.63%).

**결론: 이 표본(1일)에서 이 게이트는 그날 손실의 대부분(09:00 동시 3건)을
구조적으로 못 본다 — 타이밍 문제이지 임계값 문제가 아니다.** 늦게 진입하는
전략(intraday_scan)에는 유의미하게 걸렸지만, 개장 직후 진입 전략 두 개에는
사실상 개입하지 않는다. "일관되게 손실을 크게 줄인다"는 근거가 아니라
"혼재/부분적"이다 — 그래서 기본 모드는 **shadow**다(아래 "모드" 절).
다중검정 편향 경고: 위 X 셋 중 하나가 이 표본에서 다른 것보다 나아 보여도
"최적값"이 아니다 — 표본 1일에 맞춘 값이다.

## 사용법 — 순수 함수 둘

- `anchor_drawdown(bars)`: 당일 시가 대비 최근 완성봉 종가의 등락률(%,
  음수=하락). `bars`의 마지막 봉 날짜를 "오늘"로 삼아 그날 봉만 걸러 계산한다
  (호출부가 market/tz를 안 넘겨도 되게 하는 설계 — bars 자체가 이미 해당
  시장의 tz로 인덱싱돼 있다는 전제, `ctx.data.history`가 항상 그렇게 준다).
  당일 봉이 하나도 없으면(세션 첫 분봉도 안 닫힘, 데이터 공급 장애 등) None.
- `market_risk_off(bars, *, max_drawdown_pct)`: 앵커가 `max_drawdown_pct`
  (양수, %)보다 더 빠졌으면 True. `anchor_drawdown`이 None이면 **False**
  (게이트 부재 = 기존 동작 — trend_gate.py와 동일 원칙: 데이터가 없다고
  진입을 막으면, 데이터 공급 장애가 조용히 전략을 무력화시킨다).

`ANCHOR_SYMBOLS`(KR→069500, US→QQQ)는 이미 유니버스/백필에 있는 심볼이라
이 게이트를 위한 추가 수집 비용이 0이다.

## 모드 — scalp_1m의 `trend_gate_mode`와 동일 관례(off/shadow/block)

값 자체는 각 전략(news_momentum.py/intraday_scan.py/news_scalp.py)의
생성자 파라미터(`market_risk_gate_mode`)로 받는다 — 이 모듈은 계산만 하고
모드 분기는 호출부(전략) 책임이다(trend_gate.py와 동일 분리: 순수 함수 vs
배선). 기본값은 **shadow**(판정만 계산해 신호 사유에 `[시장:리스크오프 ...]`
로 표기하고 진입은 막지 않음) — 위 측정 근거. block으로 바꾸면 임계 초과 시
신규 롱 진입 자체를 거부한다. off는 앵커 조회 자체를 하지 않는다(계산 비용 0).

`max_drawdown_pct` 기본값 0.5(%)는 스윕에서 테스트한 세 값(-0.3/-0.5/-1.0)의
중간값일 뿐 — burn-in 전, 이 저장소 실데이터로 "이 값이 최적"이라고 검증된
것이 아니다.
"""
from __future__ import annotations

import pandas as pd

# 시장별 앵커 심볼 — KR: KODEX200(코스피200 추종 ETF), US: QQQ(나스닥100 추종
# ETF). 둘 다 이미 유니버스/백필 경로에 있는 종목이라 이 게이트 전용의 추가
# 데이터 수집이 필요 없다(모듈 docstring 참고).
ANCHOR_SYMBOLS: dict[str, str] = {"KR": "069500", "US": "QQQ"}


def anchor_drawdown(bars: pd.DataFrame) -> float | None:
    """앵커의 당일 시가 대비 최근 완성봉 종가 등락률(%). 음수=하락.

    `bars`의 마지막 봉이 속한 날짜를 "오늘"로 삼는다(호출부가 market/tz를
    별도로 넘길 필요가 없도록 — bars 인덱스는 `ctx.data.history`가 이미 해당
    시장 기준으로 준 것이라는 전제). 그날 봉이 하나도 없으면(당일 첫 완성봉도
    아직 없는 개장 직후 등) None — 모듈 docstring "측정 결과" 절이 이 케이스가
    실제로 얼마나 자주 발생하는지(개장 직후 진입 전략에는 사실상 항상)를
    실측으로 보여준다.
    """
    if bars is None or bars.empty:
        return None
    last_date = bars.index[-1].date()
    today_bars = bars[bars.index.date == last_date]
    if today_bars.empty:
        return None
    today_open = float(today_bars["open"].iloc[0])
    if today_open <= 0:
        return None
    cur = float(today_bars["close"].iloc[-1])
    return (cur / today_open - 1) * 100


def market_risk_off(bars: pd.DataFrame, *, max_drawdown_pct: float) -> bool:
    """앵커가 당일 시가 대비 `max_drawdown_pct`(%, 양수로 전달)보다 더 빠졌으면
    True. 데이터 없음/계산 불가(`anchor_drawdown`이 None)면 **False** — 게이트
    부재 = 기존 동작(모듈 docstring 참고, trend_gate.py와 동일 원칙)."""
    dd = anchor_drawdown(bars)
    if dd is None:
        return False
    return dd <= -abs(max_drawdown_pct)
