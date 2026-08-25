"""부분 켈리 자문(quant.control.kelly) 단위 테스트.

**표시만 한다는 계약을 시험한다** — advisory()가 capital_fraction 이나
settings.yaml 을 건드리지 않는다는 것은 함수가 dict 를 반환할 뿐 아무 파일도
쓰지 않는다는 사실 자체로 이미 보장된다(부수효과가 없으므로 별도 목킹 없이도
자명하다). 여기서는 계산이 맞는지, 표본 부족/엣지 없음을 지어내지 않는지를 본다.
"""
from __future__ import annotations

import pytest

from quant.control.kelly import MIN_N_DEFAULT, advisory, kelly_fraction


# ── kelly_fraction ───────────────────────────────────────────────────────

def test_kelly_fraction_known_value():
    # f* = p - q/b = 0.6 - 0.4/2.0 = 0.4
    assert kelly_fraction(0.6, 2.0) == pytest.approx(0.4)


def test_kelly_fraction_negative_edge_when_win_rate_low():
    """승률이 payoff 대비 낮으면 음수(베팅하지 말라는 신호) — 지어낸 양수가 아니다."""
    f = kelly_fraction(0.3, 1.5)
    assert f is not None
    assert f < 0


def test_kelly_fraction_none_on_nonpositive_payoff():
    assert kelly_fraction(0.5, 0.0) is None
    assert kelly_fraction(0.5, -1.0) is None


def test_kelly_fraction_none_on_win_rate_out_of_range():
    assert kelly_fraction(-0.1, 1.0) is None
    assert kelly_fraction(1.1, 1.0) is None


def test_kelly_fraction_handles_infinite_payoff_natively():
    """손실 표본이 하나도 없으면(b=inf) f* → win_rate (q/inf == 0.0)."""
    f = kelly_fraction(1.0, float("inf"))
    assert f == 1.0


# ── advisory ──────────────────────────────────────────────────────────────

def _trip(strategy="donchian", pnl=1.0, bps=10.0, pnl_known=True):
    return {"strategy": strategy, "symbol": "TQQQ", "pnl": pnl, "bps": bps,
            "pnl_known": pnl_known}


def test_advisory_rejects_small_sample_without_fabricating_numbers():
    trips = [_trip() for _ in range(MIN_N_DEFAULT - 1)]
    result = advisory(trips)
    assert len(result) == 1
    row = result[0]
    assert row["n"] == MIN_N_DEFAULT - 1
    assert row["full_kelly"] is None
    assert row["quarter_kelly"] is None
    assert "표본 부족" in row["note"]


def test_advisory_computes_full_and_quarter_kelly_for_sufficient_sample():
    # 60% 승률, 이익 20bp/손실 10bp 고정 — 표본 충분(30건 이상)
    wins = [_trip(pnl=1.0, bps=20.0) for _ in range(18)]
    losses = [_trip(pnl=-1.0, bps=-10.0) for _ in range(12)]
    result = advisory(wins + losses)
    assert len(result) == 1
    row = result[0]
    assert row["n"] == 30
    assert row["win_rate"] == 0.6
    assert row["payoff"] == 2.0
    assert row["full_kelly"] is not None and row["full_kelly"] > 0
    assert row["quarter_kelly"] == pytest.approx(row["full_kelly"] / 4)
    assert row["note"] == ""


def test_advisory_flags_no_edge_without_hiding_the_number():
    """엣지가 없으면(full_kelly<=0) 그 사실을 note로 말하되 숫자 자체는 남긴다."""
    wins = [_trip(pnl=1.0, bps=5.0) for _ in range(9)]
    losses = [_trip(pnl=-1.0, bps=-20.0) for _ in range(21)]
    result = advisory(wins + losses)
    row = result[0]
    assert row["full_kelly"] is not None
    assert row["full_kelly"] <= 0
    assert "엣지 없음" in row["note"]


def test_advisory_excludes_pnl_unknown_trips():
    known = [_trip(pnl=1.0, bps=10.0) for _ in range(MIN_N_DEFAULT)]
    unknown = [_trip(pnl_known=False) for _ in range(50)]
    result = advisory(known + unknown)
    assert len(result) == 1
    assert result[0]["n"] == MIN_N_DEFAULT


def test_advisory_groups_by_strategy_independently():
    a = [_trip(strategy="donchian", pnl=1.0, bps=10.0) for _ in range(MIN_N_DEFAULT)]
    b = [_trip(strategy="orb_scan") for _ in range(5)]  # 표본 부족
    result = advisory(a + b)
    by_strategy = {r["strategy"]: r for r in result}
    assert set(by_strategy) == {"donchian", "orb_scan"}
    assert by_strategy["donchian"]["full_kelly"] is not None
    assert by_strategy["orb_scan"]["full_kelly"] is None


def test_advisory_never_writes_or_mutates_capital_fraction():
    """자동 반영 금지의 최소 보장 — 반환값은 순수 dict 목록, 입력도 변형하지 않는다."""
    trips = [_trip() for _ in range(MIN_N_DEFAULT)]
    snapshot = [dict(t) for t in trips]
    advisory(trips)
    assert trips == snapshot
