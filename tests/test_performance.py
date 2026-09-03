"""공개 성과 JSON(`quant.control.performance.build_performance_payload`) 테스트.

이 스위트가 고정하는 것 (프롬프트 계약):
- 손익은 수수료 차감 후.
- 지분곡선은 아시아(KRW)/미국(USD)로 완전히 분리되고, FX 환산 없이 각자 통화
  그대로 집계된다(오너 지시 2026-09-02) — `phases[].seed_krw`(총자산 KRW 요약)만
  FX를 쓴다.
- 실계좌 이식 정리 매도는 성과에서 제외되고 `excluded`에만 집계된다.
- 날짜 귀속은 KST 08:00 경계(US 밤 체결도 시작일 거래일로).
- 표본 30건 미만은 `sample_warning: true` — `strategies[].total`과
  `by_market.{asia,us}` 각각 독립으로.
- 종목 코드·수량·계좌 잔고 절대값 문자열이 출력에 없다.
- 사용자 노출 문구는 전부 `_en` 짝을 가지고, 한글이 섞이지 않는다.
"""
from __future__ import annotations

import json
import re

import pytest

from quant.control.performance import (
    FX_KRW_PER_USD,
    PAPER_SEED_KRW,
    PAPER_SEED_USD,
    SEEDING_LIQUIDATION_MARKER,
    build_performance_payload,
)

EXECUTION_CFG = {
    "fee_bps": {"US": 10, "KR": 1.5},
    "kr_stock_sell_tax_bps": 20,
}

_HANGUL_RE = re.compile(r"[가-힣]")


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
    strategies[].total.expectancy_bp는 순손익(880) 기준이어야지 gross(1000)
    기준이면 안 된다."""
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
    assert strat["total"]["trips"] == 1 and strat["total"]["wins"] == 1
    assert strat["total"]["expectancy_bp"] == round(expected_bps, 2)
    # gross 기준(수수료 무시)이었다면 1000/100000*1e4 = 100bp — 순손익 기준과 달라야 한다
    assert strat["total"]["expectancy_bp"] != round(1000.0 / notional * 1e4, 2)


def test_currencies_are_not_mixed_in_equity():
    """아시아(KRW)/미국(USD) 지분곡선이 완전히 분리돼 있고, FX 환산이 곡선
    계산 자체엔 전혀 쓰이지 않는다 — 각자 자기 통화 시드(paper 시대는
    PAPER_SEED_KRW/PAPER_SEED_USD)로만 정규화한다."""
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
    assert payload["equity_asia"]["currency"] == "KRW"
    assert payload["equity_us"]["currency"] == "USD"
    (asia_row,) = payload["equity_asia"]["rows"]
    (us_row,) = payload["equity_us"]["rows"]
    assert asia_row["day_pct"] == round(500.0 / PAPER_SEED_KRW * 100, 4)
    assert us_row["day_pct"] == round(2.0 / PAPER_SEED_USD * 100, 4)
    # 전략 통계는 통화 무관 bps 축이라 total 엔 US/KR 라운드트립이 함께 잡히고,
    # by_market 엔 시장별로 각각 따로 잡힌다.
    (strat,) = payload["strategies"]
    assert strat["total"]["trips"] == 2 and set(strat["total"]["markets"]) == {"KR", "US"}
    assert strat["by_market"]["asia"]["trips"] == 1
    assert strat["by_market"]["us"]["trips"] == 1


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
    # 이 fixture 의 정상 체결 2건은 **이식 경계 이전**(2026-08-10)이라 공개 곡선이
    # 아니라 prior_paper 로 간다(2026-09-02 곡선 재구성). 이식 정리 매도가 어느
    # 집계에도 섞이지 않는다는 것이 이 테스트의 요지 — 그 요지는 그대로 두고
    # 확인 대상만 실제 집계 위치로 옮긴다(약화 아님).
    assert payload["prior_paper"]["fills"] == 2, "이식 정리 매도는 모의 시대 집계에서도 빠져야 한다"
    assert payload["period"]["total_fills"] == 0, "이식 후 정상 체결이 없으므로 곡선은 비어 있다"
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
    assert payload["equity_asia"]["rows"] == []
    (row,) = payload["equity_us"]["rows"]
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
    assert strat["total"]["trips"] == 1
    assert strat["total"]["sample_warning"] is True


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
    assert strat["total"]["trips"] == 30
    assert strat["total"]["sample_warning"] is False


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
            "id", "name_ko", "total", "by_market",
            # 2026-09-02 추가 — 셋 다 종목/수량과 무관한 요약값이다.
            "trades_per_day", "avg_hold_minutes", "enabled",
            # 2026-09-03(F6) — 공개 사이트 EN 로케일용 영문 표시명.
            "name_en",
        }
        assert set(strat["total"].keys()) == {
            "trips", "wins", "win_rate", "ci_low", "ci_high",
            "expectancy_bp", "verdict", "sample_warning", "markets",
        }
        for block in strat["by_market"].values():
            if block is not None:
                assert set(block.keys()) == {
                    "trips", "wins", "win_rate", "ci_low", "ci_high",
                    "expectancy_bp", "verdict", "sample_warning",
                }


def test_real_seed_krw_derived_from_cash_after_and_usd_proceeds():
    """real_seeded 시대 phases[].seed_krw(총자산 KRW 요약) = 마지막 이식 정리
    행의 cash_after(KRW) + US 이식 정리 매도 체결대금 합(qty*price - fee) *
    FX_KRW_PER_USD. (이 값은 phases 스텝퍼 카드 서술용 — 지분곡선 자체의 시드는
    별도로 통화별 분리, 아래 `test_seed_krw_includes_carryover_position_valuation`
    계열 참고.)"""
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
    assert payload["equity_asia"]["rows"] == []
    assert payload["equity_us"]["rows"] == []
    assert payload["strategies"] == []
    assert payload["period"] == {
        "start": None, "end": None, "sessions": 0, "total_fills": 0,
        # 스코프 명시(2026-09-02) — 이식 이벤트가 없으니 아직 paper 시대다.
        "scope": "paper",
        "note": "모의 운용 전체 구간(실계좌 이식 이전)",
        "note_en": "Entire paper-trading period (before the real-account transplant)",
    }
    assert payload["prior_paper"] == {}


# ---------------------------------------------------------------------------
# 2026-09-02 소유자 지시: 경계는 거래일이 아니라 시각, 공개 곡선은 real_seeded만,
# 시드는 이월 보유 포함 총자산 + 지분곡선은 아시아/미국 통화별 분리.
# ---------------------------------------------------------------------------

REASON = f"{SEEDING_LIQUIDATION_MARKER} — 소유자 지시 2026-09-01"

# 실제 이식(2026-09-01T14:01:08 UTC = 23:01:08 KST)과 같은 모양의 경계.
BOUNDARY_TS = "2026-09-01T14:01:08+00:00"

SNAPSHOT = {
    "holdings": [
        {"symbol": "005930", "currency": "KRW", "qty": 6.0, "avg_cost": 263416.666667,
         "price": 255000.0},
        {"symbol": "009150", "currency": "KRW", "qty": 1.0, "avg_cost": 1435000.0,
         "price": 1401000.0},
    ],
}


def _mixed_day_trades():
    """같은 거래일(2026-09-01)에 경계 이전(paper) KR 라운드트립과, 경계
    (KR+US 이식 정리 매도 각각 — 두 통화 풀 모두에 시드를 남긴다), 경계
    이후(real_seeded) US 체결 2건(다른 거래일 2건)이 섞인 원장."""
    return [
        # 경계 이전 — paper 시대, prior_paper로만 집계돼야 한다
        _trade(ts="2026-09-01T00:30:00+00:00", strategy_id="scalp_1m", symbol="069500",
               side="buy", qty=1, price=10000.0, fee=5.0, realized_pnl=0.0, market="KR"),
        _trade(ts="2026-09-01T00:45:00+00:00", strategy_id="scalp_1m", symbol="069500",
               side="sell", qty=1, price=10500.0, fee=5.0, realized_pnl=500.0, market="KR"),
        # 경계(이식 정리 매도, KR) — 성과에서 제외, KRW 풀 최종 cash_after
        _trade(ts=BOUNDARY_TS, strategy_id="legacy", symbol="009150",
               side="sell", qty=1, price=1401000.0, fee=2802.0, realized_pnl=-34000.0,
               market="KR", reason=REASON, cash_after=1000000.0),
        # 경계(이식 정리 매도, US) — 성과에서 제외, USD 풀 시드(FX 미적용, 5000.0)
        _trade(ts=BOUNDARY_TS, strategy_id="legacy", symbol="SOXL",
               side="sell", qty=100, price=50.0, fee=0.0, realized_pnl=-500.0,
               market="US", reason=REASON),
        # 경계 이후, 같은 거래일(2026-09-01) — US 지분곡선에 이 체결만 잡혀야 한다
        _trade(ts="2026-09-01T15:00:00+00:00", strategy_id="gap_fade", symbol="TQQQ",
               side="sell", qty=1, price=71.0, fee=1.0, realized_pnl=100.0, market="US"),
        # 경계 이후, 다음 거래일(2026-09-02)
        _trade(ts="2026-09-02T01:00:00+00:00", strategy_id="gap_fade", symbol="TQQQ",
               side="sell", qty=1, price=70.5, fee=0.5, realized_pnl=50.0, market="US"),
    ]


def test_same_trading_day_boundary_split_by_timestamp_not_date():
    """경계 시각이 하루 중간이면, 그 거래일의 US 지분곡선 행에는 경계 이후
    체결만 잡혀야 한다 — 이전엔 trading_day() 단위로만 나눠 이식 전/후가 한
    점에 섞였다. 이 fixture의 KR 라운드트립은 전부 경계 이전(같은 거래일)이라
    equity_asia에는 아예 안 잡힌다(통화별로 분리된 곡선이라 애초에 섞일 길이
    없다는 것도 함께 확인한다)."""
    payload = build_performance_payload(_mixed_day_trades(), EXECUTION_CFG,
                                         real_account_snapshot=SNAPSHOT)
    assert payload["equity_asia"]["rows"] == [], "경계 이전 KR 라운드트립이 곡선에 새면 안 된다"
    row = next(r for r in payload["equity_us"]["rows"] if r["date"] == "2026-09-01")
    assert row["fills"] == 1
    assert row["phase"] == "real_seeded"
    assert row["day_pct"] == 1.98  # 99(=100-1 수수료) / 5000(USD 시드) * 100
    assert row["cum_pct"] == 1.98, "real_seeded 첫 점은 0에서 출발해야 한다"


def test_real_seeded_equity_curve_starts_at_zero_and_accumulates():
    payload = build_performance_payload(_mixed_day_trades(), EXECUTION_CFG,
                                         real_account_snapshot=SNAPSHOT)
    rows = payload["equity_us"]["rows"]
    assert [r["date"] for r in rows] == ["2026-09-01", "2026-09-02"]
    assert rows[0]["cum_pct"] == rows[0]["day_pct"] == 1.98
    assert rows[1]["day_pct"] == 0.99  # 49.5(=50-0.5) / 5000 * 100
    assert rows[1]["cum_pct"] == 2.97


def test_prior_paper_excluded_from_equity_but_summarized():
    payload = build_performance_payload(_mixed_day_trades(), EXECUTION_CFG,
                                         real_account_snapshot=SNAPSHOT)
    dates = {r["date"] for r in payload["equity_us"]["rows"]}
    dates |= {r["date"] for r in payload["equity_asia"]["rows"]}
    assert "2026-08-31" not in dates  # 경계 이전 날짜 자체가 없다는 것도 재확인
    assert payload["prior_paper"] == {
        "sessions": 1,
        "fills": 2,
        "net_krw": 490,
        "note": (
            "가상 자본 1천만원 시대 — 실계좌 이식 전 기록이라 현재 곡선에 포함하지 않는다"
        ),
        "note_en": (
            "From the virtual-capital (10,000,000 KRW) era — predates the "
            "real-account transition, so it is excluded from the current curve"
        ),
    }


def test_phases_boundary_is_timestamp_not_date():
    payload = build_performance_payload(_mixed_day_trades(), EXECUTION_CFG,
                                         real_account_snapshot=SNAPSHOT)
    paper = next(p for p in payload["phases"] if p["id"] == "paper")
    real = next(p for p in payload["phases"] if p["id"] == "real_seeded")
    assert paper["from"] == "2026-09-01T09:30:00+09:00"
    assert paper["to"] == "2026-09-01T23:01:08+09:00"
    assert real["from"] == "2026-09-01T23:01:08+09:00"
    assert real["to"] is None


def test_seed_krw_includes_carryover_position_valuation():
    """phases[].seed_krw(총자산 KRW 요약) = KR 정리 매도의 cash_after(현금,
    1,000,000) + US 정리 매도 체결대금(5,000 USD) * FX_KRW_PER_USD(1376.7) +
    이월 보유(005930) 6주 × 스냅샷 평단(263,416.67) — 현금만 쓰면 이월 보유분
    (약 158만원)만큼 분모가 작아 수익률이 부풀려진다."""
    payload = build_performance_payload(_mixed_day_trades(), EXECUTION_CFG,
                                         real_account_snapshot=SNAPSHOT)
    real = next(p for p in payload["phases"] if p["id"] == "real_seeded")
    # 1,000,000 + 5,000*1376.7 + round(6*263,416.666667) = 9,464,000
    assert real["seed_krw"] == 9464000
    assert real["seed_basis"] == "현금+이월보유"


def test_seed_krw_falls_back_to_cash_only_without_snapshot_file():
    """스냅샷이 없는 환경(로컬 테스트, 스냅샷 파일 미존재)에서도 정상 동작해야
    한다 — 이월 보유 평가액 없이 현금만으로 폴백하고 그 사실을 note에 남긴다."""
    payload = build_performance_payload(_mixed_day_trades(), EXECUTION_CFG)
    real = next(p for p in payload["phases"] if p["id"] == "real_seeded")
    assert real["seed_krw"] == 7883500  # 1,000,000 + 5,000*1376.7
    assert real["seed_basis"] == "현금만"
    assert "스냅샷 파일 없어" in real["note"]


def test_no_forbidden_fields_with_carryover_snapshot():
    """이월 보유 스냅샷을 넣어도 종목코드·평단·수량이 출력에 새지 않아야 한다 —
    시드 총액 하나로만 녹여야 한다."""
    payload = build_performance_payload(_mixed_day_trades(), EXECUTION_CFG,
                                         real_account_snapshot=SNAPSHOT)
    blob = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("005930", "009150", "263416", "1435000", "1401000"):
        assert forbidden not in blob, f"금지 필드 유출: {forbidden!r}"


def test_period_fills_counts_only_the_published_curve():
    """period 의 start/end/sessions 가 이식 후 구간 기준이면 total_fills 도 그래야 한다.

    2026-09-02 실측: 이식 후 2거래일(53체결)인데 total_fills 가 전체 602(모의
    549 포함)로 나가 사이트 히어로에 "2 Trading days / 602 Fills" 라는 모순된
    숫자가 찍혔다. 이식 전 체결 수는 prior_paper.fills 로 따로 보인다. 아시아/
    미국 두 곡선으로 나뉜 뒤에도 total_fills 는 두 곡선 fills 의 합이어야 한다."""
    from quant.control.performance import build_performance_payload

    trades = [
        # 이식 전(모의) 2건
        {"ts": "2026-09-01T00:10:00+00:00", "strategy_id": "s", "symbol": "005930",
         "side": "buy", "qty": 1, "price": 1000.0, "fee": 1.0, "market": "KR"},
        {"ts": "2026-09-01T01:10:00+00:00", "strategy_id": "s", "symbol": "005930",
         "side": "sell", "qty": 1, "price": 1010.0, "fee": 1.0,
         "realized_pnl": 10.0, "market": "KR"},
        # 이식 정리(경계)
        {"ts": "2026-09-01T14:01:08+00:00", "strategy_id": "seed", "symbol": "005930",
         "side": "sell", "qty": 1, "price": 1000.0, "fee": 1.0, "realized_pnl": 0.0,
         "market": "KR", "reason": "실계좌 이식 정리 — 레거시", "cash_after": 1_000_000.0},
        # 이식 후 1건
        {"ts": "2026-09-01T15:00:00+00:00", "strategy_id": "s", "symbol": "005930",
         "side": "buy", "qty": 1, "price": 1000.0, "fee": 1.0, "market": "KR"},
    ]
    out = build_performance_payload(trades, {})
    curve_fills = sum(r["fills"] for r in out["equity_asia"]["rows"])
    curve_fills += sum(r["fills"] for r in out["equity_us"]["rows"])
    assert out["period"]["total_fills"] == curve_fills
    assert out["period"]["total_fills"] == 1          # 이식 후 체결만
    assert out["prior_paper"]["fills"] == 2           # 모의 시대는 따로 보존


# ---------------------------------------------------------------------------
# 2026-09-02 소유자 지시: 영문 짝(_en) + 렌더 준비 완료 JSON.
# ---------------------------------------------------------------------------


def test_every_user_facing_note_has_an_english_pair():
    """disclaimer/phases.note/phases.seed_basis/prior_paper.note/
    excluded.note/costs.note/equity_*.seed_basis 전부 `_en` 짝이 있어야
    한다 — 없으면 프론트가 한/영 대응표를 못 찾아 한글이 그대로 샌다."""
    payload = build_performance_payload(_mixed_day_trades(), EXECUTION_CFG,
                                         real_account_snapshot=SNAPSHOT)
    assert payload["disclaimer_en"]
    for phase in payload["phases"]:
        assert phase.get("note_en"), phase
        assert phase.get("label_en"), phase
        if "seed_basis" in phase:
            assert phase.get("seed_basis_en"), phase
    assert payload["prior_paper"].get("note_en")
    assert payload["excluded"]["seeding_liquidation"].get("note_en")
    assert payload["costs"].get("note_en")
    for book in (payload["equity_asia"], payload["equity_us"]):
        assert book.get("seed_basis_en")


def test_english_fields_contain_no_hangul():
    """`_en` 문구 자체에 한글이 섞이면(반쪽 번역) 영어 화면에 한/영 혼재가
    남는다 — 실제로 관측된 결함(단계 카드/각주에 한글 문장이 그대로 붙어
    나온 것)의 재발 방지."""
    payload = build_performance_payload(_mixed_day_trades(), EXECUTION_CFG,
                                         real_account_snapshot=SNAPSHOT)

    def _en_values(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.endswith("_en"):
                    yield f"{path}.{k}", v
                else:
                    yield from _en_values(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                yield from _en_values(v, f"{path}[{i}]")

    for path, value in _en_values(payload):
        if isinstance(value, str):
            assert not _HANGUL_RE.search(value), f"{path} 에 한글 잔존: {value!r}"


def test_strategy_stats_split_by_market_with_own_sample_threshold():
    """by_market.asia/us 는 각자 시장의 라운드트립만으로 독립적으로
    win_rate/CI/sample_warning을 계산해야 한다 — total(통화 무관 합산)과
    섞이면 안 된다."""
    trades = [
        _trade(ts="2026-08-10T00:00:00+00:00", strategy_id="confluence", symbol="069500",
               side="buy", qty=1, price=10000.0, fee=0.0, realized_pnl=0.0, market="KR"),
        _trade(ts="2026-08-10T01:00:00+00:00", strategy_id="confluence", symbol="069500",
               side="sell", qty=1, price=10100.0, fee=0.0, realized_pnl=100.0, market="KR"),
        _trade(ts="2026-08-10T02:00:00+00:00", strategy_id="confluence", symbol="TQQQ",
               side="buy", qty=1, price=70.0, fee=0.0, realized_pnl=0.0, market="US"),
        _trade(ts="2026-08-10T03:00:00+00:00", strategy_id="confluence", symbol="TQQQ",
               side="sell", qty=1, price=69.0, fee=0.0, realized_pnl=-1.0, market="US"),
    ]
    payload = build_performance_payload(trades, EXECUTION_CFG)
    (strat,) = payload["strategies"]
    assert strat["total"]["trips"] == 2
    assert strat["by_market"]["asia"]["trips"] == 1
    assert strat["by_market"]["asia"]["win_rate"] == 1.0
    assert strat["by_market"]["us"]["trips"] == 1
    assert strat["by_market"]["us"]["win_rate"] == 0.0
    # 표본이 없는 시장은 지어내지 않고 None
    assert strat["by_market"]["asia"] is not None and strat["by_market"]["us"] is not None


def test_by_market_is_none_when_no_trips_in_that_market():
    trades = [
        _trade(ts="2026-08-10T00:00:00+00:00", strategy_id="scalp_1m", symbol="069500",
               side="buy", qty=1, price=10000.0, fee=0.0, realized_pnl=0.0, market="KR"),
        _trade(ts="2026-08-10T01:00:00+00:00", strategy_id="scalp_1m", symbol="069500",
               side="sell", qty=1, price=10100.0, fee=0.0, realized_pnl=100.0, market="KR"),
    ]
    payload = build_performance_payload(trades, EXECUTION_CFG)
    (strat,) = payload["strategies"]
    assert strat["by_market"]["us"] is None
    assert strat["by_market"]["asia"] is not None


def test_equity_chart_axis_is_render_ready():
    """`equity_us.chart.y_axis`는 프론트가 하던 min/max/18% 패딩/5눈금 계산을
    그대로 옮긴 값이어야 한다 — 0이 항상 눈금에 포함되고 min<=0<=max."""
    payload = build_performance_payload(_mixed_day_trades(), EXECUTION_CFG,
                                         real_account_snapshot=SNAPSHOT)
    axis = payload["equity_us"]["chart"]["y_axis"]
    assert axis["min"] <= 0.0 <= axis["max"]
    assert len(axis["ticks"]) == 5
    assert axis["ticks"][2] == 0.0
    assert axis["zero"] == 0.0
    # equity_asia 는 이 fixture에서 빈 곡선이라 폴백 범위(-1..1)를 써야 한다
    empty_axis = payload["equity_asia"]["chart"]["y_axis"]
    assert empty_axis == {"min": -1.0, "max": 1.0, "ticks": [1.0, 0.5, 0.0, -0.5, -1.0], "zero": 0.0}


# ---------------------------------------------------------------------------
# 2026-09-02: 한 JSON 안의 두 스코프 + 렌더 준비 지표 (F4).
# ---------------------------------------------------------------------------

_SEED_REASON = "실계좌 이식 정리 — 소유자 지시 2026-09-01"


def _transplanted_ledger() -> list[dict]:
    """모의 시대 왕복 1건 + 이식 정리 1건 + 이식 이후 왕복 2건."""
    return [
        _trade(ts="2026-08-20T00:10:00+00:00", strategy_id="gap_fade", symbol="TQQQ",
               side="buy", qty=1, price=70.0, fee=0.07, market="US"),
        _trade(ts="2026-08-20T01:10:00+00:00", strategy_id="gap_fade", symbol="TQQQ",
               side="sell", qty=1, price=71.0, fee=0.07, realized_pnl=1.0, market="US"),
        _trade(ts="2026-09-01T14:01:08+00:00", strategy_id="legacy", symbol="SOXL",
               side="sell", qty=13, price=105.67, fee=1.4, realized_pnl=-706.42,
               market="US", reason=_SEED_REASON, cash_after=2_979_569.0),
        _trade(ts="2026-09-01T15:00:00+00:00", strategy_id="gap_fade", symbol="TQQQ",
               side="buy", qty=1, price=69.2, fee=0.07, market="US"),
        _trade(ts="2026-09-01T16:00:00+00:00", strategy_id="gap_fade", symbol="TQQQ",
               side="sell", qty=1, price=70.5, fee=0.08, realized_pnl=1.3, market="US"),
        _trade(ts="2026-09-02T15:00:00+00:00", strategy_id="gap_fade", symbol="TQQQ",
               side="buy", qty=1, price=70.0, fee=0.07, market="US"),
        _trade(ts="2026-09-02T16:00:00+00:00", strategy_id="gap_fade", symbol="TQQQ",
               side="sell", qty=1, price=69.0, fee=0.07, realized_pnl=-1.0, market="US"),
    ]


def test_scope_fields_say_which_window_each_block_covers():
    """히어로("세션 2 · 체결 53")와 257왕복 표가 나란히 찍혀 JSON이 스스로 모순돼
    보였다 — 표를 줄이는 대신(표본이 0에 가까워진다) 스코프를 명시한다."""
    payload = build_performance_payload(_transplanted_ledger(), EXECUTION_CFG)
    assert payload["period"]["scope"] == "real_seeded"
    assert payload["period"]["note"] == "실계좌 이식 이후"
    assert payload["period"]["note_en"] == "Since real-account transplant"
    assert payload["strategies_scope"] == "lifetime"
    # enabled_count 는 settings 기준 — 왕복 기록이 없는 활성 전략도 센다
    assert payload["enabled_count"] == 0
    cfg = {"a": {"enabled": True}, "b": {"enabled": False}, "never_traded": {"enabled": True}}
    with_cfg = build_performance_payload(_transplanted_ledger(), EXECUTION_CFG, strategies_cfg=cfg)
    assert with_cfg["enabled_count"] == 2
    trips = sum(s["total"]["trips"] for s in payload["strategies"])
    assert str(trips) in payload["strategies_note"]
    assert str(trips) in payload["strategies_note_en"]
    # 전략 표는 모의 시대를 포함한 누적 — period(이식 후 2일)보다 넓다
    assert trips == 3
    assert payload["period"]["sessions"] == 2


def test_strategy_stats_include_turnover_hold_and_enabled():
    payload = build_performance_payload(
        _transplanted_ledger(), EXECUTION_CFG,
        strategies_cfg={"gap_fade": {"enabled": True}},
    )
    gap = next(s for s in payload["strategies"] if s["id"] == "gap_fade")
    assert gap["enabled"] is True
    assert gap["trades_per_day"] == 1.0          # 3왕복 / 3거래일
    assert gap["avg_hold_minutes"] == 60.0


def test_enabled_is_false_when_strategy_absent_from_settings():
    """모르면 꺼진 것으로 본다 — 켜져 있다고 지어내지 않는다."""
    payload = build_performance_payload(_transplanted_ledger(), EXECUTION_CFG)
    assert all(s["enabled"] is False for s in payload["strategies"])


def test_max_drawdown_is_computed_from_cum_pct():
    payload = build_performance_payload(_transplanted_ledger(), EXECUTION_CFG)
    rows = payload["equity_us"]["rows"]
    assert len(rows) == 2 and rows[0]["cum_pct"] > rows[1]["cum_pct"]
    peak = 1 + rows[0]["cum_pct"] / 100
    trough = 1 + rows[1]["cum_pct"] / 100
    assert payload["equity_us"]["max_drawdown_pct"] == pytest.approx(
        round((peak - trough) / peak * 100, 4)
    )
    # 아시아 곡선은 이 원장에 점이 없다 — 낙폭은 두 점이 있어야 정의된다
    assert payload["equity_asia"]["max_drawdown_pct"] is None


def test_fee_drag_is_measured_on_post_boundary_rows_only():
    payload = build_performance_payload(_transplanted_ledger(), EXECUTION_CFG)
    # 이식 후 gross = 1.3 - 1.0 = 0.3, 수수료 = 0.07+0.08+0.07+0.07 = 0.29
    assert payload["costs"]["fee_drag_pct_of_gross"] == pytest.approx(96.67, abs=0.02)


def test_fee_drag_is_null_when_gross_is_zero():
    """0으로 나눌 수 없으면 None — 0%로 위장하지 않는다."""
    trades = [
        _trade(ts="2026-08-10T00:00:00+00:00", strategy_id="s", symbol="TQQQ",
               side="buy", qty=1, price=70.0, fee=0.07, market="US"),
    ]
    payload = build_performance_payload(trades, EXECUTION_CFG)
    assert payload["costs"]["fee_drag_pct_of_gross"] is None


def test_strategy_table_gains_the_trip_hidden_by_transplant_phantom_inventory():
    """이식 시점에 열려 있던 lot 을 안 버리면 이식 이후 왕복이 통째로 사라진다
    (실측: 2026-09-01 gap_fade TQQQ +$1.13)."""
    trades = [
        # 이식 시점에 열려 있던(상계 행 없이 사라진) 매수
        _trade(ts="2026-09-01T13:50:00+00:00", strategy_id="gap_fade", symbol="TQQQ",
               side="buy", qty=9, price=69.4, fee=0.62, market="US"),
        _trade(ts="2026-09-01T14:01:08+00:00", strategy_id="legacy", symbol="SOXL",
               side="sell", qty=13, price=105.67, fee=1.4, realized_pnl=-706.42,
               market="US", reason=_SEED_REASON, cash_after=2_979_569.0),
        _trade(ts="2026-09-01T14:06:20+00:00", strategy_id="gap_fade", symbol="TQQQ",
               side="buy", qty=1, price=69.2, fee=0.07, market="US"),
        _trade(ts="2026-09-01T15:47:26+00:00", strategy_id="gap_fade", symbol="TQQQ",
               side="sell", qty=1, price=70.5, fee=0.08, realized_pnl=1.29, market="US"),
    ]
    payload = build_performance_payload(trades, EXECUTION_CFG)
    gap = next(s for s in payload["strategies"] if s["id"] == "gap_fade")
    assert gap["total"]["trips"] == 1


def test_every_enabled_strategy_has_non_id_name_in_both_languages():
    """F6(2026-09-03) — 공개 대시보드에 켜진 전략의 원문 id가 그대로 새면
    안 된다(감사 #6, orb_rvol의 KO 이름이 없어 id가 그대로 노출됐었다).
    KO/EN 둘 다 사람이 붙인 표시명이어야 한다."""
    import yaml

    from quant.control.performance import _strategy_name_en, _strategy_name_ko

    with open("config/settings.yaml", encoding="utf-8") as f:
        strategies_cfg = yaml.safe_load(f)["strategies"]

    checked = 0
    for sid, params in strategies_cfg.items():
        if not isinstance(params, dict) or not params.get("enabled"):
            continue
        checked += 1
        ko = _strategy_name_ko(sid)
        en = _strategy_name_en(sid)
        assert ko and ko != sid, f"{sid}: KO 표시명 없음(원문 id 노출)"
        assert en and en != sid, f"{sid}: EN 표시명 없음(원문 id 노출)"
    assert checked > 0, "config/settings.yaml에 활성 전략이 하나도 없다 — 가드가 공회전 중"
