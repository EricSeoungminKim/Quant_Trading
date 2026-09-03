"""PnL 귀속(`quant/control/pnl_attribution.py`) — 2026-09-02 신규, 결정론(LLM 없음).

고정하는 계약:
① [엣지 − 수수료 − 세금] == 순손익(net) — 이 산수가 이 모듈의 유일한 존재
   이유다.
② 세금은 KR **매도**에만, `qty*price*tax_bps/1e4`로 추정한다(ETF 면제는
   원장에서 알 수 없어 상한 추정 — ETF 가 섞이면 클램프된다).
③ US/KR 매수 체결·US 매도 체결은 세금 성분에 기여하지 않는다.
④ 전략별 상/하위 1개 — 전략이 하나뿐이면 최고/최저가 같은 전략을 가리킨다.
"""
from __future__ import annotations

from quant.control.pnl_attribution import decompose, format_summary, top_bottom_strategies


def _session(gross=100_000.0, fees=20_000.0):
    net = gross - fees
    return {"gross_realized": gross, "fees": fees, "net_realized": net}


def _sell(symbol="005930", qty=10, price=70_000.0, market="KR"):
    return {"symbol": symbol, "side": "SELL", "qty": qty, "price": price, "market": market}


def _buy(**over):
    d = _sell(**over)
    d["side"] = "BUY"
    return d


# ---------------------------------------------------------------- 산수 계약(핵심)

def test_edge_minus_commission_minus_tax_equals_net():
    """① [엣지 − 수수료 − 세금] == 순손익 — 항상 성립해야 하는 항등식."""
    session = _session(gross=100_000.0, fees=20_000.0)
    trades = [_sell(qty=10, price=70_000.0)]  # notional 700,000
    decomp = decompose(session, trades, kr_stock_sell_tax_bps=20.0)
    assert decomp["edge"] - decomp["commission"] - decomp["tax"] == decomp["net"]
    assert decomp["net"] == 80_000.0


def test_tax_only_from_kr_sell_notional():
    """② 세금 = notional * tax_bps / 1e4."""
    session = _session(gross=100_000.0, fees=20_000.0)
    trades = [_sell(qty=10, price=70_000.0)]  # notional 700,000 * 20bp = 1,400
    decomp = decompose(session, trades, kr_stock_sell_tax_bps=20.0)
    assert decomp["tax"] == 1_400.0
    assert decomp["commission"] == 20_000.0 - 1_400.0


def test_buys_and_us_sells_do_not_contribute_tax():
    """③ 매수 체결·US 매도 체결은 세금에 기여하지 않는다."""
    session = _session(gross=50_000.0, fees=5_000.0)
    trades = [
        _buy(qty=10, price=70_000.0),          # KR 매수 — 세금 0
        _sell(qty=5, price=200.0, market="US"),  # US 매도 — 세금 0(이 모듈은 US 세금 미분해)
    ]
    decomp = decompose(session, trades, kr_stock_sell_tax_bps=20.0)
    assert decomp["tax"] == 0.0
    assert decomp["commission"] == decomp["edge"] - decomp["net"]


def test_tax_upper_bound_is_clamped_to_fees_total():
    """세금 추정치가 실제 수수료 총액을 넘으면(ETF 위주 세션 등) 0 아래로
    내려가지 않게 클램프하고 그 사실을 tax_clamped 로 남긴다."""
    session = _session(gross=100_000.0, fees=100.0)  # 수수료 총액이 아주 작다
    trades = [_sell(qty=100, price=1_000_000.0)]  # notional 1억 * 20bp = 200,000 (fees 를 초과)
    decomp = decompose(session, trades, kr_stock_sell_tax_bps=20.0)
    assert decomp["tax"] == 100.0, "세금이 수수료 총액을 넘지 않는다(클램프)"
    assert decomp["commission"] == 0.0
    assert decomp["tax_clamped"] is True


def test_zero_tax_bps_means_no_tax():
    session = _session()
    trades = [_sell(qty=10, price=70_000.0)]
    decomp = decompose(session, trades, kr_stock_sell_tax_bps=0.0)
    assert decomp["tax"] == 0.0
    assert decomp["commission"] == decomp["edge"] - decomp["net"]


# ---------------------------------------------------------------- 전략별 상/하위

def test_top_bottom_strategies_picks_best_and_worst():
    by_strategy = {
        "donchian": {"gross": 50_000.0, "fees": 5_000.0},
        "orb_scan": {"gross": -10_000.0, "fees": 2_000.0},
        "intraday_scan": {"gross": 20_000.0, "fees": 1_000.0},
    }
    top, bottom = top_bottom_strategies(by_strategy)
    assert top["strategy"] == "donchian"
    assert top["net"] == 45_000.0
    assert bottom["strategy"] == "orb_scan"
    assert bottom["net"] == -12_000.0


def test_top_bottom_strategies_single_strategy():
    """④ 전략이 하나뿐이면 최고/최저가 같은 전략을 가리킨다."""
    by_strategy = {"donchian": {"gross": 10_000.0, "fees": 1_000.0}}
    top, bottom = top_bottom_strategies(by_strategy)
    assert top["strategy"] == bottom["strategy"] == "donchian"


def test_top_bottom_strategies_empty():
    assert top_bottom_strategies({}) == (None, None)


# ---------------------------------------------------------------- 4줄 요약

def test_format_summary_is_exactly_four_lines():
    session = _session(gross=100_000.0, fees=20_000.0)
    trades = [_sell(qty=10, price=70_000.0)]
    decomp = decompose(session, trades, kr_stock_sell_tax_bps=20.0)
    by_strategy = {
        "donchian": {"gross": 60_000.0, "fees": 10_000.0},
        "orb_scan": {"gross": 40_000.0, "fees": 10_000.0},
    }
    top, bottom = top_bottom_strategies(by_strategy)
    text = format_summary("KR", "2026-09-02", decomp, top, bottom)
    lines = text.splitlines()
    assert len(lines) == 4
    assert "순손익" in lines[0]
    assert "엣지" in lines[1] and "수수료" in lines[1] and "세금" in lines[1]
    assert "최고 기여" in lines[2]
    assert "최저 기여" in lines[3]


# ⑤ 이식 정리 제외가 귀속까지 전파되는가 (2026-09-02) --------------------------

def test_attribution_sees_program_only_pnl_after_seeding_exclusion():
    """`decompose`는 `session_pnl_summary` 출력을 그대로 쓴다 — 그 출력이 이식
    정리를 빼고 나면 귀속 카드도 자동으로 프로그램 매매분만 말한다."""
    from datetime import date

    from quant.control.ledger import session_pnl_summary, trades_in_session

    reason = "실계좌 이식 정리 — 소유자 지시 2026-09-01"
    trades = [
        {"ts": "2026-09-01T14:30:00+00:00", "strategy_id": "gap_fade", "symbol": "TQQQ",
         "side": "buy", "qty": 1, "price": 69.2, "fee": 0.07, "market": "US"},
        {"ts": "2026-09-01T15:30:00+00:00", "strategy_id": "gap_fade", "symbol": "TQQQ",
         "side": "sell", "qty": 1, "price": 70.5, "fee": 0.08, "realized_pnl": 1.3,
         "market": "US"},
        {"ts": "2026-09-01T14:01:08+00:00", "strategy_id": "legacy", "symbol": "SOXL",
         "side": "sell", "qty": 13, "price": 105.67, "fee": 1.4,
         "realized_pnl": -706.42, "market": "US", "reason": reason},
    ]
    session = session_pnl_summary(trades, "US", date(2026, 9, 1))
    session_trades = [t for t in trades_in_session(trades, "US", date(2026, 9, 1))
                      if reason not in (t.get("reason") or "")]
    decomp = decompose(session, session_trades, kr_stock_sell_tax_bps=20.0)
    assert decomp["edge"] == 1.3                      # -706.42 가 섞이지 않는다
    assert round(decomp["net"], 4) == 1.15
    top, bottom = top_bottom_strategies(session["by_strategy"])
    assert top["strategy"] == "gap_fade" and bottom["strategy"] == "gap_fade"
