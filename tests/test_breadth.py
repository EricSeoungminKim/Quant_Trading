"""quant/trade/indicators/breadth.py 단위 테스트 — 시장 리스크오프 게이트 순수
함수(anchor_drawdown/market_risk_off). 합성 1분봉으로 당일 시가 대비 등락률
계산과 임계 판정을 고정한다. 배선(전략별 모드/캐시)은 각 전략 테스트
(test_news_momentum.py 등)가 담당한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.trade.indicators.breadth import ANCHOR_SYMBOLS, anchor_drawdown, market_risk_off

KST = ZoneInfo("Asia/Seoul")


def _bars(closes: list[float], *, opens: list[float] | None = None, start=None) -> pd.DataFrame:
    """1분 간격 합성 봉. open은 기본으로 첫 봉의 close와 같게(단순화) — 첫 봉의
    open만 anchor_drawdown의 "당일 시가" 기준이므로 그 값만 정확하면 된다."""
    start = start or datetime(2026, 8, 18, 9, 0, tzinfo=KST)
    idx = [start + timedelta(minutes=i) for i in range(len(closes))]
    opens = opens or closes
    rows = [{"open": o, "high": max(o, c), "low": min(o, c), "close": c, "volume": 1000.0}
            for o, c in zip(opens, closes)]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def test_anchor_drawdown_none_on_empty_bars():
    assert anchor_drawdown(pd.DataFrame()) is None
    assert anchor_drawdown(None) is None


def test_anchor_drawdown_computes_pct_change_from_today_open():
    bars = _bars([100.0, 99.0, 98.0], opens=[100.0, 99.0, 98.0])
    dd = anchor_drawdown(bars)
    assert dd == pytest.approx(-2.0)


def test_anchor_drawdown_positive_when_price_up_from_open():
    bars = _bars([100.0, 100.5, 101.0], opens=[100.0, 100.5, 101.0])
    dd = anchor_drawdown(bars)
    assert dd == pytest.approx(1.0)


def test_anchor_drawdown_only_uses_last_bar_date():
    """여러 날짜가 섞인 lookback 시계열에서도 마지막 봉의 날짜(오늘)만 걸러
    "당일 시가"를 잡는다 — 전날 봉이 시가 계산을 오염시키지 않는다."""
    day1 = _bars([90.0, 91.0], start=datetime(2026, 8, 17, 9, 0, tzinfo=KST))
    day2 = _bars([100.0, 98.0], start=datetime(2026, 8, 18, 9, 0, tzinfo=KST))
    bars = pd.concat([day1, day2])
    dd = anchor_drawdown(bars)
    assert dd == pytest.approx(-2.0)  # 100 -> 98, 전날 90/91과 무관


def test_market_risk_off_false_when_data_missing():
    """데이터 없음(빈 DataFrame) — 게이트 부재 = 기존 동작(False)."""
    assert market_risk_off(pd.DataFrame(), max_drawdown_pct=0.5) is False


def test_market_risk_off_true_when_drawdown_exceeds_threshold():
    bars = _bars([100.0, 99.0, 99.4], opens=[100.0, 99.0, 99.4])  # -0.6%
    assert market_risk_off(bars, max_drawdown_pct=0.5) is True


def test_market_risk_off_false_when_within_threshold():
    bars = _bars([100.0, 99.8], opens=[100.0, 99.8])  # -0.2%
    assert market_risk_off(bars, max_drawdown_pct=0.5) is False


def test_market_risk_off_false_when_anchor_is_up():
    bars = _bars([100.0, 100.5], opens=[100.0, 100.5])  # +0.5%
    assert market_risk_off(bars, max_drawdown_pct=0.5) is False


def test_anchor_symbols_kr_us():
    assert ANCHOR_SYMBOLS == {"KR": "069500", "US": "QQQ"}
