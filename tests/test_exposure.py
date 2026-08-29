"""전략 간 합산 노출(`quant.control.exposure`) — 관측+경고가 정확한지를 시험한다.

이 모듈의 실패 모드는 "숫자가 틀렸다"보다 **"사각지대가 여전히 안 보인다"**다:
두 전략이 같은 심볼을 들고 있는데 표시가 안 되거나, TQQQ+SQQQ 동시 보유인데
경고가 안 뜨거나, 총자본을 모르는데 0%로 위장하는 것. 아래 테스트는 그 셋을
각각 못 박는다.
"""
from __future__ import annotations

from quant.core.fx import FixedFxProvider

from quant.control.exposure import (
    DEFAULT_ALERT_PCT,
    KNOWN_OFFSETTING_PAIRS,
    build_report,
)

# US 심볼(TQQQ/SQQQ)은 to_krw가 환율을 곱한다 — 테스트 산수를 KRW 1:1로
# 유지하려고 고정환율 1.0을 준다(test_loop_resilience.py와 같은 관례).
_FX = FixedFxProvider(1.0)


# ── 기본 계산 — 명목/레버리지 가중 ──────────────────────────────────────

def test_single_strategy_single_symbol_notional():
    report = build_report(
        lots={"TQQQ": {"donchian": 100.0}},
        prices={"TQQQ": 70.0},
        capital_krw=10_000_000.0,
        fx=_FX,
    )
    assert report.total_notional_krw == 7_000.0
    assert len(report.by_symbol) == 1
    assert report.by_symbol[0].n_strategies == 1
    assert report.duplicates == ()
    assert report.offsetting_pairs == ()
    assert report.alert is False


def test_leverage_weighted_exposure_uses_abs_leverage():
    report = build_report(
        lots={"TQQQ": {"donchian": 100.0}},
        prices={"TQQQ": 10.0},
        leverage_of={"TQQQ": 3.0},
        fx=_FX,
    )
    assert report.total_notional_krw == 1_000.0
    assert report.total_leveraged_notional_krw == 3_000.0
    assert report.by_symbol[0].leverage == 3.0


def test_missing_price_symbol_is_skipped_not_invented():
    report = build_report(
        lots={"TQQQ": {"donchian": 100.0}, "SQQQ": {"donchian": 50.0}},
        prices={"TQQQ": 10.0},  # SQQQ 가격 없음
        fx=_FX,
    )
    assert {s.symbol for s in report.by_symbol} == {"TQQQ"}
    assert report.total_notional_krw == 1_000.0


def test_nonpositive_qty_lot_is_ignored():
    report = build_report(
        lots={"TQQQ": {"donchian": 0.0, "mean_reversion": 50.0}},
        prices={"TQQQ": 10.0},
    )
    assert report.by_symbol[0].strategies == {"mean_reversion": 50.0}


# ── 중복 보유 — 사각지대 1 ──────────────────────────────────────────────

def test_two_strategies_same_symbol_flagged_as_duplicate():
    report = build_report(
        lots={"TQQQ": {"donchian": 100.0, "mean_reversion": 40.0}},
        prices={"TQQQ": 70.0},
    )
    assert len(report.duplicates) == 1
    dup = report.duplicates[0]
    assert dup.symbol == "TQQQ"
    assert dup.n_strategies == 2
    assert dup.strategies == {"donchian": 100.0, "mean_reversion": 40.0}


def test_single_strategy_symbol_is_not_a_duplicate():
    report = build_report(
        lots={"TQQQ": {"donchian": 100.0}},
        prices={"TQQQ": 70.0},
    )
    assert report.duplicates == ()


# ── 상쇄 쌍 — 사각지대 2 ────────────────────────────────────────────────

def test_tqqq_sqqq_both_held_is_offsetting_pair_and_alerts():
    report = build_report(
        lots={"TQQQ": {"donchian": 100.0}, "SQQQ": {"mean_reversion": 200.0}},
        prices={"TQQQ": 70.0, "SQQQ": 10.0},
    )
    assert len(report.offsetting_pairs) == 1
    pair = report.offsetting_pairs[0]
    assert (pair.long_symbol, pair.inverse_symbol) == ("TQQQ", "SQQQ")
    assert report.alert is True  # 상쇄 쌍 존재만으로도 경고 대상


def test_only_one_side_of_pair_held_is_not_offsetting():
    report = build_report(
        lots={"TQQQ": {"donchian": 100.0}},
        prices={"TQQQ": 70.0},
    )
    assert report.offsetting_pairs == ()
    assert report.alert is False


def test_all_known_pairs_detected():
    for long_sym, inv_sym in KNOWN_OFFSETTING_PAIRS:
        report = build_report(
            lots={long_sym: {"s1": 10.0}, inv_sym: {"s2": 10.0}},
            prices={long_sym: 100.0, inv_sym: 100.0},
        )
        assert len(report.offsetting_pairs) == 1, f"{long_sym}/{inv_sym} 쌍이 안 잡힘"


# ── 임계 초과 — 사각지대 3 ──────────────────────────────────────────────

def test_leveraged_exposure_over_threshold_alerts():
    report = build_report(
        lots={"TQQQ": {"donchian": 100.0}},
        prices={"TQQQ": 100.0},
        leverage_of={"TQQQ": 3.0},
        capital_krw=10_000.0,  # 명목 10,000 x 3배 = 30,000 = 300%
        alert_threshold_pct=1.0,
        fx=_FX,
    )
    assert report.leveraged_exposure_pct == 3.0
    assert report.alert is True


def test_leveraged_exposure_under_threshold_no_alert():
    report = build_report(
        lots={"TQQQ": {"donchian": 100.0}},
        prices={"TQQQ": 100.0},
        leverage_of={"TQQQ": 3.0},
        capital_krw=1_000_000.0,  # 30,000 / 1,000,000 = 3%
        alert_threshold_pct=1.0,
        fx=_FX,
    )
    assert report.alert is False


def test_missing_capital_krw_gives_none_pct_not_zero():
    """총자본을 모르면 비율을 0%로 위장하지 않고 None으로 남긴다."""
    report = build_report(
        lots={"TQQQ": {"donchian": 100.0}},
        prices={"TQQQ": 100.0},
        leverage_of={"TQQQ": 3.0},
        capital_krw=None,
        fx=_FX,
    )
    assert report.leveraged_exposure_pct is None
    assert "총자본 모름" in report.summary_line()


# ── 요약/알림 텍스트 ────────────────────────────────────────────────────

def test_empty_lots_summary_says_no_holdings():
    report = build_report(lots={}, prices={})
    assert report.summary_line() == "보유 없음"
    assert report.alert_text() is None


def test_alert_text_none_when_not_alerting():
    report = build_report(
        lots={"TQQQ": {"donchian": 10.0}}, prices={"TQQQ": 10.0}, capital_krw=1_000_000.0,
    )
    assert report.alert is False
    assert report.alert_text() is None


def test_alert_text_present_and_prefixed_when_alerting():
    report = build_report(
        lots={"TQQQ": {"donchian": 10.0}, "SQQQ": {"mean_reversion": 10.0}},
        prices={"TQQQ": 10.0, "SQQQ": 10.0},
    )
    text = report.alert_text()
    assert text is not None
    assert text.startswith("⚠️")
    assert "상쇄 쌍" in text


def test_default_alert_threshold_is_100_percent():
    assert DEFAULT_ALERT_PCT == 1.0


def test_to_dict_roundtrips_key_fields():
    report = build_report(
        lots={"TQQQ": {"donchian": 100.0}, "SQQQ": {"mean_reversion": 50.0}},
        prices={"TQQQ": 70.0, "SQQQ": 10.0},
        capital_krw=1_000_000.0,
    )
    d = report.to_dict()
    assert d["alert"] is True
    assert len(d["offsetting_pairs"]) == 1
    assert d["offsetting_pairs"][0]["long_symbol"] == "TQQQ"
    assert isinstance(d["summary"], str)
    assert isinstance(d["alert_text"], str)
