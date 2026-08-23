"""`quant.analyze.foreign_flow_v2` — 외국인 수급 추종 v2(실무 규칙, 서브프로젝트 O).

순수 함수 — 네트워크 없음. 이 모듈은 원래 `quant/backtest/report_replay.py`에
있었다가(A/B 실측에서 이겨) `quant/analyze/`로 옮겨졌다 — 그 이동을 검증하는
회귀 테스트이기도 하다(라이브 스코어러 `intraday_score.py`도 여기서 같은
함수를 임포트한다).
"""
from __future__ import annotations

from datetime import date, timedelta

from quant.analyze.foreign_flow_v2 import (
    FOREIGN_V2_MAX,
    FOREIGN_V2_STREAK2_POINTS,
    FOREIGN_V2_STREAK3_POINTS,
    FOREIGN_V2_STRENGTH_HIGH_POINTS,
    FOREIGN_V2_STRENGTH_MED_POINTS,
    FOREIGN_V2_TANDEM_POINTS,
    FOREIGN_V2_TREND_ALIGN_POINTS,
    foreign_intensity_ratio,
    foreign_score_v2,
)

D = date(2026, 8, 15)


def _flow(d: date, foreign: float, inst: float = 0.0) -> dict:
    return {"date": d.isoformat(), "foreign_net": foreign, "inst_net": inst}


# ------------------------------------------------------------------ foreign_score_v2 — 쌍끌이

def test_foreign_score_v2_empty_series_is_zero():
    score, evidence = foreign_score_v2([], {})
    assert score == 0
    assert evidence == ["수급 시계열 없음"]


def test_foreign_score_v2_tandem_bonus_on():
    # 직전일 대량 이탈로 정합/연속매수 축은 죽여두고, 최근일 외국인+기관 동반
    # 순매수만 남겨 쌍끌이 가점을 단독으로 확인한다.
    series = [_flow(D - timedelta(days=2), -1000, 0), _flow(D - timedelta(days=1), 50, 30)]
    score, evidence = foreign_score_v2(series, {})
    assert score == FOREIGN_V2_TANDEM_POINTS
    assert any("쌍끌이" in e for e in evidence)


def test_foreign_score_v2_tandem_bonus_off_when_inst_not_positive():
    series = [_flow(D - timedelta(days=2), -1000, 0), _flow(D - timedelta(days=1), 50, -5)]
    score, evidence = foreign_score_v2(series, {})
    assert not any("쌍끌이" in e for e in evidence)
    assert score == 0


# ------------------------------------------------------------------ foreign_score_v2 — 연속 순매수일수

def test_foreign_score_v2_streak_three_or_more_days():
    series = [
        _flow(D - timedelta(days=4), -500, 0), _flow(D - timedelta(days=3), 5, 0),
        _flow(D - timedelta(days=2), 5, 0), _flow(D - timedelta(days=1), 5, 0),
    ]
    score, evidence = foreign_score_v2(series, {})
    assert score == FOREIGN_V2_STREAK3_POINTS
    assert any("연속 순매수 3일" in e for e in evidence)


def test_foreign_score_v2_streak_exactly_two_days():
    series = [
        _flow(D - timedelta(days=3), -500, 0), _flow(D - timedelta(days=2), 5, 0), _flow(D - timedelta(days=1), 5, 0),
    ]
    score, evidence = foreign_score_v2(series, {})
    assert score == FOREIGN_V2_STREAK2_POINTS
    assert any("연속 순매수 2일" in e for e in evidence)


def test_foreign_score_v2_streak_of_one_day_gives_no_streak_bonus():
    series = [_flow(D - timedelta(days=1), 5, 0)]
    score, evidence = foreign_score_v2(series, {})
    assert not any("연속 순매수" in e for e in evidence)
    # 1일치뿐이라 1·5·20일 창이 전부 같은 값이 되어 정합 보너스는 그대로 트리거된다(정직한 부작용).
    assert score == FOREIGN_V2_TREND_ALIGN_POINTS


# ------------------------------------------------------------------ foreign_score_v2 — 추세 정합(1·5·20일)

def test_foreign_score_v2_trend_alignment_all_windows_positive():
    # 20일 창 전체를 채운다 — 앞 19일 소폭 순매도, 마지막 날 큰 순매수로
    # 1/5/20일 누적 합이 모두 양수가 되도록. 연속매수 길이는 1일이라 스트릭
    # 가점과는 섞이지 않는다.
    series = [_flow(D - timedelta(days=20 - i), -1, 0) for i in range(19)]
    series.append(_flow(D - timedelta(days=1), 25, 0))
    score, evidence = foreign_score_v2(series, {})
    assert score == FOREIGN_V2_TREND_ALIGN_POINTS
    assert any("정합" in e for e in evidence)


def test_foreign_score_v2_trend_alignment_off_when_any_window_negative():
    series = [_flow(D - timedelta(days=2), -1000, 0), _flow(D - timedelta(days=1), 50, 0)]
    score, evidence = foreign_score_v2(series, {})
    assert not any("정합" in e for e in evidence)


# ------------------------------------------------------------------ foreign_score_v2 — 강도(|f_net|/거래량)

def test_foreign_score_v2_intensity_high_when_ratio_at_least_3pct():
    series = [_flow(D - timedelta(days=1), 310, 0)]  # ratio = 310/10000 = 3.1%
    bars = {D - timedelta(days=1): {"volume": 10000.0}}
    score, evidence = foreign_score_v2(series, bars)
    # 단일 1일 시계열이라 정합(+8)도 함께 트리거된다.
    assert score == FOREIGN_V2_TREND_ALIGN_POINTS + FOREIGN_V2_STRENGTH_HIGH_POINTS
    assert any("강도" in e for e in evidence)


def test_foreign_score_v2_intensity_medium_when_ratio_at_least_1pct():
    series = [_flow(D - timedelta(days=1), 150, 0)]  # ratio = 1.5%
    bars = {D - timedelta(days=1): {"volume": 10000.0}}
    score, evidence = foreign_score_v2(series, bars)
    assert score == FOREIGN_V2_TREND_ALIGN_POINTS + FOREIGN_V2_STRENGTH_MED_POINTS


def test_foreign_score_v2_intensity_below_threshold_no_bonus():
    series = [_flow(D - timedelta(days=1), 50, 0)]  # ratio = 0.5%
    bars = {D - timedelta(days=1): {"volume": 10000.0}}
    score, evidence = foreign_score_v2(series, bars)
    assert not any("강도" in e for e in evidence)
    assert score == FOREIGN_V2_TREND_ALIGN_POINTS


def test_foreign_score_v2_intensity_missing_bar_is_none_not_zero_disguised():
    series = [_flow(D - timedelta(days=1), 50, 0)]
    score, evidence = foreign_score_v2(series, {})  # 거래량 데이터 자체가 없음
    assert not any("강도" in e for e in evidence)
    assert score == FOREIGN_V2_TREND_ALIGN_POINTS


def test_foreign_intensity_ratio_none_on_empty_bars_by_date():
    """`bars_by_date={}` 는 호출부(intraday_verify.py 등 봉 캐시 없는 하네스)가
    정당하게 주는 값이다 — 예외가 아니라 정직한 None."""
    series = [_flow(D - timedelta(days=1), 50, 0)]
    assert foreign_intensity_ratio(series, {}) is None


# ------------------------------------------------------------------ foreign_score_v2 — 이탈 중(연속 매도 ≥2일)

def test_foreign_score_v2_exit_penalty_floors_at_zero_not_negative():
    series = [_flow(D - timedelta(days=2), -100, 0), _flow(D - timedelta(days=1), -50, 0)]
    score, evidence = foreign_score_v2(series, {})
    assert score == 0
    assert any("이탈" in e for e in evidence)


def test_foreign_score_v2_single_day_outflow_is_not_exit_streak():
    series = [_flow(D - timedelta(days=1), -50, 0)]
    score, evidence = foreign_score_v2(series, {})
    assert not any("이탈" in e for e in evidence)
    assert score == 0
    assert evidence == ["트리거된 규칙 없음"]


# ------------------------------------------------------------------ foreign_score_v2 — 순수성/결정론/no look-ahead

def test_foreign_score_v2_is_deterministic():
    series = [_flow(D - timedelta(days=2), -1000, 0), _flow(D - timedelta(days=1), 50, 30)]
    assert foreign_score_v2(series, {}) == foreign_score_v2(series, {})


def test_foreign_score_v2_ignores_future_bar_entries():
    # series 는 이미 D 미만으로 필터된 값(호출자 계약) — bars_by_date 에 D 당일(미래)
    # 항목이 섞여 있어도 마지막 관찰일(D-1)의 봉만 참조해야 한다.
    series = [_flow(D - timedelta(days=2), -1000, 0), _flow(D - timedelta(days=1), 310, 30)]
    bars_no_future = {D - timedelta(days=1): {"volume": 10000.0}}
    bars_with_future = {**bars_no_future, D: {"volume": 1.0}}
    score1, _ = foreign_score_v2(series, bars_no_future)
    score2, _ = foreign_score_v2(series, bars_with_future)
    assert score1 == score2


def test_foreign_score_v2_max_score_capped_at_nominal_max():
    score, _ = foreign_score_v2(
        [_flow(D - timedelta(days=2), 100, 100), _flow(D - timedelta(days=1), 200, 200)],
        {D - timedelta(days=1): {"volume": 100.0}},  # ratio 200% → 강도 고점
    )
    assert 0 <= score <= FOREIGN_V2_MAX
