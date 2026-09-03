"""swing_signals.py 회귀 테스트 — 바닥 10분위 선정(단기반전), 거래량충격 임계,
유니버스 필터, "마지막 완성 세션만 본다"를 검증한다. 전부 합성 바(pandas),
네트워크·파일 없음(모듈 docstring "이 모듈이 하지 않는 것")."""
from __future__ import annotations

import pandas as pd
import pytest

from quant.analyze import swing_signals


def _bars(closes: list[float], opens: list[float] | None = None,
          volumes: list[float] | None = None, start: str = "2026-01-01") -> pd.DataFrame:
    n = len(closes)
    opens = opens or closes
    volumes = volumes if volumes is not None else [1000.0] * n
    idx = pd.bdate_range(start=start, periods=n, tz="UTC")
    return pd.DataFrame(
        {"open": opens, "high": closes, "low": closes, "close": closes, "volume": volumes},
        index=idx,
    )


# --------------------------------------------------------------- short_term_reversal


def test_short_term_reversal_selects_bottom_decile_by_5day_return():
    # 10종목, 5일 전 대비 수익률이 -10%~+8%로 고르게 퍼짐. bottom_pct=0.10 →
    # 최저 1종목(가장 많이 빠진 것)만 선정돼야 한다.
    bars = {}
    base = [100.0] * 6  # 5일 전(=index 0)부터 오늘(index 5)까지 6개 세션
    for i in range(10):
        ret = -0.10 + i * 0.02  # -10%, -8%, ..., +8%
        closes = base[:-1] + [100.0 * (1 + ret)]
        bars[f"S{i}"] = _bars(closes)

    out = swing_signals.short_term_reversal_candidates(bars, lookback=5, bottom_pct=0.10, min_names=3)

    assert len(out) == 1
    assert out[0]["symbol"] == "S0"  # -10%, 가장 강한 하락
    assert out[0]["signal"] == "short_term_reversal"
    assert out[0]["value"] == pytest.approx(-0.10)
    assert out[0]["hold_days"] == 5
    assert out[0]["horizon"] == "D+5"
    assert "-5%" in out[0]["invalidation"]


def test_short_term_reversal_returns_empty_below_min_names():
    bars = {
        "A": _bars([100.0, 100.0, 100.0, 100.0, 100.0, 90.0]),
        "B": _bars([100.0, 100.0, 100.0, 100.0, 100.0, 95.0]),
    }
    # 유효 종목 2개 < min_names(3) → 빈 리스트.
    assert swing_signals.short_term_reversal_candidates(bars, min_names=3) == []


def test_short_term_reversal_skips_symbols_with_insufficient_history():
    bars = {"A": _bars([100.0, 99.0])}  # lookback=5 인데 세션 2개뿐
    assert swing_signals.short_term_reversal_candidates(bars, lookback=5, min_names=1) == []


def test_short_term_reversal_uses_last_row_as_reference_session():
    # 6개 세션 중 마지막(index 5)이 완성 세션이어야 한다 — 중간 행이 아니라.
    closes = [100.0, 100.0, 100.0, 100.0, 100.0, 80.0]
    bars = {"A": _bars(closes), "B": _bars([100.0] * 6), "C": _bars([100.0] * 6)}

    out = swing_signals.short_term_reversal_candidates(bars, lookback=5, bottom_pct=0.34, min_names=3)

    assert out[0]["symbol"] == "A"
    assert out[0]["ref_price"] == pytest.approx(80.0)
    assert out[0]["ref_date"] == str(pd.bdate_range("2026-01-01", periods=6, tz="UTC")[-1].date())


# --------------------------------------------------------------------- volume_shock


def test_volume_shock_fires_on_turnover_multiple_and_up_close():
    # 종가를 20일 내내 100으로 고정해 거래대금(종가×거래량) 배율이 거래량
    # 배율과 같아지게 만든다 — 기준선(중앙값) 거래량 1000, 마지막날 3000(3배)
    # + 상승마감(종가100 > 시가95).
    volumes = [1000.0] * 20 + [3000.0]
    closes = [100.0] * 21
    opens = [100.0] * 20 + [95.0]
    bars = {"A": _bars(closes, opens=opens, volumes=volumes)}

    out = swing_signals.volume_shock_candidates(bars, mult=2.5, window=20)

    assert len(out) == 1
    rec = out[0]
    assert rec["symbol"] == "A"
    assert rec["signal"] == "volume_shock_premium"
    assert rec["value"] == pytest.approx(3.0)
    assert rec["ref_price"] == pytest.approx(100.0)
    assert rec["hold_days"] == 10
    assert rec["horizon"] == "D+10"
    assert "-5%" in rec["invalidation"]


def test_volume_shock_skips_below_multiple_threshold():
    volumes = [1000.0] * 20 + [2000.0]  # 2배 < mult(2.5)
    closes = [100.0] * 21
    opens = [100.0] * 20 + [95.0]
    bars = {"A": _bars(closes, opens=opens, volumes=volumes)}

    assert swing_signals.volume_shock_candidates(bars, mult=2.5, window=20) == []


def test_volume_shock_requires_up_close_even_if_turnover_spikes():
    volumes = [1000.0] * 20 + [5000.0]  # 5배(>mult) 지만 하락마감
    closes = [100.0] * 20 + [95.0]
    opens = [100.0] * 20 + [100.0]
    bars = {"A": _bars(closes, opens=opens, volumes=volumes)}

    assert swing_signals.volume_shock_candidates(bars, mult=2.5, window=20) == []


def test_volume_shock_baseline_excludes_todays_bar():
    """오늘 거래량이 기준선(중앙값) 계산에 섞이면 배율이 달라진다 — window=3로
    작게 잡아 대조가 뚜렷하게 나오도록 한다. 기준 3일 거래량 [1, 1, 1000]의
    중앙값은 1(오늘 제외)이어야 한다 — 오늘(3000)까지 넣은 4개 [1,1,1000,3000]의
    중앙값 500.5와는 확연히 다른 배율이 나온다."""
    volumes = [1.0, 1.0, 1000.0, 3000.0]
    closes = [100.0] * 4
    opens = [100.0, 100.0, 100.0, 90.0]
    bars = {"A": _bars(closes, opens=opens, volumes=volumes)}

    out = swing_signals.volume_shock_candidates(bars, mult=2.5, window=3)

    # 정답(오늘 제외 중앙값=1): 300000/100 = 3000. 오늘을 포함했다면(중앙값
    # 500.5 × 종가100=50050) 300000/50050 ≈ 5.99 로 전혀 다른 값이 나왔을 것.
    assert out[0]["value"] == pytest.approx(3000.0)


def test_volume_shock_sorts_by_multiple_descending():
    closes = [100.0] * 21
    opens = [100.0] * 20 + [95.0]
    bars = {
        "WEAK": _bars(closes, opens=opens, volumes=[1000.0] * 20 + [2600.0]),
        "STRONG": _bars(closes, opens=opens, volumes=[1000.0] * 20 + [4000.0]),
    }

    out = swing_signals.volume_shock_candidates(bars, mult=2.5, window=20)

    assert [r["symbol"] for r in out] == ["STRONG", "WEAK"]


# ----------------------------------------------------------------- largecap_universe


def test_largecap_universe_requires_both_gates():
    market_caps = {"A": 4e11, "B": 2e11, "C": 5e11}  # B는 시총 미달
    turnover = {"A": 6e9, "B": 6e9, "C": 4e9}  # C는 거래대금 미달

    out = swing_signals.largecap_universe(market_caps, turnover, min_cap=3e11, min_turnover=5e9)

    assert out == ["A"]


def test_largecap_universe_excludes_missing_turnover_entry():
    market_caps = {"A": 4e11}
    turnover: dict[str, float] = {}  # A는 거래대금 데이터 자체가 없음

    assert swing_signals.largecap_universe(market_caps, turnover) == []


def test_largecap_universe_sorts_by_market_cap_descending():
    market_caps = {"A": 4e11, "B": 9e11}
    turnover = {"A": 6e9, "B": 6e9}

    assert swing_signals.largecap_universe(market_caps, turnover) == ["B", "A"]
