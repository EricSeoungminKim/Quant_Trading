"""`quant.analyze.foreign_trend.classify` — 외국인 수급 추종 라벨(서브프로젝트 I).

2026-08-17 사용자 지시(§외국인 수급 추종 규칙, `docs/superpowers/specs/
2026-08-17-foreign-flow-report-design.md`)의 8/3~8/6 예시를 결정론 시나리오
테스트로 그대로 인코딩한다. 시리즈는 date-asc `[{date, foreign_net, inst_net}]`.
"""
from __future__ import annotations

from quant.analyze.foreign_trend import classify


def _series(*foreign_and_inst: tuple[float, float]) -> list[dict]:
    return [
        {"date": f"2026-08-{i + 1:02d}", "foreign_net": f, "inst_net": inst}
        for i, (f, inst) in enumerate(foreign_and_inst)
    ]


def test_partial_exit_after_inflow_is_watch_residual():
    """+100억 → 익일 -50억: 잔여 자본이 남아 있다 — 관망(잔여 있음)."""
    series = _series((100, 0), (-50, 0))

    result = classify(series)

    assert result["label"] == "관망(잔여 있음)"
    assert result["residual"] == 50
    assert result["days"] == 2


def test_consecutive_outflow_is_outflow_trend():
    """+100, -50, -30, -20 연속 이탈 — 이탈 추세(부분 매도/중립 고려)."""
    series = _series((100, 0), (-50, 0), (-30, 0), (-20, 0))

    result = classify(series)

    assert result["label"] == "이탈 추세(부분 매도/중립 고려)"
    assert result["residual"] == 0
    assert result["days"] == 4


def test_reinflow_exceeding_prior_outflow_is_buy_signal():
    """+100, -50, +80, +40 — 재유입(120)이 직전 이탈(50)을 넘는다 — 매수 시그널(재유입)."""
    series = _series((100, 0), (-50, 0), (80, 0), (40, 0))

    result = classify(series)

    assert result["label"] == "매수 시그널(재유입)"
    assert result["residual"] == 170
    assert result["days"] == 4


def test_reinflow_not_exceeding_prior_outflow_is_watch_another_day():
    """+100, -50, +30 — 재유입(30)이 직전 이탈(50)을 못 넘는 교차 — 관망(하루 더)."""
    series = _series((100, 0), (-50, 0), (30, 0))

    result = classify(series)

    assert result["label"] == "관망(하루 더)"
    assert result["residual"] == 80
    assert result["days"] == 3


def test_insufficient_data_single_day_is_neutral_with_honest_days():
    series = _series((100, 0))

    result = classify(series)

    assert result["label"] == "중립"
    assert result["days"] == 1
    assert result["residual"] == 100


def test_insufficient_data_empty_series_is_neutral_with_zero_days():
    result = classify([])

    assert result["label"] == "중립"
    assert result["days"] == 0
    assert result["residual"] == 0
    assert result["inst_follows"] is False


def test_institution_alone_does_not_change_the_label():
    """기관 단독 유입은 어떤 라벨도 올리지 않는다(스펙 원칙: 기관은 노이즈).

    외국인은 연속 이탈(이탈 추세감)인데 기관이 그 기간 내내 강하게 순매수해도
    라벨은 여전히 외국인 흐름만으로 결정된 '이탈 추세'다."""
    series = [
        {"date": "2026-08-01", "foreign_net": 100, "inst_net": 500},
        {"date": "2026-08-02", "foreign_net": -50, "inst_net": 500},
        {"date": "2026-08-03", "foreign_net": -30, "inst_net": 500},
        {"date": "2026-08-04", "foreign_net": -20, "inst_net": 500},
    ]

    result = classify(series)

    assert result["label"] == "이탈 추세(부분 매도/중립 고려)"
    # 최근일(마지막 행)이 외국인 이탈이므로 '유입일'이 아니다 — 기관이 아무리
    # 사도 따라붙을 유입 자체가 없다.
    assert result["inst_follows"] is False


def test_inst_follows_true_when_institution_co_buys_on_latest_inflow_day():
    series = _series((100, -500), (-50, -500), (80, 300), (40, 300))

    result = classify(series)

    assert result["label"] == "매수 시그널(재유입)"
    assert result["inst_follows"] is True


def test_inst_follows_false_when_latest_day_is_foreign_outflow_even_if_inst_buys():
    series = _series((100, 0), (-50, 300))

    result = classify(series)

    assert result["label"] == "관망(잔여 있음)"
    assert result["inst_follows"] is False


def test_continuous_inflow_with_no_reversal_is_buy_signal():
    """처음부터 계속 유입(이탈 자체가 없음) — 재유입 대상이 없어도 매수 시그널."""
    series = _series((10, 0), (20, 0), (30, 0))

    result = classify(series)

    assert result["label"] == "매수 시그널(재유입)"


def test_continuous_outflow_with_no_prior_inflow_is_outflow_trend():
    series = _series((-10, 0), (-20, 0), (-30, 0))

    result = classify(series)

    assert result["label"] == "이탈 추세(부분 매도/중립 고려)"


def test_classify_is_deterministic():
    series = _series((100, 0), (-50, 0), (80, 0), (40, 0))

    assert classify(series) == classify(series)
