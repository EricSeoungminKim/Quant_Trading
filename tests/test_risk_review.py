"""독립 리스크 리뷰(`quant/control/risk_review.py`) — 2026-09-02 신규.

고정하는 계약:
① **판정은 결정론** — threshold_breach/reasons 는 LLM 을 거치지 않고
   `deterministic_flags()`가 순수 함수로 정한다.
② LLM 은 "상위 3문제 + 권고"만 낸다 — 결근해도 판정은 흔들리지 않는다.
③ **연속 손실은 "지금" 기준** — 승리를 만나면 그 즉시 계산을 멈춘다.
④ **멱등 적재** — 같은 날짜는 하루 한 번만 기록된다.
"""
from __future__ import annotations

import json

from quant.control.risk_review import (
    CONCENTRATION_THRESHOLD, LOSS_STREAK_THRESHOLD, append_ledger, build_dossier,
    deterministic_flags, format_card, parse_issues, run_review, strategy_consecutive_losses,
    to_record,
)


def _trip(strategy="donchian", pnl=1.0, exit_ts="2026-09-01T10:00:00+09:00", pnl_known=True):
    return {"strategy": strategy, "pnl": pnl, "exit_ts": exit_ts, "pnl_known": pnl_known}


# ---------------------------------------------------------------- 연속 손실

def test_consecutive_losses_counts_only_trailing_streak():
    """③ 승리 → 손실 → 손실 순서면 뒤에서부터의 연속 2건만 센다."""
    trips = [
        _trip(pnl=+1.0, exit_ts="2026-09-01T09:00:00+09:00"),
        _trip(pnl=-1.0, exit_ts="2026-09-01T10:00:00+09:00"),
        _trip(pnl=-1.0, exit_ts="2026-09-01T11:00:00+09:00"),
    ]
    out = strategy_consecutive_losses(trips)
    assert out["donchian"] == 2


def test_consecutive_losses_ignores_pnl_unknown():
    trips = [_trip(pnl=-1.0, pnl_known=False), _trip(pnl=-1.0)]
    out = strategy_consecutive_losses(trips)
    assert out["donchian"] == 1


def test_consecutive_losses_zero_when_last_trip_won():
    trips = [_trip(pnl=-5.0, exit_ts="2026-09-01T09:00:00+09:00"),
             _trip(pnl=+0.1, exit_ts="2026-09-01T10:00:00+09:00")]
    out = strategy_consecutive_losses(trips)
    assert out["donchian"] == 0


# ---------------------------------------------------------------- 결정론 판정

def test_deterministic_flags_no_breach_when_clean():
    flags = deterministic_flags(exposure=None, consecutive={"donchian": 1})
    assert flags == {"breach": False, "reasons": []}


def test_deterministic_flags_breach_on_offsetting_pair():
    exposure = {
        "offsetting_pairs": [{"long_symbol": "TQQQ", "inverse_symbol": "SQQQ"}],
        "by_symbol": [], "duplicates": [],
    }
    flags = deterministic_flags(exposure, consecutive={})
    assert flags["breach"] is True
    assert any("상쇄 쌍" in r for r in flags["reasons"])


def test_deterministic_flags_breach_on_concentration():
    exposure = {
        "offsetting_pairs": [], "duplicates": [],
        "by_symbol": [
            {"symbol": "005930", "notional_krw": 8_000_000},
            {"symbol": "000660", "notional_krw": 2_000_000},
        ],
    }
    flags = deterministic_flags(exposure, consecutive={})
    assert flags["breach"] is True
    assert any("집중" in r and "005930" in r for r in flags["reasons"])
    assert CONCENTRATION_THRESHOLD == 0.30


def test_deterministic_flags_breach_on_loss_streak():
    flags = deterministic_flags(exposure=None, consecutive={"scalp_1m": LOSS_STREAK_THRESHOLD})
    assert flags["breach"] is True
    assert any("연속 손실" in r and "scalp_1m" in r for r in flags["reasons"])


# ---------------------------------------------------------------- 서류 + LLM 파싱

def test_build_dossier_shows_deterministic_verdict_first():
    flags = {"breach": True, "reasons": ["단일 종목 집중: 005930 40%"]}
    dossier = build_dossier("스코어보드", {"summary": "합산 명목 1억"}, flags)
    assert dossier.index("[결정론 판정]") < dossier.index("[노출 요약]")
    assert "005930 40%" in dossier


def test_parse_issues_caps_at_three_and_ignores_garbage():
    text = json.dumps({"issues": [
        {"issue": f"문제{i}", "recommendation": f"권고{i}"} for i in range(5)
    ]})
    out = parse_issues(text)
    assert len(out) == 3


def test_parse_issues_none_on_garbage():
    assert parse_issues("산문만") is None
    assert parse_issues('{"issues": "말이 안 됨"}') is None
    assert parse_issues(None) is None


def test_run_review_none_when_narrate_fails():
    assert run_review("서류", lambda prompt: None) is None


def test_run_review_survives_llm_failure_end_to_end():
    """② LLM 결근이어도 record 자체는 항상 유효하다(threshold_breach 는 살아있다)."""
    flags = deterministic_flags(exposure=None, consecutive={"donchian": LOSS_STREAK_THRESHOLD})
    issues = run_review("서류", lambda prompt: None)
    record = to_record("2026-09-02", flags, issues)
    assert record["threshold_breach"] is True
    assert record["llm_ok"] is False
    assert record["issues"] == []
    card = format_card(record)
    assert card.startswith("BREACH: yes")


# ---------------------------------------------------------------- 멱등 적재

def test_append_ledger_idempotent_per_day(tmp_path):
    path = tmp_path / "risk_review.jsonl"
    record = to_record("2026-09-02", {"breach": False, "reasons": []}, [])
    assert append_ledger(record, path) is True
    assert append_ledger(record, path) is False
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
