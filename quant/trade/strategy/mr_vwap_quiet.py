"""저거래량 VWAP 평균회귀 스캘핑 — **조용한 종목**의 일시적 하방 이탈을 산다.

기존 스캘프(`scalp_1m`, `orb_scan`, `intraday_scan`)가 전부 "돌파를 쫓는" 설계인
것과 정반대 방향의 전략이다. 같은 관심종목 유니버스에서 병행 운용하고 원장
스코어보드가 승부를 가린다.

## 왜 이 방향인가 — 우리 실측 두 겹이 지지한다 (2026-08-28 소유자 지시)

1. **고거래량 진입이 나쁘다(이 저장소 실측)**: D+1 스피어만 **-0.46**, 장중
   고RVOL **-79.8bp** vs 저RVOL **-31.3bp**. "거래량 급증 종목을 쫓는" 설계는
   우리 데이터에서 부호가 반대다. 이 전략은 정반대로 조용한 종목만 노린다
   (`rvol_max` 필터가 그 직접 구현이다).
2. **문헌이 같은 이야기를 한다**: 단기 반전은 과잉반응이 아니라 **일시적 유동성
   불균형**에서 온다(1시간 미만 스케일). 고RVOL=정보=지속, 저RVOL=유동성=반전.
3. **구조적으로 고점 추격이 불가능하다**: "밴드 **밖으로 나갔다가 밴드 안으로
   복귀한 종가**"를 진입 조건으로 요구하므로, `scalp_1m` 의 실패 모드(파동 고점
   매수)가 정의상 재발할 수 없다 — 진입은 항상 VWAP **아래**에서만 일어난다.

## 정직한 경고 — 이 전략이 먼저 죽을 곳

같은 문헌군이 반복해서 말하는 것: **호가스프레드로 비용을 조정하면 단기 반전의
통계적 유의성이 급감한다.** 즉 이 전략의 엣지는 스프레드에 가장 민감한 종류다.

이 저장소의 비용 실측: US 왕복 **20bp**, KR 개별주 왕복 **30bp**(매도세 20bp
포함). 1분 절대변동 중앙 8.3bp. 그래서:

- **1분봉 청산은 구조적으로 진다** — 그래서 주력 봉을 **5분봉**으로 잡고
  목표를 60~150bp, 보유를 20~75분 규모로 뒀다(`_INTERVAL`, `target_min_bp`,
  `timeout_minutes` 기본값의 근거).
- **KR 개별주에서 먼저 죽을 가능성이 높다.** KR ETF·US 에서만 살아남을 수
  있다고 보는 것이 정직한 사전 기대다. 이건 예측이지 검증된 사실이 아니다 —
  원장 스코어보드가 시장별로 판정할 때까지 성과 주장을 하지 않는다.
- 백테스트 표본이 없다. Toss 5분봉은 1분봉을 리샘플한 것이고 1분봉은 4거래일
  롤링만 제공한다(`docs/data-availability.md`) — **paper 번인이 유일한 검증
  경로다.** 아래 파라미터 기본값은 어느 것도 이 저장소 데이터로 최적화된 값이
  아니다(과최적화가 없다는 뜻이기도 하고, 검증이 없다는 뜻이기도 하다).

## 밴드 기준선을 VWAP 으로 잡은 이유 (볼린저를 쓰지 않았다)

`quant/trade/indicators/__init__.py` 에 `bollinger()` 가 이미 있고 재구현 금지가
원칙이지만, 이 전략은 **볼린저를 쓰지 않는다**. 이유는 취향이 아니라 정합성이다:

- 이 전략의 **청산 목표는 중심선(평균)** 이다. 진입 트리거의 기준선과 청산
  목표의 기준선이 **같은 평균이어야** "밴드 하단에서 사서 평균까지 간다"는
  한 문장이 성립한다. 볼린저 하단(SMA 기반)으로 진입하고 VWAP 으로 청산하면
  두 개의 서로 다른 평균을 섞는 셈이라, 볼린저 하단이 VWAP **위**에 놓이는
  구간에서는 "진입하자마자 목표 미달" 이 구조적으로 발생한다.
- VWAP 은 **세션 앵커드**(매 세션 리셋)라 "오늘 이 종목의 평균 체결가"라는
  뜻이 명확하다. 이 전략의 논지("세션 안의 일시적 유동성 불균형")와 시간
  스케일이 정확히 일치한다. 볼린저의 롤링 SMA 는 세션 경계를 넘어 미끄러지므로
  전날 가격이 오늘의 중심선을 끌고 온다.
- VWAP 은 **거래량 가중**이다. "조용한 종목의 평균가"를 말할 때 체결량 가중이
  단순 종가평균보다 그 종목의 실제 체결 무게중심에 가깝다.

σ 는 **세션 내 typical price 의 모집단 표준편차**(ddof=0, `bollinger()` 와 같은
관례)를 쓴다. 각 봉 시점까지의 **누적(expanding)** 값이라 look-ahead 가 없다.
정직한 한계: σ 는 typical price 자신의 산술평균 주위에서 재는데 밴드는 VWAP
주위에 그린다 — 거래량 분포가 크게 치우친 세션에서는 밴드가 약간 비대칭으로
어긋난다(거래량 가중 분산을 쓰면 없어지지만 손계산 검증이 어려워진다).

## 진입 (전부 5분봉 완성봉, 결정론)

전부 통과해야 한다. **하나라도 확인할 수 없으면 진입하지 않는다**(아래 "확인
불가" 절).

1. **밴드 밖 → 안 복귀**: 직전 `reentry_lookback`(기본 3)봉 중 **종가가 그
   시점 하단 밴드 아래**였던 봉이 있고, **마지막 완성봉 종가가 하단 밴드
   위**로 마감. 꼬리가 아니라 종가로 판정한다 — 봉이 닫히기 전에는 판단하지
   않는다(꼬리만 뚫고 종가는 안쪽인 경우가 흔하다).
2. **평균 아래에서만 산다**: 마지막 완성봉 종가 < VWAP. 이게 없으면 목표(VWAP)가
   진입가 아래에 놓여 "진입 즉시 청산"이 된다.
3. **비용 문턱**: (VWAP − 현재가)/현재가 ≥ `target_min_bp`(기본 60bp). 왕복
   20~30bp 비용 실측의 직접 구현 — 목표가 비용을 못 넘는 자리는 애초에 안 든다.
4. **저RVOL**: RVOL < `rvol_max`(기본 1.2). 우리 -0.46 실측의 직접 구현.
5. **비추세**: ADX(14) < `adx_max`(기본 20). 추세일에 평균회귀를 하면 밴드
   하단이 계속 새로 갱신된다 — 실무 소스들이 "선택이 아니라 생존 조건"이라
   표현하는 필터다.
6. **갭 제한**: |당일 시가/전일 종가 − 1| < `max_gap_pct`(기본 1.0%). 갭은
   보통 정보(뉴스)라 "정보 없는 유동성 이탈"이라는 논지와 반대다.
7. **시간대**: 개장+`entry_after_open_minutes`(기본 60) ~ 연속거래
   종료−`entry_before_close_minutes`(기본 60). 개장 직후는 VWAP 표본이 얇아
   밴드가 무의미하고, 마감 직전은 목표까지 갈 시간이 없다.
8. **연속 거래 구간**: `quant.core.session.in_continuous_session` — KR 연속매매는
   **15:20 에 끝난다**. 15:20~15:30 동시호가에는 현재가로 체결되지 않는다
   (2026-08-26 실사고: 실재할 수 없는 체결이 원장에 기록됐다).

세션당 심볼당 진입 `max_entries_per_session`(기본 **1**)회. 기본을 1 로 둔
이유는 이 저장소의 중심 사실이 "수수료가 엣지보다 크다"이기 때문이다 — 같은
종목에서 같은 날 반복 진입하는 것은 엣지보다 비용을 먼저 늘린다.

## 청산

1. **손절**: 신호봉 저가 × (1 − `stop_buffer_pct`/100). 진입가 이상이면 진입
   자체를 하지 않는다.
2. **목표**: **진입 시점의 VWAP**(랏에 고정 저장). 현재 VWAP 을 매 사이클
   다시 목표로 쓰지 않는 이유: 진입 게이트 3번이 검증한 것은 *그 시점의* VWAP
   까지의 거리가 비용을 넘는다는 사실이다. VWAP 이 아래로 흘러내리는 것을
   그대로 목표로 따라가면 그 비용 검증이 조용히 무효가 되고 수수료 미만
   이익으로 청산된다. 대신 VWAP 이 위로 가면 그만큼을 못 먹는다 — 의도된
   교환이다(상방보다 비용 방어가 먼저).
3. **타임아웃**: `timeout_minutes`(기본 75). 평균회귀가 그 안에 안 왔으면
   논지가 틀린 것이다.
4. **EoD 청산 + 오버나잇 금지**: 아래 `_should_flatten` 참고 — 이 전략은
   `Clock.minutes_to_close`(정규장 마감 기준)만 믿지 않고 **연속 거래 종료**
   (KR 15:20)를 기준으로도 청산한다.

위 방어선(진입가·손절·목표·진입시각)은 **인스턴스 상태가 아니라 브로커 포지션의
lot 에 산다** — 장중에 엔진을 재시작해도 손절이 사라지지 않는다. 근거와 경로는
`MrVwapQuietStrategy` 클래스 docstring "상태가 두 갈래로 흐른다" 절에 있다.

## "확인 불가"는 통과가 아니라 거부다

`quant/trade/indicators/trend_gate.py` 는 "게이트 부재 = 통과"를 원칙으로 둔다
(일봉 조회 장애가 조용히 전략을 무력화하지 않도록). **이 전략은 의도적으로
반대로 간다**: ADX·RVOL·갭·VWAP 중 하나라도 계산할 수 없으면 진입하지 않는다.
근거는 두 원칙이 다른 상황을 다루기 때문이다 —

- trend_gate 의 게이트는 이미 성립한 진입 논지에 얹는 **선택적 방어**다.
  데이터가 없다고 원래 되던 진입을 막으면 장애가 전략을 죽인다.
- 여기서 ADX/RVOL/갭은 진입 논지 **자체의 전제**다("추세가 아니고, 조용하고,
  뉴스가 없는 종목의 이탈"). 전제를 확인 못 한 채 진입하는 것은 다른 전략을
  실행하는 것이지 이 전략을 실행하는 게 아니다.

이 선택의 대가는 정직하게 적어 둔다: 데이터 공급이 끊기면 이 전략은 **조용히
0 건**이 된다. 그게 손실보다 낫다고 판단한 것이다(`structure.py` 의 "손절선을
정할 수 없는 자리는 그 자체가 위험 정보다"와 같은 계열의 판단).

## 데이터 — 5분봉 획득 경로 (추측 아님, 코드 추적)

`ctx.data.history(symbol, "5m", n)` 경로를 코드로 확인했다:

1. `MarketDataService.history`(`quant/adapters/data/service.py:222`) — 봉 경계
   캐시(`_bar_boundary`)를 태운 뒤 `_finalize_bars` → `_filter_completed_bars`
   로 **미완성봉을 잘라낸다**(`interval="5m"` → 5분). 이 전략이 "완성봉만 본다"
   고 말할 수 있는 근거가 여기다.
2. `TossDataFeed.history`(`quant/adapters/brokers/toss/datafeed.py:200`) —
   `interval` 이 "1d"/"1m" 이 아니면 `resample_1m(bars_1m, _interval_minutes(interval))`
   로 1분봉을 리샘플한다. `_interval_minutes("5m") == 5`(같은 파일 50행).
3. **이미 라이브에서 쓰이는 경로다** — `orb_scan` 이 `bar_interval_minutes`
   기본 5 로 `self.interval = "5m"` 을 만들어(`orb_scan.py:71`)
   `ctx.data.history(symbol, self.interval, ...)`(238행)로 조회한다. 새 배선이
   필요 없다.

일봉(`"1d"`)은 갭 계산 전용이다. 장중에는 `_filter_completed_bars` 가 오늘
일봉(마감이 미래)을 잘라내므로 마지막 행이 **전일 종가**가 된다
(`_interval_minutes("1d") == 24*60`, `service.py:108`).

## 비목표

- 부분 익절 없음. 목표가 하나(VWAP)뿐인 전략에서 절반만 먹는 것은 왕복 비용을
  한 번 더 무는 것과 같다(원장 실측: `scalp_1m` 절반 익절은 평균 -67.5bp 로
  전량 익절보다 **나빴다**).
- 트레일링 스탑 없음. 목표가 고정된 평균회귀에는 고수위 추적이 의미가 없다.
- 숏 없음(롱 온리) — 유니버스 전체가 롱 온리로 배선돼 있다.
"""
from __future__ import annotations

from datetime import date as dtdate, datetime
from typing import Any, Mapping

import pandas as pd

from quant.core.models import Signal, SignalAction, market_of_symbol
from quant.core.session import continuous_window, in_continuous_session, market_tz
from quant.core.strategy_api import DataNeeds, Decision, StrategySnapshot
from quant.trade.fmt import fmt_price
from quant.trade.indicators.trend_gate import adx_di
from quant.trade.strategy.shell import PureStrategyShell

# 주력 봉. 모듈 docstring "정직한 경고"/"데이터" 절 — 1분봉 청산은 왕복 20~30bp
# 앞에서 구조적으로 진다.
_INTERVAL = "5m"

# 갭 계산용 일봉 조회 개수. 전일 종가 하나만 쓰지만 휴장일/결손을 감안해 여유를 둔다.
_DAILY_COUNT = 5

# 정규장 길이(분) — KR 09:00~15:30, US 09:30~16:00 둘 다 390분. 5분봉 lookback
# 산정에 쓴다(세션 전체 = 78봉).
_FULL_SESSION_MINUTES = 390


# ---------------------------------------------------------------- 순수 지표

def typical_price(bars: pd.DataFrame) -> pd.Series:
    """(고가 + 저가 + 종가) / 3 — VWAP 의 표준 가격 대표값."""
    return (bars["high"].astype(float) + bars["low"].astype(float)
            + bars["close"].astype(float)) / 3.0


def session_vwap_bands(
    session_bars: pd.DataFrame, band_k: float
) -> tuple[pd.Series, pd.Series, pd.Series] | None:
    """세션 시작부터의 **누적** VWAP 과 ±`band_k`σ 밴드. `(vwap, lower, upper)`.

    각 시점 i 의 값은 세션 첫 봉~i 봉만 쓴다(expanding) — **look-ahead 가 없다.**
    그래서 "그 봉이 마감된 시점에 밴드가 어디였나"를 과거 봉에 대해서도 정확히
    되물을 수 있고, 진입 조건("직전 봉 종가가 *그때의* 하단 밖이었나")이
    사후확증 없이 성립한다.

    VWAP = Σ(typical × volume) / Σvolume (누적).
    σ = typical price 의 모집단 표준편차(ddof=0, `bollinger()` 와 같은 관례).
    첫 봉은 σ 를 정의할 수 없으므로(관측 1개) NaN 이다 — 밴드 없음 = 판단 불가.

    누적 거래량이 0 인 구간(거래 정지 등)은 VWAP 이 NaN 이다 — 0 으로 채우거나
    직전 값을 끌어오지 않는다(값을 지어내지 않는다).
    반환 None = 봉이 아예 없다.
    """
    if session_bars is None or session_bars.empty:
        return None
    tp = typical_price(session_bars)
    vol = session_bars["volume"].astype(float)
    cum_vol = vol.cumsum()
    vwap = (tp * vol).cumsum() / cum_vol.where(cum_vol > 0)
    sigma = tp.expanding(min_periods=2).std(ddof=0)
    return vwap, vwap - band_k * sigma, vwap + band_k * sigma


def relative_volume(bars: pd.DataFrame, lookback: int) -> float | None:
    """마지막 완성봉 거래량 / 직전 `lookback`봉 평균 거래량. 부족하면 None.

    **분자 봉 자신을 분모에서 뺀다** — 포함하면 서지가 자기 평균을 끌어올려
    RVOL 이 체계적으로 1 쪽으로 눌린다(저RVOL 필터가 무뎌진다).

    봉 단위 RVOL 이다. 우리 -0.46 실측은 **일 단위**(D-1 거래량 / D-2~D-21 20일
    평균, `quant/backtest/report_replay.py`)였으므로 **같은 양이 아니다** —
    같은 방향의 번역이다. 일 단위를 쓰지 않는 이유는 데이터다: Toss 1분봉은
    4거래일 롤링만 제공해(모듈 docstring "정직한 경고" 절) 세션 내 시각대별
    baseline 을 20일치 쌓을 수 없고, 일봉 기반 세션누적 RVOL 은 장중 경과
    비율에 따라 값이 0→1 로 흘러 `rvol_max` 같은 고정 문턱을 쓸 수 없다.
    """
    if bars is None or lookback <= 0 or len(bars) < lookback + 1:
        return None
    vol = bars["volume"].astype(float)
    base = float(vol.iloc[-(lookback + 1):-1].mean())
    if not (base > 0):
        return None
    last = float(vol.iloc[-1])
    if pd.isna(last):
        return None
    return last / base


def gap_pct(daily_bars: pd.DataFrame | None, session_open_price: float) -> float | None:
    """|당일 시가 / 전일 종가 − 1| × 100. 계산 불가면 None.

    전일 종가 = 마지막 완성 일봉의 종가. 장중에 오늘 일봉이 섞이지 않는 근거는
    모듈 docstring "데이터" 절(`_filter_completed_bars`)에 코드로 적어 뒀다.
    """
    if daily_bars is None or daily_bars.empty or session_open_price <= 0:
        return None
    prev_close = float(daily_bars["close"].iloc[-1])
    if not (prev_close > 0) or pd.isna(prev_close):
        return None
    return abs(session_open_price / prev_close - 1.0) * 100.0


# ---------------------------------------------------------------- 전략

class MrVwapQuietStrategy:
    """저거래량 VWAP 평균회귀 — `PureStrategy`(quant.core.strategy_api) 구현.

    **"Pure" 접미사가 없는 이유**: 신규 전략이라 이전할 레거시 쌍둥이가 없다.
    `Scalp1mPureStrategy`/`CloseBetPureStrategy` 는 기존 `on_cycle(ctx)` 구현과의
    동치를 증명해야 해서 이름으로 둘을 구분했지만, 여기는 순수 구현이 유일한
    구현이다. 껍질(`MrVwapQuietShell`)만 `Strategy` Protocol 을 만족한다.

    ## 상태가 두 갈래로 흐른다 — 장중 재시작 생존의 핵심

    | # | 값 | 어디로 | 왜 |
    |---|---|---|---|
    | 1 | `session_date` = `{market: "YYYY-MM-DD"}` | `next_state` | 세션 롤 감지. 하루 안에서만 산다 |
    | 2 | `entries_today` = `{symbol: int}` | `next_state` | 세션당 진입 횟수. 날짜가 바뀌면 어차피 리셋된다 |
    | 3 | `entry`/`stop`/`target`/`entered_at`/`session` | **`Signal.state_update` → lot → `snap.lots`** | **포지션이 살아 있는 한 필요하다.** `next_state` 에 두면 재시작 때 잃는다 |

    3번을 `next_state` 에 두지 않는 이유는 **2026-08-28 실제 사건**이다: 소유자가
    포지션 8개를 보유한 채 **장중에 엔진을 재시작했다**. 방어선을 인스턴스 상태에
    두면 그 순간 열린 랏의 손절·목표·진입시각이 통째로 증발하고, 포지션은 브로커에
    남았는데 **아무도 손절을 보지 않는** 상태가 된다. 모의라도 성적을 왜곡하고
    실거래면 그대로 손실 경로다.

    `donchian_pure`/`scalp_1m_pure` 가 "재시작 복구는 범위 밖"이라고 쓸 수 있었던
    근거는 "당일 안에 닫힌다"였는데, 그건 **프로세스가 하루를 넘긴다는 가정**에
    기댄 말이지 장중 재시작을 막아주지 않는다. `CloseBetPureStrategy` 가 밤을
    넘겨야 해서 같은 문제에 먼저 부딪혔고 이미 답을 만들어 뒀다 — 이 전략은 그
    경로를 그대로 쓴다:

    > 진입 `Signal.state_update` → `loop._execute_signal` 이 **체결을 확인한
    > 뒤에만** `Position.meta["lots"][id]` 에 적용(`quant/trade/loop.py:412-422`)
    > → 포지션은 브로커/상태파일에 영속 → 다음 사이클 껍질이
    > `snap.lots[symbol]` 로 돌려준다(`shell.py` `_snapshot`).

    부수 효과로 Phase A 의 공통 한계("`next_state` 는 체결 여부와 무관하게
    적용된다")를 **방어선에 한해서는** 넘어선다 — risk 거부/미체결이면 lot 자체가
    생기지 않으므로 유령 방어선이 남지 않는다. 그래서 `open`/`pending` 같은
    인스턴스측 보유 장부를 아예 두지 않았다: **보유의 진실은 `snap.lots` 하나뿐**
    이고, 진실이 한 벌이면 재시작이 그것을 갈라놓을 수 없다.

    `decide()` 는 받은 `state` 의 **사본만** 고쳐 반환한다 — 입력 매핑도, 그
    안의 중첩 dict 도 in-place 로 건드리지 않는다(`test_decide_does_not_mutate_state`
    가 고정한다).

    ## 아직 못 하는 것 (정직하게)

    1. `next_state`(표 1·2번)는 **체결 확인과 무관하게** 매 사이클 적용된다
       (Phase A 공통 한계, `shell.py` docstring). risk 가 거부하거나 주문이
       미체결이어도 `entries_today` 는 이미 올라가 있다 — 그 세션의 그 심볼은
       그대로 소진된다. **방어선(표 3번)은 이 한계 밖이다**(위 절).
    2. **`pos.avg_cost` 폴백이 없다.** `entry` 가 없는 lot(= `state_update` 가
       아직/영영 적용되지 않은 랏)은 관리하지 않고 건너뛴다 —
       `StrategySnapshot.lots` 는 lot 필드만 주고 심볼 합산 평단을 주지 않기
       때문이다(`shell.py`). `CloseBetPureStrategy` 와 같은 한계이고 같은 이유로
       **정상 경로에서는 발생하지 않는다**(진입 신호가 항상 `entry` 를 싣고,
       루프가 체결 직후 같은 호출 안에서 lot 에 쓴다).
    3. **고아 포지션을 볼 수 없다.** `DataNeeds` 가 정적으로 `self.symbols` 만
       선언하므로, 유니버스에서 빠진 뒤 남은 보유분은 이 전략이 관리하지
       못한다(관심종목 기반 전략 공통 문제).
    4. **조회 최적화가 없다.** 껍질이 매 사이클 전 심볼의 5분봉+일봉을 다시
       요청한다. `MarketDataService` 의 봉 경계 캐시가 실제 API 호출은 봉당
       1회로 눌러 주지만(`service.py` docstring), 전략 레벨의 세션 게이트는
       없다. 신호 정확성에는 영향이 없다(완성봉만 오므로).
    5. **연속 거래 구간 판정이 캘린더를 모른다.** `in_continuous_session` 은
       주말만 거른다 — 공휴일·조기폐장은 모른다. 그런 날은 세션 봉이 비어
       있어(진입 조건 미충족) 안전하게 아무 일도 하지 않지만, **조기폐장일의
       EoD 청산은 `snap.minutes_to_close`(캘린더 기반) 쪽 경로에만 의존한다.**
    6. **KR 개별주에서 살아남을 근거가 없다.** 모듈 docstring "정직한 경고" 절.
    """

    def __init__(self, symbols: list[str], params: dict,
                 market: str = "US", id: str = "mr_vwap_quiet"):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Strategy Protocol 호환용 — 실제 판정은 심볼별 시장 추론

        # 밴드 — VWAP ± band_k × σ (모듈 docstring "밴드 기준선" 절).
        self.band_k: float = float(params.get("band_k", 2.0))
        # 밴드 밖 이탈을 몇 봉 전까지 인정할 것인가. 3봉 = 15분 — "일시적"
        # 유동성 불균형이라는 논지의 시간 스케일.
        self.reentry_lookback: int = int(params.get("reentry_lookback", 3))
        # 저RVOL 필터 — 우리 -0.46 실측의 직접 구현(relative_volume() docstring
        # 에 일 단위 원측정과의 차이를 적어 뒀다).
        self.rvol_max: float = float(params.get("rvol_max", 1.2))
        self.rvol_lookback: int = int(params.get("rvol_lookback", 20))
        # 추세일 배제. ADX 는 **5분봉**(거래 시간축)에서 잰다 — 일봉 ADX 는
        # "몇 주 단위로 추세인가"를 답하지 "오늘 장중이 추세인가"를 답하지
        # 않는다. 이 전략이 피해야 하는 것은 후자다.
        self.adx_max: float = float(params.get("adx_max", 20.0))
        self.adx_period: int = int(params.get("adx_period", 14))
        # 갭 = 정보(뉴스). "정보 없는 유동성 이탈"이라는 논지와 반대다.
        self.max_gap_pct: float = float(params.get("max_gap_pct", 1.0))
        # 진입 시간대 — 개장 직후는 VWAP 표본이 얇고, 마감 직전은 목표까지 갈
        # 시간이 없다. 기준선은 **연속 거래 구간**(KR 09:00~15:20)이다.
        self.entry_after_open_minutes: float = float(params.get("entry_after_open_minutes", 60))
        self.entry_before_close_minutes: float = float(params.get("entry_before_close_minutes", 60))
        # 목표(VWAP)가 왕복 비용을 넘지 못하는 자리는 들지 않는다. 실측 왕복
        # US 20bp / KR 개별주 30bp — 60bp 는 그 2~3배 여유다. 0 = 비활성.
        self.target_min_bp: float = float(params.get("target_min_bp", 60.0))
        # 손절 = 신호봉 저가 × (1 - buffer/100).
        self.stop_buffer_pct: float = float(params.get("stop_buffer_pct", 0.3))
        # 평균회귀가 이 시간 안에 안 왔으면 논지가 틀린 것이다.
        self.timeout_minutes: float = float(params.get("timeout_minutes", 75))
        # EoD 청산 여유. 연속 거래 종료(KR 15:20 / US 16:00) 기준.
        self.flatten_minutes: float = float(params.get("flatten_before_close_minutes", 2))
        # 세션당 심볼당 진입 횟수. 기본 1 — 이 저장소의 중심 사실이 "수수료가
        # 엣지보다 크다"이므로 같은 날 같은 종목 반복 진입은 비용부터 늘린다.
        self.max_entries_per_session: int = int(params.get("max_entries_per_session", 1))
        # 전략 배정 자본 대비 비중. scalp_1m 과 같은 값 — 동시 보유 2 종목까지를
        # 상정한 보수적 기본값이다(하드레일은 risk 레이어가 따로 건다).
        self.target_weight: float = float(params.get("target_weight", 0.5))

        if self.band_k <= 0:
            raise ValueError("band_k는 양수여야 합니다.")
        if self.reentry_lookback < 1:
            raise ValueError("reentry_lookback은 1 이상이어야 합니다.")
        if self.rvol_max <= 0:
            raise ValueError("rvol_max는 양수여야 합니다.")
        if self.rvol_lookback < 1:
            raise ValueError("rvol_lookback은 1 이상이어야 합니다.")
        if self.adx_max <= 0:
            raise ValueError("adx_max는 양수여야 합니다.")
        if self.adx_period < 2:
            raise ValueError("adx_period는 2 이상이어야 합니다.")
        if self.max_gap_pct <= 0:
            raise ValueError("max_gap_pct는 양수여야 합니다.")
        if self.entry_after_open_minutes < 0:
            raise ValueError("entry_after_open_minutes는 0 이상이어야 합니다.")
        if self.entry_before_close_minutes < 0:
            raise ValueError("entry_before_close_minutes는 0 이상이어야 합니다.")
        if self.target_min_bp < 0:
            raise ValueError("target_min_bp는 0(비활성) 이상이어야 합니다.")
        if self.stop_buffer_pct < 0:
            raise ValueError("stop_buffer_pct는 0 이상이어야 합니다.")
        if self.timeout_minutes <= 0:
            raise ValueError("timeout_minutes는 양수여야 합니다.")
        if self.flatten_minutes < 0:
            raise ValueError("flatten_before_close_minutes는 0 이상이어야 합니다.")
        if self.max_entries_per_session < 1:
            raise ValueError("max_entries_per_session은 1 이상이어야 합니다.")
        if not 0 < self.target_weight <= 1:
            raise ValueError("target_weight는 0 초과 1 이하여야 합니다.")

        # 5분봉 조회 개수 — 세션 전체(78봉) + ADX 워밍업(2×period) + RVOL
        # baseline 을 모두 덮는다. ADX/RVOL 은 세션 경계를 넘는 연속 시계열에서
        # 계산하므로(개장 직후 워밍업 부족 회피 — scalp_1m 과 같은 이유) 세션
        # 봉 수만으로는 부족하다.
        self._lookback_bars = max(
            int(params.get("lookback_bars", 200)),
            _FULL_SESSION_MINUTES // 5 + 2 * self.adx_period + self.rvol_lookback + 1,
        )

    # ------------------------------------------------------------------ 계약

    def requirements(self) -> DataNeeds:
        """5분봉(판정 전부) + 일봉(갭 전용) + 현재가 + 포지션."""
        bars = tuple((s, _INTERVAL, self._lookback_bars) for s in self.symbols)
        bars += tuple((s, "1d", _DAILY_COUNT) for s in self.symbols)
        return DataNeeds(bars=bars, quotes=tuple(self.symbols), needs_positions=True)

    @staticmethod
    def _my_lot(snap: StrategySnapshot, symbol: str) -> Mapping[str, Any] | None:
        """내가 **방어선을 써 넣은** 열린 랏만 돌려준다.

        `snap.lots[symbol]` 이 빈 dict(`{}`)인 경우가 두 가지 있는데
        (`shell.py` `_snapshot`) 스냅샷만으로는 구분되지 않는다: (a) 다른 전략이
        그 심볼을 보유 중이라 내 lot 이 없다, (b) 내가 방금 체결됐는데 아직 lot
        필드가 없다. `entry` 유무로 판정하면 두 경우 모두 "내 관리 대상이 아니다"
        로 안전하게 떨어지고, (a) 에서 남의 포지션을 내 것으로 오인해 청산 주문을
        내는 사고가 구조적으로 불가능해진다.
        """
        lot = snap.lots.get(symbol)
        if not lot or lot.get("entry") is None:
            return None
        return lot

    def decide(self, snap: StrategySnapshot, state: Mapping[str, Any]) -> Decision:
        # 입력 state 는 절대 in-place 로 건드리지 않는다 — 중첩 dict 까지 복사한다.
        session_date: dict[str, str] = dict(state.get("session_date", {}))
        entries_today: dict[str, int] = dict(state.get("entries_today", {}))

        signals: list[Signal] = []
        markets = sorted({market_of_symbol(s) for s in self.symbols})

        # 0) 세션 롤 — 관리(1단계)보다 먼저 돈다. 나중에 돌면 그날 첫 사이클에
        #    막 채운 상태를 같은 사이클 안에서 지워버린다(scalp_1m 의 실패 사례).
        #    여기서 지우는 것은 **하루짜리 값뿐**이다 — 방어선은 lot 에 있으므로
        #    세션 롤이 건드리지 않고, 그래서 오버나잇 레일(아래 `_manage`)이
        #    다음 세션 첫 사이클에 진입 세션을 정확히 비교할 수 있다.
        for market in markets:
            if not snap.market_open.get(market, False):
                continue
            today_iso = snap.now.astimezone(market_tz(market)).date().isoformat()
            if today_iso == session_date.get(market):
                continue
            session_date[market] = today_iso
            for symbol in [s for s in entries_today if market_of_symbol(s) == market]:
                entries_today.pop(symbol, None)

        # 1) 보유 관리 — 진입보다 먼저. 보유의 진실은 `snap.lots` 하나뿐이라
        #    (클래스 docstring "상태가 두 갈래로" 절) 인스턴스 장부와 어긋날 수
        #    없고, 프로세스를 재시작해도 그대로 이어진다. 시장이 닫혀 있으면
        #    건너뛴다 — 가격이 안 움직이고 청산 주문도 낼 수 없다.
        for symbol in self.symbols:
            market = market_of_symbol(symbol)
            if not snap.market_open.get(market, False):
                continue
            lot = self._my_lot(snap, symbol)
            if lot is None:
                continue
            signal = self._manage(symbol, lot, market, snap)
            if signal is not None:
                signals.append(signal)

        # 2) 진입
        for market in markets:
            if not snap.market_open.get(market, False):
                continue
            if not self._in_entry_window(market, snap):
                continue
            today = snap.now.astimezone(market_tz(market)).date()
            for symbol in sorted(s for s in self.symbols if market_of_symbol(s) == market):
                if self._my_lot(snap, symbol) is not None:
                    continue  # 보유 중엔 신규 진입 평가 없음
                if entries_today.get(symbol, 0) >= self.max_entries_per_session:
                    continue
                signal = self._check_entry(symbol, market, snap, today, entries_today)
                if signal is not None:
                    signals.append(signal)

        return Decision(
            signals=tuple(signals),
            next_state={"session_date": session_date, "entries_today": entries_today},
        )

    # ------------------------------------------------------------------ 시간 게이트

    def _in_entry_window(self, market: str, snap: StrategySnapshot) -> bool:
        """개장+N ~ 연속거래종료−M, 그리고 연속 거래 구간 안.

        기준선이 정규장 마감(KR 15:30)이 아니라 **연속 거래 종료**(KR 15:20)인
        것이 핵심이다 — `quant.core.session` 이 그 구분을 갖고 있고, 15:20 이후
        '현재가'는 실재하는 체결가가 아니다(2026-08-26 실사고).
        """
        if not in_continuous_session(market, snap.now):
            return False
        open_t, end_t = continuous_window(market)
        tz = market_tz(market)
        now_local = snap.now.astimezone(tz)
        today = now_local.date()
        since_open = (now_local - datetime.combine(today, open_t, tzinfo=tz)).total_seconds() / 60
        to_end = (datetime.combine(today, end_t, tzinfo=tz) - now_local).total_seconds() / 60
        return since_open >= self.entry_after_open_minutes and to_end >= self.entry_before_close_minutes

    def _should_flatten(self, market: str, snap: StrategySnapshot) -> bool:
        """EoD 강제청산 시점인가 — 두 경로의 **논리합**이다.

        (a) **연속 거래 종료 기준**(주경로): KR 15:20 / US 16:00 까지 남은 시간이
            `flatten_minutes` 미만. `Clock.minutes_to_close` 는 정규장 마감
            (KR 15:30)까지를 세므로 그것만 쓰면 KR 청산 신호가 **동시호가 안에서**
            나간다 — 체결될 수 없는 주문이다.
        (b) **캘린더 기준**(조기폐장 방어): `snap.minutes_to_close` 는
            `SessionCalendar` 를 통해 조기폐장을 안다(`quant/core/session.py`).
            (a) 의 고정 시간표는 그걸 모른다.

        둘 다 `cadence_minutes` 를 빼서 판정한다 — 다음 사이클이 오기 전에 창이
        닫히면 이번 사이클에 나가야 한다(`Clock._should_flatten` 과 같은 공식).
        """
        mtc = snap.minutes_to_close.get(market)
        if mtc is not None and 0 < mtc and mtc - snap.cadence_minutes < self.flatten_minutes:
            return True
        tz = market_tz(market)
        now_local = snap.now.astimezone(tz)
        _, end_t = continuous_window(market)
        remaining = (
            datetime.combine(now_local.date(), end_t, tzinfo=tz) - now_local
        ).total_seconds() / 60
        return 0 < remaining and remaining - snap.cadence_minutes < self.flatten_minutes

    @staticmethod
    def _session_bars(bars: pd.DataFrame, market: str, today: dtdate) -> pd.DataFrame:
        """오늘 **연속 거래 개장 이후**의 봉만. VWAP 은 세션 앵커드라 이 분할이
        틀리면 중심선 자체가 틀린다(프리마켓 봉이 섞이면 세션 시가/평균이 오염된다).
        """
        tz = market_tz(market)
        open_t, _ = continuous_window(market)
        local = bars.index.tz_convert(tz)
        return bars[(local.date == today) & (local.time >= open_t)]

    # ------------------------------------------------------------------ 진입

    def _check_entry(
        self, symbol: str, market: str, snap: StrategySnapshot, today: dtdate,
        entries_today: dict[str, int],
    ) -> Signal | None:
        """모듈 docstring "진입" 절 1~6번. **하나라도 확인 불가면 None**
        ("확인 불가는 통과가 아니라 거부다" 절)."""
        bars = snap.bars.get((symbol, _INTERVAL))
        if bars is None or bars.empty:
            return None
        sess = self._session_bars(bars, market, today)
        if len(sess) < 2:
            return None

        # (1)(2) 밴드 밖 → 안 복귀 + 평균 아래
        bands = session_vwap_bands(sess, self.band_k)
        if bands is None:
            return None
        vwap_s, lower_s, _upper_s = bands
        vwap, lower = float(vwap_s.iloc[-1]), float(lower_s.iloc[-1])
        last_close = float(sess["close"].iloc[-1])
        if pd.isna(vwap) or pd.isna(lower) or pd.isna(last_close):
            return None
        if last_close < lower:
            return None  # 아직 밴드 밖에 머문다 — 복귀 마감이 아니다
        if last_close >= vwap:
            return None  # 평균 위 — 목표가 진입가 아래가 된다

        window = slice(-(1 + self.reentry_lookback), -1)
        breached = (sess["close"].iloc[window].astype(float) < lower_s.iloc[window])
        if not bool(breached.fillna(False).any()):
            return None  # 밴드 밖으로 나간 적이 없다 — 그냥 밴드 안 횡보다

        # (4) 저RVOL — 이 전략의 존재 이유
        rvol = relative_volume(bars, self.rvol_lookback)
        if rvol is None or rvol >= self.rvol_max:
            return None

        # (5) 비추세 — 5분봉 ADX
        di = adx_di(bars, self.adx_period)
        if di is None or di[0] >= self.adx_max:
            return None

        # (6) 갭 제한
        gap = gap_pct(snap.bars.get((symbol, "1d")), float(sess["open"].iloc[0]))
        if gap is None or gap >= self.max_gap_pct:
            return None

        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            return None
        entry = quote.price

        # (3) 비용 문턱 — 목표(VWAP)까지의 거리가 왕복 비용을 넘는가
        if entry >= vwap:
            return None
        if self.target_min_bp and (vwap / entry - 1.0) * 1e4 < self.target_min_bp:
            return None

        stop = float(sess["low"].iloc[-1]) * (1 - self.stop_buffer_pct / 100)
        if not (stop < entry):
            return None  # 손절선을 진입가 아래에 그을 수 없다 — 들지 않는다

        entries_today[symbol] = entries_today.get(symbol, 0) + 1
        return Signal(
            strategy_id=self.id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=self.target_weight,
            reason=(
                f"VWAP 평균회귀(저거래량) 진입: {symbol} w={self.target_weight:.2f} "
                f"현재={fmt_price(entry, symbol)} 하단={fmt_price(lower, symbol)} "
                f"목표(VWAP)={fmt_price(vwap, symbol)} 손절={fmt_price(stop, symbol)} "
                f"[RVOL={rvol:.2f} ADX={di[0]:.1f} 갭={gap:.2f}%]"
            ),
            stop=stop,
            target=vwap,
            # **방어선은 여기로 나간다** — `next_state` 가 아니다. 루프가 체결을
            # 확인한 뒤에만 `Position.meta["lots"][id]` 에 쓰고(loop.py:412-422),
            # 다음 사이클 껍질이 `snap.lots` 로 돌려준다. 장중 재시작이 손절을
            # 지우지 못하는 이유가 이 한 줄이다(클래스 docstring 표 3번).
            state_update={
                "entry": entry, "stop": stop, "target": vwap,
                "session": today.isoformat(), "entered_at": snap.now.isoformat(),
                "strategy": self.id,
            },
        )

    # ------------------------------------------------------------------ 관리

    def _manage(
        self, symbol: str, lot: Mapping[str, Any], market: str, snap: StrategySnapshot
    ) -> Signal | None:
        """`lot` 은 껍질이 `Position.meta["lots"][id]` 에서 순수 조회해 채운
        `snap.lots[symbol]` 이다(`shell.py`). **인스턴스 상태가 아니라 브로커
        포지션이 방어선의 출처**이므로 프로세스를 재시작해도 그대로 이어진다
        (클래스 docstring "상태가 두 갈래로" 절). 읽기만 한다 — 여기서 lot 을
        고쳐도 `Position.meta` 에 반영되지 않는다(껍질이 준 사본이다).

        판정 순서는 **보수적**이다: 오버나잇 → EoD → 손절 → 목표 → 타임아웃.
        손절과 목표가 같은 사이클에 함께 성립하면(5분 안에 양쪽을 다 지나간
        경우) 나쁜 쪽을 택한다 — 봉 안의 순서를 우리는 모른다.
        """
        quote = snap.quotes.get(symbol)
        if quote is None:
            return None
        price = quote.price
        tz = market_tz(market)
        entry = float(lot["entry"])   # _my_lot 이 None 아님을 이미 보장한다
        stop_raw, target_raw = lot.get("stop"), lot.get("target")
        if stop_raw is None or target_raw is None:
            return None  # 방어선이 반쪽인 랏 — 지어내지 않는다(docstring 못하는것 2번)
        stop, target = float(stop_raw), float(target_raw)

        def _exit(reason: str) -> Signal:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0, reason=reason,
            )

        entry_session = lot.get("session")
        if entry_session and entry_session != snap.now.astimezone(tz).date().isoformat():
            return _exit(
                f"세션 롤 강제청산(오버나잇 금지): 진입 {entry_session} "
                f"현재={fmt_price(price, symbol)}"
            )
        if self._should_flatten(market, snap):
            return _exit(
                f"EoD 청산: entry={fmt_price(entry, symbol)} 현재={fmt_price(price, symbol)}"
            )
        if price <= stop:
            return _exit(
                f"손절: entry={fmt_price(entry, symbol)} stop={fmt_price(stop, symbol)} "
                f"현재={fmt_price(price, symbol)}"
            )
        if price >= target:
            return _exit(
                f"목표(VWAP) 도달 청산: entry={fmt_price(entry, symbol)} "
                f"목표={fmt_price(target, symbol)} 현재={fmt_price(price, symbol)}"
            )
        entered_at = lot.get("entered_at")
        if entered_at:
            held = (snap.now - datetime.fromisoformat(entered_at)).total_seconds() / 60
            if held >= self.timeout_minutes:
                return _exit(
                    f"타임아웃 청산({self.timeout_minutes:g}분): "
                    f"entry={fmt_price(entry, symbol)} 목표={fmt_price(target, symbol)} "
                    f"현재={fmt_price(price, symbol)} 경과={held:.0f}분"
                )
        return None


class MrVwapQuietShell(PureStrategyShell):
    """`STRATEGY_REGISTRY`/`build_strategies` 가 기존 전략과 같은 방식으로
    (`cls(symbols=..., params=..., market=..., id=...)`) 생성할 수 있게 하는 얇은
    팩토리 — `DonchianPureShell`/`Scalp1mPureShell` 과 동일 패턴.

    **레지스트리 배선은 이 파일 밖이다**(`quant/trade/strategy/__init__.py` 의
    `STRATEGY_REGISTRY` + `config/settings.yaml` 의 `strategies:` 블록).
    """

    def __init__(self, symbols: list[str], params: dict,
                 market: str = "US", id: str = "mr_vwap_quiet"):
        super().__init__(MrVwapQuietStrategy(symbols, params, market=market, id=id))
