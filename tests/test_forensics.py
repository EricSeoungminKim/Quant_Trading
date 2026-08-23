"""거래 부검(`quant/control/forensics.py`) — 2026-08-21 손으로 한 분석의 고정판.

여기서 지키는 계약은 셋이다:
1. **커버리지를 숨기지 않는다** — 재생 못 한 건수가 항상 보고된다.
2. **판정하지 않는다** — 숫자만 낸다(표본 부족은 부족하다고 쓴다).
3. **탐색하지 않는다** — 청산 규칙은 호출부가 준 목록만 재생한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from quant.control.forensics import (
    entry_range_control, forensics_text, replay_all, replay_trip,
    simulate_exit_rules, summarize,
)

T0 = datetime(2026, 8, 20, 0, 30, tzinfo=timezone.utc)


def _bars(closes, highs=None, lows=None, start=T0):
    """1분봉 프레임. highs/lows 미지정이면 close 와 같다(꼬리 없음)."""
    idx = pd.DatetimeIndex([start + timedelta(minutes=i) for i in range(len(closes))])
    return pd.DataFrame({
        "open": closes,
        "high": highs if highs is not None else closes,
        "low": lows if lows is not None else closes,
        "close": closes,
        "volume": [100.0] * len(closes),
    }, index=idx)


def _loader(frame):
    return lambda symbol, ts: frame


def _trip(entry_min=0, exit_min=5, bps=0.0, strategy="scalp_1m", symbol="005930"):
    return {
        "strategy": strategy, "symbol": symbol, "bps": bps,
        "entry_ts": T0 + timedelta(minutes=entry_min),
        "exit_ts": T0 + timedelta(minutes=exit_min),
    }


# ── 부검 산수 ────────────────────────────────────────────────────────────

def test_replay_computes_mfe_mae_and_realized():
    # 100 → 최고 110(+1000bp) → 95(-500bp) → 종료 98(-200bp)
    closes = [100.0, 110.0, 95.0, 98.0, 98.0, 98.0]
    r = replay_trip(_trip(exit_min=5), _loader(_bars(closes)))
    assert r["mfe_bp"] == pytest.approx(1000.0)
    assert r["mae_bp"] == pytest.approx(-500.0)
    assert r["realized_bp"] == pytest.approx(-200.0)


def test_exit_efficiency_is_realized_over_mfe():
    closes = [100.0, 110.0, 105.0, 105.0, 105.0, 105.0]
    r = replay_trip(_trip(exit_min=5), _loader(_bars(closes)))
    # 실현 +500bp / MFE +1000bp = 0.5 — 이익의 절반을 반납했다
    assert r["exit_efficiency"] == pytest.approx(0.5)


def test_exit_efficiency_is_none_when_no_profit_was_available():
    """MFE<=0이면 잡을 이익이 애초에 없었다 — 효율을 매기면 청산 탓으로 오독된다."""
    closes = [100.0, 99.0, 98.0, 97.0, 96.0, 95.0]
    r = replay_trip(_trip(exit_min=5), _loader(_bars(closes)))
    assert r["mfe_bp"] <= 0
    assert r["exit_efficiency"] is None


def test_range_position_uses_only_bars_up_to_entry():
    """look-ahead 금지 — 진입 후에 만들어진 고가는 레인지에 들어가면 안 된다."""
    # 0~5분: 90~100 레인지, 진입(5분)=100 → 위치 1.0. 이후 200으로 폭등해도 불변.
    closes = [90.0, 95.0, 92.0, 98.0, 96.0, 100.0, 200.0, 200.0]
    r = replay_trip(_trip(entry_min=5, exit_min=7), _loader(_bars(closes)))
    assert r["range_pos"] == pytest.approx(1.0)


def test_range_position_none_when_too_few_bars_before_entry():
    closes = [100.0, 101.0, 102.0, 103.0]
    r = replay_trip(_trip(entry_min=1, exit_min=3), _loader(_bars(closes)))
    assert r["range_pos"] is None


def test_replay_returns_none_when_bars_missing():
    assert replay_trip(_trip(), lambda s, t: None) is None


# ── 커버리지 ────────────────────────────────────────────────────────────

def test_replay_all_counts_skipped_as_coverage():
    frame = _bars([100.0] * 8)
    calls = {"n": 0}

    def loader(symbol, ts):
        calls["n"] += 1
        return frame if symbol == "OK" else None

    trips = [_trip(symbol="OK"), _trip(symbol="MISSING"), _trip(symbol="OK")]
    rows, skipped = replay_all(trips, loader)
    assert (len(rows), skipped) == (2, 1)
    assert calls["n"] == 3


def test_text_reports_coverage_first_and_flags_small_sample():
    frame = _bars([100.0, 110.0, 105.0, 105.0, 105.0, 105.0])
    rows, skipped = replay_all([_trip(exit_min=5)] * 3, _loader(frame))
    out = forensics_text(rows, skipped=7)
    assert "재생 3/10건 (30%)" in out
    assert "표본 부족" in out


# ── 진입 대조군 ─────────────────────────────────────────────────────────

def test_entry_range_control_separates_winners_from_losers():
    rows = [
        {"range_pos": 0.2, "ledger_bps": 50.0},
        {"range_pos": 0.3, "ledger_bps": 30.0},
        {"range_pos": 0.9, "ledger_bps": -40.0},
        {"range_pos": 0.95, "ledger_bps": -60.0},
    ]
    ctl = entry_range_control(rows)
    assert (ctl["n_win"], ctl["n_lose"]) == (2, 2)
    assert ctl["range_pos_median_win"] == pytest.approx(0.25)
    assert ctl["range_pos_median_lose"] == pytest.approx(0.925)


def test_entry_range_control_rho_none_below_eight_samples():
    rows = [{"range_pos": 0.5, "ledger_bps": 1.0}] * 5
    assert entry_range_control(rows)["rho"] is None


def test_text_says_range_position_does_not_discriminate_when_rho_is_flat():
    """2026-08-21 실제 결과(rho=+0.00)를 사람이 읽을 문장으로 옮긴다."""
    rows = [{"range_pos": 0.5 + (i % 2) * 0.4, "ledger_bps": 10.0 if i < 5 else -10.0}
            for i in range(10)]
    out = forensics_text(rows[:0], skipped=0)  # 빈 경우 경로
    assert "종결 거래가 없다" in out


# ── 청산 규칙 재생 ──────────────────────────────────────────────────────

def test_simulate_exit_rules_take_profit_fires_on_close():
    """봉 종가 기준 — 고가가 문턱을 넘어도 종가가 안 넘으면 익절 안 된다(보수적)."""
    # high 는 120까지 가지만 close 는 105 -> +500bp 익절만 발동
    closes = [100.0, 105.0, 100.0, 100.0, 100.0, 100.0]
    highs = [100.0, 120.0, 100.0, 100.0, 100.0, 100.0]
    res = simulate_exit_rules(
        [_trip(exit_min=5)], _loader(_bars(closes, highs=highs)),
        rules=[("익절+300bp", 300.0, None)], cost_bp=0.0,
    )
    assert res[0]["median_bp"] == pytest.approx(500.0)


def test_simulate_exit_rules_stop_takes_precedence_within_a_bar():
    closes = [100.0, 90.0, 200.0, 200.0, 200.0, 200.0]
    res = simulate_exit_rules(
        [_trip(exit_min=5)], _loader(_bars(closes)),
        rules=[("익절+100/손절-100", 100.0, 100.0)], cost_bp=0.0,
    )
    # -1000bp 에서 손절 — 그 뒤 폭등은 못 먹는다
    assert res[0]["median_bp"] == pytest.approx(-1000.0)


def test_simulate_exit_rules_subtracts_cost():
    closes = [100.0] * 6
    res = simulate_exit_rules(
        [_trip(exit_min=5)], _loader(_bars(closes)),
        rules=[("무규칙", None, None)], cost_bp=20.0,
    )
    assert res[0]["mean_bp"] == pytest.approx(-20.0)


def test_simulate_only_runs_rules_the_caller_specified():
    """이 함수는 파라미터를 탐색하지 않는다 — 준 만큼만 돈다(다중검정 고지의 전제)."""
    closes = [100.0, 105.0, 105.0, 105.0, 105.0, 105.0]
    rules = [("A", 100.0, None), ("B", None, 100.0)]
    res = simulate_exit_rules([_trip(exit_min=5)], _loader(_bars(closes)), rules=rules)
    assert [r["rule"] for r in res] == ["A", "B"]


def test_summarize_empty_is_not_an_error():
    assert summarize([]) == {"n": 0}
