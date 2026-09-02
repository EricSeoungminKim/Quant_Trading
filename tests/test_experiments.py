"""자동 판정 루프(`quant/control/experiments.py`) — 2026-08-24.

지켜야 하는 계약:
1. **변경을 사람 규율에 의존하지 않고 감지한다** (지문 비교, 멱등).
2. **관심종목 변경은 파라미터 변경이 아니다** — 매일 바뀌므로 포함하면 판정이
   영원히 리셋된다.
3. **표본이 안 차면 판정하지 않는다** — pending 이 기본값이고 알림도 안 나간다.
4. **장세를 대조군으로 분리한다** — 순진한 전후 비교는 장이 좋아진 것과 내
   변경이 좋았던 것을 구분하지 못한다.
5. **나쁜 결과를 숨기지 않는다** — 악화도 개선과 같은 경로로 나간다.
"""
from __future__ import annotations

from datetime import date

import pytest

from quant.control.experiments import (
    KILL_P_THRESHOLD, consecutive_dead_candidates, daily_report, death_watch,
    did_compare, load_changes, params_fingerprint, pending_experiments,
    permutation_p, record_death_watch, record_fingerprints, split_trips, verdict,
)

D = date(2026, 8, 24)


def _cfg(**params):
    return {"class": "scalp_1m", "enabled": True, "params": params}


def _trip(strategy, day, bps):
    return {"strategy": strategy, "bps": bps, "exit_ts": f"{day}T06:00:00+00:00"}


# ── 1. 변경 감지 ────────────────────────────────────────────────────────

def test_fingerprint_ignores_symbols_because_watchlist_changes_daily():
    """관심종목은 매일 바뀐다 — 지문에 넣으면 매일이 '변경'이 되어 판정 불가."""
    a = {**_cfg(take_profit_bps=100), "symbols": ["005930"]}
    b = {**_cfg(take_profit_bps=100), "symbols": ["005930", "000660", "TQQQ"]}
    assert params_fingerprint(a) == params_fingerprint(b)


def test_fingerprint_changes_when_params_or_enabled_change():
    base = _cfg(take_profit_bps=100)
    assert params_fingerprint(base) != params_fingerprint(_cfg(take_profit_bps=150))
    disabled = {**base, "enabled": False}
    assert params_fingerprint(base) != params_fingerprint(disabled)


def test_first_run_records_baseline_and_does_not_create_experiments(tmp_path):
    p = tmp_path / "changes.jsonl"
    added = record_fingerprints({"scalp_1m": _cfg(x=1)}, D, p)
    assert len(added) == 1 and added[0]["baseline"] is True
    assert pending_experiments(load_changes(p), D) == []


def test_second_run_with_same_params_writes_nothing(tmp_path):
    p = tmp_path / "changes.jsonl"
    cfg = {"scalp_1m": _cfg(x=1)}
    record_fingerprints(cfg, D, p)
    assert record_fingerprints(cfg, D, p) == []
    assert len(load_changes(p)) == 1


def test_param_change_creates_an_experiment(tmp_path):
    p = tmp_path / "changes.jsonl"
    record_fingerprints({"scalp_1m": _cfg(x=1)}, date(2026, 8, 1), p)
    record_fingerprints({"scalp_1m": _cfg(x=2)}, date(2026, 8, 22), p)
    exps = pending_experiments(load_changes(p), D)
    assert len(exps) == 1
    assert exps[0]["change_date"] == "2026-08-22"
    assert exps[0]["prev_date"] == "2026-08-01"
    assert exps[0]["superseded"] is False


def test_a_later_change_supersedes_the_earlier_experiment(tmp_path):
    """다음 변경 이후 데이터는 다른 실험 것이다 — 섞으면 둘 다 못 읽는다."""
    p = tmp_path / "changes.jsonl"
    record_fingerprints({"s": _cfg(x=1)}, date(2026, 8, 1), p)
    record_fingerprints({"s": _cfg(x=2)}, date(2026, 8, 10), p)
    record_fingerprints({"s": _cfg(x=3)}, date(2026, 8, 20), p)
    exps = pending_experiments(load_changes(p), D)
    assert [e["change_date"] for e in exps] == ["2026-08-10", "2026-08-20"]
    assert exps[0]["superseded"] is True
    assert exps[1]["superseded"] is False


def test_corrupt_line_does_not_break_loading(tmp_path):
    p = tmp_path / "changes.jsonl"
    p.write_text('{"strategy":"a","date":"2026-08-01","fingerprint":"x"}\n깨진줄\n',
                 encoding="utf-8")
    assert len(load_changes(p)) == 1


# ── 2. 전후 분할 ────────────────────────────────────────────────────────

def test_change_day_trips_count_as_after_because_deploy_is_post_close():
    trips = [_trip("s", "2026-08-21", 10), _trip("s", "2026-08-22", 20)]
    before, after = split_trips(trips, "s", "2026-08-22")
    assert before == [10.0] and after == [20.0]


def test_split_drops_data_older_than_the_previous_change():
    trips = [_trip("s", "2026-08-01", 1), _trip("s", "2026-08-15", 2),
             _trip("s", "2026-08-25", 3)]
    before, after = split_trips(trips, "s", "2026-08-20", since="2026-08-10")
    assert before == [2.0] and after == [3.0]


def test_split_ignores_other_strategies():
    trips = [_trip("s", "2026-08-25", 1), _trip("other", "2026-08-25", 999)]
    _, after = split_trips(trips, "s", "2026-08-20")
    assert after == [1.0]


# ── 3. 이중차분 — 장세 분리 ─────────────────────────────────────────────

def test_did_subtracts_market_move_captured_by_control_group():
    """변경군이 +50 올랐는데 대조군도 +50 올랐다면 순효과는 0이다 (장세)."""
    trips = []
    trips += [_trip("treated", "2026-08-01", 0) for _ in range(10)]
    trips += [_trip("treated", "2026-08-25", 50) for _ in range(10)]
    trips += [_trip("ctl", "2026-08-01", 0) for _ in range(10)]
    trips += [_trip("ctl", "2026-08-25", 50) for _ in range(10)]

    cmp = did_compare(trips, "treated", "2026-08-20", control_strategies=["ctl"])
    assert cmp["treated_delta"] == pytest.approx(50.0)
    assert cmp["control_delta"] == pytest.approx(50.0)
    assert cmp["did"] == pytest.approx(0.0)


def test_did_credits_the_change_when_control_did_not_move():
    trips = []
    trips += [_trip("treated", "2026-08-01", 0) for _ in range(10)]
    trips += [_trip("treated", "2026-08-25", 40) for _ in range(10)]
    trips += [_trip("ctl", "2026-08-01", 0) for _ in range(10)]
    trips += [_trip("ctl", "2026-08-25", 0) for _ in range(10)]

    cmp = did_compare(trips, "treated", "2026-08-20", control_strategies=["ctl"])
    assert cmp["did"] == pytest.approx(40.0)


def test_did_is_none_without_a_control_sample():
    trips = [_trip("treated", "2026-08-01", 0), _trip("treated", "2026-08-25", 40)]
    cmp = did_compare(trips, "treated", "2026-08-20", control_strategies=[])
    assert cmp["did"] is None


# ── 4. 순열검정 + 판정 ──────────────────────────────────────────────────

def test_permutation_needs_five_per_side():
    assert permutation_p([1.0] * 4, [2.0] * 9) is None
    assert permutation_p([1.0] * 9, [2.0] * 4) is None


def test_permutation_is_deterministic_for_the_same_input():
    a, b = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0], [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
    assert permutation_p(a, b) == permutation_p(a, b)


def test_permutation_detects_a_large_separation():
    p = permutation_p([0.0] * 20, [500.0] * 20)
    assert p is not None and p < 0.01


def test_permutation_reports_noise_when_groups_overlap():
    a = [float(i % 10) for i in range(30)]
    b = [float((i + 3) % 10) for i in range(30)]
    p = permutation_p(a, b)
    assert p is not None and p > 0.10


def _cmp(nb, na, did, p):
    return {"strategy": "s", "change_date": "2026-08-22", "n_before": nb, "n_after": na,
            "did": did, "p_value": p, "mean_before": 0.0, "mean_after": did,
            "treated_delta": did, "control_delta": 0.0,
            "control_n_before": 50, "control_n_after": 50, "control_strategies": ["ctl"]}


def test_verdict_is_pending_below_min_sample_on_either_side():
    assert verdict(_cmp(29, 50, 40.0, 0.01))[0] == "pending"
    assert verdict(_cmp(50, 29, 40.0, 0.01))[0] == "pending"


def test_verdict_improved_worsened_and_no_effect():
    assert verdict(_cmp(50, 50, 40.0, 0.01))[0] == "improved"
    assert verdict(_cmp(50, 50, -40.0, 0.01))[0] == "worsened"
    assert verdict(_cmp(50, 50, 40.0, 0.40))[0] == "no_effect"


def test_verdict_pending_when_control_missing_even_with_enough_samples():
    """대조군이 없으면 장세를 못 뺀다 — 표본이 많아도 판정하지 않는다."""
    assert verdict(_cmp(50, 50, None, 0.01))[0] == "pending"


# ── 5. 사망 감시 ────────────────────────────────────────────────────────

def test_death_watch_flags_significantly_negative_strategy():
    trips = [_trip("bad", "2026-08-10", -80.0) for _ in range(40)]
    out = death_watch(trips)
    assert len(out) == 1 and out[0]["strategy"] == "bad"
    assert out[0]["p_value"] <= 0.05


def test_death_watch_ignores_small_samples():
    trips = [_trip("bad", "2026-08-10", -80.0) for _ in range(10)]
    assert death_watch(trips) == []


def test_death_watch_ignores_positive_strategies():
    trips = [_trip("good", "2026-08-10", 40.0) for _ in range(40)]
    assert death_watch(trips) == []


def test_death_watch_ignores_noisy_negative_that_is_not_significant():
    """부호만 보면 매주 다른 답이 나온다 — 유의성을 요구한다."""
    vals = [-500.0, 480.0] * 20 + [-1.0] * 4  # 평균은 살짝 음수, 분산이 거대
    trips = [_trip("noisy", "2026-08-10", v) for v in vals]
    assert death_watch(trips) == []


# ── 6. 일일 리포트 — 조용한 기본값 ──────────────────────────────────────

def test_daily_report_is_silent_when_nothing_is_decided(tmp_path):
    """매일 '아직 모릅니다'를 보내면 사람이 안 읽고, 진짜 경보까지 묻힌다."""
    p = tmp_path / "c.jsonl"
    record_fingerprints({"s": _cfg(x=1)}, date(2026, 8, 1), p)
    record_fingerprints({"s": _cfg(x=2)}, date(2026, 8, 22), p)
    trips = [_trip("s", "2026-08-25", 10.0) for _ in range(3)]
    msg, settled = daily_report(trips, load_changes(p), D)
    assert msg is None and settled == []


def test_daily_report_speaks_when_a_verdict_lands(tmp_path):
    p = tmp_path / "c.jsonl"
    record_fingerprints({"s": _cfg(x=1), "ctl": _cfg(y=1)}, date(2026, 8, 1), p)
    record_fingerprints({"s": _cfg(x=2), "ctl": _cfg(y=1)}, date(2026, 8, 20), p)

    trips = []
    trips += [_trip("s", "2026-08-10", -100.0 + i) for i in range(35)]
    trips += [_trip("s", "2026-08-25", 100.0 + i) for i in range(35)]
    trips += [_trip("ctl", "2026-08-10", 0.0 + i) for i in range(35)]
    trips += [_trip("ctl", "2026-08-25", 0.0 + i) for i in range(35)]

    msg, settled = daily_report(trips, load_changes(p), D)
    assert msg is not None
    assert "자동 판정" in msg and "[s]" in msg
    assert settled == ["s@2026-08-20"]
    assert "인과가 아니라 증거" in msg


def test_daily_report_surfaces_a_worsening_change_too(tmp_path):
    """나쁜 결과를 숨기지 않는다."""
    p = tmp_path / "c.jsonl"
    record_fingerprints({"s": _cfg(x=1), "ctl": _cfg(y=1)}, date(2026, 8, 1), p)
    record_fingerprints({"s": _cfg(x=2), "ctl": _cfg(y=1)}, date(2026, 8, 20), p)

    trips = []
    trips += [_trip("s", "2026-08-10", 100.0 + i) for i in range(35)]
    trips += [_trip("s", "2026-08-25", -100.0 + i) for i in range(35)]
    trips += [_trip("ctl", "2026-08-10", 0.0 + i) for i in range(35)]
    trips += [_trip("ctl", "2026-08-25", 0.0 + i) for i in range(35)]

    msg, _ = daily_report(trips, load_changes(p), D)
    assert msg is not None and "악화" in msg


def test_daily_report_includes_death_alert_even_without_experiments(tmp_path):
    """자리를 비운 동안 묻지 않아도 소리쳐야 하는 것."""
    p = tmp_path / "c.jsonl"
    record_fingerprints({"bad": _cfg(x=1)}, date(2026, 8, 1), p)
    trips = [_trip("bad", "2026-08-10", -80.0) for _ in range(40)]
    msg, settled = daily_report(trips, load_changes(p), D)
    assert msg is not None and "사망 경보" in msg
    assert "자동 정지는 하지 않는다" in msg
    assert settled == []



# ── 7. 사망 판정 지속 → 자동 비활성 후보 (작업2, 2026-09-02) ────────────────

def test_record_death_watch_ignores_undersampled_but_logs_healthy_as_not_dead(tmp_path):
    path = tmp_path / "death_watch.jsonl"
    trips = [_trip("good", "2026-08-10", 40.0) for _ in range(40)] + \
            [_trip("thin", "2026-08-10", -80.0) for _ in range(5)]
    added = record_death_watch(trips, D, path)
    assert [r["strategy"] for r in added] == ["good"]
    assert added[0]["dead"] is False and added[0]["p_value"] is None


def test_record_death_watch_flags_significantly_negative_strategy(tmp_path):
    path = tmp_path / "death_watch.jsonl"
    trips = [_trip("bad", "2026-08-10", -80.0) for _ in range(40)]
    added = record_death_watch(trips, D, path)
    assert len(added) == 1
    row = added[0]
    assert row["strategy"] == "bad" and row["n"] == 40
    assert row["p_value"] < KILL_P_THRESHOLD
    assert row["dead"] is True


def test_record_death_watch_is_idempotent_when_stats_are_unchanged(tmp_path):
    """새 거래가 없는 날(주말 등) 다시 불러도 중복 기록하지 않는다 —
    record_fingerprints 와 같은 멱등 관례. 중복 기록은 K일 연속 판정을
    부풀린다."""
    path = tmp_path / "death_watch.jsonl"
    trips = [_trip("bad", "2026-08-10", -80.0) for _ in range(40)]
    record_death_watch(trips, D, path)
    added_again = record_death_watch(trips, D, path)  # 같은 trips, 같은 날
    assert added_again == []
    assert len(load_changes(path)) == 1


def test_consecutive_dead_candidates_requires_k_consecutive_recorded_days(tmp_path):
    path = tmp_path / "death_watch.jsonl"
    days = [date(2026, 8, d) for d in range(10, 15)]  # 5 거래일
    for i, d in enumerate(days):
        # 매일 살짝 다른 숫자를 줘서 멱등 스킵에 걸리지 않게 한다(n 을 늘린다).
        trips = [_trip("bad", "2026-08-01", -80.0) for _ in range(40 + i)]
        record_death_watch(trips, d, path)
    out = consecutive_dead_candidates(path, k_days=5)
    assert len(out) == 1
    assert out[0]["strategy"] == "bad"
    assert out[0]["streak_days"] == 5
    assert out[0]["since"] == days[0].isoformat()
    assert out[0]["until"] == days[-1].isoformat()


def test_consecutive_dead_candidates_needs_the_full_streak_not_just_enough_rows(tmp_path):
    """4일치만 있으면(기본 K=5) 아직 후보가 아니다 — 하루짜리 나쁜 스냅샷으로
    전략을 죽이지 않는다는 원칙의 핵심."""
    path = tmp_path / "death_watch.jsonl"
    days = [date(2026, 8, d) for d in range(10, 14)]  # 4 거래일뿐
    for i, d in enumerate(days):
        trips = [_trip("bad", "2026-08-01", -80.0) for _ in range(40 + i)]
        record_death_watch(trips, d, path)
    assert consecutive_dead_candidates(path, k_days=5) == []


def test_consecutive_dead_candidates_streak_breaks_on_recovery(tmp_path):
    """중간에 회복(양수 전환)이 끼면 그 이전 사망일은 최신 스트릭에 안 들어간다."""
    path = tmp_path / "death_watch.jsonl"
    d1, d2, d3, d4, d5, d6 = [date(2026, 8, d) for d in range(10, 16)]
    for i, d in enumerate([d1, d2, d3]):
        record_death_watch(
            [_trip("bad", "2026-08-01", -80.0) for _ in range(40 + i)], d, path)
    # d4: 회복(평균 양수) — _negative_edge_stats 에 안 잡혀 새 줄이 안 생긴다.
    record_death_watch([_trip("bad", "2026-08-01", 80.0) for _ in range(43)], d4, path)
    for i, d in enumerate([d5, d6]):
        record_death_watch(
            [_trip("bad", "2026-08-01", -80.0) for _ in range(44 + i)], d, path)
    # 최신 연속 구간은 d5,d6 둘뿐 — K=5 를 못 채운다.
    assert consecutive_dead_candidates(path, k_days=5) == []
    assert len(consecutive_dead_candidates(path, k_days=2)) == 1
