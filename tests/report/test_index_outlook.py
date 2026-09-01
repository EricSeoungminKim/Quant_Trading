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

from quant.analyze.index_outlook import (
    empirical_probability, factor_outlook, shrinkage_probability,
)
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


# ── 3b. shrinkage_probability(v2) 버킷 계산 손검증 ────────────────


def test_shrinkage_probability_hand_verified_bucket():
    """v1과 같은 합성 10개 종가(trend_days=2)로 수축 확률을 손검증한다.

    alpha=7로 골라 분수가 깔끔해지게 했다(값 자체의 의미는 없다, 검산용).
    v1 테스트의 버킷 분해를 그대로 재사용:
      전체 버킷 outcomes = [1,0,1,0,1,0,1](t=2..8) → p0 = 4/7.
      오늘 버킷(+1,+1): k=0, n=3(t=3,5,7)
        → up_prob = (0 + 7*4/7)/(3+7) = 4/10 = 0.4, shrinkage = 7/10 = 0.7.
      단일요인(전일 부호=+1): outcomes[t=3,5,7]=[0,0,0] → k=0,n=3
        → prev_rate = (0+4)/10 = 0.4 → contribution = 0.4 - 4/7 ≈ -17.1%p.
      단일요인(추세 부호=+1): outcomes[t=3,5,6,7]=[0,0,1,0] → k=1,n=4
        → trend_rate = (1+4)/11 = 5/11 ≈ 0.4545 → contribution ≈ -11.7%p.
    """
    closes = [100, 101, 99, 103, 97, 105, 104, 108, 102, 110]
    out = shrinkage_probability(closes, trend_days=2, alpha=7.0)
    assert out["up_prob"] == 0.4
    assert out["down_prob"] == 0.6
    assert out["n_samples"] == 3
    assert out["shrinkage"] == 0.7
    assert out["brier_vs_base"] is None  # 7개 관측 < MIN_SPLIT_BARS
    names = [f["name"] for f in out["factors"]]
    assert names == ["전일 등락 부호", "2일 추세 부호"]
    assert out["factors"][0]["contribution"] == "-17.1%p"
    assert out["factors"][1]["contribution"] == "-11.7%p"


def test_shrinkage_probability_no_matching_bucket_fully_shrinks_to_p0():
    """오늘과 같은 버킷이 과거에 한 번도 없었으면(n=0) up_prob는 p0와 정확히
    같아야 한다(수축 100%) — "표본 부족이면 침묵" 대신 "표본이 없으면 전체
    평균"이 v1과의 핵심 차이다."""
    # 끝까지 단조 하락하다 마지막 하루만 급등시켜 오늘 버킷(+1,+1)이 과거
    # 관측(t=2..8, 전부 (-1,-1))에 단 한 번도 등장하지 않게 만든다.
    closes = [100, 99, 98, 97, 96, 95, 94, 93, 92, 110]
    out = shrinkage_probability(closes, trend_days=2)
    assert out["n_samples"] == 0
    assert out["shrinkage"] == 1.0
    # p0(무조건부 상승률, 7개 관측 중 1개 상승 = 1/7)과 정확히 같아야 한다
    assert out["up_prob"] == pytest.approx(1 / 7, abs=1e-4)


def test_shrinkage_probability_down_prob_is_complement_of_up_prob():
    closes = [float(100 + (i % 7) - 3) for i in range(150)]
    out = shrinkage_probability(closes)
    assert out["up_prob"] is not None
    assert round(out["up_prob"] + out["down_prob"], 6) == 1.0


def test_shrinkage_probability_converges_toward_raw_rate_as_alpha_shrinks():
    """alpha를 0에 가깝게 주면 수축이 거의 사라지고 순수 조건부 비율(k/n)에
    가까워져야 한다(수축 공식의 극한 검증)."""
    closes = [100, 101, 99, 103, 97, 105, 104, 108, 102, 110]
    out = shrinkage_probability(closes, trend_days=2, alpha=1e-6)
    # 오늘 버킷(+1,+1)의 raw 비율은 0/3 = 0.0
    assert out["up_prob"] == pytest.approx(0.0, abs=1e-4)
    assert out["shrinkage"] == pytest.approx(0.0, abs=1e-6)


def test_shrinkage_probability_too_few_bars_returns_none():
    out = shrinkage_probability([100.0, 101.0, 99.0], trend_days=5)
    assert out["up_prob"] is None and out["down_prob"] is None
    assert out["n_samples"] == 0
    assert out["shrinkage"] is None
    assert out["factors"] == []
    assert out["brier_vs_base"] is None


def test_shrinkage_probability_brier_vs_base_computed_with_enough_bars():
    """MIN_SPLIT_BARS(100) 이상의 버킷 표본이 있으면 walk-forward Brier 차를
    낸다(부호는 데이터에 따라 다를 수 있으므로 float 여부만 검증)."""
    closes = [100.0 + (i % 11) - 5 + 0.3 * i for i in range(400)]
    out = shrinkage_probability(closes)
    assert isinstance(out["brier_vs_base"], float)


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
            # v2(2026-09-02, 수축 기저율) — 이름 고정 계약
            "up_prob", "down_prob", "n_samples", "shrinkage", "method",
            "factors", "brier_vs_base",
        }
        assert set(entry["probability"]) == {"prob", "n", "method", "reason"}
    assert out["kospi"]["proxy_symbol"] == "069500"
    assert out["kosdaq"]["proxy_symbol"] == "229200"
    # 로컬 parquet 이 전혀 없는 tmp_path 이므로 확률은 정직하게 결측이어야 한다
    assert out["kospi"]["probability"]["prob"] is None
    assert out["kospi"]["probability"]["reason"]
    # v2도 마찬가지로 결측이어야 한다(파티션이 없으면 수축할 표본 자체가 없다)
    assert out["kospi"]["up_prob"] is None
    assert out["kospi"]["down_prob"] is None
    assert out["kospi"]["factors"] == []
    # 코스닥엔 앵커 요인이 없다(강제로 채우지 않는다) — span 이 코스피보다 작다
    assert out["kosdaq"]["span"] < out["kospi"]["span"]


def test_build_index_outlook_v2_populates_when_history_exists(tmp_path):
    """로컬 parquet 이 있으면 v2 필드(up_prob/down_prob/...)가 실제로 채워지고
    down_prob = 1 - up_prob 를 만족해야 한다."""
    part_dir = tmp_path / "data" / "history" / "069500" / "1d" / "2024"
    part_dir.mkdir(parents=True)
    idx = pd.date_range("2024-01-02", periods=150, freq="B", tz="UTC")
    closes = [100.0 + (i % 9) - 4 + 0.05 * i for i in range(150)]
    df = pd.DataFrame({
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1] * 150,
    }, index=idx)
    df.to_parquet(part_dir / "01.parquet")

    out = wiring.build_index_outlook(_kr_snap(), tmp_path)
    kospi = out["kospi"]
    assert kospi["up_prob"] is not None
    assert round(kospi["up_prob"] + kospi["down_prob"], 6) == 1.0
    assert kospi["n_samples"] >= 0
    assert 0.0 <= kospi["shrinkage"] <= 1.0
    assert len(kospi["factors"]) == 2
    # 코스닥은 여전히 파티션이 없으므로(이 테스트에선 069500만 만들었다) 결측
    assert out["kosdaq"]["up_prob"] is None


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
