"""추세일(trend day) 지속 — 15분봉 개장 레인지 확장 + VWAP 기준 넓은 손절.
**순수 계약 전용 신규 전략**(레거시 쌍둥이 없음).

## 왜 15분봉인가 (이 전략의 존재 이유)

2026-08~09 이 저장소에서 1분·5분 아이디어는 **전부** 왕복 비용(US 25.2bp) 앞에서
죽었다 — scalp_1m, 5분 pullback_impulse, 5분 orb_rvol(ETF·400종목 양쪽),
tug-of-war, slot persistence, LETF 종가, 장막판 반전, eod_reversal
(`docs/vault/변경기록.md` 상단). 죽은 이유는 방향이 틀려서가 아니라 **거래 한 건이
먹는 총 엣지가 비용의 몇 배가 안 돼서**다. 그래서 이 전략은 신호 방향을 바꾸지
않고 **거래의 크기**를 키운다: 봉을 15분으로 늘려 반응 횟수를 줄이고, 손절을 ATR
기준으로 넓게 잡고, 마감까지 들고 간다(하루 최대 1건).

문헌 배경: Zarattini·Barbon·Aziz(2024, SSRN 4729284)의 ORB 가 5분 레인지 + ATR
손절을 쓰고(이 저장소 `orb_rvol.py`가 그 규격), 실무 문헌은 "trend day 는 개장
30분 안에 대부분 식별되고 그런 날은 종가가 고가 근처에서 난다"고 말한다. 이
전략은 그 둘을 합쳐 **개장 30분 레인지가 ATR14 대비 벌어진 날**만 골라 그 날의
몸통을 노린다.

## 실측 결과를 먼저 밝힌다 — 이 전략은 게이트를 통과하지 못했다

2026-09-04 스크리닝(`quant-backtest` `qb.screen_intraday --idea trend_day_15m`,
US 400종목 1분봉 2024-09~2026-08, 왕복 25.2bp):

| 슬라이스 | n | 총(gross) bp | 순(net) bp | 승률 | clustered t |
|---|---|---|---|---|---|
| 전체 | 7,472 | -9.4 | -34.6 | 0.374 | -8.9 |
| QQQ>20일선(상승) | 4,643 | **+3.2** | -22.0 | 0.393 | -6.4 |
| QQQ<20일선(하락) | 2,829 | **-30.1** | -55.3 | 0.343 | -6.3 |
| 상승 + 갭업 | 2,530 | +7.4 | -17.8 | 0.402 | -4.7 |

읽는 법: **국면 게이트는 소유자 직관대로 작동한다**(상승장 +3.2bp vs 하락장
-30.1bp — 부호가 갈리고 폭이 크다). 그런데 가장 좋은 슬라이스조차 총 엣지가
+7.4bp 로 왕복 비용 25.2bp 의 1/3 이다. 즉 **방향은 맞는데 크기가 모자라다.**
그래서 `enabled: false` 로 들어온다 — 코드·측정 기준점으로 남기고, 비용이
내려가거나(수수료 협상·체결 개선) 더 강한 선별이 붙었을 때 다시 잰다.

트레일 스탑은 **측정해서 뺐다**: 15분봉 스윙로우 2개로 트레일하면 거래의 96%가
트레일에 털려 총 엣지가 +3.2bp → -13.1bp 로 나빠졌다(같은 표본). 이긴 거래를
먼저 잘라내기 때문이다. 그래서 여기엔 트레일이 아예 없다 — 초기 손절 하나로 간다.

### 다음 반복의 출발점 — `or_atr_mult` 는 단조롭다

같은 표본에서 개장 레인지 문턱만 올려가며 잰 것(트레일 없음, 전 국면):

| `or_atr_mult` | n | 총 bp | 상승 국면 총 bp | 날짜군집 t |
|---|---|---|---|---|
| 0.8 | 7,472 | -9.4 | +3.2 | -8.9 |
| 1.2 | 1,617 | -15.1 | +3.6 | -4.7 |
| 1.6 | 512 | +6.1 | +20.7 | -1.1 |
| 2.0 | 220 | +26.7 | +29.9 | -0.1 |
| 2.5 | 82 | +41.2 | +40.4 | -0.2 |

**거래당 총 엣지가 문턱과 함께 단조 증가한다** — "적고 큰 거래" 라는 방향 자체는
맞다는 증거다. 그런데 순엣지가 양수로 돌아서는 2.0~2.5 구간에서는 n 이 2년 400종목
전체에서 82~220건(월 3~9건)으로 줄고 **날짜군집 t 가 0 근처**다: 소수의 큰 변동성
날짜에 거래가 몰려 있어 독립 표본이 사실상 몇십 개뿐이다. 게다가 문턱 5개 × 슬라이스
4개를 한 표본에서 훑어 고른 값이라 그 자체가 다중검정이다. 그래서 **기본값은 1차
스크리닝과 같은 0.8 로 둔다** — 좋아 보이는 2.5 를 기본값으로 심는 것이 곧
in-sample 파라미터 채택이다. 다음 반복이 할 일은 문턱을 올리는 게 아니라 **그런
날을 더 많이 만드는 것**(유니버스·기간 확대)이다.

## 규칙

1. **사전 필터** — 현재가 ≥ `min_price`(KR 1,000원 / US $5), 최근
   `avg_volume_days`(14) 일봉 평균 거래량 ≥ `min_avg_volume`.
2. **국면 게이트** — 시장 대리 지수(`regime_symbols`: KR 069500 / US QQQ)의
   **완성 일봉** 종가가 `regime_ma_days`(20)일 이동평균 위여야 한다. 아래면 그
   시장은 그날 통째로 진입하지 않는다("하락장에서는 조금만 잃고" = 안 하는 것).
3. **갭 게이트**(`require_gap_up`) — 그 종목의 당일 시가가 전일 종가 이상.
4. **개장 레인지(OR)** — 연속 거래 개장 직후 `or_minutes`(30)분. 15분봉 2개의
   고가 최대/저가 최소. `OR 폭 > or_atr_mult`(0.8) `× ATR14(일봉)` 이어야 한다.
5. **진입** — OR 창이 끝난 뒤, **마지막 완성 15분봉 종가**가 OR 고가 위이면서
   동시에 세션 VWAP 위이면 롱. 개장 후 `entry_window_min`(330)분이 지나면 그날은
   보지 않는다. 심볼당 세션당 1회.
6. **손절** — 진입 시점 세션 VWAP − `stop_atr_mult`(0.25) × ATR14. **진입가
   기준이 아니라 VWAP 기준**이다: 추세일에서 되돌림의 자연스러운 바닥이 VWAP
   이고, 진입가 기준 고정폭은 진입이 늦을수록 의미가 없어진다.
7. **청산** — 마감 `eod_exit_min`(3)분 전 전량(오버나잇 금지) 또는 손절. 목표
   없음 — 추세일의 몸통을 끝까지 들고 가는 게 설계다.

## 데이터

- `"15m"` — 개장 레인지 + 세션 VWAP + 돌파 판정. 한 세션(390분 ÷ 15 = 26봉)에
  여유를 더해 두 세션치를 요청한다(장 초반에도 직전 세션 봉이 있어야 세션
  경계를 인식할 수 있다).
- `"1d"` — ATR14(손절폭), 평균 거래량(필터 1), 전일 종가(갭 게이트).
  `sma_atr`(`quant/trade/indicators/__init__.py`)을 그대로 쓴다. 장중에는 완성봉
  필터가 오늘 일봉을 잘라내므로 마지막 행이 전일이다(`vol_breakout` 과 같은 전제).
- `"1d"`(대리 지수) — 국면 게이트. **거래하지 않는 심볼의 봉을 요청한다**
  (`intraday_momentum` 이 신호 심볼 QQQ 봉만 받는 것과 같은 패턴). 전략 평면은
  `regime.json` 을 읽을 수 없으므로(`quant/control/` 소관) 국면을 **스스로 봉에서
  계산한다** — 이게 이 저장소 평면 규칙 안에서 국면을 아는 유일한 길이다.

## 상태가 두 갈래로 흐른다

| # | 값 | 어디로 | 왜 |
|---|---|---|---|
| 1 | `session_date`/`entries_today`/`last_reject` | `next_state` | 하루 안에서만 의미가 있다 |
| 2 | `entry`/`stop`/`session`/`entered_at` | **`Signal.state_update` → 체결 확인 후 `Position.meta["lots"]` → 다음 사이클 `snap.lots`** | 포지션이 살아 있는 한 필요하다 |

2026-08-28 실사건(포지션 8개를 든 채 장중 재시작)에서 인스턴스 상태는 증발하지만
브로커 포지션은 남는다 — 방어선의 정본이 lot 이면 재시작이 그것을 갈라놓지 못한다.

## 아직 못 하는 것 (정직하게)

1. **스크리닝을 통과하지 못했다.** 위 표. 이건 "아직 안 켰다"가 아니라 "지금
   비용 구조에서는 진다는 실측이 있다"는 뜻이다. 켜려면 새 근거가 있어야 한다.
2. **VWAP 을 15분봉으로 계산한다.** 스크리너는 1분봉 누적으로 쟀다 — 15분봉의
   전형가는 그 15분 안의 체결 분포를 뭉갠 값이라 실제 거래량가중평균과 조금
   다르다. 1분봉을 요청하면 세션당 390봉이라 콜드 페치가 너무 비싸서 택한
   타협이고, 그만큼 스크리닝 숫자와 백테스트 숫자가 정확히 일치하지 않는다.
3. **국면 대리 지수 봉이 없으면 그 시장은 통째로 쉰다.** "확인 불가는 통과가
   아니라 거부다" — 국면을 모르는 채로 넓은 손절 전략을 켜는 것이 이 전략에서
   가장 비싼 실수다(하락장 -30bp/거래).
4. **고아 포지션을 볼 수 없다.** `DataNeeds` 가 정적으로 `self.symbols` 만
   선언하므로 유니버스에서 빠진 뒤 남은 보유분은 관리되지 않는다(관심종목 전략
   공통 한계).
5. **재시작이 "1일 1회" 게이트를 되돌린다.** 보유 중인 심볼은 `snap.lots` 가
   막지만, "진입 → 청산 → 재시작"이 같은 날 일어나면 재진입이 날 수 있다.
6. **거래대금이 아니라 주식 수로 유동성을 잰다** — `orb_rvol` 과 같은 한계.
   KR 기본값(100,000주)은 문헌 근거가 아니라 우리 추정이다.
"""
from __future__ import annotations

from datetime import date as dtdate, datetime
from typing import Any, Mapping

import pandas as pd

from quant.core.models import Signal, SignalAction, market_of_symbol
from quant.core.session import continuous_window, in_continuous_session, market_tz
from quant.core.strategy_api import DataNeeds, Decision, StrategySnapshot
from quant.trade.fmt import fmt_price
from quant.trade.indicators import sma, sma_atr
from quant.trade.strategy import kernel
from quant.trade.strategy.shell import PureStrategyShell

_INTERVAL = "15m"
_BAR_MINUTES = 15
# 정규장 길이(분) — KR 09:00~15:30, US 09:30~16:00 둘 다 390분.
_FULL_SESSION_MINUTES = 390
_BARS_PER_SESSION = _FULL_SESSION_MINUTES // _BAR_MINUTES  # 26
_BAR_SLACK = 10
# 시장별 국면 대리 지수 — KR 은 KODEX200, US 는 QQQ(스크리닝에서 쓴 것과 같다).
_DEFAULT_REGIME_SYMBOLS = {"KR": "069500", "US": "QQQ"}
_DEFAULT_MIN_PRICE = {"KR": 1000.0, "US": 5.0}
_DEFAULT_MIN_AVG_VOLUME = {"KR": 100_000.0, "US": 1_000_000.0}


def _market_floor(spec: Any, market: str, defaults: Mapping[str, float]) -> float:
    """`{KR: .., US: ..}` 형태의 시장별 하한을 읽는다. 스칼라면 두 시장에 같은
    값을 쓴다(설정 손편집 편의). 선언이 없으면 `defaults` — `orb_rvol` 과 동일."""
    if spec is None:
        return float(defaults[market])
    if isinstance(spec, Mapping):
        value = spec.get(market)
        return float(defaults[market]) if value is None else float(value)
    return float(spec)


def session_vwap(session_bars: pd.DataFrame) -> float | None:
    """세션 시작부터의 누적 VWAP(마지막 값). 전형가 = (고+저+종)/3.

    각 시점은 세션 첫 봉~그 봉만 쓰므로 look-ahead 가 없다. 누적 거래량이 0 이면
    `None`(거래 정지 등) — 0 으로 채우거나 직전 값을 끌어오지 않는다."""
    if session_bars is None or session_bars.empty:
        return None
    tp = (session_bars["high"].astype(float) + session_bars["low"].astype(float)
          + session_bars["close"].astype(float)) / 3.0
    vol = session_bars["volume"].astype(float)
    total = float(vol.sum())
    if not (total > 0):
        return None
    value = float((tp * vol).sum() / total)
    return None if pd.isna(value) else value


class TrendDayPureStrategy:
    """추세일 지속 — `PureStrategy` 구현. 모듈 docstring 참고."""

    def __init__(self, symbols: list[str], params: dict,
                 market: str = "US", id: str = "trend_day"):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Protocol 호환용 — 실제 판정은 심볼별 시장 추론

        self.or_minutes: float = float(params.get("or_minutes", 30))
        self.or_atr_mult: float = float(params.get("or_atr_mult", 0.8))
        self.stop_atr_mult: float = float(params.get("stop_atr_mult", 0.25))
        self.atr_period: int = int(params.get("atr_period", 14))
        self.avg_volume_days: int = int(params.get("avg_volume_days", 14))
        self.regime_ma_days: int = int(params.get("regime_ma_days", 20))
        self.require_gap_up: bool = bool(params.get("require_gap_up", True))
        self.entry_window_min: float = float(params.get("entry_window_min", 330))
        self.eod_exit_min: float = float(params.get("eod_exit_min", 3))
        self.target_weight: float = float(params.get("target_weight", 0.5))
        self.min_stop_bp: float = kernel.parse_min_stop_bp(params, default=40.0)
        self._min_price = params.get("min_price")
        self._min_avg_volume = params.get("min_avg_volume")
        self.regime_symbols: dict[str, str] = {
            **_DEFAULT_REGIME_SYMBOLS, **dict(params.get("regime_symbols") or {})
        }

        if self.or_minutes <= 0:
            raise ValueError("or_minutes는 양수여야 합니다.")
        if self.or_minutes % _BAR_MINUTES:
            raise ValueError(f"or_minutes는 {_BAR_MINUTES}의 배수여야 합니다(봉 경계).")
        if self.or_atr_mult <= 0:
            raise ValueError("or_atr_mult는 양수여야 합니다.")
        if self.stop_atr_mult <= 0:
            raise ValueError("stop_atr_mult는 양수여야 합니다.")
        if self.atr_period < 1:
            raise ValueError("atr_period는 1 이상이어야 합니다.")
        if self.avg_volume_days < 1:
            raise ValueError("avg_volume_days는 1 이상이어야 합니다.")
        if self.regime_ma_days < 2:
            raise ValueError("regime_ma_days는 2 이상이어야 합니다.")
        if self.entry_window_min <= self.or_minutes:
            raise ValueError("entry_window_min은 or_minutes보다 커야 합니다.")
        if self.eod_exit_min <= 0:
            raise ValueError("eod_exit_min은 양수여야 합니다.")
        if not 0 < self.target_weight <= 1:
            raise ValueError("target_weight는 0 초과 1 이하여야 합니다.")

        self._session_bars_n = _BARS_PER_SESSION * 2 + _BAR_SLACK
        self._daily_count = max(self.atr_period, self.avg_volume_days) + 5
        self._regime_daily_count = self.regime_ma_days + 5
        # 실제로 쓰는 대리 지수만 요청한다 — 심볼이 US 뿐이면 KR 지수를 받을
        # 이유가 없다(콜드 페치는 비싸다).
        self._active_regime_symbols = sorted({
            self.regime_symbols[m]
            for m in {market_of_symbol(s) for s in self.symbols}
            if m in self.regime_symbols
        })

    # ------------------------------------------------------------------ 계약

    def requirements(self) -> DataNeeds:
        """15분봉(OR·VWAP·돌파) + 일봉(ATR14·평균거래량·전일종가) + 대리 지수
        일봉(국면) + 현재가 + 포지션. 근거는 모듈 docstring "데이터" 절."""
        bars = tuple((s, _INTERVAL, self._session_bars_n) for s in self.symbols)
        bars += tuple((s, "1d", self._daily_count) for s in self.symbols)
        bars += tuple(
            (s, "1d", self._regime_daily_count) for s in self._active_regime_symbols
        )
        return DataNeeds(bars=bars, quotes=tuple(self.symbols), needs_positions=True)

    @staticmethod
    def _my_lot(snap: StrategySnapshot, symbol: str) -> Mapping[str, Any] | None:
        return kernel.my_lot(snap.lots, symbol)

    def decide(self, snap: StrategySnapshot, state: Mapping[str, Any]) -> Decision:
        session_date: dict[str, str] = dict(state.get("session_date", {}))
        entries_today: dict[str, str] = dict(state.get("entries_today", {}))
        last_reject: dict[str, str] = dict(state.get("last_reject", {}))

        signals: list[Signal] = []
        markets = sorted({market_of_symbol(s) for s in self.symbols})

        # 0) 세션 롤 — 관리보다 먼저. 하루짜리 값만 지운다.
        for market in markets:
            if not snap.market_open.get(market, False):
                continue
            today_iso = snap.now.astimezone(market_tz(market)).date().isoformat()
            if not kernel.session_rolled(session_date.get(market), today_iso):
                continue
            session_date[market] = today_iso
            for symbol in [s for s in entries_today if market_of_symbol(s) == market]:
                entries_today.pop(symbol, None)
            for symbol in [s for s in last_reject if market_of_symbol(s) == market]:
                last_reject.pop(symbol, None)

        # 1) 보유 관리 — 진입보다 먼저. 보유의 진실은 `snap.lots` 하나뿐이다.
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

        # 2) 진입 — 연속 거래 구간 + 국면 게이트 + 개장 진입창 안에서만.
        for market in markets:
            if not snap.market_open.get(market, False):
                continue
            if not in_continuous_session(market, snap.now):
                continue
            self._entries_for_market(market, snap, entries_today, last_reject, signals)

        return Decision(
            signals=tuple(signals),
            next_state={
                "session_date": session_date,
                "entries_today": entries_today,
                "last_reject": last_reject,
            },
        )

    # ------------------------------------------------------------- 진입 파이프라인

    def _entries_for_market(
        self, market: str, snap: StrategySnapshot,
        entries_today: dict[str, str], last_reject: dict[str, str],
        signals: list[Signal],
    ) -> None:
        tz = market_tz(market)
        today = snap.now.astimezone(tz).date()
        today_iso = today.isoformat()
        symbols = sorted(s for s in self.symbols if market_of_symbol(s) == market)

        elapsed = self._minutes_since_open(market, snap.now)
        if elapsed < self.or_minutes:
            return  # 개장 레인지가 아직 안 닫혔다 — 정상 대기, 사유를 남기지 않는다
        if elapsed > self.entry_window_min:
            for symbol in symbols:
                if self._my_lot(snap, symbol) is None and entries_today.get(symbol) != today_iso:
                    last_reject[symbol] = f"개장 후 {self.entry_window_min:g}분 진입창 종료"
            return

        regime_ok, regime_reason = self._regime_ok(market, snap)
        if not regime_ok:
            for symbol in symbols:
                if self._my_lot(snap, symbol) is None:
                    last_reject[symbol] = regime_reason
            return

        for symbol in symbols:
            if self._my_lot(snap, symbol) is not None:
                continue  # 보유 중엔 신규 진입 평가 없음
            if entries_today.get(symbol) == today_iso:
                last_reject[symbol] = "1일 1회 진입 소진"
                continue
            signal = self._check_entry(
                symbol, market, snap, today, today_iso, entries_today, last_reject
            )
            if signal is not None:
                signals.append(signal)

    def _regime_ok(self, market: str, snap: StrategySnapshot) -> tuple[bool, str]:
        """대리 지수 완성 일봉 종가 > `regime_ma_days` 이동평균이면 통과.

        확인 불가(봉 없음/부족)는 **거부**다 — 국면을 모르는 채로 넓은 손절
        전략을 켜는 것이 이 전략에서 가장 비싼 실수다(모듈 docstring 한계 3번)."""
        proxy = self.regime_symbols.get(market)
        if proxy is None:
            return False, f"{market} 국면 대리 지수 미설정"
        daily = snap.bars.get((proxy, "1d"))
        if daily is None or len(daily) < self.regime_ma_days:
            return False, f"국면 확인 불가({proxy} 일봉 부족)"
        closes = daily["close"].astype(float)
        ma = sma(closes, self.regime_ma_days)
        last_close, last_ma = float(closes.iloc[-1]), float(ma.iloc[-1])
        if pd.isna(last_ma):
            return False, f"국면 확인 불가({proxy} 이동평균 결손)"
        if last_close <= last_ma:
            return False, (
                f"하락 국면({proxy} {fmt_price(last_close, proxy)} ≤ "
                f"{self.regime_ma_days}일선 {fmt_price(last_ma, proxy)}) — 쉰다"
            )
        return True, ""

    def _check_entry(
        self, symbol: str, market: str, snap: StrategySnapshot,
        today: dtdate, today_iso: str,
        entries_today: dict[str, str], last_reject: dict[str, str],
    ) -> Signal | None:
        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            last_reject[symbol] = "현재가 없음"
            return None
        price = float(quote.price)

        min_price = _market_floor(self._min_price, market, _DEFAULT_MIN_PRICE)
        if price < min_price:
            last_reject[symbol] = (
                f"저가주 제외: {fmt_price(price, symbol)} < {fmt_price(min_price, symbol)}"
            )
            return None

        daily = snap.bars.get((symbol, "1d"))
        avg_volume = self._avg_daily_volume(daily)
        min_avg_volume = _market_floor(self._min_avg_volume, market, _DEFAULT_MIN_AVG_VOLUME)
        if avg_volume is None:
            last_reject[symbol] = "일봉 평균 거래량 확인 불가"
            return None
        if avg_volume < min_avg_volume:
            last_reject[symbol] = f"평균 거래량 {avg_volume:,.0f} < 최소 {min_avg_volume:,.0f}"
            return None

        atr = self._atr(daily)
        if atr is None or atr <= 0:
            last_reject[symbol] = "ATR 계산 불가(일봉 부족)"
            return None

        session = self._session_bars(snap.bars.get((symbol, _INTERVAL)), market, today)
        if session is None or session.empty:
            last_reject[symbol] = "당일 15분봉 확인 불가"
            return None

        or_bars = int(self.or_minutes // _BAR_MINUTES)
        if len(session) <= or_bars:
            last_reject[symbol] = f"개장 레인지 봉 부족({len(session)}/{or_bars + 1})"
            return None
        opening = session.iloc[:or_bars]
        or_high = float(opening["high"].max())
        or_low = float(opening["low"].min())
        if pd.isna(or_high) or pd.isna(or_low):
            last_reject[symbol] = "개장 레인지 값 결손"
            return None
        or_range = or_high - or_low
        if not (or_range > self.or_atr_mult * atr):
            last_reject[symbol] = (
                f"개장 {self.or_minutes:g}분 레인지 {or_range / atr:.2f}×ATR "
                f"< {self.or_atr_mult:g}× — 추세일 아님"
            )
            return None

        if self.require_gap_up:
            prev_close = self._prev_close(daily)
            day_open = float(session["open"].iloc[0])
            if prev_close is None:
                last_reject[symbol] = "전일 종가 확인 불가(갭 게이트)"
                return None
            if day_open < prev_close:
                last_reject[symbol] = (
                    f"갭다운 시가 {fmt_price(day_open, symbol)} < 전일 종가 "
                    f"{fmt_price(prev_close, symbol)}"
                )
                return None

        last_close = float(session["close"].iloc[-1])
        vwap = session_vwap(session)
        if vwap is None or pd.isna(last_close):
            last_reject[symbol] = "VWAP 계산 불가(당일 거래량 0)"
            return None
        if last_close <= or_high or last_close <= vwap:
            return None  # 아직 돌파 전 — 정상 대기, 사유를 남기지 않는다

        entry = price
        stop = vwap - self.stop_atr_mult * atr
        if stop >= entry or stop <= 0:
            last_reject[symbol] = "손절가 계산 불가(VWAP 기준선이 진입가 이상)"
            return None
        stop_bp = (entry - stop) / entry * 1e4
        if not kernel.stop_bp_gate_ok(stop_bp, self.min_stop_bp):
            last_reject[symbol] = f"손절폭 {stop_bp:.0f}bp < 최소 {self.min_stop_bp:g}bp"
            return None

        entries_today[symbol] = today_iso
        last_reject.pop(symbol, None)
        return Signal(
            strategy_id=self.id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=self.target_weight,
            reason=(
                f"추세일 지속(개장 {self.or_minutes:g}분 레인지 {or_range / atr:.2f}×ATR): "
                f"{symbol} w={self.target_weight:.2f} OR고가={fmt_price(or_high, symbol)} "
                f"현재={fmt_price(price, symbol)} VWAP={fmt_price(vwap, symbol)} "
                f"손절={fmt_price(stop, symbol)} [ATR{self.atr_period}={fmt_price(atr, symbol)}]"
            ),
            stop=stop,
            state_update={
                "entry": entry, "stop": stop,
                "session": today_iso, "entered_at": snap.now.isoformat(),
                "strategy": self.id,
            },
        )

    # ------------------------------------------------------------------ 데이터 헬퍼

    @staticmethod
    def _minutes_since_open(market: str, now: datetime) -> float:
        """연속 거래 개장 이후 경과 분. 개장 전이면 음수."""
        tz = market_tz(market)
        local = now.astimezone(tz)
        open_t, _ = continuous_window(market)
        open_dt = datetime.combine(local.date(), open_t, tzinfo=tz)
        return (local - open_dt).total_seconds() / 60

    @staticmethod
    def _session_bars(
        bars: pd.DataFrame | None, market: str, today: dtdate
    ) -> pd.DataFrame | None:
        """오늘 **연속 거래 개장 이후**의 15분봉만. 프리마켓 봉이 섞이면 개장
        레인지도 세션 VWAP 도 다른 값이 된다(`orb_rvol._opening_bars` 와 같은
        필터)."""
        if bars is None or bars.empty:
            return None
        tz = market_tz(market)
        open_t, _ = continuous_window(market)
        local = bars.index.tz_convert(tz)
        mask = (local.date == today) & (local.time >= open_t)
        sub = bars[mask]
        return None if sub.empty else sub

    def _avg_daily_volume(self, daily: pd.DataFrame | None) -> float | None:
        if daily is None or daily.empty:
            return None
        vols = daily["volume"].dropna().tail(self.avg_volume_days)
        if vols.empty:
            return None
        return float(vols.mean())

    @staticmethod
    def _prev_close(daily: pd.DataFrame | None) -> float | None:
        """전일 종가. 완성봉 필터가 오늘 일봉을 잘라내므로 마지막 행이 전일이다."""
        if daily is None or daily.empty:
            return None
        value = float(daily["close"].iloc[-1])
        return None if pd.isna(value) else value

    def _atr(self, daily: pd.DataFrame | None) -> float | None:
        """일봉 ATR(단순평균). 봉이 `atr_period+1` 개 미만이면 계산하지 않는다 —
        표본 2~3개짜리 ATR 로 손절폭을 정하는 건 지어내는 것에 가깝다."""
        if daily is None or len(daily) < self.atr_period + 1:
            return None
        value = sma_atr(daily, self.atr_period)
        return None if pd.isna(value) else float(value)

    # ------------------------------------------------------------------ 관리

    def _should_flatten(self, market: str, snap: StrategySnapshot) -> bool:
        """`kernel.should_flatten_dual` — 캘린더(조기폐장) **또는** 연속거래 종료
        벽시계 기준. KR 은 15:20 에 연속매매가 끝나므로 캘린더(15:30)만 보면
        체결될 수 없는 청산 주문이 나간다."""
        return kernel.should_flatten_dual(
            market, snap.now, snap.minutes_to_close.get(market),
            snap.cadence_minutes, self.eod_exit_min,
        )

    def _manage(
        self, symbol: str, lot: Mapping[str, Any], market: str, snap: StrategySnapshot
    ) -> Signal | None:
        """판정 순서: 오버나잇 안전망 → EoD → 손절(목표 없음)."""
        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            return None
        price = float(quote.price)
        entry = float(lot["entry"])
        stop_raw = lot.get("stop")
        stop = float(stop_raw) if stop_raw is not None else None

        def _exit(reason: str) -> Signal:
            return kernel.exit_signal(self.id, symbol, reason)

        today_iso = snap.now.astimezone(market_tz(market)).date().isoformat()
        if kernel.is_overnight_carry(lot, today_iso):
            return _exit(
                f"세션 롤 강제청산(오버나잇 금지): entry={fmt_price(entry, symbol)} "
                f"현재={fmt_price(price, symbol)}"
            )
        if self._should_flatten(market, snap):
            return _exit(
                f"EoD 청산(마감 {self.eod_exit_min:g}분 전): entry={fmt_price(entry, symbol)} "
                f"현재={fmt_price(price, symbol)}"
            )
        if stop is not None and price <= stop:
            return _exit(
                f"손절: entry={fmt_price(entry, symbol)} stop={fmt_price(stop, symbol)} "
                f"현재={fmt_price(price, symbol)}"
            )
        return None


class TrendDayShell(PureStrategyShell):
    """`STRATEGY_REGISTRY`/`build_strategies` 가 다른 전략과 같은 방식으로
    생성할 수 있게 하는 얇은 팩토리 — `OrbRvolShell` 과 동일 패턴."""

    def __init__(self, symbols: list[str], params: dict,
                 market: str = "US", id: str = "trend_day"):
        super().__init__(TrendDayPureStrategy(symbols, params, market=market, id=id))
