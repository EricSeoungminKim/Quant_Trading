"""공개 성과 JSON(`quant.control.performance.build_performance_payload`) 테스트.

이 스위트가 고정하는 것 (프롬프트 계약):
- 손익은 수수료 차감 후.
- 통화 혼합 없음(KR/US 각자 통화로 집계, KRW 환산은 고정환율 한 곳에서만).
- 실계좌 이식 정리 매도는 성과에서 제외되고 `excluded`에만 집계된다.
- 날짜 귀속은 KST 08:00 경계(US 밤 체결도 시작일 거래일로).
- 표본 30건 미만은 `sample_warning: true`.
- 종목 코드·수량·계좌 잔고 절대값 문자열이 출력에 없다.
"""
from __future__ import annotations

import json

from quant.control.performance import (
    FX_KRW_PER_USD,
    SEEDING_LIQUIDATION_MARKER,
    build_performance_payload,
)

EXECUTION_CFG = {
    "fee_bps": {"US": 10, "KR": 1.5},
    "kr_stock_sell_tax_bps": 20,
}


def _trade(
    *, ts, strategy_id, symbol, side, qty, price, fee=0.0, realized_pnl=None,
    market="KR", reason="", cash_after=None,
):
    return {
        "ts": ts, "strategy_id": strategy_id, "symbol": symbol, "side": side,
        "qty": qty, "price": price, "fee": fee, "realized_pnl": realized_pnl,
        "cash_after": cash_after, "reason": reason, "market": market,
    }


def test_pnl_is_after_fees_not_gross():
    """라운드트립 1건: gross realized_pnl=1000, 수수료(매수+매도)=120 →
    strategies[].expectancy_bp는 순손익(880) 기준이어야지 gross(1000) 기준이면 안 된다."""
    trades = [
        _trade(ts="2026-08-10T00:00:00+00:00", strategy_id="orb_scan", symbol="069500",
               side="buy", qty=10, price=10000.0, fee=50.0, realized_pnl=0.0),
        _trade(ts="2026-08-10T01:00:00+00:00", strategy_id="orb_scan", symbol="069500",
               side="sell", qty=10, price=10100.0, fee=70.0, realized_pnl=1000.0),
    ]
    payload = build_performance_payload(trades, EXECUTION_CFG)
    (strat,) = payload["strategies"]
    notional = 10 * 10000.0
    net_pnl = 1000.0 - (50.0 + 70.0)
    expected_bps = net_pnl / notional * 1e4
    assert strat["trips"] == 1 and strat["wins"] == 1
    assert strat["expectancy_bp"] == round(expected_bps, 2)
    # gross 기준(수수료 무시)이었다면 1000/100000*1e4 = 100bp — 순손익 기준과 달라야 한다
    assert strat["expectancy_bp"] != round(1000.0 / notional * 1e4, 2)


def test_currencies_are_not_mixed_in_equity():
    """같은 날 KR(원화)과 US(달러) 손익이 섞이지 않고, USD→KRW 환산은
    고정환율 FX_KRW_PER_USD 한 곳(equity의 day_pnl 합산)에서만 일어난다."""
    trades = [
        _trade(ts="2026-08-10T00:00:00+00:00", strategy_id="a", symbol="069500",
               side="buy", qty=1, price=10000.0, fee=0.0, realized_pnl=0.0, market="KR"),
        _trade(ts="2026-08-10T01:00:00+00:00", strategy_id="a", symbol="069500",
               side="sell", qty=1, price=10500.0, fee=0.0, realized_pnl=500.0, market="KR"),
        _trade(ts="2026-08-10T02:00:00+00:00", strategy_id="a", symbol="TQQQ",
               side="buy", qty=1, price=70.0, fee=0.0, realized_pnl=0.0, market="US"),
        _trade(ts="2026-08-10T03:00:00+00:00", strategy_id="a", symbol="TQQQ",
               side="sell", qty=1, price=72.0, fee=0.0, realized_pnl=2.0, market="US"),
    ]
    payload = build_performance_payload(trades, EXECUTION_CFG)
    (row,) = payload["equity"]
    expected_krw = 500.0 + 2.0 * FX_KRW_PER_USD
    seed = payload["phases"][0]["seed_krw"]
    assert row["day_pct"] == round(expected_krw / seed * 100, 4)
    # 전략 통계는 통화 무관 bps 축이라 US/KR 라운드트립이 동일 전략에 함께 잡힌다
    (strat,) = payload["strategies"]
    assert strat["trips"] == 2 and set(strat["markets"]) == {"KR", "US"}


def test_seeding_liquidation_excluded_from_performance():
    trades = [
        # 정상 체결 — 성과에 포함
        _trade(ts="2026-08-10T00:00:00+00:00", strategy_id="scalp_1m", symbol="069500",
               side="buy", qty=1, price=10000.0, fee=10.0, realized_pnl=0.0),
        _trade(ts="2026-08-10T01:00:00+00:00", strategy_id="scalp_1m", symbol="069500",
               side="sell", qty=1, price=10100.0, fee=10.0, realized_pnl=100.0),
        # 이식 정리 매도 — 성과에서 제외, excluded에만 집계
        _trade(ts="2026-09-01T13:00:00+00:00", strategy_id="legacy", symbol="SOXL",
               side="sell", qty=13, price=105.67, fee=5.0, realized_pnl=-700.0,
               market="US", reason=f"{SEEDING_LIQUIDATION_MARKER} — 소유자 지시 2026-09-01"),
    ]
    payload = build_performance_payload(trades, EXECUTION_CFG)
    assert payload["period"]["total_fills"] == 2, "이식 정리 매도는 total_fills에서 빠져야 한다"
    strategy_ids = {s["id"] for s in payload["strategies"]}
    assert "legacy" not in strategy_ids
    excl = payload["excluded"]["seeding_liquidation"]
    assert excl["fills"] == 1
    assert excl["usd_impact"] == round(-700.0 - 5.0, 2)
    assert excl["krw_impact"] == 0.0


def test_us_overnight_fill_attributed_to_start_trading_day():
    """KST 2026-08-16 01:00(자정 넘긴 미국 밤 세션 체결)은 KST 08:00 경계 규칙상
    거래일 2026-08-15(그날 한국장의 연장)로 귀속돼야지, 달력상 다음날(08-16)로
    잡히면 안 된다."""
    # KST 2026-08-16T01:00 == UTC 2026-08-15T16:00
    trades = [
        _trade(ts="2026-08-15T16:00:00+00:00", strategy_id="orb_scan", symbol="TQQQ",
               side="buy", qty=1, price=70.0, fee=0.0, realized_pnl=0.0, market="US"),
    ]
    payload = build_performance_payload(trades, EXECUTION_CFG)
    (row,) = payload["equity"]
    assert row["date"] == "2026-08-15"
    assert payload["period"]["start"] == "2026-08-15"
    assert payload["period"]["end"] == "2026-08-15"


def test_sample_warning_below_min_trips():
    trades = [
        _trade(ts="2026-08-10T00:00:00+00:00", strategy_id="orb_scan", symbol="069500",
               side="buy", qty=1, price=10000.0, fee=0.0, realized_pnl=0.0),
        _trade(ts="2026-08-10T01:00:00+00:00", strategy_id="orb_scan", symbol="069500",
               side="sell", qty=1, price=10100.0, fee=0.0, realized_pnl=100.0),
    ]
    payload = build_performance_payload(trades, EXECUTION_CFG)
    (strat,) = payload["strategies"]
    assert strat["trips"] == 1
    assert strat["sample_warning"] is True


def test_sample_warning_false_at_30_trips():
    # 서로 다른 종목코드를 써서 round_trips의 (전략, 종목) 버킷을 분리한다 —
    # 같은 종목에 타임스탬프가 겹치면 매수/매도가 뒤섞여 트립이 합쳐질 수 있다.
    trades = []
    for i in range(30):
        day = 10 + i % 15
        symbol = f"S{i:03d}"
        trades.append(_trade(ts=f"2026-08-{day:02d}T00:00:00+00:00", strategy_id="scalp_1m",
                              symbol=symbol, side="buy", qty=1, price=10000.0, fee=0.0,
                              realized_pnl=0.0))
        trades.append(_trade(ts=f"2026-08-{day:02d}T01:00:00+00:00", strategy_id="scalp_1m",
                              symbol=symbol, side="sell", qty=1, price=10100.0, fee=0.0,
                              realized_pnl=100.0))
    payload = build_performance_payload(trades, EXECUTION_CFG)
    (strat,) = payload["strategies"]
    assert strat["trips"] == 30
    assert strat["sample_warning"] is False


def test_no_forbidden_fields_in_output():
    """종목 코드/수량/계좌 잔고 절대값 문자열이 출력 JSON 어디에도 없어야 한다."""
    trades = [
        _trade(ts="2026-08-10T00:00:00+00:00", strategy_id="scalp_1m", symbol="005930",
               side="buy", qty=6, price=263416.67, fee=10.0, realized_pnl=0.0,
               cash_after=523860.0),
        _trade(ts="2026-08-10T01:00:00+00:00", strategy_id="scalp_1m", symbol="005930",
               side="sell", qty=6, price=270000.0, fee=10.0, realized_pnl=39500.0,
               cash_after=2100000.0),
    ]
    payload = build_performance_payload(trades, EXECUTION_CFG)
    blob = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("005930", "523860", "2100000", "263416"):
        assert forbidden not in blob, f"금지 필드 유출: {forbidden!r}"
    # 구조적으로도 종목/수량 키가 전략 통계에 없어야 한다
    for strat in payload["strategies"]:
        assert set(strat.keys()) == {
            "id", "name_ko", "trips", "wins", "win_rate", "ci_low", "ci_high",
            "expectancy_bp", "verdict", "sample_warning", "markets",
        }


def test_real_seed_krw_derived_from_cash_after_and_usd_proceeds():
    """real_seeded 시대 seed_krw = 마지막 이식 정리 행의 cash_after(KRW) +
    US 이식 정리 매도 체결대금 합(qty*price - fee) * FX_KRW_PER_USD."""
    reason = f"{SEEDING_LIQUIDATION_MARKER} — 소유자 지시 2026-09-01"
    trades = [
        _trade(ts="2026-09-01T13:00:00+00:00", strategy_id="legacy", symbol="009150",
               side="sell", qty=1, price=1401000.0, fee=2802.0, realized_pnl=-34000.0,
               market="KR", reason=reason, cash_after=2979569.0),
        _trade(ts="2026-09-01T13:05:00+00:00", strategy_id="legacy", symbol="SOXL",
               side="sell", qty=13, price=105.67, fee=1.37, realized_pnl=-700.0,
               market="US", reason=reason, cash_after=2979569.0),
    ]
    payload = build_performance_payload(trades, EXECUTION_CFG)
    real_phase = next(p for p in payload["phases"] if p["id"] == "real_seeded")
    expected = round(2979569.0 + (13 * 105.67 - 1.37) * FX_KRW_PER_USD)
    assert real_phase["seed_krw"] == expected


def test_empty_ledger_returns_no_phases_or_equity():
    payload = build_performance_payload([], EXECUTION_CFG)
    assert payload["phases"] == []
    assert payload["equity"] == []
    assert payload["strategies"] == []
    assert payload["period"] == {"start": None, "end": None, "sessions": 0, "total_fills": 0}
