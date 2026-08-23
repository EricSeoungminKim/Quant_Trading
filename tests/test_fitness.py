"""적합도 함수 — Phase 8 하네스가 최적화할 대상.

**하네스는 당신이 측정하는 것을 최적화한다.** 그래서 이 파일이 지키는 것은
"숫자가 맞나"가 아니라 **"틀린 것을 최적화하도록 유혹하지 않나"**다:

① 비용 0 백테스트는 예외로 막는다 (경고는 로그에 묻히고 하네스는 계속 돈다)
② 총수익률이 아니라 명목 대비 bps — 사이징에 오염되지 않는 유일한 단위
③ 표본 부족은 점수를 깎는 게 아니라 자격을 뺏는다
"""
from __future__ import annotations

import pandas as pd
import pytest

from quant.backtest.fitness import (
    MIN_ROUND_TRIPS,
    Fitness,
    ZeroCostBacktest,
    count_round_trips,
    evaluate,
)


class _Result:
    """BacktestResult 최소 스텁 — 엔진을 돌리지 않고 지표 계산만 시험한다."""

    def __init__(self, trades, metrics=None, benchmark=None, strategy_errors=None):
        self.trades = trades
        self.metrics = metrics or {}
        self.benchmark = benchmark or {}
        self.strategy_errors = strategy_errors or {}


def _trades(rows: list[dict]) -> pd.DataFrame:
    cols = ["ts", "symbol", "side", "qty", "price", "fee",
            "fee_krw", "realized_pnl_krw", "notional_krw", "pnl", "reason"]
    return pd.DataFrame(rows, columns=cols)


def _fill(symbol="TQQQ", side="buy", qty=10.0, notional=1_000_000.0,
          fee_krw=1_400.0, realized=0.0):
    return {"ts": None, "symbol": symbol, "side": side, "qty": qty, "price": 0.0,
            "fee": 0.0, "fee_krw": fee_krw, "realized_pnl_krw": realized,
            "notional_krw": notional, "pnl": realized - fee_krw, "reason": ""}


# ── ① 비용 0 백테스트를 막는다 ────────────────────────────────────────────

def test_zero_cost_backtest_raises_not_warns():
    """경고는 로그에 묻히고 하네스는 계속 돈다 — 여기서 멈춰야 한다."""
    r = _Result(_trades([_fill(fee_krw=0.0), _fill(side="sell", fee_krw=0.0)]))
    with pytest.raises(ZeroCostBacktest, match="비용 모델이 꺼진"):
        evaluate(r)


def test_implausibly_small_cost_is_also_rejected():
    """왕복 0.5bp 미만은 물리적으로 불가능하다 — KR ETF 편도가 1.5bp 다."""
    r = _Result(_trades([_fill(fee_krw=10.0, notional=1_000_000.0)]))  # 0.1bp
    with pytest.raises(ZeroCostBacktest):
        evaluate(r)


def test_realistic_cost_passes():
    r = _Result(_trades([_fill(fee_krw=1_400.0, notional=1_000_000.0)]))  # 14bp
    assert evaluate(r).cost_bps == pytest.approx(14.0)


def test_escape_hatch_is_test_only_and_explicit():
    """require_costs=False 는 비용 모델 자체를 시험하려고 둔 구멍이다."""
    r = _Result(_trades([_fill(fee_krw=0.0)]))
    assert evaluate(r, require_costs=False).cost_bps == 0.0


def test_no_trades_does_not_trip_the_cost_guard():
    """거래가 없으면 비용도 없다 — 그건 비용 모델이 꺼진 것과 다르다."""
    assert evaluate(_Result(_trades([]))).n_fills == 0


# ── ② 명목 대비 bps ───────────────────────────────────────────────────────

def test_bps_is_immune_to_position_sizing():
    """같은 엣지를 두 배로 태워도 bps 는 그대로여야 한다.

    이게 총수익률을 쓰지 않는 이유다 — 총수익률은 사이징에 오염된다.
    """
    small = _Result(_trades([_fill(notional=1_000_000, fee_krw=1_400, realized=10_000)]))
    large = _Result(_trades([_fill(notional=10_000_000, fee_krw=14_000, realized=100_000)]))
    assert evaluate(small).net_bps == pytest.approx(evaluate(large).net_bps)


def test_gross_and_net_are_separated_by_cost():
    r = _Result(_trades([_fill(notional=1_000_000, fee_krw=1_400, realized=9_000)]))
    f = evaluate(r)
    assert f.gross_bps == pytest.approx(90.0)
    assert f.cost_bps == pytest.approx(14.0)
    assert f.net_bps == pytest.approx(76.0)


def test_edge_below_cost_is_flagged():
    """우리가 아는 실패 모드 — 엣지 8~9bp 에 수수료 14bp."""
    r = _Result(_trades([_fill(notional=1_000_000, fee_krw=1_400, realized=850)]))
    f = evaluate(r)
    assert f.gross_bps == pytest.approx(8.5)
    assert f.edge_covers_cost is False
    assert f.net_bps < 0, "비용을 못 덮으면 순 bps 가 음수여야 한다"


def test_edge_above_cost_is_flagged():
    r = _Result(_trades([_fill(notional=1_000_000, fee_krw=1_400, realized=5_000)]))
    assert evaluate(r).edge_covers_cost is True


def test_notional_is_not_back_derived_from_fee():
    """수수료 0인 체결에서 fee_krw/fee 역산은 0으로 나눈다 — 원천 값을 쓴다."""
    r = _Result(_trades([
        _fill(notional=1_000_000, fee_krw=0.0, realized=5_000),
        _fill(notional=1_000_000, fee_krw=2_800, realized=0.0),
    ]))
    f = evaluate(r)
    assert f.total_notional_krw == pytest.approx(2_000_000)
    assert f.cost_bps == pytest.approx(14.0)


def test_missing_notional_column_is_an_error_not_a_zero():
    """조용히 0을 쓰면 bps 가 무한대가 되거나 0이 된다 — 둘 다 거짓말이다."""
    df = _trades([_fill()]).drop(columns=["notional_krw"])
    with pytest.raises(ZeroCostBacktest, match="notional_krw"):
        evaluate(_Result(df))


# ── ③ 표본 부족은 자격을 뺏는다 ───────────────────────────────────────────

def test_round_trips_counts_closes_not_fills():
    """매수 1건은 아직 결과가 없다 — 표본은 왕복 수다."""
    df = _trades([
        _fill(side="buy", qty=10), _fill(side="sell", qty=10),   # 왕복 1
        _fill(side="buy", qty=5),                                 # 미결
    ])
    assert count_round_trips(df) == 1


def test_round_trips_are_per_symbol():
    df = _trades([
        _fill(symbol="A", side="buy", qty=1), _fill(symbol="B", side="buy", qty=1),
        _fill(symbol="A", side="sell", qty=1), _fill(symbol="B", side="sell", qty=1),
    ])
    assert count_round_trips(df) == 2


def test_partial_exits_close_only_when_flat():
    df = _trades([
        _fill(side="buy", qty=10),
        _fill(side="sell", qty=5),   # 부분 익절 — 아직 왕복 아니다
        _fill(side="sell", qty=5),
    ])
    assert count_round_trips(df) == 1


def test_thin_sample_is_disqualified_not_discounted():
    """점수를 깎으면 언젠가 문턱을 넘고, 그때 근거가 없다는 사실은 사라져 있다."""
    df = _trades([_fill(side="buy"), _fill(side="sell", realized=99_999)])
    f = evaluate(_Result(df))
    assert f.n_round_trips == 1
    assert f.sufficient is False
    assert f.net_bps > 0, "성적이 좋아도 자격은 별개다"


def test_enough_round_trips_becomes_sufficient():
    rows = []
    for i in range(MIN_ROUND_TRIPS):
        rows += [_fill(symbol=f"S{i}", side="buy"), _fill(symbol=f"S{i}", side="sell")]
    f = evaluate(_Result(_trades(rows)))
    assert f.n_round_trips == MIN_ROUND_TRIPS and f.sufficient is True


# ── 맥락 ──────────────────────────────────────────────────────────────────

def test_benchmark_is_carried_so_it_cannot_be_ignored():
    """10년 -34% 를 같은 기간 단순보유 +2,941% 와 못 본 적이 있다."""
    r = _Result(_trades([_fill()]), benchmark={"buy_hold": {"total_return_pct": 2941.0}})
    assert evaluate(r).benchmark_return_pct == pytest.approx(2941.0)


def test_silent_strategy_failures_are_surfaced():
    """n_trades 0 이 '조건 미충족' 인지 '매 사이클 예외' 인지 구분돼야 한다."""
    r = _Result(_trades([]), strategy_errors={"mean_reversion": {"cycles_skipped": 42}})
    assert evaluate(r).strategy_errors == 42


def test_to_dict_is_json_serialisable_and_stable():
    import json
    r = _Result(_trades([_fill(notional=1_000_000, fee_krw=1_400, realized=5_000)]))
    d = evaluate(r).to_dict()
    assert json.loads(json.dumps(d)) == d
    assert set(d) == set(Fitness.__dataclass_fields__)
