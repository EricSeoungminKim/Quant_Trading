"""자본 자동 강등 장치 (`quant/control/allocator.py`) — 합성 데이터로 4층 방어를
고정한다: 증거(신뢰상한) · 하한 · 냉각 · 한 방향(감소만). 마지막 항목(⑦)은
CLI 배선(`quant.apps.cli.cmd_capital_review`)이 "강등 후보가 아예 없으면 아무
파일도 안 만든다"는 계약을 지키는지 확인한다 — `governor.py`/
`tests/test_governor_wiring.py`와 같은 관례.
"""
from __future__ import annotations

import json
from datetime import date

from quant.control.allocator import Demotion, StrategyStat, decide, is_losing, next_fraction

TODAY = date(2026, 8, 28)


# --- ① 표본 부족이면 무변경 -------------------------------------------------

def test_insufficient_samples_is_not_losing_regardless_of_mean():
    stat = StrategyStat(strategy="news_scalp", n=6, mean_bp=-174.5, stdev_bp=200.0)
    losing, reason = is_losing(stat, min_samples=20)
    assert losing is False
    assert "표본 부족" in reason


def test_decide_produces_nothing_for_insufficient_samples():
    stat = StrategyStat(strategy="news_scalp", n=6, mean_bp=-174.5, stdev_bp=200.0)
    out = decide([stat], {("news_scalp", "KR"): 0.1}, {"news_scalp": None}, min_samples=20)
    assert out == []


# --- ② 평균은 음수지만 신뢰상한이 0 이상이면 무변경(우연한 손실 구간 보호) ---

def test_negative_mean_with_wide_stdev_is_not_losing():
    # n=25 (>=20), 평균 -5bp지만 표준편차가 커서(100bp) 90% 신뢰상한이 0을 넘는다
    # — "지고 있다"고 말할 근거가 부족한, 우연한 손실 구간.
    stat = StrategyStat(strategy="confluence", n=25, mean_bp=-5.0, stdev_bp=100.0)
    losing, reason = is_losing(stat, min_samples=20, confidence=0.90)
    assert losing is False
    assert "우연한 손실 구간" in reason


def test_decide_produces_nothing_when_confidence_upper_bound_is_positive():
    stat = StrategyStat(strategy="confluence", n=25, mean_bp=-5.0, stdev_bp=100.0)
    out = decide([stat], {("confluence", "KR"): 0.075}, {"confluence": None})
    assert out == []


# --- ③ 명확히 지는 전략은 반감 ----------------------------------------------

def test_clearly_losing_strategy_is_flagged():
    # n=77, 평균 -57.2bp, 표준편차 150bp — 신뢰상한 = -57.2 + 1.2816*150/sqrt(77)
    # = -57.2 + 21.9 = -35.3 < 0 → 명백히 진다.
    stat = StrategyStat(strategy="scalp_1m", n=77, mean_bp=-57.2, stdev_bp=150.0)
    losing, reason = is_losing(stat, min_samples=20)
    assert losing is True
    assert "지고 있다" in reason


def test_decide_halves_capital_fraction_for_losing_strategy():
    stat = StrategyStat(strategy="scalp_1m", n=77, mean_bp=-57.2, stdev_bp=150.0)
    out = decide(
        [stat],
        {("scalp_1m", "KR"): 0.3, ("scalp_1m", "US"): 1.0},
        {"scalp_1m": None},
        factor=0.5, floor=0.05,
    )
    by_market = {d.market: d for d in out}
    assert by_market["KR"].applied is True
    assert by_market["KR"].proposed == 0.15
    assert by_market["US"].applied is True
    assert by_market["US"].proposed == 0.5


def test_decide_skips_zero_allocation_markets():
    """구조적으로 0인 시장(예: donchian의 KR)은 강등 대상이 아니다 — 후보에도 안 낀다."""
    stat = StrategyStat(strategy="donchian", n=50, mean_bp=-40.0, stdev_bp=100.0)
    out = decide([stat], {("donchian", "KR"): 0.0, ("donchian", "US"): 0.3}, {"donchian": None})
    assert len(out) == 1
    assert out[0].market == "US"


# --- ④ 하한 아래로 안 내려감 -------------------------------------------------

def test_next_fraction_halves_above_floor():
    assert next_fraction(0.2, factor=0.5, floor=0.05) == 0.1


def test_next_fraction_clamps_to_floor():
    assert next_fraction(0.06, factor=0.5, floor=0.05) == 0.05


def test_next_fraction_already_at_floor_is_unchanged():
    assert next_fraction(0.05, factor=0.5, floor=0.05) == 0.05


def test_decide_skips_strategy_already_at_floor():
    stat = StrategyStat(strategy="orb_scan", n=30, mean_bp=-93.1, stdev_bp=100.0)
    out = decide([stat], {("orb_scan", "KR"): 0.05}, {"orb_scan": None}, floor=0.05)
    assert len(out) == 1
    d = out[0]
    assert d.applied is False
    assert "하한" in d.skip_reason
    assert d.proposed == d.current == 0.05


# --- ⑤ 냉각 중이면 skip ------------------------------------------------------

def test_decide_skips_when_within_cooldown():
    stat = StrategyStat(strategy="intraday_scan", n=40, mean_bp=-72.0, stdev_bp=100.0)
    out = decide(
        [stat], {("intraday_scan", "KR"): 0.1}, {"intraday_scan": 2},
        cooldown_days=5,
    )
    assert len(out) == 1
    d = out[0]
    assert d.applied is False
    assert "냉각" in d.skip_reason
    assert d.proposed == d.current  # 값을 안 건드렸다


def test_decide_applies_when_cooldown_has_elapsed():
    stat = StrategyStat(strategy="intraday_scan", n=40, mean_bp=-72.0, stdev_bp=100.0)
    out = decide(
        [stat], {("intraday_scan", "KR"): 0.1}, {"intraday_scan": 10},
        cooldown_days=5,
    )
    assert len(out) == 1
    assert out[0].applied is True


def test_decide_no_prior_change_means_no_cooldown():
    """last_change_days 가 None(한 번도 강등된 적 없음)이면 냉각 대상이 아니다."""
    stat = StrategyStat(strategy="news_momentum", n=25, mean_bp=-90.4, stdev_bp=100.0)
    out = decide([stat], {("news_momentum", "KR"): 0.1}, {"news_momentum": None}, cooldown_days=5)
    assert len(out) == 1
    assert out[0].applied is True


# --- ⑥ 증가 방향 제안이 나오지 않음 -----------------------------------------

def test_decide_never_proposes_an_increase():
    """모든 강등 후보에 대해 proposed <= current 가 항상 성립한다 — 반감이든
    스킵이든 늘어나는 경우는 없다."""
    stats = [
        StrategyStat(strategy="scalp_1m", n=77, mean_bp=-57.2, stdev_bp=150.0),
        StrategyStat(strategy="intraday_scan", n=40, mean_bp=-72.0, stdev_bp=100.0),
    ]
    current = {("scalp_1m", "KR"): 0.3, ("scalp_1m", "US"): 1.0, ("intraday_scan", "KR"): 0.1}
    out = decide(stats, current, {"scalp_1m": None, "intraday_scan": None})
    assert out
    for d in out:
        assert d.proposed <= d.current


def test_decide_guards_against_misconfigured_factor_above_one():
    """factor > 1 로 잘못 호출돼도(설정 실수) 증가 방향 결과는 절대 나오지
    않는다 — skip 하고 사유를 남긴다."""
    stat = StrategyStat(strategy="scalp_1m", n=77, mean_bp=-57.2, stdev_bp=150.0)
    out = decide([stat], {("scalp_1m", "KR"): 0.2}, {"scalp_1m": None}, factor=1.5)
    assert len(out) == 1
    d = out[0]
    assert d.applied is False
    assert d.proposed == d.current
    assert "증가 방향" in d.skip_reason


# --- ⑦ CLI 배선: 강등 후보가 없으면 dry-run 이 아니어도 아무 파일도 안 쓴다 ---

def _write_trades(root, rows):
    path = root / "data" / "state" / "trades.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_settings(root, strategies):
    import yaml

    path = root / "config" / "settings.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"strategies": strategies}, allow_unicode=True), encoding="utf-8")


def _fill_trade(ts, strategy, symbol, side, qty, price, realized_pnl=None, fee=0.0):
    return {
        "ts": ts, "strategy_id": strategy, "symbol": symbol, "side": side,
        "qty": qty, "price": price, "fee": fee, "realized_pnl": realized_pnl,
        "market": "KR" if (symbol.isdigit() and len(symbol) == 6) else "US",
    }


def test_capital_review_writes_nothing_when_no_losing_candidates(tmp_path):
    import argparse

    from quant.apps.cli import cmd_capital_review

    root = tmp_path
    # 승리하는 전략(양의 realized_pnl) — 강등 후보가 될 수 없다.
    rows = []
    for i in range(25):
        rows.append(_fill_trade(f"2026-08-{(i % 20) + 1:02d}T09:00:00+09:00", "donchian", "TQQQ", "BUY", 1, 100))
        rows.append(_fill_trade(f"2026-08-{(i % 20) + 1:02d}T09:30:00+09:00", "donchian", "TQQQ", "SELL", 1, 105, realized_pnl=5.0))
    _write_trades(root, rows)
    _write_settings(root, {"donchian": {"capital_fraction": {"KR": 0.0, "US": 0.3}}})

    args = argparse.Namespace(root=str(root), dry_run=False, min_samples=20)
    cmd_capital_review(args)

    assert not (root / "config" / "auto_params.yaml").exists()
    assert not (root / "data" / "ledger" / "capital_decisions.jsonl").exists()
