"""시장 펄스 다이제스트(quant/analyze/market_pulse.py) 회귀 테스트.

설계는 그 모듈 docstring 참고. 여기서는:
- RSI(14)/%b/z-score/52주 거리 계산과 과매수·과매도·극단 라벨링(RSI 70/30
  경계 포함)
- 금리 스프레드 역전 판정
- VIX 버킷 경계
- 결측 종목이 조용히 빠지지 않고 "결측"으로 렌더되는지
- 텔레그램 메시지 길이(<=4096자)
- KR 외국인 수급(regime.json reasons) 파싱
- server/crontab.txt에 크론 라인이 실제로 등록됐는지
를 검증한다.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from quant.analyze import market_pulse as mp


def _closes(values: list[float]) -> pd.Series:
    idx = pd.date_range("2025-01-01", periods=len(values), freq="D")
    return pd.Series(values, index=idx)


# --------------------------------------------------------------------------- 상태 라벨 경계


def test_state_label_boundaries():
    """RSI>=70 또는 %b>=1.0 → 과매수, RSI<=30 또는 %b<=0.0 → 과매도 — 경계값 자체 포함."""
    assert mp._state_label(rsi=70.0, pct_b=None) == "▲과매수"
    assert mp._state_label(rsi=69.9, pct_b=None) == "중립"
    assert mp._state_label(rsi=30.0, pct_b=None) == "▼과매도"
    assert mp._state_label(rsi=30.1, pct_b=None) == "중립"
    assert mp._state_label(rsi=None, pct_b=1.0) == "▲과매수"
    assert mp._state_label(rsi=None, pct_b=0.0) == "▼과매도"
    assert mp._state_label(rsi=None, pct_b=None) == "중립"


def test_extreme_flag_on_zscore_boundary():
    assert mp._is_extreme(2.0) is True
    assert mp._is_extreme(-2.0) is True
    assert mp._is_extreme(1.99) is False
    assert mp._is_extreme(None) is False


# --------------------------------------------------------------------------- 지표 계산 (합성 봉)


def test_rising_series_is_overbought():
    """꾸준히 오르는 합성 봉 → RSI(14) 높음, %b>=1 근처, 상태 ▲과매수."""
    closes = _closes([100 + i for i in range(60)])  # 매일 +1, 손실 구간 없음
    inst = mp._instrument_from_closes("SPY", closes)
    assert inst.rsi14 is not None and inst.rsi14 >= 70
    assert inst.state == "▲과매수"
    assert inst.missing is False
    assert inst.last == pytest.approx(159.0)


def test_falling_series_is_oversold():
    closes = _closes([200 - i for i in range(60)])  # 매일 -1, 상승 구간 없음
    inst = mp._instrument_from_closes("QQQ", closes)
    assert inst.rsi14 is not None and inst.rsi14 <= 30
    assert inst.state == "▼과매도"


def test_percent_b_matches_hand_computation():
    # 마지막 20개가 전부 100 → std=0 → %b는 None(0.5로 위장하지 않는다)
    flat = _closes([100.0] * 25)
    assert mp._percent_b(flat) is None

    # 손으로 계산 가능한 케이스: 마지막 20개 [1..20], sma=10.5, std(표본)≈5.916
    values = list(range(1, 21))
    closes = _closes([float(v) for v in values])
    sma = pd.Series(values, dtype=float).mean()
    std = pd.Series(values, dtype=float).std()
    upper, lower = sma + 2 * std, sma - 2 * std
    want = (values[-1] - lower) / (upper - lower)
    assert mp._percent_b(closes) == pytest.approx(want)


def test_zscore_matches_hand_computation():
    values = [float(v) for v in range(1, 61)]
    closes = _closes(values)
    tail = pd.Series(values[-60:])
    want = (tail.iloc[-1] - tail.mean()) / tail.std()
    assert mp._zscore(closes) == pytest.approx(want)


def test_zscore_none_when_insufficient_history():
    assert mp._zscore(_closes([1.0] * 59)) is None


def test_dist_52w_high_low():
    closes = _closes([100.0, 120.0, 80.0, 110.0])
    dist_hi, dist_lo = mp._dist_52w(closes)
    assert dist_hi == pytest.approx((110.0 / 120.0 - 1) * 100)
    assert dist_lo == pytest.approx((110.0 / 80.0 - 1) * 100)


# --------------------------------------------------------------------------- compute_pulse — 결측


def test_missing_instrument_is_flagged_not_dropped():
    """빈 DataFrame(시세 조회 실패)을 넘겨도 목록에서 빠지지 않고 missing=True로 남는다."""
    report = mp.compute_pulse(
        {"SPY": pd.DataFrame(), "QQQ": pd.DataFrame({"close": _closes([100.0] * 60)})},
        {}, as_of=date(2026, 9, 3),
    )
    by_key = {i.key: i for i in report.instruments}
    assert by_key["SPY"].missing is True
    assert by_key["SPY"].state == "결측"
    assert by_key["QQQ"].missing is False

    msg = mp.render_telegram(report, "US")
    assert "SPY 결측" in msg


def test_macro_instrument_missing_key_is_skipped_not_forced():
    """macro 딕셔너리에 아예 없는 키(CLI가 의도적으로 안 준 경우)는 결측 행을
    만들지 않는다 — 빈 Series로 준 키(조회는 시도했으나 실패)와는 다르다."""
    report = mp.compute_pulse({}, {}, as_of=date(2026, 9, 3))
    keys = {i.key for i in report.instruments}
    assert "dollar_index" not in keys

    report2 = mp.compute_pulse({}, {"dollar_index": pd.Series(dtype=float)}, as_of=date(2026, 9, 3))
    keys2 = {i.key for i in report2.instruments}
    assert "dollar_index" in keys2


# --------------------------------------------------------------------------- 금리 스프레드 역전


def test_spread_inversion_label():
    macro = {
        "us_10y": _closes([4.0] * 25),
        "us_2y": _closes([4.5] * 25),  # 10y < 2y → 역전
    }
    report = mp.compute_pulse({}, macro, as_of=date(2026, 9, 3))
    assert report.rates.spread_10y2y == pytest.approx(-0.5)
    assert report.rates.spread_inverted is True
    assert "역전" in mp.render_telegram(report, "US")


def test_spread_not_inverted_when_positive():
    macro = {"us_10y": _closes([4.5] * 25), "us_2y": _closes([4.0] * 25)}
    report = mp.compute_pulse({}, macro, as_of=date(2026, 9, 3))
    assert report.rates.spread_inverted is False
    assert "역전" not in mp.render_telegram(report, "US")


def test_us10y_change_label_surge_and_plunge():
    # 20영업일 전 대비 +40bp(>=30bp 임계) → 급등
    values = [4.0] * 20 + [4.4]
    report = mp.compute_pulse({}, {"us_10y": _closes(values)}, as_of=date(2026, 9, 3))
    assert report.rates.us10y_chg20d_bp == pytest.approx(40.0)
    assert report.rates.us10y_label == "급등"

    values2 = [4.4] * 20 + [4.0]
    report2 = mp.compute_pulse({}, {"us_10y": _closes(values2)}, as_of=date(2026, 9, 3))
    assert report2.rates.us10y_label == "급락"


# --------------------------------------------------------------------------- VIX 버킷


@pytest.mark.parametrize("level,bucket", [
    (14.9, "저변동"), (15.0, "보통"), (25.0, "보통"),
    (25.1, "공포"), (35.0, "공포"), (35.1, "극단"),
])
def test_vix_bucket_boundaries(level, bucket):
    report = mp.compute_pulse({}, {"vix": _closes([level])}, as_of=date(2026, 9, 3))
    assert report.rates.vix_bucket == bucket


def test_vix_missing_renders_결측():
    report = mp.compute_pulse({}, {}, as_of=date(2026, 9, 3))
    assert report.rates.vix_level is None
    assert "VIX 결측" in mp.render_telegram(report, "US")


# --------------------------------------------------------------------------- KR 외국인 수급


def test_kr_flow_parses_net_from_regime_reasons():
    reasons = ["KR 추세: KODEX200 500 vs 20일 이평 490 (+1)",
               "KR 수급: 외국인+기관 순매수 +1.23조 (+1)"]
    report = mp.compute_pulse({}, {}, as_of=date(2026, 9, 3), kr_reasons=reasons)
    assert report.kr_flow.net_trillion == pytest.approx(1.23)
    assert report.kr_flow.state == "▲순매수"
    msg = mp.render_telegram(report, "KR")
    assert "외국인+기관 순매수 +1.23조" in msg


def test_kr_flow_missing_when_reason_not_found():
    report = mp.compute_pulse({}, {}, as_of=date(2026, 9, 3), kr_reasons=["KR: flow_client 미주입 — 중립"])
    assert report.kr_flow.net_trillion is None
    assert report.kr_flow.state == "결측"


def test_kr_flow_absent_for_us_market():
    """kr_reasons를 아예 안 주면(US 시장) PulseReport.kr_flow도 None — 그 섹션 자체가 안 나온다."""
    report = mp.compute_pulse({}, {}, as_of=date(2026, 9, 3))
    assert report.kr_flow is None
    assert "외국인" not in mp.render_telegram(report, "US")


# --------------------------------------------------------------------------- 메시지 길이


def test_message_length_within_telegram_limit():
    bars = {
        key: pd.DataFrame({"close": _closes([100.0 + (i % 7) - 3 + i * 0.01 for i in range(300)])})
        for key in mp.US_ROSTER
    }
    macro = {
        "us_10y": _closes([4.0 + i * 0.001 for i in range(60)]),
        "us_2y": _closes([3.5 + i * 0.001 for i in range(60)]),
        "vix": _closes([18.0] * 30),
        "dollar_index": _closes([100.0 + i * 0.01 for i in range(60)]),
        "usdkrw": _closes([1350.0 + i for i in range(60)]),
        "oil_wti": _closes([70.0 + i * 0.1 for i in range(60)]),
    }
    report = mp.compute_pulse(bars, macro, as_of=date(2026, 9, 3))
    msg = mp.render_telegram(report, "US")
    assert len(msg) <= 4096


def test_message_length_within_limit_for_kr_with_flow():
    bars = {
        key: pd.DataFrame({"close": _closes([50000.0 + i * 3 for i in range(300)])})
        for key in mp.KR_ROSTER
    }
    reasons = ["KR 추세: KODEX200 500 vs 20일 이평 490 (+1)",
               "KR 수급: 외국인+기관 순매수 -2.50조 (-1)"]
    report = mp.compute_pulse(bars, {}, as_of=date(2026, 9, 3), kr_reasons=reasons)
    msg = mp.render_telegram(report, "KR")
    assert len(msg) <= 4096
    assert "▼순매도" in msg


# --------------------------------------------------------------------------- 로컬 파일 로더


def test_load_macro_series_reads_latest_n_rows(tmp_path: Path):
    path = tmp_path / "macro_rates.jsonl"
    rows = [{"date": f"2026-01-{d:02d}", "series": "vix", "value": float(d)} for d in range(1, 11)]
    rows += [{"date": "2026-01-05", "series": "other_series", "value": 1.0}]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    out = mp.load_macro_series(path, ["vix", "us_10y"], limit=3)
    assert list(out["vix"].values) == [8.0, 9.0, 10.0]
    assert out["us_10y"].empty  # 요청했지만 파일에 없는 시리즈 — 빈 Series


def test_load_macro_series_missing_file(tmp_path: Path):
    out = mp.load_macro_series(tmp_path / "nope.jsonl", ["vix"])
    assert out["vix"].empty


def test_load_kr_regime_reasons(tmp_path: Path):
    path = tmp_path / "regime.json"
    path.write_text(json.dumps({"markets": {"KR": {"reasons": ["a", "b"]}}}), encoding="utf-8")
    assert mp.load_kr_regime_reasons(path) == ["a", "b"]
    assert mp.load_kr_regime_reasons(tmp_path / "missing.json") is None


# --------------------------------------------------------------------------- 크론 등록


def test_crontab_has_market_pulse_lines():
    text = Path("server/crontab.txt").read_text(encoding="utf-8")
    assert "./server/scripts/market_pulse.sh KR" in text
    assert "./server/scripts/market_pulse.sh US" in text
    assert "40 9 * * 1-5" in text
    assert "0 12 * * 1-5" in text
    assert "30 14 * * 1-5" in text
    assert "0 23 * * 1-5" in text
    assert "0 1,3,5 * * 2-6" in text


# --------------------------------------------------------------------------- render_telegram HTML 서식 (2026-09-04, tgfmt)

import re as _re


def _assert_balanced_html(text: str) -> None:
    stack: list[str] = []
    for m in _re.finditer(r"<(/?)([a-z]+)[^>]*>", text):
        closing, name = m.group(1), m.group(2)
        if not closing:
            stack.append(name)
        else:
            assert stack and stack[-1] == name, f"짝이 안 맞는 태그 </{name}> in: {text!r}"
            stack.pop()
    assert not stack, f"닫히지 않은 태그 {stack} in: {text!r}"


def test_render_telegram_html_is_balanced():
    bars = {
        key: pd.DataFrame({"close": _closes([100.0 + (i % 7) - 3 + i * 0.01 for i in range(300)])})
        for key in mp.US_ROSTER
    }
    report = mp.compute_pulse(bars, {}, as_of=date(2026, 9, 3))
    text = mp.render_telegram(report, "US")
    _assert_balanced_html(text)
    assert "<pre>" in text
    assert len(text) <= 4096


def test_render_telegram_missing_instruments_go_in_expandable_blockquote():
    report = mp.compute_pulse(
        {"SPY": pd.DataFrame(), "QQQ": pd.DataFrame({"close": _closes([100.0] * 60)})},
        {}, as_of=date(2026, 9, 3),
    )
    text = mp.render_telegram(report, "US")
    _assert_balanced_html(text)
    assert "<blockquote expandable>" in text


def test_render_telegram_report_url_produces_escaped_link():
    report = mp.compute_pulse({}, {}, as_of=date(2026, 9, 3))
    text = mp.render_telegram(report, "US", report_url="https://example.com/r?a=1&b=2")
    _assert_balanced_html(text)
    assert '<a href="https://example.com/r?a=1&amp;b=2">전체 리포트</a>' in text
