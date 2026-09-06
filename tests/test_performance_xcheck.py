"""`quant.control.performance_xcheck.cross_check` 테스트 — 합성 원장 하나로
사이트 payload(`build_performance_payload`의 `paper_epoch`)와 스코어보드
"에폭 이후" 절(`round_trips_since_epoch`)이 서로 맞는지 대조한다(2026-09-06
Phase 5, `docs/plans/리포트-텔레그램-사이트-실전화-2026-09-06.md` §5).

`paper_epoch_ts`/`strategy_start_capital`은 `test_performance_paper_epoch.py`와
같은 이유로 `quant.control.performance` 네임스페이스에서 monkeypatch한다 —
`data/state/strategy_books.json`(실제 운영 파일)을 테스트가 건드리지 않게.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from quant.control import performance as performance_module
from quant.control.performance_xcheck import cross_check

EXECUTION_CFG = {
    "fee_bps": {"US": 10, "KR": 1.5},
    "kr_stock_sell_tax_bps": 20,
}

EPOCH_TS = datetime(2026, 9, 7, 0, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))

STRATEGIES_CFG = {
    "gap_fade": {"enabled": True},
    "scalp_1m": {"enabled": True},
}

_START_CAPITAL = {
    "gap_fade": {"KRW": 10_000_000, "USD": 10_000},
    "scalp_1m": {"KRW": 10_000_000},
}


def _fake_start_capital(strategy_id: str) -> dict[str, float]:
    return dict(_START_CAPITAL.get(strategy_id, {}))


def _patch_epoch(monkeypatch):
    monkeypatch.setattr(performance_module, "paper_epoch_ts", lambda trades=None: EPOCH_TS)
    monkeypatch.setattr(performance_module, "strategy_start_capital", _fake_start_capital)


def _trade(
    *, ts, strategy_id, symbol, side, qty, price, fee=0.0, realized_pnl=None,
    market="KR", reason="",
):
    return {
        "ts": ts, "strategy_id": strategy_id, "symbol": symbol, "side": side,
        "qty": qty, "price": price, "fee": fee, "realized_pnl": realized_pnl,
        "reason": reason, "market": market,
    }


def _epoch_marker_row(ts: str) -> dict:
    from quant.control.ledger import PAPER_EPOCH_MARKER

    return {
        "ts": ts, "strategy_id": "epoch", "symbol": "_EPOCH_", "side": "buy",
        "qty": 0.0, "price": 0.0, "fee": 0.0, "realized_pnl": 0.0,
        "cash_after": None, "cash_after_usd": None,
        "reason": f"{PAPER_EPOCH_MARKER} — capital_policy=fixed_dual, at {ts}",
        "market": "US",
    }


def _consistent_ledger() -> list[dict]:
    """에폭 마커 + 두 전략(gap_fade: KR+US, scalp_1m: KR)의 정상 왕복.
    스코어보드 경로와 사이트 경로가 완전히 같은 체결 집합을 봐야 하므로
    (같은 필터링 규칙, 다른 구현) 두 경로 모두 이 왕복들을 그대로 재구성해야
    한다 — 대조가 통과하는 것이 baseline."""
    return [
        # 에폭 이전 이력 — 두 경로 모두 걸러내야 한다(안 걸러지면 대조가 아니라
        # 둘 다 같은 버그를 공유하는 셈이라 이 테스트만으로는 못 잡지만,
        # round_trips_since_epoch/_epoch_trades 각각의 단위 테스트가 이미 이걸 본다).
        _trade(ts="2026-08-01T00:00:00+00:00", strategy_id="gap_fade", symbol="069500",
               side="buy", qty=1, price=10000.0, realized_pnl=0.0),
        _trade(ts="2026-08-01T01:00:00+00:00", strategy_id="gap_fade", symbol="069500",
               side="sell", qty=1, price=19000.0, realized_pnl=9000.0),
        _epoch_marker_row("2026-09-07T00:00:00+09:00"),
        # gap_fade / KR — 에폭 이후, 승리 1건
        _trade(ts="2026-09-07T01:00:00+00:00", strategy_id="gap_fade", symbol="069500",
               side="buy", qty=1, price=10000.0, realized_pnl=0.0),
        _trade(ts="2026-09-07T02:00:00+00:00", strategy_id="gap_fade", symbol="069500",
               side="sell", qty=1, price=10500.0, realized_pnl=500.0),
        # gap_fade / KR — 두 번째 왕복, 손실 1건 (승률 50%를 만들어 계산이
        # 진짜 돌아가는지 확인)
        _trade(ts="2026-09-07T03:00:00+00:00", strategy_id="gap_fade", symbol="069500",
               side="buy", qty=1, price=10000.0, realized_pnl=0.0),
        _trade(ts="2026-09-07T04:00:00+00:00", strategy_id="gap_fade", symbol="069500",
               side="sell", qty=1, price=9700.0, realized_pnl=-300.0),
        # gap_fade / US — 에폭 이후
        _trade(ts="2026-09-07T05:00:00+00:00", strategy_id="gap_fade", symbol="TQQQ",
               side="buy", qty=1, price=70.0, market="US"),
        _trade(ts="2026-09-07T06:00:00+00:00", strategy_id="gap_fade", symbol="TQQQ",
               side="sell", qty=1, price=72.0, realized_pnl=2.0, market="US"),
        # scalp_1m / KR — 에폭 이후
        _trade(ts="2026-09-07T01:30:00+00:00", strategy_id="scalp_1m", symbol="005935",
               side="buy", qty=1, price=5000.0, realized_pnl=0.0),
        _trade(ts="2026-09-07T02:30:00+00:00", strategy_id="scalp_1m", symbol="005935",
               side="sell", qty=1, price=5100.0, realized_pnl=100.0),
    ]


def test_cross_check_empty_without_epoch_marker():
    """에폭을 한 번도 안 돌렸으면(마커 없음) 대조 대상 자체가 없다 — 에러 아님."""
    trades = [
        _trade(ts="2026-08-01T00:00:00+00:00", strategy_id="gap_fade", symbol="069500",
               side="buy", qty=1, price=10000.0, realized_pnl=0.0),
        _trade(ts="2026-08-01T01:00:00+00:00", strategy_id="gap_fade", symbol="069500",
               side="sell", qty=1, price=19000.0, realized_pnl=9000.0),
    ]
    assert cross_check(trades, strategies_cfg=STRATEGIES_CFG, execution_cfg=EXECUTION_CFG) == []


def test_cross_check_passes_on_a_consistent_synthetic_ledger(monkeypatch):
    """스코어보드 경로(`round_trips_since_epoch`)와 사이트 경로(`_epoch_trades`
    + `build_performance_payload`)가 같은 원장에서 같은 숫자를 내면 findings가
    비어야 한다 — 이게 이 교차대조의 정상 상태(baseline)다."""
    _patch_epoch(monkeypatch)
    trades = _consistent_ledger()
    findings = cross_check(trades, strategies_cfg=STRATEGIES_CFG, execution_cfg=EXECUTION_CFG)
    assert findings == [], f"일치해야 할 합성 원장에서 불일치 발견: {[f.to_dict() for f in findings]}"


def test_cross_check_catches_a_broken_site_side_epoch_boundary(monkeypatch):
    """사이트 경로(`_epoch_trades`)가 스코어보드 경로(`round_trips_since_epoch`)와
    다른 경계를 쓰게 되면(예: `>=` 대신 `>`로 착오) 대조가 반드시 잡아야 한다 —
    이 테스트는 실제 버그를 주입해 게이트가 살아있는지 확인한다."""
    _patch_epoch(monkeypatch)
    # _epoch_trades는 quant.control.performance 모듈 전역이다 — 여기서만 경계를
    # 하루 뒤로 밀어 스코어보드 경로와 어긋나게 만든다(사이트 쪽만 고장난 상황을
    # 흉내).
    import quant.control.performance as perf_mod

    real_epoch_trades = perf_mod._epoch_trades

    def _broken_epoch_trades(trades, epoch_ts):
        from datetime import timedelta
        # 하루 뒤로 밀면 이 합성 원장의 에폭 이후 체결이 전부(2026-09-07자)
        # 사이트 경로에서만 사라진다 — 스코어보드 경로는 그대로라 확실히 갈라진다.
        return real_epoch_trades(trades, epoch_ts + timedelta(days=1))

    monkeypatch.setattr("quant.control.performance_xcheck._epoch_trades", _broken_epoch_trades)

    trades = _consistent_ledger()
    findings = cross_check(trades, strategies_cfg=STRATEGIES_CFG, execution_cfg=EXECUTION_CFG)
    errors = [f for f in findings if f.severity == "error"]
    assert errors, "경계가 어긋났는데도 대조가 통과했다 — 게이트가 죽어 있다"


def test_cross_check_catches_a_trip_count_mismatch_via_direct_group_stats():
    """`_group_stats`(내부 헬퍼) 자체의 산수 검증 — pnl_known=False는 승패
    집계에서 빠지고, 승률/기대값이 표준 bps 공식과 같다."""
    from quant.control.performance_xcheck import _group_stats

    trips = [
        {"strategy": "gap_fade", "market": "KR", "pnl_known": True, "pnl": 500.0, "bps": 50.0},
        {"strategy": "gap_fade", "market": "KR", "pnl_known": True, "pnl": -300.0, "bps": -30.0},
        {"strategy": "gap_fade", "market": "KR", "pnl_known": False, "pnl": None, "bps": 0.0},
    ]
    stats = _group_stats(trips)
    block = stats[("gap_fade", "KR")]
    assert block["n"] == 2  # 손익미상 1건 제외
    assert block["win_rate"] == pytest.approx(0.5)
    assert block["pnl"] == pytest.approx(200.0)
    # expectancy = wr*aw - (1-wr)*al = 0.5*50 - 0.5*30 = 10.0
    assert block["expectancy_bp"] == pytest.approx(10.0)
