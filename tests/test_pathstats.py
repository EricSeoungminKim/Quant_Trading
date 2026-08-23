"""경로 통계(quant/research/pathstats.py) 단위 테스트.

답을 해석적으로 아는 합성 자산곡선만 쓴다 — 실데이터를 넣고 "그럴듯한 숫자가
나온다"를 확인하는 것은 검증이 아니다. 특히 3번 테스트(지수곡선 vs 한 번 점프)는
이 모듈이 존재하는 이유 그 자체다: **총수익이 같아도 과정이 다르면 R^2이 갈린다.**
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant.research import report
from quant.research.pathstats import (
    compute_path_stats,
    consistency,
    drawdown_analytics,
    log_equity_trend,
    rolling_window_returns,
    trade_bootstrap,
)

_START = 1_000_000.0
_GROWTH = 0.20  # 연 20% 복리
_YEARS = 4


def _daily_index(days: int, start: str = "2020-01-01") -> pd.DatetimeIndex:
    return pd.date_range(start=start, periods=days, freq="D", name="ts")


def _exponential_curve(days: int = 365 * _YEARS + 1) -> pd.Series:
    """정확히 연 _GROWTH로 복리 성장하는 곡선 — 기울기/R^2의 정답을 안다."""
    idx = _daily_index(days)
    t = (idx - idx[0]).total_seconds().to_numpy() / (365.25 * 24 * 3600)
    return pd.Series(_START * np.exp(np.log(1 + _GROWTH) * t), index=idx, name="equity")


def _jump_curve(days: int = 365 * _YEARS + 1, jump_at: float = 0.85) -> pd.Series:
    """총수익은 지수곡선과 **똑같지만** 한 순간의 점프로 전부 벌어들이는 곡선."""
    exp_curve = _exponential_curve(days)
    final = float(exp_curve.iloc[-1])
    jump_pos = int(days * jump_at)
    values = np.full(days, _START)
    values[jump_pos:] = final
    return pd.Series(values, index=exp_curve.index, name="equity")


def _trades(pnl: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "ts": _daily_index(len(pnl)),
        "symbol": ["TQQQ"] * len(pnl),
        "side": ["sell"] * len(pnl),
        "pnl": pnl,
    })


# --- 1. 로그자산 추세 ---------------------------------------------------------

def test_exponential_curve_recovers_known_growth_and_perfect_fit():
    trend = log_equity_trend(_exponential_curve())
    assert trend.sufficient
    assert trend.n_obs == 365 * _YEARS + 1
    assert trend.growth_pct_per_year == pytest.approx(_GROWTH * 100, rel=1e-6)
    assert trend.slope_log_per_year == pytest.approx(np.log(1 + _GROWTH), rel=1e-9)
    assert trend.r_squared == pytest.approx(1.0, abs=1e-12)
    # 완전 결정론적 지수곡선이므로 잔차가 0 -> 표준오차도 0
    assert trend.stderr_log_per_year == pytest.approx(0.0, abs=1e-12)


def test_flat_curve_slope_is_zero_and_not_distinguishable_from_zero():
    """완전 평평 + 잡음이 있는 평평 둘 다 "0과 구분 안 됨"으로 나와야 한다."""
    idx = _daily_index(400)
    flat = pd.Series(np.full(len(idx), _START), index=idx)
    trend = log_equity_trend(flat)
    assert trend.slope_log_per_year == pytest.approx(0.0, abs=1e-12)
    assert abs(trend.t_stat) < 2  # 0과 구분 불가

    # 상수 주위를 결정론적으로 진동(자기상관 없는 잔차) — 표준오차가 기울기를 압도해야 한다
    zigzag = pd.Series(_START * (1 + 0.01 * (-1.0) ** np.arange(len(idx))), index=idx)
    z = log_equity_trend(zigzag)
    assert z.sufficient
    assert abs(z.growth_pct_per_year) < 1.0
    assert z.stderr_log_per_year > 0
    assert abs(z.t_stat) < 2, "잡음뿐인 곡선에서 유의한 추세가 나오면 안 된다"
    assert z.growth_pct_lo < 0 < z.growth_pct_hi  # 95% 구간이 0을 포함


def test_same_total_return_but_one_jump_has_far_lower_r_squared():
    """이 모듈의 존재 이유. 끝점 지표는 두 곡선을 구분하지 못한다 — R^2은 한다."""
    smooth, jumpy = _exponential_curve(), _jump_curve()

    smooth_total = float(smooth.iloc[-1] / smooth.iloc[0] - 1)
    jumpy_total = float(jumpy.iloc[-1] / jumpy.iloc[0] - 1)
    assert jumpy_total == pytest.approx(smooth_total, rel=1e-12), "총수익은 같아야 대조가 성립한다"

    s, j = log_equity_trend(smooth), log_equity_trend(jumpy)
    assert s.r_squared > 0.999
    assert j.r_squared < 0.5
    assert s.r_squared - j.r_squared > 0.4, "이 격차가 '운 한 방' vs '꾸준한 과정'의 신호다"
    # 점프 곡선은 기울기 추정도 훨씬 불확실해야 한다
    assert j.stderr_log_per_year > s.stderr_log_per_year


def test_trend_too_short_is_insufficient_not_a_number():
    idx = _daily_index(2)
    trend = log_equity_trend(pd.Series([_START, _START * 1.5], index=idx))
    assert not trend.sufficient
    assert trend.growth_pct_per_year == 0.0  # 만들어낸 숫자가 아니라 미계산 상태


# --- 2. 롤링 창 분포 ----------------------------------------------------------

def test_rolling_windows_on_exponential_curve_match_compounding():
    rows = {r.label: r for r in rolling_window_returns(_exponential_curve())}
    one_month = rows["1M"]
    assert one_month.sufficient
    assert 28 <= one_month.window_bars <= 32  # 일봉 밀도에서 역산한 1개월
    expected = ((1 + _GROWTH) ** (one_month.window_bars / 365.25) - 1) * 100
    assert one_month.median_pct == pytest.approx(expected, rel=1e-6)
    assert one_month.frac_positive == 1.0
    assert one_month.count == len(_exponential_curve()) - one_month.window_bars
    assert rows["1Y"].median_pct == pytest.approx(_GROWTH * 100, rel=1e-2)


def test_rolling_window_longer_than_data_reports_insufficient():
    """6개월 데이터에 1Y 창 — 숫자를 만들지 않고 '표본 부족'."""
    short = _exponential_curve(days=180)
    rows = {r.label: r for r in rolling_window_returns(short)}
    assert rows["1Y"].sufficient is False
    assert rows["1Y"].median_pct == 0.0
    assert rows["3M"].sufficient is True

    rendered = report.render_path_stats(compute_path_stats(short))
    assert "표본 부족" in rendered


# --- 3. 낙폭 -----------------------------------------------------------------

def _known_drawdown_curve() -> pd.Series:
    # 고점120 -> 저점90(-25%) -> 120 회복, 이후 고점130 -> 100(-23.08%) 미회복
    return pd.Series([100, 120, 90, 110, 120, 130, 100, 105.0], index=_daily_index(8))


def test_drawdown_episodes_have_exact_depth_duration_and_recovery():
    stats = drawdown_analytics(_known_drawdown_curve())
    assert stats.n_bars == 8
    assert stats.n_episodes == 2

    first, second = stats.episodes
    assert first.recovered is True
    assert str(first.start.date()) == "2020-01-02"  # 고점 120
    assert str(first.trough.date()) == "2020-01-03"  # 저점 90
    assert str(first.recovery.date()) == "2020-01-05"  # 120 회복
    assert first.depth_pct == pytest.approx(-25.0)
    assert first.underwater_bars == 2  # 고점 아래 있던 봉(110, 90)
    assert first.recovery_bars == 2  # 저점 -> 회복

    assert second.recovered is False
    assert second.recovery is None
    assert second.recovery_bars is None
    assert second.depth_pct == pytest.approx(100 / 130 * 100 - 100)
    assert second.underwater_bars == 2

    assert stats.max_depth_pct == pytest.approx(-25.0)
    assert stats.time_under_water_frac == pytest.approx(4 / 8)
    assert stats.longest_underwater_bars == 2
    assert stats.unrecovered is True


def test_ulcer_index_penalizes_length_not_just_depth():
    """같은 깊이여도 오래 끌면 Ulcer가 커진다 — MDD는 둘을 구분하지 못한다."""
    idx = _daily_index(11)
    quick = pd.Series([100, 80] + [100] * 9, index=idx, dtype=float)
    slow = pd.Series([100] + [80] * 9 + [100], index=idx, dtype=float)

    q, s = drawdown_analytics(quick), drawdown_analytics(slow)
    assert q.max_depth_pct == pytest.approx(s.max_depth_pct)  # MDD는 같다
    assert s.ulcer_index > q.ulcer_index * 2  # Ulcer는 길이를 본다
    assert s.time_under_water_frac > q.time_under_water_frac


def test_curve_with_no_drawdown_has_zero_episodes():
    stats = drawdown_analytics(_exponential_curve(days=100))
    assert stats.n_episodes == 0
    assert stats.max_depth_pct == pytest.approx(0.0)
    assert stats.ulcer_index == pytest.approx(0.0)
    assert stats.time_under_water_frac == 0.0


# --- 4. 거래 부트스트랩 -------------------------------------------------------

def test_bootstrap_is_deterministic_under_fixed_seed():
    trades = _trades([1000.0, -500.0, 2000.0, -1500.0] * 10)
    a = trade_bootstrap(trades, start_equity=_START, n_paths=500, seed=42)
    b = trade_bootstrap(trades, start_equity=_START, n_paths=500, seed=42)
    c = trade_bootstrap(trades, start_equity=_START, n_paths=500, seed=7)
    assert a == b
    assert a.final_return_pct_median != c.final_return_pct_median


def test_all_positive_pnl_yields_no_negative_ending_paths():
    stats = trade_bootstrap(_trades([500.0] * 40), start_equity=_START, n_paths=500, seed=42)
    assert stats.sufficient
    assert stats.frac_negative == 0.0
    assert stats.mdd_pct_median == pytest.approx(0.0)  # 손실 거래가 없으니 낙폭도 없다
    assert stats.final_return_pct_median == pytest.approx(40 * 500 / _START * 100)


def test_bootstrap_mdd_rank_separates_lucky_from_unlucky_ordering():
    """같은 거래손익 주머니, 순서만 다름. 낙폭 백분위가 그 차이를 잡아내야 한다.

    백분위 = 부트스트랩 경로 중 관측보다 **더 깊은** 낙폭을 겪은 비율.
    높으면 실제 경로가 분포보다 얕게 지나갔다는 뜻(= 운이 좋았다).
    """
    pnl = [300.0] * 30 + [-200.0] * 10
    lucky = _trades([300.0, 300.0, 300.0, -200.0] * 10)  # 손실이 흩어져 낙폭이 쌓이지 않음
    unlucky = _trades(pnl)  # 손실 10건이 끝에 몰림 -> 낙폭이 한 번에 누적

    lucky_stats = trade_bootstrap(lucky, start_equity=_START, n_paths=2000, seed=42)
    unlucky_stats = trade_bootstrap(unlucky, start_equity=_START, n_paths=2000, seed=42)

    assert lucky_stats.observed_mdd_pct > unlucky_stats.observed_mdd_pct  # 더 얕다
    assert lucky_stats.observed_mdd_pct_rank > 90
    assert unlucky_stats.observed_mdd_pct_rank < 10
    # 재추출은 평균을 보존하므로 최종수익 백분위는 순서와 무관하게 구조적으로 50 근처다
    assert 35 < lucky_stats.observed_final_pct_rank < 65
    assert lucky_stats.observed_final_return_pct == pytest.approx(
        unlucky_stats.observed_final_return_pct
    )


def test_bootstrap_too_few_trades_is_insufficient():
    stats = trade_bootstrap(_trades([100.0] * 5), start_equity=_START)
    assert not stats.sufficient
    assert stats.n_trades == 5
    assert stats.n_paths == 0
    rendered = report.render_path_stats(
        compute_path_stats(_exponential_curve(days=400), _trades([100.0] * 5))
    )
    assert "표본 부족" in rendered


# --- 5. 일관성 ----------------------------------------------------------------

def test_monthly_and_yearly_consistency_on_exponential_curve():
    stats = consistency(_exponential_curve())
    assert stats.months_sufficient and stats.years_sufficient
    assert stats.n_months == 12 * _YEARS  # 2020-01-01 ~ 2023-12-31 = 정확히 48개월
    assert stats.frac_positive_months == 1.0
    assert stats.longest_losing_streak_months == 0
    assert stats.n_years == _YEARS
    assert stats.best_year.return_pct == pytest.approx(_GROWTH * 100, rel=0.05)


def test_losing_streak_counts_consecutive_negative_months():
    idx = pd.date_range("2021-01-31", periods=6, freq="ME")
    # 월말값: -10%, -10%, -10%, +10%, -10%, +10% -> 최장 연속손실 3개월
    values = [900_000.0, 810_000.0, 729_000.0, 801_900.0, 721_710.0, 793_881.0]
    curve = pd.Series(values, index=idx)
    curve = pd.concat([pd.Series([_START], index=[pd.Timestamp("2021-01-01")]), curve])
    stats = consistency(curve)
    assert stats.n_months == 6
    assert stats.longest_losing_streak_months == 3
    assert stats.frac_positive_months == pytest.approx(2 / 6)
    assert stats.worst_month.return_pct == pytest.approx(-10.0, rel=1e-6)


def test_single_month_curve_is_insufficient():
    stats = consistency(_exponential_curve(days=10))
    assert stats.n_months == 1
    assert not stats.months_sufficient
    assert not stats.years_sufficient


# --- 통합 + 렌더링 ------------------------------------------------------------

def test_compute_path_stats_and_render_are_complete_and_ordered():
    equity = _exponential_curve()
    trades = _trades([1000.0, -400.0] * 25)
    stats = compute_path_stats(equity, trades, n_paths=200, seed=42)
    assert stats.n_bars == len(equity)
    assert stats.n_trades == 50
    assert stats.span_years == pytest.approx(_YEARS, rel=0.01)

    text = report.render_path_stats(stats, title="donchian")
    assert "경로 통계: donchian" in text
    # 표본 크기가 모든 블록에 드러나야 한다
    assert f"봉 {stats.n_bars}개" in text and "라운드트립(매도) 50건" in text
    assert "원본 거래 50건" in text
    # 낙폭이 수익보다 먼저 온다(스킬 §4)
    assert text.index("1. 낙폭") < text.index("2. 롤링 창 수익 분포") < text.index("3. 로그자산 추세")
    # 집계 점수를 만들지 않는다
    assert "종합점수" not in text and "score" not in text.lower()


def test_render_path_report_matches_two_step_call():
    equity = _exponential_curve(days=400)
    trades = _trades([500.0, -100.0] * 20)
    assert report.render_path_report(equity, trades, title="x") == report.render_path_stats(
        compute_path_stats(equity, trades), title="x",
    )


def test_rejects_non_series_and_unsorted_input():
    with pytest.raises(TypeError):
        log_equity_trend(_exponential_curve().to_frame())
    unsorted = _exponential_curve(days=10).iloc[::-1]
    with pytest.raises(ValueError):
        drawdown_analytics(unsorted)
