"""`quant.analyze.entry_grade.entry_grade` — 진입 시점 5단계 결정론 등급
(서브프로젝트 W part 3)."""
from __future__ import annotations

from quant.analyze.entry_grade import EntryGrade, entry_grade


def _series(*foreign_and_inst: tuple[float, float]) -> list[dict]:
    return [
        {"date": f"2026-08-{i + 1:02d}", "symbol": "005930", "foreign_net": f, "inst_net": inst}
        for i, (f, inst) in enumerate(foreign_and_inst)
    ]


def test_bearish_veto_forces_grade_1_regardless_of_flows():
    series = _series((100, 100), (200, 200), (300, 300))
    result = entry_grade(series, bullish_hits=5, bearish_veto=True)
    assert result.grade == 1
    assert result.label == "관망"
    assert "veto" in result.reasons[0]


def test_consecutive_sell_2_days_is_grade_1():
    series = _series((100, 0), (-50, 0), (-30, 0))
    result = entry_grade(series)
    assert result.grade == 1
    assert result.label == "관망"


def test_data_under_3_days_is_grade_2():
    series = _series((100, 0), (50, 0))
    result = entry_grade(series)
    assert result.grade == 2
    assert result.label == "기다림"
    assert "데이터 부족" in result.reasons[0]


def test_empty_rows_is_grade_2():
    result = entry_grade([])
    assert result.grade == 2
    assert "데이터 부족" in result.reasons[0]


def test_neutral_no_clear_inflow_is_grade_2():
    series = _series((100, 0), (-50, 0), (0, 0))
    result = entry_grade(series)
    assert result.grade == 2


def test_single_day_exit_with_residual_is_grade_2():
    """foreign_trend 관망(잔여 있음) — 단발 이탈, 뚜렷한 유입 없음."""
    series = _series((100, 0), (-50, 0), (0, 0), (-10, 0))
    result = entry_grade(series)
    assert result.grade == 2


def test_recent_inflow_start_is_grade_3():
    series = _series((-50, 0), (-30, 0), (20, 0))
    result = entry_grade(series)
    assert result.grade == 3
    assert result.label == "관심"


def test_consecutive_buy_2_days_with_positive_cumulative_is_grade_4():
    series = _series((10, 0), (20, 0), (30, 0))
    result = entry_grade(series)
    assert result.grade == 4
    assert result.label == "분할 매수"


def test_tandem_buy_with_bullish_marker_is_grade_5():
    series = _series((10, 0), (20, 5), (30, 10))
    result = entry_grade(series, bullish_hits=1)
    assert result.grade == 5
    assert result.label == "적극 매수"
    assert any("쌍끌이" in r for r in result.reasons)


def test_streak_3_days_with_bullish_marker_is_grade_5():
    series = _series((10, 0), (20, -5), (30, -5))
    result = entry_grade(series, bullish_hits=2)
    assert result.grade == 5
    assert any("연속 순매수 3일" in r for r in result.reasons)


def test_streak_3_days_without_bullish_marker_stays_grade_4():
    """수급만으로는 5등급을 주지 않는다 — 호재 마커 없으면 4등급(분할 매수)에 머문다."""
    series = _series((10, 0), (20, -5), (30, -5))
    result = entry_grade(series, bullish_hits=0)
    assert result.grade == 4


def test_tandem_without_bullish_marker_stays_below_grade_5():
    series = _series((10, 0), (20, 5), (30, 10))
    result = entry_grade(series, bullish_hits=0)
    assert result.grade < 5


def test_returns_entry_grade_dataclass():
    result = entry_grade(_series((10, 0), (20, 0), (30, 0)))
    assert isinstance(result, EntryGrade)
