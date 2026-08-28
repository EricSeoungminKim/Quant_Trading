"""RSI(2) 눌림매수 (Connors RSI(2) Dip Buy) — 추세 위 단기 과매도 눌림을 산다.

Larry Connors의 RSI(2) 평균회귀는 지수 ETF에서 승률 75~79%로 광범위하게 문헌화된
단기 스윙 전략이다(`Short Term Trading Strategies That Work`, Connors & Alvarez).
핵심 논지는 두 가지의 결합이다 — **장기 추세는 위(추세 필터)**, **단기는 과매도
극단(RSI(2))**. 둘 다 성립하는 날만 산다. 이 저장소의 무대는 지수 ETF(비용이 가장
싼 KR ETF 4bp + US QQQ) — Connors 원전이 검증된 바로 그 자산군이다.

**오버나이트 보유형이다** — 며칠에 걸쳐 포지션을 들고 갈 수 있으므로
`overnight_drift.py`(이 저장소의 오버나이트 순수 전략 예시)의 "상태가 두 갈래로
흐른다" 패턴을 그대로 따른다: 세션을 넘겨야 하는 값(진입가·손절·진입일)은
`Signal.state_update` → `Position.meta["lots"]` → `snap.lots`로만 흐르고,
`next_state`에는 하루 안에서만 의미 있는 게이트만 둔다.

## 규칙

1. **진입**: 일봉 종가 기준 RSI(2) < `entry_rsi`(기본 10, 과매도) **그리고**
   종가 > SMA(`trend_sma_days`)(추세 필터). 둘 다 성립해야 세션 마감
   `entry_before_close_minutes`(기본 10)분 전 창에서 매수한다.
2. **청산** — 세 갈래, 우선순위는 아래 "판정 순서" 절.
   - (a) 일봉 RSI(2) > `exit_rsi`(기본 60)이 되면 다음 판단 시점에 청산.
   - (b) 보유 `max_hold_days`(기본 5 거래일) 초과 시 청산.
   - (c) 하드 레일: 진입가 대비 `-hard_stop_pct`(기본 5%) 이탈 시 **요일과 무관하게
     즉시** 청산. Connors 원전은 손절을 두지 않지만(평균회귀는 손절이 있으면
     "가장 좋은 진입점에서 바로 털린다"는 것이 원전의 반론이다), 실계좌에서
     방향이 틀렸을 때 무한정 물려 있을 수는 없다 — 레일은 원전의 통계적 우위를
     훼손하지 않을 만큼 넓게(5%) 잡고, 있는 이유를 여기 정직하게 적어 둔다.
3. **심볼당 동시 1포지션.** 재진입은 **청산 다음 날부터**(청산 당일 재진입 금지 —
   RSI(2)는 일봉 기준이라 청산 직후 같은 세션 안에서는 값이 사실상 그대로라
   "청산하자마자 다시 진입"하는 무의미한 왕복을 코드로 막는다).

## 판정 순서 (같은 사이클에 여러 조건이 겹칠 때)

`_manage`는 **하드 레일을 항상 가장 먼저** 본다 — 요일 무관, 진입 당일이라도
확인한다(자본 보호가 우선). 하드 레일을 통과하면 **진입 당일에는 그 밖의 어떤
청산도 하지 않는다**(`overnight_drift`/`mr_vwap_quiet`와 같은 원칙 — "밤을 넘기는
것이 이 전략이다"). 그다음 날부터 보유기간 초과 → RSI 청산 순으로 본다.

## 일봉 조회 한도 — 실측 확인 (지어내지 않았다)

RSI(2)는 일봉 최소 5개, 추세 필터는 `trend_sma_days`개의 일봉이 필요하다. 이
저장소에서 일봉이 어디서 오는지, 몇 개까지 조회 가능한지를 코드와 기존 실측
문서로 확인했다(이 작업 세션에는 실계좌 네트워크 접근이 없어 새로 라이브 호출을
하지는 않았다 — 아래는 코드 추적 + 기존 실측 문서 인용이다):

1. **KR/US 모두 일봉(`"1d"`) 소스는 Toss뿐이다.** `KiwoomUSDataFeed.history()`는
   `interval == "1d"`에 명시적으로 `DataSourceError`를 던진다("usa06010은 분봉
   전용")(`quant/adapters/brokers/kiwoom/us_datafeed.py`). `KiwoomRealtimeSource`는
   `history()` 자체를 지원하지 않는다(`quant/adapters/brokers/kiwoom/datafeed.py`).
   `quant/apps/assembly.py`는 그래서 kiwoom 라우트들에 `Capability.BARS`를 등록하지
   않는다 — 일봉 요청은 시장과 무관하게 전부 Toss로 간다.
2. **Toss 일봉 조회에는 200개짜리 하드 캡이 없다.** `TossClient.candles()`
   (`quant/adapters/brokers/toss/client.py:268`)는 API의 "200개/요청" 한도를
   `before`/`nextBefore` 커서로 자동 페이징해 넘는다 — docstring 그대로: "paging
   past the API's 200-per-call cap". `MarketDataService._history_raw`
   (`quant/adapters/data/service.py:287`)도 `n+1`을 그대로 요청할 뿐 자체 상한을
   두지 않고, `TossDataFeed._load_1d`(`quant/adapters/brokers/toss/datafeed.py:191`)
   역시 `max(n, 90)`을 그대로 클라이언트에 넘긴다.
3. **실측 근거**: `docs/data-availability.md`(2026-07-28, 실계좌 자격증명으로
   실행한 실제 API 호출 기록)는 TQQQ/SQQQ 일봉을 **21회 페이징, 4,138봉,
   2010-02-11(두 ETF의 실제 상장일)까지** 받아온 결과를 기록한다 — "더 이상
   페이지가 없다"는 API 자연 종료였지 예산 소진이 아니었다. 즉 실측된 깊이가
   4천 봉대이고, 이 코드 경로(`candles()`)에 그 이후 어떤 상한도 추가되지 않았다.

**결론**: `trend_sma_days` 기본값을 200 밑으로 낮출 이유가 없다 — Connors 원전의
표준값 그대로 쓴다. 200이 임계치에 가까웠다면(예: 실측 한도가 50~100 사이였다면)
기본값을 낮추고 그 실측치를 여기 적었을 것이다 — 그런 상황이 아니었다.

## 상태가 두 갈래로 흐른다

| # | 값 | 어디로 | 왜 |
|---|---|---|---|
| 1 | `entered_date` = `{symbol: "YYYY-MM-DD"}`(당일 진입 여부, "진입 창 안에서 이미 샀다") | `next_state` | 하루 안에서만 의미 있는 "1일 1회" 게이트 — `overnight_drift`와 동일 |
| 2 | `last_exit_date` = `{symbol: "YYYY-MM-DD"}`(마지막 청산일, "청산 당일 재진입 금지"용) | `next_state` | 역시 하루 단위 게이트. 프로세스 재시작으로 소실돼도 최악의 결과가 "청산 당일 재진입 금지가 한 번 안 걸릴 수 있다"뿐이라 `next_state`로 충분하다 — 밤을 넘겨 지켜야 하는 방어선이 아니다 |
| 3 | `entry`/`stop`/`entered_date`(포지션의 진입일)/`entered_at` | **`Signal.state_update` → `Position.meta["lots"]` → `snap.lots`** | **밤을, 그리고 최대 며칠을 넘겨야 한다.** `next_state`에 두면 인스턴스 필드로만 존재해 재시작에 증발한다(`shell.py` docstring, `overnight_drift.py` "상태가 두 갈래로 흐른다" 절과 동일 근거) |

2번의 키 이름이 3번의 `entered_date`(포지션 안의 진입일)와 겹쳐 보이지만 서로 다른
맥락의 값이다 — 2번은 "그 심볼을 마지막으로 언제 팔았나"(하루짜리 게이트), 3번은
"지금 들고 있는 포지션을 언제 샀나"(보유기간 계산의 기준, 랏에 영속).

## RSI(2) 계산 — Wilder 평활, 파일 안에 순수 구현

`quant/trade/indicators/__init__.py`에 이미 Wilder RSI 구현(`rsi()`, 기본 14)이
있지만, 이 전략의 신호 정확성이 실거래 손익에 직결되므로 이 파일 안에서 계산이
완결적으로 검증 가능해야 한다는 요구(작업 스펙)에 따라 `_wilder_rsi()`를
독립적으로 구현했다 — `indicators.rsi()`와 알고리즘은 동일하고(재구현이지 새
공식이 아니다), `period=2`로 이 전략에서만 쓴다. `tests/test_rsi2_dip.py`가 손으로
계산한 수열로 정확성을 고정한다.

## 세션 마감 근접 판단 — "오늘 종가"를 실시간 가격으로 근사한다

RSI(2)/SMA는 **완성된 일봉 종가**로 정의되지만, 이 전략은 "오늘 마감 N분 전에
사라"는 규칙이라 오늘 일봉은 아직 완성되지 않았다(장중에는 `_filter_completed_bars`
가 오늘 봉을 잘라낸다 — `overnight_drift.py`/`mr_vwap_quiet.py`와 같은 전제). 그래서
마감 근접 시점의 **현재가를 오늘 종가의 근사치**로 취급해 과거 완성 일봉 뒤에
이어 붙인 뒤(`extended` 시리즈) 그 위에서 RSI/SMA를 계산한다. `close_bet.py`/
`overnight_drift.py`가 "마감 직전 현재가 ≈ 그날 종가"로 체결을 모델링하는 것과
같은 전제다. 청산 판정(RSI > exit_rsi)도 같은 근사를 쓴다 — "다음 판단 시점"마다
그 시점의 현재가를 오늘 종가 근사로 매겨 재계산한다.

## 아직 못 하는 것 (정직하게)

1. **우리 원장 실측이 없다.** 위 승률 숫자는 Connors 원전 인용이다. 백테스트도
   아직 돌리지 않았다 — validation.status: burn_in.
2. **고아 포지션을 볼 수 없다.** `DataNeeds`는 정적으로 `self.symbols`만
   선언한다 — 밤새 유니버스가 갈려 보유 심볼이 빠지면 관리 주체가 사라진다
   (`overnight_drift`/`mr_vwap_quiet`와 동일한 관심종목 기반 전략 공통 한계).
3. **보유기간은 "완성된 일봉 수"로 센다.** 오늘(진입 당일)은 하드 레일 외에는
   건드리지 않으므로(위 "판정 순서" 절), 보유기간 계산은 진입 당일을 제외한
   그 뒤의 완성 일봉 수 + 2(진입일 + 오늘)로 근사한다 — 정확한 달력일이 아니라
   **거래일** 기준이라는 것이 이 근사의 목적이다(휴장일에 시간 손절이 당겨지지
   않는다).
4. **`lot`에 `entered_date`가 없으면 그 포지션을 관리하지 않는다.** 진입일을 모른
   채 보유기간/재진입 판정을 하면 지어낸 값 위에서 손절을 계산하게 된다 — 정상
   경로에서는 발생하지 않는다(진입 `state_update`가 항상 싣는다).
"""
from __future__ import annotations

from datetime import date as dtdate
from typing import Any, Mapping

import pandas as pd

from quant.core.models import Signal, SignalAction, market_of_symbol
from quant.core.session import in_continuous_session, market_tz
from quant.core.strategy_api import DataNeeds, Decision, StrategySnapshot
from quant.trade.fmt import fmt_price
from quant.trade.indicators import sma
from quant.trade.strategy.shell import PureStrategyShell

# Connors RSI(2) — 이 전략 고유의 고정 기간. 설정으로 노출하지 않는다("RSI(2)
# 눌림매수"가 전략의 정의 자체이기 때문 — 기간을 파라미터화하면 다른 전략이 된다).
_RSI_PERIOD = 2

# 추세 필터 기본값 — Connors 원전 표준값(200일 SMA). 이 저장소 데이터로 최적화한
# 값이 아니다. 모듈 docstring "일봉 조회 한도" 절이 이 값을 낮출 필요가 없다는
# 근거를 코드 추적 + 기존 실측 문서로 남긴다.
_DEFAULT_TREND_SMA_DAYS = 200

# RSI(2) 계산에 필요한 절대 최소 일봉 수 — Wilder 시드(첫 유효값)에는
# `period + 1 = 3`개면 이론상 충분하지만, 시드 직후 값은 아직 평활되지 않은
# 편향을 갖는다(`indicators.rsi()` docstring: "시드 방식이 달라 초반 값이
# 어긋난다"). 5는 이 저장소가 요구하는 안전 하한(작업 스펙 리터럴)이다.
_RSI_MIN_DAILY_BARS = 5

# RSI 평활이 시드 편향을 벗어나 안정되기까지, 그리고 휴장일 결손을 감안하는
# 여유분. `trend_sma_days`가 이보다 훨씬 크면(기본 200) 사실상 무관해진다.
_WARMUP_BUFFER_DAYS = 20


def _wilder_rsi(closes: pd.Series, period: int) -> pd.Series:
    """Wilder 평활 RSI — `quant.trade.indicators.rsi()`와 같은 알고리즘의 독립
    구현(모듈 docstring "RSI(2) 계산" 절 — 이 파일 안에서 완결적으로 검증
    가능해야 한다는 요구에 따른 재구현이며, 새 공식이 아니다).

    표준 정의: 첫 `period`개 상승분/하락분을 단순평균으로 시드한 뒤
    (`평균_t = (평균_{t-1} × (period-1) + 현재값) / period`) 재귀적으로 평활한다.
    첫 유효값은 인덱스 `period`(0-base)에서 나온다. 상승분/하락분이 둘 다
    0(가격 불변)이면 50, 하락분만 0이면 100 — 워밍업 미달 구간은 NaN 그대로 둔다
    (0이나 임의값으로 채우지 않는다 — 호출부가 `pd.isna()`로 직접 판단한다).
    """
    delta = closes.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = pd.Series(index=closes.index, dtype=float)
    avg_loss = pd.Series(index=closes.index, dtype=float)

    if len(closes) > period:
        avg_gain.iloc[period] = gain.iloc[1:period + 1].mean()
        avg_loss.iloc[period] = loss.iloc[1:period + 1].mean()
        for i in range(period + 1, len(closes)):
            avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
            avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + loss.iloc[i]) / period

    rs = avg_gain / avg_loss
    result = 100 - (100 / (1 + rs))
    result = result.mask(avg_loss == 0, 100.0)
    result = result.mask((avg_gain == 0) & (avg_loss == 0), 50.0)
    return result


class Rsi2DipStrategy:
    """RSI(2) 눌림매수 — `PureStrategy`(quant.core.strategy_api) 구현.

    **"Pure" 접미사가 없는 이유**: 신규 전략이라 이전할 레거시 쌍둥이가 없다
    (`OvernightDriftStrategy`/`MrVwapQuietStrategy`와 같다). 순수 구현이 유일한
    구현이고, 껍질(`Rsi2DipShell`)만 `Strategy` Protocol을 만족한다.

    설계 근거는 전부 모듈 docstring에 있다 — 특히 "판정 순서", "일봉 조회 한도",
    "상태가 두 갈래로 흐른다", "세션 마감 근접 판단" 네 절.
    """

    def __init__(self, symbols: list[str], params: dict,
                 market: str = "US", id: str = "rsi2_dip"):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Strategy Protocol 호환용 — 판정은 심볼별 시장 추론

        self.entry_rsi: float = float(params.get("entry_rsi", 10.0))
        self.exit_rsi: float = float(params.get("exit_rsi", 60.0))
        self.entry_before_close_minutes: float = float(
            params.get("entry_before_close_minutes", 10))
        self.trend_sma_days: int = int(params.get("trend_sma_days", _DEFAULT_TREND_SMA_DAYS))
        self.max_hold_days: int = int(params.get("max_hold_days", 5))
        self.hard_stop_pct: float = float(params.get("hard_stop_pct", 5.0))
        self.target_weight: float = float(params.get("target_weight", 0.5))

        if not 0 < self.entry_rsi < 100:
            raise ValueError("entry_rsi는 (0, 100) 범위여야 합니다.")
        if not 0 < self.exit_rsi <= 100:
            raise ValueError("exit_rsi는 (0, 100] 범위여야 합니다.")
        if self.exit_rsi <= self.entry_rsi:
            raise ValueError("exit_rsi는 entry_rsi보다 커야 합니다.")
        if self.entry_before_close_minutes <= 0:
            raise ValueError("entry_before_close_minutes는 양수여야 합니다.")
        if self.trend_sma_days < 2:
            raise ValueError("trend_sma_days는 2 이상이어야 합니다.")
        if self.max_hold_days < 1:
            raise ValueError("max_hold_days는 1 이상이어야 합니다.")
        if self.hard_stop_pct <= 0:
            raise ValueError("hard_stop_pct는 양수여야 합니다.")
        if not 0 < self.target_weight <= 1:
            raise ValueError("target_weight는 0 초과 1 이하여야 합니다.")

        # 일봉 조회 개수 — 추세 필터(trend_sma_days)와 RSI 워밍업 중 큰 쪽 +
        # 여유분(모듈 docstring "일봉 조회 한도" 절, 상수 정의 참고).
        self._daily_count = max(self.trend_sma_days, _RSI_MIN_DAILY_BARS) + _WARMUP_BUFFER_DAYS

    # ------------------------------------------------------------------ 계약

    def requirements(self) -> DataNeeds:
        """일봉(RSI+추세 필터 전부) + 현재가(오늘 종가 근사) + 포지션."""
        bars = tuple((s, "1d", self._daily_count) for s in self.symbols)
        return DataNeeds(bars=bars, quotes=tuple(self.symbols), needs_positions=True)

    def decide(self, snap: StrategySnapshot, state: Mapping[str, Any]) -> Decision:
        # 입력 state는 절대 in-place로 건드리지 않는다 — 중첩 dict까지 복사.
        entered_date: dict[str, str] = dict(state.get("entered_date", {}))
        last_exit_date: dict[str, str] = dict(state.get("last_exit_date", {}))

        signals: list[Signal] = []
        markets = sorted({market_of_symbol(s) for s in self.symbols})

        # 1) 보유 관리 — 진입보다 먼저. 보유의 진실은 `snap.lots` 하나뿐이라
        #    프로세스를 재시작해도 그대로 이어진다(모듈 docstring "상태가 두
        #    갈래로 흐른다" 절).
        for symbol in self.symbols:
            market = market_of_symbol(symbol)
            if not self._tradable(market, snap):
                continue
            lot = self._my_lot(snap, symbol)
            if lot is None:
                continue
            today_iso = snap.now.astimezone(market_tz(market)).date().isoformat()
            signal = self._manage(symbol, lot, market, snap, today_iso)
            if signal is not None:
                signals.append(signal)
                last_exit_date[symbol] = today_iso  # 청산 당일 재진입 금지 게이트

        # 2) 진입 — 마감 N분 전 창, 심볼당 1포지션, 1일 1회, 청산 당일 재진입 금지.
        for market in markets:
            if not self._tradable(market, snap):
                continue
            if not self._in_entry_window(market, snap):
                continue
            today_iso = snap.now.astimezone(market_tz(market)).date().isoformat()
            for symbol in sorted(s for s in self.symbols if market_of_symbol(s) == market):
                if self._my_lot(snap, symbol) is not None:
                    continue  # 심볼당 1포지션
                if entered_date.get(symbol) == today_iso:
                    continue  # 1일 1회
                if last_exit_date.get(symbol) == today_iso:
                    continue  # 청산 당일 재진입 금지 — 재진입은 다음 날부터
                signal = self._check_entry(symbol, market, snap, today_iso)
                if signal is not None:
                    entered_date[symbol] = today_iso
                    signals.append(signal)

        return Decision(
            signals=tuple(signals),
            next_state={"entered_date": entered_date, "last_exit_date": last_exit_date},
        )

    # ------------------------------------------------------------------ 게이트

    @staticmethod
    def _tradable(market: str, snap: StrategySnapshot) -> bool:
        """시장이 열려 있고 연속 거래 구간인가 — `overnight_drift._tradable`과
        동일 판정(근거도 동일: 2026-08-26 실사고, `quant/core/session.py`)."""
        return bool(snap.market_open.get(market, False)) and in_continuous_session(market, snap.now)

    def _in_entry_window(self, market: str, snap: StrategySnapshot) -> bool:
        """정규장 마감까지 남은 시간이 `entry_before_close_minutes` 이하인가.
        `overnight_drift._in_entry_window`와 동일 판정 — `snap.minutes_to_close`가
        `SessionCalendar`를 통하므로 조기폐장에도 맞고, `None`(세션 모름)은
        진입하지 않는다."""
        mtc = snap.minutes_to_close.get(market)
        if mtc is None:
            return False
        return 0 < mtc <= self.entry_before_close_minutes

    @staticmethod
    def _my_lot(snap: StrategySnapshot, symbol: str) -> Mapping[str, Any] | None:
        """내가 **진입가를 써 넣은** 열린 랏만 돌려준다 — 남의 포지션을 내 것으로
        오인해 청산 주문을 내는 사고가 구조적으로 불가능해진다
        (`overnight_drift._my_lot`/`MrVwapQuietStrategy._my_lot`과 같은 판정)."""
        lot = snap.lots.get(symbol)
        if not lot or lot.get("entry") is None:
            return None
        return lot

    @staticmethod
    def _extended_closes(daily: pd.DataFrame, price: float) -> pd.Series:
        """완성 일봉 종가 뒤에 오늘 현재가(오늘 종가 근사)를 이어 붙인다(모듈
        docstring "세션 마감 근접 판단" 절). `ignore_index=True` — 이후 계산은
        순서만 쓰고 날짜 인덱스는 쓰지 않는다."""
        return pd.concat(
            [daily["close"].astype(float), pd.Series([price], dtype=float)],
            ignore_index=True,
        )

    # ------------------------------------------------------------------ 진입

    def _check_entry(self, symbol: str, market: str, snap: StrategySnapshot,
                     today_iso: str) -> Signal | None:
        """RSI(2) < entry_rsi 그리고 종가 > SMA(trend_sma_days) — 하나라도 확인
        불가면 진입하지 않는다("확인 불가는 통과가 아니라 거부다" —
        `mr_vwap_quiet`/`overnight_drift`와 같은 원칙)."""
        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            return None
        price = float(quote.price)

        daily = snap.bars.get((symbol, "1d"))
        if daily is None or daily.empty:
            return None  # 일봉이 없다 — 확인 불가

        extended = self._extended_closes(daily, price)

        sma_today = sma(extended, self.trend_sma_days).iloc[-1]
        if pd.isna(sma_today):
            return None  # 추세 필터 계산 불가(일봉 부족) — 확인 불가
        if not price > sma_today:
            return None  # 추세 아래 — 눌림매수의 전제(추세 위)가 성립하지 않는다

        rsi_today = _wilder_rsi(extended, _RSI_PERIOD).iloc[-1]
        if pd.isna(rsi_today):
            return None  # RSI 계산 불가 — 확인 불가
        if not rsi_today < self.entry_rsi:
            return None  # 과매도가 아니다

        stop = price * (1 - self.hard_stop_pct / 100)
        return Signal(
            strategy_id=self.id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=self.target_weight,
            reason=(
                f"RSI(2) 눌림매수 진입: {symbol} w={self.target_weight:.2f} "
                f"RSI(2)={rsi_today:.1f} SMA{self.trend_sma_days}={fmt_price(sma_today, symbol)} "
                f"현재={fmt_price(price, symbol)} 손절={fmt_price(stop, symbol)} "
                f"(마감 {self.entry_before_close_minutes:g}분 전)"
            ),
            stop=stop,
            # 목표가 없음 — Connors RSI(2) 청산은 RSI 임계값 기준이지 가격 목표가
            # 아니다(모듈 docstring "규칙" 절).
            # **밤을, 최대 며칠을 넘는 값은 여기로만 나간다**: 루프가 체결을 확인한
            # 뒤에만 lot에 쓰고, 다음 사이클 이후 껍질이 `snap.lots`로 돌려준다.
            state_update={
                "entry": price, "stop": stop, "entered_date": today_iso,
                "entered_at": snap.now.isoformat(), "strategy": self.id,
            },
        )

    # ------------------------------------------------------------------ 관리

    def _manage(self, symbol: str, lot: Mapping[str, Any], market: str,
                snap: StrategySnapshot, today_iso: str) -> Signal | None:
        """청산 세 갈래 — 판정 순서는 모듈 docstring "판정 순서" 절: 하드 레일이
        항상 먼저(진입 당일 포함), 그다음(진입 당일이 아닐 때만) 보유기간 →
        RSI 순."""
        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            return None
        price = float(quote.price)
        entry = float(lot["entry"])  # _my_lot이 존재를 이미 보장한다
        entered_date = lot.get("entered_date")
        if not entered_date:
            return None  # 진입일을 모른다 — 지어내지 않는다(모듈 docstring 4번)

        stop_raw = lot.get("stop")
        stop = float(stop_raw) if stop_raw is not None else entry * (1 - self.hard_stop_pct / 100)

        def _exit(reason: str) -> Signal:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0, reason=reason,
            )

        pnl_bp = (price / entry - 1.0) * 1e4
        base = (f"진입={fmt_price(entry, symbol)} 현재={fmt_price(price, symbol)} "
                f"({pnl_bp:+.0f}bp)")

        # (c) 하드 레일 — 요일 무관, 진입 당일이라도 즉시.
        if price <= stop:
            return _exit(f"RSI(2) 눌림매수 하드 손절(-{self.hard_stop_pct:g}%): "
                         f"{base} 손절선={fmt_price(stop, symbol)}")

        if entered_date == today_iso:
            return None  # 진입 당일은 하드 레일 외 청산하지 않는다

        daily = snap.bars.get((symbol, "1d"))

        # (b) 보유기간 초과 — 거래일 기준(모듈 docstring "아직 못 하는 것" 3번).
        if daily is not None and not daily.empty:
            local_dates = daily.index.tz_convert(market_tz(market)).date
            entered = dtdate.fromisoformat(entered_date)
            completed_after_entry = sum(1 for d in local_dates if d > entered)
            holding_days = completed_after_entry + 2  # 진입일 + 오늘
            if holding_days > self.max_hold_days:
                return _exit(f"RSI(2) 눌림매수 보유기간 청산"
                             f"({self.max_hold_days}거래일 초과): {base} "
                             f"경과={holding_days}거래일")

        # (a) RSI 청산.
        if daily is not None and not daily.empty:
            extended = self._extended_closes(daily, price)
            rsi_today = _wilder_rsi(extended, _RSI_PERIOD).iloc[-1]
            if pd.notna(rsi_today) and rsi_today > self.exit_rsi:
                return _exit(f"RSI(2) 눌림매수 청산(RSI(2)={rsi_today:.1f} > "
                             f"{self.exit_rsi:g}): {base}")

        return None


class Rsi2DipShell(PureStrategyShell):
    """`STRATEGY_REGISTRY`/`build_strategies`가 기존 전략과 같은 방식으로
    (`cls(symbols=..., params=..., market=..., id=...)`) 생성할 수 있게 하는 얇은
    팩토리 — `OvernightDriftShell`/`MrVwapQuietShell`과 동일 패턴.

    **레지스트리 배선은 이 파일 밖이다**(`quant/trade/strategy/__init__.py`의
    `STRATEGY_REGISTRY` + `config/settings.yaml`의 `strategies:` 블록). 배선 시
    `quant/trade/loop.py`의 `_OVERNIGHT_STRATEGIES`에 `"rsi2_dip"` 등재가
    **반드시 필요하다** — 이 전략은 며칠에 걸쳐 포지션을 들고 가는 것이 설계다.
    누락하면 다른 일중 전략들의 EoD 강제청산 레일에 걸려 진입한 날 마감에 그대로
    털려 전략이 통째로 무효화된다(`overnight_drift.py` 모듈 docstring "오버나이트
    — 배선할 때 반드시 함께 할 일" 절과 같은 이유).
    """

    def __init__(self, symbols: list[str], params: dict,
                 market: str = "US", id: str = "rsi2_dip"):
        super().__init__(Rsi2DipStrategy(symbols, params, market=market, id=id))
