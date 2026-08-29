"""파라미터 자동 반영 거버너 — 방어층이 실제로 막는지 고정한다.

사용자 결정(2026-08-13): 자동 반영을 하되 "한번에 크게 망할 수 있으니 방어층을
두껍게". 이 테스트들은 각 층이 **무엇을 막는지**를 하나씩 못 박는다.

2026-08-30 재정의: ALLOWED 는 이제 config/settings.yaml 의 실제 경로다(이전
7개는 코드 어디에도 없는 이름이었다 — 2026-08-28 실측, tests/test_governor_wiring.py
의 스키마 테스트가 재발을 막는다). 그리고 방향 제약이 새로 생겼다
(raise_only/lower_only) — "자동 반영은 리스크를 줄이는 방향만, 나머지는 전부
제안"(소유자 승인). 이 파일은 실제로 등재된 이름(vol_breakout min_stop_bp,
cold_fetch_budget_per_cycle)을 그대로 써서 가짜 이름으로 테스트만 통과하는
함정을 피한다.
"""
from datetime import date

import pytest

from quant.control.governor import (
    ALLOWED,
    FORBIDDEN,
    MAX_CHANGES_PER_RUN,
    MIN_SAMPLES,
    Proposal,
    decide,
    evaluate,
    record,
    rollback_candidates,
    summary,
)

TODAY = date(2026, 8, 30)

RAISE_ONLY = "strategies.vol_breakout.params.min_stop_bp"  # (raise_only, 40, 120, 20, 3)
LOWER_ONLY = "engine.cold_fetch_budget_per_cycle"           # (lower_only, 4, 8, 2, 3)


def _p(name=RAISE_ONLY, current=40, proposed=60, samples=50, improvement=0.20):
    return Proposal(name=name, current=current, proposed=proposed,
                    samples=samples, expected_improvement=improvement)


# --- 층 0: 폭발 반경 — 가장 중요한 층 ---

@pytest.mark.parametrize("name", sorted(FORBIDDEN))
def test_risk_parameters_can_never_be_auto_applied(name):
    """사이징을 잘못 키우면 계좌가 날아가고 되돌릴 자산이 남지 않는다.

    근거가 아무리 좋아도(표본 999건, 개선 99%) 문 자체를 열지 않는다.
    """
    d = evaluate(_p(name=name, current=1, proposed=2, samples=999, improvement=0.99), TODAY, [])
    assert not d.accepted
    assert d.layer == "0-blast-radius"


def test_forbidden_and_allowed_never_overlap():
    """실수로 ALLOWED 에 리스크 파라미터를 넣는 것을 여기서 잡는다."""
    assert not (FORBIDDEN & set(ALLOWED)), "리스크 파라미터가 자동 반영 목록에 들어갔다"


def test_unknown_parameter_is_rejected():
    d = evaluate(_p(name="아무거나"), TODAY, [])
    assert not d.accepted and d.layer == "0-blast-radius"


# --- 층 0b: 방향 — "리스크를 줄이는 방향만 자동, 나머지는 제안" ---

def test_lowering_a_raise_only_parameter_is_rejected():
    """min_stop_bp 는 raise_only — 손절을 좁히는(리스크를 키우는) 제안은 거부."""
    d = evaluate(_p(name=RAISE_ONLY, current=60, proposed=40, improvement=0.5), TODAY, [])
    assert not d.accepted and d.layer == "0-direction"


def test_raising_a_lower_only_parameter_is_rejected():
    """cold_fetch_budget_per_cycle 은 lower_only — 예산을 늘리는 제안은 거부."""
    d = evaluate(_p(name=LOWER_ONLY, current=6, proposed=8, improvement=0.5), TODAY, [])
    assert not d.accepted and d.layer == "0-direction"


def test_correct_direction_passes_the_direction_layer():
    d = evaluate(_p(name=RAISE_ONLY, current=40, proposed=60, improvement=0.5), TODAY, [])
    assert d.accepted


def test_no_op_proposal_does_not_violate_direction():
    """proposed == current 는 어느 방향으로도 위반이 아니다(그냥 움직이지 않는다)."""
    d = evaluate(_p(name=RAISE_ONLY, current=40, proposed=40, improvement=0.5), TODAY, [])
    assert d.accepted


# --- 층 1: 봉투 ---

def test_value_outside_envelope_is_rejected():
    d = evaluate(_p(current=40, proposed=999), TODAY, [])
    assert not d.accepted and d.layer in ("1-envelope", "2-step-limit")


def test_envelope_is_checked_even_for_a_small_step():
    """상한이 120인데 현재가 120이면 121로 한 칸 움직이는 것도 막혀야 한다."""
    d = evaluate(_p(current=120, proposed=121), TODAY, [])
    assert not d.accepted and d.layer == "1-envelope"


# --- 층 2: 보폭 ---

def test_large_jump_is_clamped_not_rejected():
    """40 → 100 같은 도약을 막되, 방향이 맞으면 한 칸(max_step=20)은 간다."""
    d = evaluate(_p(current=40, proposed=100, improvement=0.5), TODAY, [])
    assert d.accepted
    assert d.applied_value == 60  # max_step 20
    assert d.layer == "2-step-limit"


def test_wildly_out_of_range_proposal_is_rejected_not_clamped():
    """봉투 밖은 잘라서 반영하지 않고 **거부**한다.

    보폭 제한(층 2)은 "방향은 맞는데 너무 크다"를 다루는 층이고, 봉투(층 1)는
    "애초에 있을 수 없는 값"을 다루는 층이다. 터무니없는 제안을 조용히 sane 한
    값으로 깎아 반영하면 **하네스가 잘못 보정됐다는 신호가 사라진다.**
    """
    d = evaluate(_p(current=100, proposed=500), TODAY, [])
    assert not d.accepted
    assert d.layer == "1-envelope"
    assert d.applied_value is None


def test_in_envelope_but_too_big_a_step_is_clamped():
    """층 1과 층 2의 역할 분담을 못 박는다 — 봉투 안이면 깎아서 한 칸 간다."""
    _, lo, hi, max_step, _ = ALLOWED[RAISE_ONLY]
    d = evaluate(_p(current=40, proposed=int(hi), improvement=0.5), TODAY, [])
    assert d.accepted and d.applied_value == 40 + max_step


# --- 층 3: 냉각 ---

def test_same_parameter_cannot_change_twice_within_cooldown():
    """매일 흔들면 그 파라미터의 성과를 영영 측정할 수 없다."""
    history = [{"date": "2026-08-28", "name": RAISE_ONLY, "accepted": True}]
    d = evaluate(_p(), TODAY, history)
    assert not d.accepted and d.layer == "3-cooldown"


def test_cooldown_expires():
    history = [{"date": "2026-08-01", "name": RAISE_ONLY, "accepted": True}]
    assert evaluate(_p(), TODAY, history).accepted


def test_rejected_attempts_do_not_start_a_cooldown():
    """거부는 변경이 아니다 — 거부가 냉각을 걸면 한 번 거부당한 파라미터가 묶인다."""
    history = [{"date": "2026-08-29", "name": RAISE_ONLY, "accepted": False}]
    assert evaluate(_p(), TODAY, history).accepted


# --- 층 4: 증거 ---

def test_small_sample_is_rejected():
    d = evaluate(_p(samples=MIN_SAMPLES - 1), TODAY, [])
    assert not d.accepted and d.layer == "4-evidence"


def test_marginal_improvement_is_not_worth_shaking_the_system():
    d = evaluate(_p(improvement=0.01), TODAY, [])
    assert not d.accepted and d.layer == "4-evidence"


# --- 층 5: 동시 변경 상한 ---

def test_only_one_parameter_changes_per_run():
    """둘을 같이 바꾸면 어느 쪽이 효과였는지 영영 모른다."""
    props = [_p(name=RAISE_ONLY, improvement=0.30),
             _p(name=LOWER_ONLY, current=8, proposed=6, improvement=0.25),
             _p(name="strategies.gap_fade.params.min_stop_bp", improvement=0.20)]
    out = decide(props, TODAY, ledger_path=date and __import__("pathlib").Path("/nonexistent"))
    assert sum(1 for d in out if d.accepted) == MAX_CHANGES_PER_RUN


def test_the_strongest_proposal_wins_the_single_slot():
    props = [_p(name=RAISE_ONLY, improvement=0.10),
             _p(name=LOWER_ONLY, current=8, proposed=6, improvement=0.40)]
    out = decide(props, TODAY, __import__("pathlib").Path("/nonexistent"))
    accepted = [d for d in out if d.accepted]
    assert len(accepted) == 1 and accepted[0].proposal.name == LOWER_ONLY


# --- 층 6: 자동 롤백 ---

def test_a_change_that_made_things_worse_is_flagged_for_rollback():
    """하네스의 가설이 틀릴 수 있다는 전제가 이 층의 존재 이유다."""
    hist = [{"name": RAISE_ONLY, "accepted": True, "applied": 60},
            {"name": LOWER_ONLY, "accepted": True, "applied": 6}]
    out = rollback_candidates(hist, {RAISE_ONLY: -0.25, LOWER_ONLY: +0.10})
    assert [r["name"] for r in out] == [RAISE_ONLY]


def test_rejected_changes_are_never_rollback_candidates():
    hist = [{"name": RAISE_ONLY, "accepted": False, "applied": None}]
    assert rollback_candidates(hist, {RAISE_ONLY: -0.9}) == []


# --- 감사 로그 ---

def test_rejections_are_recorded_too(tmp_path):
    """방어층이 실제로 일하는지는 거부 기록으로만 확인할 수 있다."""
    path = tmp_path / "decisions.jsonl"
    decisions = [evaluate(_p(name="capital_fraction"), TODAY, []),
                 evaluate(_p(), TODAY, [])]
    assert record(decisions, TODAY, path) == 2
    rows = [__import__("json").loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert [r["accepted"] for r in rows] == [False, True]
    assert rows[0]["layer"] == "0-blast-radius"


def test_summary_names_the_blocking_layer(tmp_path):
    text = summary([evaluate(_p(name="stop_loss_pct"), TODAY, [])])
    assert "0-blast-radius" in text and "stop_loss_pct" in text
