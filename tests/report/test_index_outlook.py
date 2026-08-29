"""지수별 전망(코스피/코스닥, S&P500/나스닥) — 순수 계산 + 배선 테스트.

소유자 요청(2026-08-29): "한국장 리포트면 코스피 상승 확률/코스닥 상승 확률을
나누면 좋겠다. 미국장도 비슷하게." 커버리지:

1. `factor_outlook` 가감 정확성(합성 입력)
2. span 이 실제 요인 수를 따르는지(없는 요인은 분모에서도 빠진다)
3. `empirical_probability` 버킷 계산 손검증(작은 합성 일봉, 직접 계산한 기대값)
4. 표본(n) < MIN_SAMPLES → prob=None
5. 로컬 parquet 없으면 `load_daily_closes` → None(결측 위장 금지)
6. `build_index_outlook` payload 키 스키마(KR: kospi/kosdaq, US: sp500/nasdaq)
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from quant.analyze.index_outlook import empirical_probability, factor_outlook
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult
from quant.core.report_clock import KST
from quant.report.collect import index_outlook as wiring

_AT = datetime(2026, 8, 29, 8, 0, tzinfo=KST)


def _res(key, data=None, ok=True, error=None):
    return SourceResult(key, ok, data, error, "https://x.test", _AT, 10)


def _snap(mkt="KR", **results) -> Snapshot:
    return Snapshot(SCHEMA_VERSION, mkt, date(2026, 8, 29), _AT, results)


# ── 1. factor_outlook 가감 정확성 ────────────────────────────────


def test_factor_outlook_stacks_all_positive_factors():
    out = factor_outlook(
        index_label="KOSPI", index_change_pct=2.0,
        flow_row={"외국인": 5000, "기관계": 4000}, vix=10.0,
        anchor_avg_pct=5.0, anchor_label="반도체",
    )
    assert out["score"] == 5 and out["span"] == 5
    assert out["score100"] == 100
    assert out["label"] == "강한 상승 신호"
    assert out["negatives"] == []
    assert any("KOSPI" in p for p in out["positives"])
    assert any("외국인" in p for p in out["positives"])
    assert any("기관계" in p for p in out["positives"])
    assert any("VIX" in p for p in out["positives"])
    assert any("반도체" in p for p in out["positives"])


def test_factor_outlook_stacks_all_negative_factors():
    out = factor_outlook(
        index_label="KOSDAQ", index_change_pct=-2.0,
        flow_row={"외국인": -5000, "기관계": -4000}, vix=30.0,
        anchor_avg_pct=-5.0, anchor_label="반도체",
    )
    assert out["score"] == -5 and out["span"] == 5
    assert out["score100"] == 0
    assert out["label"] == "강한 하락 신호"
    assert out["positives"] == []


def test_factor_outlook_imminent_event_subtracts_without_growing_span():
    baseline = factor_outlook(index_label="KOSPI", index_change_pct=0.0, vix=18.0)
    with_event = factor_outlook(
        index_label="KOSPI", index_change_pct=0.0, vix=18.0,
        imminent_event_text="CPI D-1",
    )
    assert with_event["span"] == baseline["span"]  # 이벤트는 span에 안 들어간다
    assert with_event["score"] == baseline["score"] - 1
    assert "CPI D-1" in with_event["negatives"]


def test_factor_outlook_below_threshold_move_is_neutral():
    out = factor_outlook(index_label="KOSPI", index_change_pct=0.3)
    assert out["score"] == 0
    assert out["positives"] == [] and out["negatives"] == []


# ── 2. span 이 요인 수를 따른다 ───────────────────────────────────


def test_span_grows_only_with_available_factors():
    idx_only = factor_outlook(index_label="S&P500", index_change_pct=0.5)
    assert idx_only["span"] == 1  # 지수 모멘텀만

    idx_vix = factor_outlook(index_label="S&P500", index_change_pct=0.5, vix=18.0)
    assert idx_vix["span"] == 2  # + VIX

    idx_vix_flow = factor_outlook(
        index_label="KOSPI", index_change_pct=0.5, vix=18.0,
        flow_row={"외국인": 100, "기관계": 100},
    )
    assert idx_vix_flow["span"] == 4  # + 수급 2주체

    full = factor_outlook(
        index_label="KOSPI", index_change_pct=0.5, vix=18.0,
        flow_row={"외국인": 100, "기관계": 100},
        anchor_avg_pct=1.0, anchor_label="반도체",
    )
    assert full["span"] == 5  # + 앵커


def test_span_zero_yields_no_score_not_fake_neutral():
    """요인이 하나도 없으면 50점(가짜 중립)이 아니라 None 이어야 한다."""
    out = factor_outlook(index_label="KOSPI", index_change_pct=None)
    assert out["span"] == 0
    assert out["score100"] is None
    assert out["label"] is None


# ── 3. empirical_probability 버킷 계산 손검증 ────────────────────


def test_empirical_probability_hand_verified_bucket(monkeypatch):
    """trend_days=2 인 10개 종가로 버킷을 손으로 계산해 기대값과 대조한다.

    closes = [100, 101, 99, 103, 97, 105, 104, 108, 102, 110]

    t=2..8 에서 버킷(prev_sign, trend_sign)과 다음날 결과:
      t=2: prev=sign(99/101-1)=-1, trend=sign(99/100-1)=-1  → (-1,-1), 다음날 103>99 → 1
      t=3: prev=sign(103/99-1)=+1, trend=sign(103/101-1)=+1 → (+1,+1), 다음날 97<103  → 0
      t=4: prev=sign(97/103-1)=-1, trend=sign(97/99-1)=-1   → (-1,-1), 다음날 105>97  → 1
      t=5: prev=sign(105/97-1)=+1, trend=sign(105/103-1)=+1 → (+1,+1), 다음날 104<105 → 0
      t=6: prev=sign(104/105-1)=-1, trend=sign(104/97-1)=+1 → (-1,+1), 다음날 108>104 → 1
      t=7: prev=sign(108/104-1)=+1, trend=sign(108/105-1)=+1→ (+1,+1), 다음날 102<108 → 0
      t=8: prev=sign(102/108-1)=-1, trend=sign(102/104-1)=-1→ (-1,-1), 다음날 110>102 → 1

    오늘(마지막 종가 110): prev=sign(110/102-1)=+1, trend=sign(110/108-1)=+1 → (+1,+1)
    버킷(+1,+1) 표본 = [0, 0, 0](t=3,5,7) → n=3, 상승 0회, prob=0.0.
    """
    monkeypatch.setattr("quant.analyze.index_outlook.MIN_SAMPLES", 3)
    closes = [100, 101, 99, 103, 97, 105, 104, 108, 102, 110]
    out = empirical_probability(closes, trend_days=2)
    assert out["n"] == 3
    assert out["prob"] == 0.0
    assert out["reason"] is None
    assert "3회 중 상승 0회" in out["method"]


# ── 4. 표본 부족 → None ──────────────────────────────────────────


def test_empirical_probability_below_min_samples_is_none():
    closes = [100, 101, 99, 103, 97, 105, 104, 108, 102, 110]
    out = empirical_probability(closes, trend_days=2)  # 기본 MIN_SAMPLES=100, 버킷당 최대 3표본
    assert out["prob"] is None
    assert out["n"] == 3
    assert out["reason"] == "표본 부족"


def test_empirical_probability_too_few_bars_is_none():
    out = empirical_probability([100.0, 101.0, 99.0], trend_days=5)
    assert out["prob"] is None and out["n"] == 0
    assert out["reason"] == "일봉 부족"


# ── 5. 로컬 parquet 결측 처리 ─────────────────────────────────────


def test_load_daily_closes_missing_partition_returns_none(tmp_path):
    assert wiring.load_daily_closes("069500", tmp_path) is None


def test_load_daily_closes_reads_existing_partition(tmp_path):
    part_dir = tmp_path / "data" / "history" / "069500" / "1d" / "2024"
    part_dir.mkdir(parents=True)
    idx = pd.date_range("2024-01-02", periods=3, freq="D", tz="UTC")
    df = pd.DataFrame({"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3],
                        "close": [10.0, 11.0, 12.0], "volume": [1, 1, 1]}, index=idx)
    df.to_parquet(part_dir / "01.parquet")
    closes = wiring.load_daily_closes("069500", tmp_path)
    assert closes == [10.0, 11.0, 12.0]


# ── 6. build_index_outlook payload 키 스키마 ──────────────────────


def _kr_snap():
    market_src = _res("market", {
        "quotes": {
            "^KS11": {"label": "KOSPI", "close": 2500.0, "prev": 2450.0, "change_pct": 2.0},
            "^KQ11": {"label": "KOSDAQ", "close": 800.0, "prev": 810.0, "change_pct": -1.2},
            "^VIX": {"label": "VIX", "close": 14.0, "prev": 14.0, "change_pct": 0.0},
        },
        "anchors": {
            "005930.KS": {"label": "삼성전자", "symbol": "005930", "close": 70000.0,
                          "prev": 68000.0, "change_pct": 2.9},
        },
        "crosscheck": {"checked": [], "warnings": []},
    })
    kospi_flow = _res("kospi_flow", {"market": "KOSPI", "unit": "억원",
                                     "rows": [{"date": "2026-08-29", "외국인": 4000, "기관계": -500}]})
    kosdaq_flow = _res("kosdaq_flow", {"market": "KOSDAQ", "unit": "억원",
                                       "rows": [{"date": "2026-08-29", "외국인": -600, "기관계": 200}]})
    return _snap("KR", market=market_src, kospi_flow=kospi_flow, kosdaq_flow=kosdaq_flow)


def test_build_index_outlook_kr_schema(tmp_path):
    out = wiring.build_index_outlook(_kr_snap(), tmp_path)
    assert set(out) == {"kospi", "kosdaq"}
    for key in ("kospi", "kosdaq"):
        entry = out[key]
        assert set(entry) >= {
            "score", "span", "score100", "label", "positives", "negatives",
            "probability", "proxy_symbol",
        }
        assert set(entry["probability"]) == {"prob", "n", "method", "reason"}
    assert out["kospi"]["proxy_symbol"] == "069500"
    assert out["kosdaq"]["proxy_symbol"] == "229200"
    # 로컬 parquet 이 전혀 없는 tmp_path 이므로 확률은 정직하게 결측이어야 한다
    assert out["kospi"]["probability"]["prob"] is None
    assert out["kospi"]["probability"]["reason"]
    # 코스닥엔 앵커 요인이 없다(강제로 채우지 않는다) — span 이 코스피보다 작다
    assert out["kosdaq"]["span"] < out["kospi"]["span"]


def _us_snap():
    market_src = _res("market", {
        "quotes": {
            "^GSPC": {"label": "S&P500", "close": 5000.0, "prev": 4950.0, "change_pct": 1.0},
            "^IXIC": {"label": "NASDAQ", "close": 16000.0, "prev": 15800.0, "change_pct": 1.3},
            "^VIX": {"label": "VIX", "close": 16.0, "prev": 16.0, "change_pct": 0.0},
        },
        "anchors": {},
        "crosscheck": {"checked": [], "warnings": []},
    })
    return _snap("US", market=market_src)


def test_build_index_outlook_us_schema_with_anchor(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, "fetch_symbol_quotes",
                        lambda syms: {"SOXX": {"close": 200.0, "change_pct": 4.0}})
    out = wiring.build_index_outlook(_us_snap(), tmp_path)
    assert set(out) == {"sp500", "nasdaq"}
    for key in ("sp500", "nasdaq"):
        assert set(out[key]) >= {
            "score", "span", "score100", "label", "positives", "negatives",
            "probability", "proxy_symbol",
        }
    assert out["sp500"]["proxy_symbol"] == "SPY"
    assert out["nasdaq"]["proxy_symbol"] == "QQQ"
    # S&P 엔 반도체 앵커가 없고 나스닥엔 있다 — 나스닥 span 이 더 크다
    assert out["nasdaq"]["span"] > out["sp500"]["span"]
    assert any("반도체" in p for p in out["nasdaq"]["positives"])


def test_build_index_outlook_us_anchor_fetch_failure_does_not_crash(tmp_path, monkeypatch):
    def _boom(syms):
        raise RuntimeError("network down")

    monkeypatch.setattr(wiring, "fetch_symbol_quotes", _boom)
    out = wiring.build_index_outlook(_us_snap(), tmp_path)
    assert out["nasdaq"]["score100"] is not None or out["nasdaq"]["span"] >= 0
    assert not any("반도체" in p for p in out["nasdaq"]["positives"] + out["nasdaq"]["negatives"])


def test_build_index_outlook_unknown_market_raises(tmp_path):
    with pytest.raises(ValueError):
        wiring.build_index_outlook(_snap("JP"), tmp_path)
