"""결정론적 합성 1분봉 데이터 (seed=42). 백테스트/스모크 전용 — paper/live에서는 쓰지 않는다.

symbol별로 독립된 seed+i 스트림에서 세션(09:30-16:00 America/New_York) 정렬된 1분봉을
생성한다. quote()/history()는 set_now()로 지정된 시점까지만 노출한다 — look-ahead 금지.
평범한 가우시안 노이즈만으로는 breakout+거래량 조건이 거의 안 터지므로, 15분 bin 단위로
주기적인 드리프트+거래량 버스트를 심어 백테스트에서 실제로 진입이 발생하게 한다.
"""
from __future__ import annotations

import math
import random
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from quant.adapters.data.resample import resample_1m
from quant.core.models import Quote

START_PRICE = 50.0
NY = ZoneInfo("America/New_York")
SESSION_OPEN = dtime(9, 30)
SESSION_CLOSE = dtime(16, 0)
SESSION_ANCHOR = date(2024, 1, 1)  # 고정 앵커 — 재현 가능한 합성 데이터.
# quant.backtest.engine.run_backtest(end=...)가 walk-forward 윈도우 계산에
# 재사용한다(quant/research/) — 이름 바꿀 때 그쪽도 같이 확인할 것.

_BURST_EVERY_N_BINS = 8
# 버스트 드리프트는 부호를 번갈아 준다. 예전에는 평균 +0.0016으로 한쪽으로만
# 밀어서 8분에 한 번씩 순드리프트가 쌓였고, 평균회귀 항이 없어 가격이 무한
# 발산했다(실측: 30일 $50→$493, 400일 $50→$1,632조). 그 상태의 stub 백테스트는
# 전략이 아니라 드리프트 상수를 측정한다.
_BURST_DRIFT_MEAN, _BURST_DRIFT_STD = 0.0016, 0.0006
# 앵커 대비 로그편차를 되돌리는 약한 복원력(Ornstein-Uhlenbeck 계수).
# 임의 길이 구간에서도 가격이 START_PRICE 주변 밴드에 머무르게 한다.
_MEAN_REVERSION = 0.0015
_BURST_VOLUME_MEAN, _BURST_VOLUME_STD = 300_000.0, 40_000.0
_BASE_DRIFT_MEAN, _BASE_DRIFT_STD = 0.0, 0.0006
_BASE_VOLUME_MEAN, _BASE_VOLUME_STD = 100_000.0, 20_000.0


class StubDataFeed:
    """DataFeed 구현. bars_1m은 백테스트 엔진이 리플레이 타임라인을 뽑기 위해 공개해둔다."""

    def __init__(self, symbols: list[str], seed: int = 42, days: int = 120):
        self.symbols = list(symbols)
        self._seed = seed
        self.bars_1m: dict[str, pd.DataFrame] = {
            s: self._make_1m_session_bars(i, days) for i, s in enumerate(self.symbols)
        }
        last_ts = [df.index[-1] for df in self.bars_1m.values() if not df.empty]
        self._now = max(last_ts) if last_ts else datetime.now(NY)

    def set_now(self, now: datetime) -> None:
        self._now = now

    def quote(self, symbol: str) -> Quote | None:
        """현재가 = self._now까지 **마감된** 1분봉의 종가. index는 봉 시가 시각이므로
        `index <= now`로 거르면 now에 막 열린(형성 중) 봉이 섞여 1분치 미래 종가를
        준다 — history("1m")가 이미 그 봉을 제외하는 것과 같은 기준을 쓴다."""
        bars = self.bars_1m.get(symbol)
        if bars is None:
            return None
        visible = bars[bars.index + pd.Timedelta(minutes=1) <= self._now]
        if visible.empty:
            return None
        last = visible.iloc[-1]
        return Quote(symbol=symbol, ts=visible.index[-1], price=float(last["close"]))

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        bars = self.bars_1m.get(symbol)
        if bars is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        visible = bars[bars.index <= self._now]
        if interval == "1m":
            # ts == self._now인 마지막 행은 그 순간 막 열린, 아직 형성 중인 봉 — 제외.
            if not visible.empty and visible.index[-1] == self._now:
                visible = visible.iloc[:-1]
            return visible.tail(n)
        minutes = 24 * 60 if interval == "1d" else int(interval.rstrip("m"))
        return resample_1m(visible, minutes).tail(n)

    def _make_1m_session_bars(self, index: int, n_days: int) -> pd.DataFrame:
        """n_days 거래일(주말 제외) 분량의 세션 내 1분봉을 결정론적으로 생성한다(오래된 것부터)."""
        rng = random.Random(self._seed + index)
        price = START_PRICE
        rows = []
        minute_idx = 0
        is_burst_bin = False
        d = 0
        day_count = 0
        while day_count < n_days:
            day = SESSION_ANCHOR + timedelta(days=d)
            d += 1
            if day.weekday() >= 5:
                continue
            day_count += 1
            ts = datetime.combine(day, SESSION_OPEN, tzinfo=NY)
            end = datetime.combine(day, SESSION_CLOSE, tzinfo=NY)
            while ts < end:
                bin_number, minute_in_bin = divmod(minute_idx, 15)
                if minute_in_bin == 0:
                    is_burst_bin = bin_number % _BURST_EVERY_N_BINS == _BURST_EVERY_N_BINS - 1
                minute_idx += 1

                if is_burst_bin:
                    # 버스트마다 방향을 뒤집어 순드리프트를 0으로 만든다 —
                    # 돌파가 발생하는 성질(거래량 급증 + 큰 변동)은 유지된다.
                    burst_sign = 1.0 if (bin_number // _BURST_EVERY_N_BINS) % 2 == 0 else -1.0
                    drift = burst_sign * rng.gauss(_BURST_DRIFT_MEAN, _BURST_DRIFT_STD)
                    volume = max(rng.gauss(_BURST_VOLUME_MEAN, _BURST_VOLUME_STD), 5_000.0)
                else:
                    drift = rng.gauss(_BASE_DRIFT_MEAN, _BASE_DRIFT_STD)
                    volume = max(rng.gauss(_BASE_VOLUME_MEAN, _BASE_VOLUME_STD), 1_000.0)
                drift -= _MEAN_REVERSION * math.log(price / START_PRICE)
                price = max(price * (1 + drift), 0.5)
                high = max(price * (1 + abs(rng.gauss(0.0, 0.0004))), price)
                low = min(price * (1 - abs(rng.gauss(0.0, 0.0004))), price)
                rows.append({
                    "ts": ts, "open": price, "high": high, "low": low,
                    "close": price, "volume": volume,
                })
                ts += timedelta(minutes=1)
        return pd.DataFrame(rows).set_index("ts")
