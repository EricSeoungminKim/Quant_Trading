"""다중검정 보정 통계 — 알려진 값과 항등식으로 검증한다.

이 모듈이 틀리면 **틀린 방향이 정해져 있다**: 관대해진다. 그래서 여기서 지키는
것은 "숫자가 나온다"가 아니라

① 탐색 횟수를 늘리면 기준선이 반드시 올라가고 DSR은 반드시 내려간다
② n_trials=1 이면 보정이 꺼져 PSR과 정확히 같아진다 (경계 규약)
③ 표본을 못 믿을 이유(작은 n, 두꺼운 꼬리)는 확률을 반드시 낮춘다
④ 샤프가 기준선 이하면 MinTRL은 큰 수가 아니라 ∞다 ("조금만 더 모으면"이 아니다)
"""
from __future__ import annotations

import math

import pytest

from quant.backtest.statistics import (
    deflated_sharpe,
    expected_max_sharpe,
    min_track_record_length,
    probabilistic_sharpe,
    to_per_observation,
)


# ── PSR ────────────────────────────────────────────────────────────────────

def test_psr_at_benchmark_is_half():
    """샤프가 벤치마크와 정확히 같으면 "더 크다"일 확률은 50%다."""
    assert probabilistic_sharpe(0.2, 0.2, 100) == pytest.approx(0.5)


def test_psr_rises_with_sample_size():
    """같은 샤프라도 표본이 많을수록 더 믿을 만하다."""
    small = probabilistic_sharpe(0.1, 0.0, 30)
    large = probabilistic_sharpe(0.1, 0.0, 500)
    assert 0.5 < small < large < 1.0


def test_psr_penalises_fat_left_tail():
    """음의 왜도 + 두꺼운 꼬리는 같은 샤프의 신뢰도를 떨어뜨린다.

    정규 가정이 낙관 쪽이라는 것을 고정한다 — 우리 수익률 분포는 정규가 아니다.
    """
    normal = probabilistic_sharpe(0.15, 0.0, 200, skew=0.0, kurtosis=3.0)
    fat = probabilistic_sharpe(0.15, 0.0, 200, skew=-1.5, kurtosis=8.0)
    assert fat < normal


def test_psr_rejects_single_observation():
    with pytest.raises(ValueError):
        probabilistic_sharpe(0.5, 0.0, 1)


def test_psr_rejects_impossible_moments():
    """분산항이 0 이하가 되는 왜도/첨도 조합은 조용히 통과시키지 않는다."""
    with pytest.raises(ValueError):
        probabilistic_sharpe(1.0, 0.0, 100, skew=5.0, kurtosis=1.0)


# ── E[max SR] ──────────────────────────────────────────────────────────────

def test_expected_max_sharpe_is_zero_for_single_trial():
    """탐색하지 않았으면 깎을 것도 없다 — N=1은 0 (Φ⁻¹(0) 발산의 경계 규약)."""
    assert expected_max_sharpe(1, 490) == 0.0


def test_expected_max_sharpe_grows_with_trials():
    baseline = expected_max_sharpe(2, 490)
    assert baseline < expected_max_sharpe(10, 490) < expected_max_sharpe(1000, 490)


def test_expected_max_sharpe_shrinks_with_more_observations():
    """관측이 많을수록 우연히 높은 샤프가 나오기 어렵다(V ≈ 1/n)."""
    assert expected_max_sharpe(100, 2000) < expected_max_sharpe(100, 490)


def test_expected_max_sharpe_our_actual_numbers():
    """우리 실제 규모(관측 490일 · 시험 100회)의 기준선 — docstring의 예시와 같은 값.

    **엣지가 0인 전략 100개를 490일 창에서 돌리면 그중 최고는 연율 샤프 1.8쯤이
    나온다.** 이 테스트는 그 사실을 문서가 아니라 코드로 고정한다.
    """
    per_obs = expected_max_sharpe(100, 490)
    assert per_obs == pytest.approx(0.1143, abs=5e-4)
    assert per_obs * math.sqrt(252) == pytest.approx(1.81, abs=0.02)


def test_expected_max_sharpe_uses_given_trial_variance():
    """시험한 샤프들의 실제 분산을 알면 그걸 쓴다(근사 1/n 보다 정확하다)."""
    approx = expected_max_sharpe(50, 100)                      # V = 1/100
    measured = expected_max_sharpe(50, 100, trial_variance=0.04)  # V = 0.04 > 0.01
    assert measured > approx


# ── DSR ────────────────────────────────────────────────────────────────────

def test_deflated_equals_probabilistic_when_one_trial():
    """탐색 1회면 DSR은 PSR(vs 0)과 **정확히** 같다 — 보정이 꺼진 상태."""
    for sharpe in (0.05, 0.1, 0.3):
        assert deflated_sharpe(sharpe, 1, 490) == pytest.approx(
            probabilistic_sharpe(sharpe, 0.0, 490)
        )


def test_deflated_falls_as_trials_rise():
    """같은 성과라도 더 많이 뒤져서 찾았으면 덜 믿는다 — 이 모듈의 존재 이유."""
    sharpe = 0.12
    values = [deflated_sharpe(sharpe, n, 490) for n in (1, 5, 50, 500)]
    assert values == sorted(values, reverse=True)
    assert values[0] > 0.9 and values[-1] < 0.5


def test_deflated_kills_a_lucky_looking_sharpe():
    """우연의 기준선과 같은 샤프는 DSR 0.5 — "50:50"이지 성과가 아니다."""
    n_trials, n_obs = 100, 490
    lucky = expected_max_sharpe(n_trials, n_obs)
    assert deflated_sharpe(lucky, n_trials, n_obs) == pytest.approx(0.5)


# ── MinTRL ─────────────────────────────────────────────────────────────────

def test_min_track_record_length_is_infinite_below_benchmark():
    """기준선 이하 샤프는 표본을 늘려도 유의해지지 않는다 — 큰 수가 아니라 ∞."""
    assert min_track_record_length(0.05, benchmark=0.05) == math.inf
    assert min_track_record_length(-0.2, benchmark=0.0) == math.inf


def test_min_track_record_length_inverts_psr():
    """MinTRL 만큼 관측을 모으면 PSR이 정확히 그 신뢰수준이 된다(역함수 관계)."""
    sharpe, conf = 0.1, 0.95
    n = min_track_record_length(sharpe, 0.0, confidence=conf)
    assert probabilistic_sharpe(sharpe, 0.0, round(n)) == pytest.approx(conf, abs=1e-3)


def test_min_track_record_length_grows_as_sharpe_shrinks():
    assert min_track_record_length(0.3) < min_track_record_length(0.1)


def test_min_track_record_length_rejects_bad_confidence():
    with pytest.raises(ValueError):
        min_track_record_length(0.2, confidence=1.0)


# ── 단위 변환 ──────────────────────────────────────────────────────────────

def test_to_per_observation_roundtrip():
    """연율 샤프를 그대로 넣는 것이 이 파일에서 가장 쉬운 자멸 경로다."""
    assert to_per_observation(1.81, 252) == pytest.approx(0.114, abs=1e-3)


def test_to_per_observation_rejects_nonpositive_periods():
    with pytest.raises(ValueError):
        to_per_observation(1.0, 0)
