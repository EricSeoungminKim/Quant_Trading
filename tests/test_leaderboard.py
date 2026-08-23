"""리더보드 승격 규칙 — Phase 7.5. 순수 함수, 네트워크·DB 없음.

## 이 파일이 막는 것: 표본을 잘못 세기

선정 원장은 하루에 76~123종목을 남긴다(2026-08-14 실측: 2거래일 199행). 그걸 n=199
로 세고 t검정을 돌리면 **거의 무엇이든 유의해진다.** 같은 날의 종목들은 같은 시장
움직임을 공유하기 때문이다 — 독립 관측이 아니다.

그래서 단위를 **"하루의 횡단면 순위 상관(rank IC)"** 으로 잡고, 표본 수는 **거래일
수**로 센다. 이러면 시장 전체가 오른 날의 공통 성분이 순위에서 상쇄되고, 남는 것이
"이 생산자가 그날 **어느 종목이 더 오를지** 순서를 맞췄나"다. 그게 우리가 알고 싶은
것이다.

## 문턱을 감으로 정하지 않는다

주식 횡단면의 일별 IC 표준편차는 대략 0.10~0.15다. IC 평균 0.05(약한 엣지)를
t=2 로 잡아내려면:

    t = mean / (sd / sqrt(n))  →  2 = 0.05 / (0.12 / sqrt(n))  →  n ≈ 23

그래서 **최소 20거래일**이다. 지금 2일이므로 어떤 승격 판정도 근거가 없다 —
그리고 이 모듈은 그 사실을 "판단 불가"로 **말해야** 한다(0을 "엣지 없음"으로,
우연을 "실력"으로 읽지 않는다).

## 다중검정

프롬프트·모델을 K개 시험하면 그중 최고는 **반드시** 좋아 보인다. 우연이다.
그래서 시행 횟수를 받아 요구 t를 올린다(Bonferroni). 시행 횟수를 세지 않는
리더보드는 우연을 실력으로 승격한다 — 계획서가 Phase 8 을 마지막에 둔 이유와 같다.
"""
from __future__ import annotations

import math

import pytest

from quant.control.leaderboard import (
    MIN_DAYS,
    daily_rank_ic,
    promotion_verdict,
    required_t,
    verdicts_from_ledger,
)


# ── 하루의 횡단면 순위 상관 ───────────────────────────────────────────────

def test_perfect_ranking_scores_one():
    scores = {"A": 3.0, "B": 2.0, "C": 1.0}
    returns = {"A": 300.0, "B": 200.0, "C": 100.0}

    assert daily_rank_ic(scores, returns) == pytest.approx(1.0)


def test_reversed_ranking_scores_minus_one():
    """**부호가 뒤집힌 생산자는 실력이 없는 게 아니라 반대로 유용하다** —
    그래도 승격 대상은 아니다. 판정은 promotion_verdict 가 한다."""
    scores = {"A": 3.0, "B": 2.0, "C": 1.0}
    returns = {"A": 100.0, "B": 200.0, "C": 300.0}

    assert daily_rank_ic(scores, returns) == pytest.approx(-1.0)


def test_ic_uses_ranks_not_levels():
    """순위 상관이라 척도가 달라도 같은 답이 나온다 — 생산자별 점수 척도가 다르기
    때문에 이게 필요하다(LLM 이 0~10 을, 스코어러가 0~100 을 낼 수 있다)."""
    returns = {"A": 300.0, "B": 200.0, "C": 100.0}
    a = daily_rank_ic({"A": 3.0, "B": 2.0, "C": 1.0}, returns)
    b = daily_rank_ic({"A": 100.0, "B": 50.0, "C": 1.0}, returns)

    assert a == pytest.approx(b)


def test_market_wide_move_cancels_out():
    """**이 테스트가 단위 선택의 근거다.**

    그날 전 종목이 +500bp 올라도 순위가 그대로면 IC 는 변하지 않는다. 시장 공통
    성분이 상쇄되므로, 남는 것이 "순서를 맞췄나"다. 수익률 평균을 쓰면 시장이 오른
    날에는 아무 생산자나 훌륭해 보인다.
    """
    scores = {"A": 3.0, "B": 2.0, "C": 1.0}
    flat = daily_rank_ic(scores, {"A": 300.0, "B": 200.0, "C": 100.0})
    lifted = daily_rank_ic(scores, {"A": 800.0, "B": 700.0, "C": 600.0})

    assert flat == pytest.approx(lifted)


def test_symbols_without_outcome_are_dropped_not_treated_as_zero():
    """수익률을 못 구한 종목을 0으로 넣으면 조회 실패가 "본전"으로 순위에 낀다.

    (종목을 4개로 두는 이유: 하나를 버려도 3개가 남아야 순위가 의미를 갖는다 —
    MIN_SYMBOLS_PER_DAY.)
    """
    scores = {"A": 4.0, "B": 3.0, "C": 2.0, "D": 1.0}
    returns = {"A": 400.0, "B": None, "C": 200.0, "D": 100.0}

    assert daily_rank_ic(scores, returns) == pytest.approx(1.0)


def test_too_few_overlapping_symbols_is_none():
    """2종목의 순위 상관은 항상 ±1 이다 — 정보가 아니다."""
    assert daily_rank_ic({"A": 2.0, "B": 1.0}, {"A": 200.0, "B": 100.0}) is None


def test_all_identical_scores_is_none_not_zero():
    """전 종목에 같은 점수를 준 생산자는 **순위를 만들지 않았다.**
    IC 0(맞췄지만 중립)과 구분해야 한다."""
    assert daily_rank_ic({"A": 1.0, "B": 1.0, "C": 1.0},
                         {"A": 300.0, "B": 200.0, "C": 100.0}) is None


# ── 요구 t (다중검정) ─────────────────────────────────────────────────────

def test_required_t_rises_with_the_number_of_trials():
    """K개를 시험하면 최고값은 우연히 부풀려진다 — 요구 t 가 올라가야 한다."""
    assert required_t(1) < required_t(10) < required_t(100)


def test_single_trial_requires_about_two_sigma():
    assert 1.9 < required_t(1) < 2.1


# ── 승격 판정 ─────────────────────────────────────────────────────────────

def _ic_days(n: int, mean: float, sd: float = 0.12) -> dict[str, float]:
    """평균 `mean`, 표준편차 ~`sd` 인 일별 IC.

    **상수 IC 를 쓰면 안 된다** — 분산이 0이면 t 가 무한이 되어 다중검정 보정이 통째로
    무력화된다(구현 중 실제로 그렇게 통과했다). 실데이터의 일별 IC 는 항상 흔들리므로
    부호를 번갈아 주는 결정론적 잡음을 쓴다(재현 가능, random 없음).
    """
    return {
        f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}": mean + (sd if i % 2 == 0 else -sd)
        for i in range(n)
    }


def test_zero_variance_ic_cannot_claim_significance():
    """**분산이 0이면 유의성을 계산할 수 없다.**

    구현 중 이 분기를 t=inf 로 뒀더니 요구 t 를 항상 넘겨 다중검정 보정이 통째로
    무력화됐다. 축퇴된 표본에서 무한 확신을 주장하면 안 된다.
    """
    ic = {f"2026-01-{i + 1:02d}": 0.5 for i in range(MIN_DAYS + 5)}

    v = promotion_verdict(ic, n_trials=1)

    assert v.promote is False
    assert v.verdict == "insufficient_sample"
    assert "분산" in v.reason


def test_below_the_day_floor_is_always_insufficient():
    """**지금 상태(2거래일)가 이 분기다.**

    IC 가 완벽해도 승격하지 않는다. 2일치로는 실력과 우연을 구분할 수 없고,
    거기서 승격하면 리더보드가 우연을 실력으로 읽는 장치가 된다.
    """
    v = promotion_verdict(_ic_days(2, 0.9), n_trials=1)

    assert v.verdict == "insufficient_sample"
    assert v.promote is False
    assert v.n_days == 2
    assert str(MIN_DAYS) in v.reason


def test_enough_days_but_no_edge_is_rejected():
    v = promotion_verdict(_ic_days(MIN_DAYS + 5, 0.0), n_trials=1)

    assert v.verdict == "no_edge"
    assert v.promote is False


def test_consistent_edge_over_enough_days_is_promoted():
    v = promotion_verdict(_ic_days(MIN_DAYS + 5, 0.08), n_trials=1)

    assert v.verdict == "promote"
    assert v.promote is True
    assert v.t_stat > required_t(1)


def test_the_same_edge_fails_when_many_variants_were_tried():
    """**시행 횟수를 세지 않으면 우연이 승격된다.**

    같은 IC 인데 100개를 시험했다면 최고값이 우연일 확률이 훨씬 높다.
    """
    ic = _ic_days(MIN_DAYS + 5, 0.08)

    assert promotion_verdict(ic, n_trials=1).promote is True
    assert promotion_verdict(ic, n_trials=100).promote is False


def test_negative_edge_is_never_promoted_even_if_significant():
    """부호가 반대로 유의한 생산자는 승격 대상이 아니다(뒤집어 쓰는 건 별개 판단)."""
    v = promotion_verdict(_ic_days(MIN_DAYS + 5, -0.20), n_trials=1)

    assert v.promote is False
    assert v.verdict == "no_edge"


def test_noisy_days_are_not_dropped_silently():
    """IC 를 못 구한 날(None)은 표본에서 빠지되 **몇 날이 빠졌는지 남긴다** —
    조용히 줄어든 표본으로 유의성을 주장하면 안 된다."""
    ic = dict(_ic_days(MIN_DAYS, 0.08))
    ic["2026-12-01"] = None

    v = promotion_verdict(ic, n_trials=1)

    assert v.n_days == MIN_DAYS
    assert v.n_days_dropped == 1


def test_verdict_carries_the_numbers_a_human_needs():
    """판정만 주고 숫자를 숨기면 사람이 검산할 수 없다.

    (거래일을 짝수로 두는 이유: 교대 부호 잡음이 정확히 상쇄돼 평균이 딱 mean 이 된다.
    홀수면 sd/n 만큼 밀린다 — 픽스처 산수이고 구현 문제가 아니다.)
    """
    v = promotion_verdict(_ic_days(MIN_DAYS + 6, 0.08), n_trials=3)

    assert v.mean_ic == pytest.approx(0.08)
    assert v.n_days == MIN_DAYS + 6
    assert v.required_t == pytest.approx(required_t(3))
    assert math.isfinite(v.t_stat)
    assert "IC" in v.reason


# ── verdicts_from_ledger: cmd_close_report/cmd_leaderboard 공유 추출 (2026-08-15 리뷰 I2) ──
#
# `quant/apps/cli.py` 의 `cmd_close_report`(904-924행)와 `cmd_leaderboard`
# (1055-1077행)가 각자 들고 있던 동일한 20줄(원장 두 벌 → 생산자별 일별 rank
# IC → 승격 판정)을 이 함수 하나로 뽑았다. 여기서는 그 산식이 원본 인라인
# 코드와 같은 결과를 내는지만 확인한다 — IC/판정 자체의 성질은 위 테스트들이
# 이미 검증한다.

def test_verdicts_from_ledger_computes_daily_ic_per_producer():
    sel_rows = [
        {"date": "2026-01-01", "symbol": "A", "outcome_d5_bps": 300.0},
        {"date": "2026-01-01", "symbol": "B", "outcome_d5_bps": 200.0},
        {"date": "2026-01-01", "symbol": "C", "outcome_d5_bps": 100.0},
    ]
    judgments = [
        {"producer": "watch_scorer", "producer_version": "2",
         "session_date": "2026-01-01", "symbol": "A", "score": 3.0},
        {"producer": "watch_scorer", "producer_version": "2",
         "session_date": "2026-01-01", "symbol": "B", "score": 2.0},
        {"producer": "watch_scorer", "producer_version": "2",
         "session_date": "2026-01-01", "symbol": "C", "score": 1.0},
    ]

    out = verdicts_from_ledger(sel_rows, judgments, horizon_days=5, n_trials=1)

    assert set(out) == {("watch_scorer", "2")}
    v, ic_by_day = out[("watch_scorer", "2")]
    assert ic_by_day == {"2026-01-01": pytest.approx(1.0)}
    assert v.verdict == "insufficient_sample"  # 거래일 1일 — MIN_DAYS 미달
    assert v.n_days == 1


def test_verdicts_from_ledger_keeps_producer_versions_separate_and_drops_unscored_judgments():
    """**같은 생산자의 다른 버전은 다른 표본이다** — v1/v2 가 섞이면 표본이
    오염된다(§C1 리뷰와 같은 원칙). score=None 인 판단("채점 못 함")은 표본에서
    빠져야 한다 — 그 버전에 유효한 판단이 하나도 없으면 결과에 나타나지 않는다."""
    sel_rows = [
        {"date": "2026-01-01", "symbol": "A", "outcome_d1_bps": 100.0},
        {"date": "2026-01-01", "symbol": "B", "outcome_d1_bps": 50.0},
        {"date": "2026-01-01", "symbol": "C", "outcome_d1_bps": 10.0},
    ]
    judgments = [
        {"producer": "watch_scorer", "producer_version": "1",
         "session_date": "2026-01-01", "symbol": "A", "score": None},
        {"producer": "watch_scorer", "producer_version": "2",
         "session_date": "2026-01-01", "symbol": "A", "score": 90},
        {"producer": "watch_scorer", "producer_version": "2",
         "session_date": "2026-01-01", "symbol": "B", "score": 50},
        {"producer": "watch_scorer", "producer_version": "2",
         "session_date": "2026-01-01", "symbol": "C", "score": 10},
    ]

    out = verdicts_from_ledger(sel_rows, judgments, horizon_days=1, n_trials=1)

    assert ("watch_scorer", "1") not in out
    assert ("watch_scorer", "2") in out
