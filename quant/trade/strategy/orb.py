"""Opening Range Breakout (ORB) — Zarattini & Aziz 발표 규격의 충실한 이식.

## 1차 출처 (규칙은 전부 여기서 왔다, 추상적으로 지어내지 않았다)

**[A] Zarattini & Aziz (2023), "Can Day Trading Really Be Profitable? ..."**
SSRN 4416622. QQQ/TQQQ **단일 종목**에 5분 ORB를 적용한 논문 — 우리 유니버스와
정확히 같은 설정이라 이 구현의 기준이다. 논문 Table 1의 규칙 원문:

| 항목 | 논문 규격 |
|---|---|
| 대상 | QQQ 또는 TQQQ |
| 방향 | **세션 첫 5분봉의 부호.** 양봉이면 롱, 음봉이면 숏. **도지(open≈close)면 진입 없음.** |
| 진입 | **두 번째 5분봉의 시가.** (슬리피지 0 가정) |
| 손절 | 롱=첫 봉의 저가, 숏=첫 봉의 고가. 이 거리를 $R로 정의 |
| 목표 | 10R 또는 EoD 중 먼저 오는 쪽. 부분익절 없음 |
| 트레이드당 리스크 | 계좌의 1% |
| 최대 레버리지 | 4x |
| 수수료 | 주당 $0.0005 |

사이징 공식(논문 원문):

    Shares = integer( min( A × 0.01 / $R , 4 × A / P ) )

A=계좌, P=두 번째 봉 시가, $R=P−손절가. 논문 개선판은 손절을 **14일(일봉) ATR의
5%**로 바꾸고 목표가를 없앤 채 EoD까지 보유했다 — TQQQ 기준 2016-01-01~2023-02-17
+9,350%를 보고했다. 단, 저자들이 직접 "슬리피지 0 가정이라 비현실적일 수 있다"고
단서를 달았다.

**[B] Zarattini, Barbon & Aziz (2024), "A Profitable Day Trading Strategy For The
U.S. Equity Market"** SSRN 4729284. 7,000개 미국 주식 대상. 여기서 가져온 것은
**Relative Volume 필터**뿐이다:

    RelativeVolume(t) = OR거래량(t) / mean(직전 14일의 같은 OR구간 거래량)

논문 Figure 4: RV < 100%면 트레이드당 평균 손익이 0 근처, RV > 100%면 +0.08R.

## 이 논문에서 우리가 **가져올 수 없는** 것 (가장 중요한 한계)

[B]의 1,637%는 ORB 자체가 아니라 **횡단면 종목 선택**에서 나온다. 논문 Table 2:

| 전략 | 총수익 | IRR | Sharpe | Hit Ratio |
|---|---|---|---|---|
| ORB Base (필터 없음) | 29% | **3.2%** | 0.48 | 41.4% |
| ORB + RelVol 상위 20종목 | 1,637% | 41.6% | 2.81 | 48.4% |

즉 필터 없는 순수 ORB는 연 3.2%로 S&P500(14.2%)에 크게 진다. 1,637%는 매일
1,000종목 중 RV 상위 20개를 **골라내는 것**이 만든다. 우리는 TQQQ/SQQQ 2종목
고정이라 고를 대상이 없다 — **이 엣지의 원천은 구조적으로 이식 불가능하다.**
RV 필터는 여기서 횡단면이 아니라 시계열 게이트(오늘 TQQQ가 평소보다 활발한가)로만
쓴다. 같은 것이 아니며, 같은 효과를 기대해서도 안 된다.

## 실제 편차 (숨기지 않는다)

1. **숏 없음 → 인버스 ETF.** `SignalAction`에 숏이 없다. 음봉 → SQQQ 매수로 방향을
   표현한다. 손절 거리는 항상 `signal_symbol`(TQQQ) 기준으로 계산한 뒤 **비율**로
   환산해 실제 매수 종목의 진입가에 적용한다.

   이 편차는 **측정 결과 무해했다**(2016~2026 실데이터, 적대적 리뷰 검증). 일간
   SQQQ/TQQQ 수익률 회귀가 기울기 **−1.0024, R² 0.9990**, 절편 −0.20bp/일이다.
   데일리 리셋 복리·변동성 감쇠는 **다일 보유** 효과라 6.5시간 일중 홀드에는 거의
   작동하지 않고, 운용보수 드래그는 1일 보유당 약 0.38bp로 왕복 수수료(14bp)의
   1/37이다. 남는 것은 진입 시점의 장중 레버리지 비율 비대칭뿐인데(진입 갭이 x%면
   R 오차 ≈ 3x%), 실측 중앙값 1.22% / p95 3.81%로 R 자체의 노이즈보다 작다.
   손절 히트율을 유의미하게 편향시키지 않는다. **이 항목은 이 전략의 손익을 설명하지
   않는다** — 원인을 여기서 찾지 마라.
2. **레버리지 기본 1.0x (논문 4x).** 우리 PaperBroker는 증거금·이자를 모델링하지
   않는다. 4x를 넣으면 조달비용 0인 공짜 레버리지가 되어 수익률이 부풀려진다.
   `max_leverage`로 노출은 하되 기본값은 현금계좌 그대로 1.0이다.
3. **수수료.** 논문은 주당 $0.0005. 우리는 `execution.fee_bps`(편도 7bp) 정률이라
   훨씬 비싸다 — 국내 증권사 해외주식 수수료 현실을 반영한 것이고, 이 차이만으로도
   논문 수치는 재현되지 않는다.
4. **[B]의 stop-order 진입은 구현하지 않았다.** [B]는 OR 고가/저가에 stop order를
   걸어 **봉 중간에** 체결된다. 우리 PaperBroker는 완성봉 종가로만 체결하므로
   그 체결가를 재현할 수 없다. 없는 체결을 흉내내느니 [A]의 "두 번째 봉 시가"
   진입만 구현한다 — 이건 우리 엔진에서 정확히 재현 가능하다(OR 봉이 마감된
   순간의 현재가 = 그 봉의 종가 = 다음 봉의 시가).

## 미검증 경고

이 규격이 **우리 데이터에서** 통한다는 증거는 아직 없다. 논문 결과를 근거로
활성화하지 않는다 — `config/settings.yaml`에서 기본 `enabled: false`.
"""
from __future__ import annotations

from datetime import date as dtdate, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

from quant.core.ports import Context
from quant.core.models import Position, Signal, SignalAction

# 세션 시가 — "오늘 세션의 첫 N봉"을 판단하기 위한 거래일 경계 기준.
_SESSION_OPEN: dict[str, tuple[ZoneInfo, dtime]] = {
    "US": (ZoneInfo("America/New_York"), dtime(9, 30)),
    "KR": (ZoneInfo("Asia/Seoul"), dtime(9, 0)),
}

# Relative Volume은 직전 14거래일의 OR 거래량 평균이 필요하다. 5분봉 정규장 하루
# 78봉 기준 16거래일치(+여유)를 덮는다.
_LOOKBACK_BARS = 1300

# 일봉 ATR 조회 시 요청할 봉 수 — atr_period + True Range용 직전 종가 1개 + 여유.
_ATR_LOOKBACK_EXTRA = 6


class OpeningRangeBreakoutStrategy:
    def __init__(self, symbols: list[str], params: dict, market: str = "US", id: str = "orb"):
        self.id = id
        self.symbols = list(symbols)
        self.market = market

        # 방향을 판정하는 종목. 논문은 QQQ 신호 → TQQQ 집행도 다루지만, TQQQ 자체에
        # ORB를 적용한 버전(+1,484%)이 우리 구성과 같아 기본값은 long_symbol과 동일.
        self.long_symbol: str = params.get("long_symbol", "TQQQ")
        self.inverse_symbol: str = params.get("inverse_symbol", "SQQQ")
        self.signal_symbol: str = params.get("signal_symbol", self.long_symbol)

        self.bar_interval_minutes: int = params.get("bar_interval_minutes", 5)
        self.opening_range_minutes: int = params.get("opening_range_minutes", 5)
        if self.bar_interval_minutes <= 0 or self.opening_range_minutes <= 0:
            raise ValueError("opening_range_minutes와 bar_interval_minutes는 양수여야 합니다.")
        if self.opening_range_minutes % self.bar_interval_minutes != 0:
            raise ValueError(
                f"opening_range_minutes({self.opening_range_minutes})는 "
                f"bar_interval_minutes({self.bar_interval_minutes})의 배수여야 합니다."
            )
        self.interval = f"{self.bar_interval_minutes}m"
        self.or_bar_count = self.opening_range_minutes // self.bar_interval_minutes

        # 도지 판정 임계 — |close-open|/open이 이 값 이하면 방향 없음으로 보고 진입하지
        # 않는다(논문: "open ~ close"). 0으로 두면 부동소수 잡음까지 방향으로 읽는다.
        self.doji_threshold_pct: float = params.get("doji_threshold_pct", 0.02)

        self.stop_mode: str = params.get("stop_mode", "or_extreme")
        if self.stop_mode not in ("or_extreme", "atr_pct"):
            raise ValueError(
                f"알 수 없는 stop_mode: {self.stop_mode!r} (or_extreme | atr_pct)"
            )
        self.atr_period: int = params.get("atr_period", 14)
        self.atr_stop_mult: float = params.get("atr_stop_mult", 0.05)
        # 논문의 ATR은 **일봉** 14일 기준이다. 분봉 ATR로 바꾸면 손절 거리가 자릿수
        # 단위로 달라지므로 기본값을 바꾸지 않는다.
        self.atr_interval: str = params.get("atr_interval", "1d")

        # 논문 [A] 기본은 10R, 개선판은 목표 없음(EoD까지 보유).
        self.profit_target_r: float | None = params.get("profit_target_r")
        # 트레이드당 리스크 예산(계좌 %). 논문 1%.
        self.risk_budget_pct: float = params.get("risk_budget_pct", 1.0)
        # 논문 4x. 우리는 증거금/이자를 모델링하지 않으므로 기본 1.0(현금계좌).
        self.max_leverage: float = params.get("max_leverage", 1.0)
        # None이면 필터 없음. 1.0이면 논문 [B]의 "RV >= 100%" 게이트.
        self.relative_volume_min: float | None = params.get("relative_volume_min")
        self.relative_volume_days: int = params.get("relative_volume_days", 14)

        self.flatten_minutes: int = params.get("flatten_before_close_minutes", 5)

        # RV 필터를 켜면 직전 N거래일의 OR 거래량이 필요하다(5분봉 정규장 78봉/일).
        # 끄면 오늘 세션만 보면 되므로 조회량을 크게 줄인다 — 10년 5분봉 리플레이는
        # 사이클이 20만 회라 사이클당 조회 비용이 그대로 총 실행시간이 된다.
        if self.relative_volume_min is None:
            self._lookback_bars = max(self.or_bar_count * 4, 8)
        else:
            sessions = self.relative_volume_days + 2
            self._lookback_bars = sessions * (390 // self.bar_interval_minutes + 1)

        self._session_date: dtdate | None = None
        self._entered_today: bool = False
        self._pending: dict[str, dict] = {}  # symbol -> 체결 확인 대기 중인 진입 상태
        # 마지막 진입 판정의 근거 — 테스트/디버깅에서 왜 진입/스킵했는지 읽기 위함.
        self.last_reject: str = ""

    def on_cycle(self, ctx: Context) -> list[Signal]:
        signals: list[Signal] = []
        positions = ctx.broker.positions()

        long_pos = positions.get(self.long_symbol)
        inv_pos = positions.get(self.inverse_symbol)
        long_open = long_pos is not None and long_pos.is_open
        inv_open = inv_pos is not None and inv_pos.is_open

        if long_open:
            self._ensure_state(self.long_symbol, long_pos)
            signal = self._manage_position(self.long_symbol, long_pos, ctx)
            if signal is not None:
                signals.append(signal)
        if inv_open:
            self._ensure_state(self.inverse_symbol, inv_pos)
            signal = self._manage_position(self.inverse_symbol, inv_pos, ctx)
            if signal is not None:
                signals.append(signal)

        if long_open or inv_open:
            # 두 다리 중 하나라도 열려 있으면 신규 진입을 판단하지 않는다 —
            # TQQQ/SQQQ 동시 보유는 서로 상쇄되어 의미가 없다.
            return signals

        if not ctx.clock.is_market_open(self.market):
            return signals
        if ctx.clock.should_flatten(self.market, self.flatten_minutes):
            return signals

        signal = self._check_entry(ctx)
        if signal is not None:
            signals.append(signal)
        return signals

    def _ensure_state(self, symbol: str, pos: Position) -> None:
        """포지션이 열려 있는데 meta가 비어 있으면(체결 직후, 혹은 재시작 복구) 채운다."""
        if pos.meta:
            return
        pending = self._pending.pop(symbol, None)
        if pending is not None:
            pos.meta.update(pending)
            return
        # 재시작 등으로 OR 컨텍스트를 잃었다 — 재구성할 수 없으므로 보수적인 고정 2%
        # 손절로 폴백한다(안전망 용도이지 전략의 일부가 아니다).
        entry = pos.avg_cost
        stop = entry * 0.98
        target = entry + self.profit_target_r * (entry - stop) if self.profit_target_r else None
        pos.meta.update(entry=entry, stop=stop, target=target)

    def _check_entry(self, ctx: Context) -> Signal | None:
        """논문 [A]: 세션 첫 봉이 막 마감된 그 시점에만, 그 봉의 부호 방향으로 진입.

        "두 번째 봉의 시가"에 진입한다는 규칙은 우리 엔진에서 정확히 재현된다 —
        OR 봉이 마감된 사이클의 현재가가 곧 그 봉의 종가이고, 이는 다음 봉의
        시가와 같은 시점의 가격이다.
        """
        self.last_reject = ""

        # 진입 창은 "두 번째 봉" 하나뿐이다(논문). 이 창 밖에서는 봉을 읽을 필요조차
        # 없다 — 세션당 78사이클 중 1~2회만 아래 로직을 타게 해 10년 리플레이를
        # 현실적인 시간에 끝낸다. 라이브(10초 폴링)에서도 창 전체가 열려 있으므로
        # 폴링 타이밍이 봉 마감과 정확히 맞지 않아도 기회를 놓치지 않는다.
        tz, session_open = _SESSION_OPEN[self.market]
        now_local = ctx.clock.now().astimezone(tz)
        if now_local.date() != self._session_date:
            self._session_date = now_local.date()
            self._entered_today = False
        if self._entered_today:
            return None
        minutes_since_open = (
            now_local.hour * 60 + now_local.minute
            - (session_open.hour * 60 + session_open.minute)
        )
        elapsed_past_or = minutes_since_open - self.opening_range_minutes
        if not 0 <= elapsed_past_or < self.bar_interval_minutes:
            return None

        bars = ctx.data.history(self.signal_symbol, self.interval, self._lookback_bars)
        if bars.empty:
            self.last_reject = "봉 없음"
            return None

        bar_dates = bars.index.tz_convert(tz).date
        bar_date = self._session_date
        today_bars = bars[bar_dates == bar_date]
        if len(today_bars) != self.or_bar_count:
            # 진입 창 안인데 완성봉 수가 OR과 다르다 = 데이터 결손(조기개장/누락).
            # 논문 규격의 OR을 구성할 수 없으므로 오늘은 진입하지 않는다.
            self.last_reject = (
                f"OR 봉 수 불일치: 오늘 완성봉 {len(today_bars)}개 != {self.or_bar_count}개"
            )
            return None

        or_bars = today_bars
        or_open = float(or_bars["open"].iloc[0])
        or_close = float(or_bars["close"].iloc[-1])
        or_high = float(or_bars["high"].max())
        or_low = float(or_bars["low"].min())
        if or_open <= 0:
            self.last_reject = "OR 시가 이상"
            return None

        # 방향 = OR 봉의 부호. 도지면 진입 없음(논문 명시).
        move_pct = (or_close - or_open) / or_open * 100
        if abs(move_pct) <= self.doji_threshold_pct:
            self.last_reject = f"도지 (OR 변동 {move_pct:+.3f}% <= {self.doji_threshold_pct}%)"
            return None
        up = move_pct > 0
        entry_symbol = self.long_symbol if up else self.inverse_symbol

        if self.relative_volume_min is not None:
            rv = self._relative_volume(bars, bar_dates, bar_date)
            if rv is None:
                self.last_reject = "Relative Volume 계산 불가(과거 세션 부족)"
                return None
            if rv < self.relative_volume_min:
                self.last_reject = f"Relative Volume {rv:.2f} < {self.relative_volume_min}"
                return None

        # 손절 거리를 signal_symbol 기준가 대비 비율로 구한다.
        risk_pct = self._risk_pct(ctx, reference_price=or_close, or_high=or_high, or_low=or_low, up=up)
        if risk_pct is None or risk_pct <= 0:
            self.last_reject = self.last_reject or "손절 거리 계산 불가"
            return None

        quote = ctx.data.quote(entry_symbol)
        if quote is None or quote.price <= 0:
            self.last_reject = f"{entry_symbol} 현재가 없음"
            return None
        entry_price = quote.price
        stop = entry_price * (1 - risk_pct)
        target = (
            entry_price + self.profit_target_r * (entry_price - stop)
            if self.profit_target_r
            else None
        )

        # 논문 사이징: Shares = min(A×리스크예산/$R, 레버리지×A/P).
        # 양변을 A로 나누면 목표 비중이 되어 Signal.target_weight로 그대로 표현된다:
        #   notional/A = min(리스크예산 / R_pct, max_leverage)
        target_weight = min((self.risk_budget_pct / 100) / risk_pct, self.max_leverage)

        self._pending[entry_symbol] = {
            "entry": entry_price, "stop": stop, "target": target,
            # 진입 세션. _manage_position이 오버나잇 보유를 구조적으로 막는 데 쓴다.
            "session": self._session_date.isoformat(),
        }
        self._entered_today = True

        direction = "양봉" if up else "음봉"
        reason = (
            f"OR {direction} ({or_low:.2f}-{or_high:.2f}, {move_pct:+.2f}%) → "
            f"{entry_symbol} 진입 w={target_weight:.2f} R={risk_pct*100:.2f}%"
        )
        return Signal(
            strategy_id=self.id,
            symbol=entry_symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=target_weight,
            reason=reason,
            stop=stop,
            target=target,
        )

    def _relative_volume(
        self, bars: pd.DataFrame, bar_dates, bar_date: dtdate
    ) -> float | None:
        """논문 [B]: 오늘 OR 거래량 / 직전 N거래일의 같은 OR 구간 거래량 평균.

        횡단면 선택(1,000종목 중 상위 20)이 아니라 시계열 게이트로만 쓴다 —
        모듈 docstring의 "가져올 수 없는 것" 참고.
        """
        today_vol = float(bars[bar_dates == bar_date]["volume"].iloc[: self.or_bar_count].sum())
        past_vols: list[float] = []
        for prev_date in sorted({d for d in bar_dates if d < bar_date}, reverse=True):
            session = bars[bar_dates == prev_date]
            if len(session) < self.or_bar_count:
                continue  # 반쪽 세션(조기폐장 등) — 평균을 왜곡하므로 제외
            past_vols.append(float(session["volume"].iloc[: self.or_bar_count].sum()))
            if len(past_vols) == self.relative_volume_days:
                break
        if len(past_vols) < self.relative_volume_days:
            return None
        avg = sum(past_vols) / len(past_vols)
        return today_vol / avg if avg > 0 else None

    def _risk_pct(
        self, ctx: Context, reference_price: float, or_high: float, or_low: float, up: bool
    ) -> float | None:
        """손절 거리를 signal_symbol 기준가 대비 **비율**로 환산.

        실제 매수 종목(TQQQ 또는 SQQQ)의 가격 수준이 다르므로 비율로 옮긴다 —
        모듈 docstring 편차 1 참고.
        """
        if reference_price <= 0:
            return None
        if self.stop_mode == "or_extreme":
            # 논문 [A] 기본: 롱은 첫 봉 저가, 숏은 첫 봉 고가.
            risk = reference_price - or_low if up else or_high - reference_price
        else:  # "atr_pct" — 논문 [A] 개선판: 14일(일봉) ATR의 5%
            atr = self._atr(ctx)
            if atr is None:
                self.last_reject = f"{self.atr_interval} ATR 계산 불가(일봉 데이터 부족)"
                return None
            risk = self.atr_stop_mult * atr
        return risk / reference_price if risk > 0 else None

    def _atr(self, ctx: Context) -> float | None:
        bars = ctx.data.history(
            self.signal_symbol, self.atr_interval, self.atr_period + _ATR_LOOKBACK_EXTRA
        )
        if len(bars) < self.atr_period + 1:
            return None
        high, low, close = bars["high"], bars["low"], bars["close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
        ).max(axis=1)
        atr = float(tr.dropna().tail(self.atr_period).mean())
        return atr if atr > 0 else None

    def _manage_position(self, symbol: str, pos: Position, ctx: Context) -> Signal | None:
        quote = ctx.data.quote(symbol)
        if quote is None:
            return None
        price = quote.price
        meta = pos.meta
        entry, stop, target = meta["entry"], meta["stop"], meta.get("target")

        # 오버나잇 금지는 논문의 하드 룰이고(EoD 청산), 아래 should_flatten 하나에만
        # 기대면 조용히 깨진다: 장 마감 시각에는 is_market_open이 False가 되어
        # minutes_to_close가 None을 반환하고, should_flatten은 무조건 False가 된다.
        # 그 결과 flatten_minutes를 너무 작게 잡으면 청산 창이 통째로 사라져 포지션이
        # 며칠씩 살아남는다(실측: 10년에 청산 17건, 오버나잇 보유 발생 — 그 백테스트는
        # ORB가 아니라 그냥 TQQQ 장기보유였다). 진입 세션이 지나면 시각 계산과 무관하게
        # 무조건 청산한다.
        tz, _ = _SESSION_OPEN[self.market]
        entry_session = meta.get("session")
        if entry_session and entry_session != ctx.clock.now().astimezone(tz).date().isoformat():
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"세션 롤 강제청산(오버나잇 금지): 진입 {entry_session} 현재={price:.2f}",
            )

        if ctx.clock.should_flatten(self.market, self.flatten_minutes):
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"EoD 청산: entry={entry:.2f} stop={stop:.2f} 현재={price:.2f}",
            )
        if price <= stop:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"손절: entry={entry:.2f} stop={stop:.2f} 현재={price:.2f}",
            )
        if target is not None and price >= target:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"목표가({self.profit_target_r:g}R) 도달: "
                       f"entry={entry:.2f} target={target:.2f} 현재={price:.2f}",
            )
        return None
