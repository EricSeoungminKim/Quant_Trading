"""라운드트립 FIFO 페어링 단일 정의(quant.backtest.roundtrips) 회귀(2026-09-03).

`engine._round_trip_pnl`과 `analytics._round_trip_detail`이 같은 FIFO 매수-수수료
배분 루프를 각자 구현하고 있었다(반환 형태만 다름 — pnl 시계열 vs 상세 테이블).
둘 다 이제 `quant.backtest.roundtrips`를 가리킨다. 이 파일은 (1) 실제 donchian
stub 백테스트 체결 로그로 두 호출부가 바이트 단위로 같은 숫자를 내는지, (2) 부분
청산(다중 매수 → 매도 여러 건)에서도 FIFO 배분이 맞는지 확인한다.
"""
from __future__ import annotations

import pandas as pd
import pytest

from quant.backtest import run_backtest
from quant.backtest.analytics import _round_trip_detail
from quant.backtest.engine import _round_trip_pnl
from quant.backtest.roundtrips import round_trip_detail, round_trip_pnl

_COLS = ["ts", "symbol", "side", "qty", "price", "fee",
         "fee_krw", "realized_pnl_krw", "notional_krw", "pnl", "reason"]


def _row(ts, symbol, side, qty, fee_krw=0.0, realized_pnl_krw=0.0, notional_krw=0.0, reason=""):
    return {"ts": pd.Timestamp(ts, tz="UTC"), "symbol": symbol, "side": side, "qty": qty,
            "price": 100.0, "fee": 0.0, "fee_krw": fee_krw,
            "realized_pnl_krw": realized_pnl_krw, "notional_krw": notional_krw,
            "pnl": realized_pnl_krw, "reason": reason}


# --- 실제 백테스트 체결 로그에서 바이트 단위 일치 -----------------------------

def test_engine_and_analytics_agree_byte_for_byte_on_donchian_stub_trades():
    """루트 CLAUDE.md 검증 커맨드와 같은 조건(donchian/90일/stub)."""
    result = run_backtest(strategy_id="donchian", days=90, interval="15m", source="stub")
    assert len(result.trades) > 0, "회귀가 의미 있으려면 실제 체결이 있어야 한다"

    from_engine = _round_trip_pnl(result.trades)
    from_analytics = _round_trip_detail(result.trades)["net_pnl_krw"]

    assert len(from_engine) == len(from_analytics)
    pd.testing.assert_series_equal(
        from_engine, from_analytics.reset_index(drop=True),
        check_names=False, check_dtype=False,
    )


def test_engine_and_analytics_are_literally_the_shared_module():
    """별칭이 다시 로컬 재구현으로 갈라지면(과거 버그의 재발 형태) 여기서 잡는다."""
    assert _round_trip_pnl is round_trip_pnl
    assert _round_trip_detail is round_trip_detail


# --- 부분 청산 FIFO 배분(합성 시나리오) --------------------------------------

def test_fifo_allocates_buy_fee_proportionally_across_two_partial_sells():
    """매수 1건(수수료 100) → 절반씩 매도 2건 = 각 매도가 수수료 50씩 부담."""
    trades = pd.DataFrame([
        _row("2026-01-05 09:00", "X", "buy", 10.0, fee_krw=100.0, notional_krw=10_000.0),
        _row("2026-01-05 09:05", "X", "sell", 5.0, realized_pnl_krw=500.0, notional_krw=5_000.0),
        _row("2026-01-05 09:10", "X", "sell", 5.0, realized_pnl_krw=300.0, notional_krw=5_000.0),
    ], columns=_COLS)

    detail = round_trip_detail(trades)
    assert len(detail) == 2
    assert detail["net_pnl_krw"].tolist() == pytest.approx([500.0 - 50.0, 300.0 - 50.0])

    pnl = round_trip_pnl(trades)
    assert pnl.tolist() == pytest.approx(detail["net_pnl_krw"].tolist())


def test_empty_trades_returns_empty_series_and_dataframe():
    empty = pd.DataFrame(columns=_COLS)
    assert round_trip_pnl(empty).empty
    assert round_trip_detail(empty).empty
