"""자본 자동 강등 장치 (`quant/control/allocator.py`) — 합성 데이터로 4층 방어를
고정한다: 증거(신뢰상한) · 하한 · 냉각 · 한 방향(감소만). 마지막 항목(⑦)은
CLI 배선(`quant.apps.cli.cmd_capital_review`)이 "강등 후보가 아예 없으면 아무
파일도 안 만든다"는 계약을 지키는지 확인한다 — `governor.py`/
`tests/test_governor_wiring.py`와 같은 관례.
"""
from __future__ import annotations

import json
from datetime import date

from quant.control.allocator import StrategyStat, decide, is_losing, next_fraction, p_value_losing
from quant.control.multiple_testing import benjamini_hochberg

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
    """last_change_days 가 None(한 번도 강등된 적 없음)이면 냉각 대상이 아니다.

    n=25→35(2026-09-07): `decide()`의 `min_samples` 기본값이 `ledger.
    MIN_TRIPS_FOR_JUDGEMENT`(30, 투자 #3 — allocator/ledger/governor가 20/30으로
    갈라져 있던 최소 표본선을 하나로 통일)로 바뀌면서 n=25는 기본값 아래로
    떨어져 "표본 부족"으로 후보에서 빠진다. 이 테스트의 관심사는 냉각 로직이지
    표본 임계값이 아니므로 새 기본값을 넉넉히 넘기는 n으로 올린다."""
    stat = StrategyStat(strategy="news_momentum", n=35, mean_bp=-90.4, stdev_bp=100.0)
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


# ── 2026-09-07: 자본 심사 범위 — 비활성 레인 제외 + 에폭 이후 트립만 ──────────────────
def test_capital_review_scope_excludes_disabled_and_pre_epoch():
    from types import SimpleNamespace

    from quant.apps.cli import _capital_review_trips, _enabled_only
    from quant.control.ledger import PAPER_EPOCH_MARKER

    settings = SimpleNamespace(strategies={
        "scalp_1m": {"enabled": True, "capital_fraction": {"KR": 0.03}},
        "intraday_scan": {"enabled": False, "capital_fraction": {"KR": 0.1, "US": 0.1}},
    })
    fr = _enabled_only({("scalp_1m", "KR"): 0.03, ("intraday_scan", "KR"): 0.1, ("intraday_scan", "US"): 0.1}, settings)
    assert fr == {("scalp_1m", "KR"): 0.03}

    old = {"ts": "2026-09-01T01:00:00+00:00", "strategy_id": "scalp_1m", "symbol": "005930", "side": "buy", "qty": 1, "price": 100.0, "fee": 0.0, "realized_pnl": 0.0, "market": "KR"}
    old_x = {**old, "ts": "2026-09-01T02:00:00+00:00", "side": "sell", "price": 90.0, "realized_pnl": -10.0}
    marker = {"ts": "2026-09-06T15:00:00+00:00", "strategy_id": "epoch", "symbol": "_EPOCH_", "side": "buy", "qty": 0.0, "price": 0.0, "fee": 0.0, "realized_pnl": 0.0, "reason": f"{PAPER_EPOCH_MARKER} — test", "market": "US"}
    new = {**old, "ts": "2026-09-07T01:00:00+00:00"}
    new_x = {**old_x, "ts": "2026-09-07T02:00:00+00:00", "price": 110.0, "realized_pnl": 10.0}
    trips = _capital_review_trips([old, old_x, marker, new, new_x])
    assert len(trips) == 1 and trips[0].get("pnl", trips[0].get("realized_pnl", 10.0)) >= 0
    assert len(_capital_review_trips([old, old_x])) == 1  # 마커 없으면 전체 원장


# ── ⑧ 다중검정 보정(FDR/BH, 결정 ⑧ 2026-09-07) ──────────────────────────────
#
# 17개 전략을 동시에 심사하면(현재 로스터 규모), 진짜 지는 전략이 딱 하나뿐
# 이어도 "우연히 신뢰상한이 0 밑으로 내려가는" 노이즈 전략이 여럿 나온다 —
# 동전을 17번 던지면 몇 개는 마이너스 구간을 지나는 것과 같다. 아래 값들은
# 손으로 계산해 고정한 것이다(무작위 시드가 아니라 결정론적 상수 — 매 실행
# 같은 결과):
#
#   신호(signal): n=100, mean=-40bp, stdev=50bp → z=-8.0, p≈6.2e-16 (압도적)
#   노이즈 12개(진짜 무해): mean -1~-9bp, 90% 신뢰상한이 전부 양수 → 애초에
#     프리필터(is_losing)를 통과 못 해 후보가 아니다.
#   노이즈 4개(우연히 유의): mean -11/-12/-13/-14bp, n=40, stdev=50bp → 90%
#     신뢰상한이 전부 음수라 **개별 검정으로는(보정 전) 후보가 된다** — 이게
#     "3~4개가 우연히 강등된다"는 실측이다.
#
# BH(q=0.10)를 17개 전체(가족 전체, 프리필터 통과 여부와 무관)에 적용하면
# 신호 하나만 남고 노이즈 4개는 전부 탈락한다(p 0.038~0.082가 문턱보다 크다).

def _signal_and_noise_stats():
    signal = StrategyStat(strategy="signal", n=100, mean_bp=-40.0, stdev_bp=50.0)
    noise_means = [-1, -2, -3, -4, -5, -6, -6.5, -7, -7.5, -8, -8.5, -9, -11, -12, -13, -14]
    noise = [
        StrategyStat(strategy=f"noise{i}", n=40, mean_bp=m, stdev_bp=50.0)
        for i, m in enumerate(noise_means)
    ]
    return [signal, *noise]


def test_raw_rule_alone_flags_the_signal_plus_several_noise_false_positives():
    """보정 전(현재 90% 신뢰상한 프리필터만) 기준으로 몇 개가 "지고 있다"로
    잘못 걸리는지 확인 — 신호 1개 + 노이즈 3~4개."""
    stats = _signal_and_noise_stats()
    flagged = [s.strategy for s in stats if is_losing(s, min_samples=30)[0]]
    assert "signal" in flagged
    false_positives = [s for s in flagged if s != "signal"]
    assert 3 <= len(false_positives) <= 4
    assert set(false_positives) == {"noise12", "noise13", "noise14", "noise15"}  # -11/-12/-13/-14


def test_bh_correction_across_the_full_run_keeps_only_the_true_signal():
    """BH(q=0.10)를 17개 전체 가족에 적용하면 압도적으로 유의한 신호만 남고
    개별로는 유의해 보였던 노이즈 4개는 다중검정 보정에 탈락한다."""
    stats = _signal_and_noise_stats()
    out = decide(
        stats,
        {("signal", "KR"): 0.1, **{(f"noise{i}", "KR"): 0.1 for i in range(16)}},
        {s.strategy: None for s in stats},
    )
    applied = {d.strategy for d in out if d.applied}
    assert applied == {"signal"}

    # 프리필터를 통과했지만 BH에서 탈락한 4개 노이즈는 그대로 기록되되
    # applied=False, passed_fdr=False로 남는다(거부도 남긴다).
    rejected_by_fdr = {d.strategy: d for d in out if not d.applied and not d.passed_fdr}
    assert set(rejected_by_fdr) == {"noise12", "noise13", "noise14", "noise15"}
    for d in rejected_by_fdr.values():
        assert d.p_value > d.bh_threshold
        assert "FDR" in d.skip_reason

    signal_demotion = next(d for d in out if d.strategy == "signal")
    assert signal_demotion.passed_fdr is True
    assert signal_demotion.p_value <= signal_demotion.bh_threshold


def test_bh_with_a_single_candidate_matches_the_raw_fdr_threshold():
    """후보가 하나뿐이면 BH 문턱은 그냥 q 자체다 — 보정할 다른 검정이 없어
    `benjamini_hochberg([p], q)`가 raw `p <= q`와 같은 답을 낸다."""
    stat = StrategyStat(strategy="scalp_1m", n=77, mean_bp=-57.2, stdev_bp=150.0)
    p = p_value_losing(stat)
    assert benjamini_hochberg([p], q=0.10) == [p <= 0.10]

    out = decide([stat], {("scalp_1m", "KR"): 0.2}, {"scalp_1m": None})
    assert len(out) == 1
    assert out[0].passed_fdr == (p <= 0.10)
    assert out[0].bh_threshold == (p if p <= 0.10 else 0.0)
