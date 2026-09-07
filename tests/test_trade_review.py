"""quant/control/trade_review.py 테스트 — 전부 합성 fills/bars, 오프라인."""
from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from quant.control.trade_review import (
    MARKET_TZ_LABEL,
    build_trade_review,
    compute_band,
    format_telegram_line,
    parse_entry_reason,
    parse_exit_reason,
    to_market_local_iso,
)

KR_TZ_OFFSET = "+09:00"


def _fill(strategy, symbol, side, qty, price, ts, *, reason="", fee=0.0, pnl=None, market="KR"):
    return {
        "ts": ts, "strategy_id": strategy, "symbol": symbol, "side": side,
        "qty": qty, "price": price, "fee": fee, "realized_pnl": pnl,
        "reason": reason, "market": market, "cash_after": None,
    }


def _bars(symbol, start: datetime, n: int, *, open_=100.0, step=0.0, high_boost=0.0, low_cut=0.0, tz="UTC"):
    # DataFeed 계약(quant.core.ports)대로 tz-aware 인덱스 — 실제 어댑터가 주는 형태.
    # `tz`(2026-09-07 추가)는 야후가 KR/US 개별 종목에 실제로 주는 거래소 로컬
    # tz를 흉내내는 용도 — 체결(UTC)과 다른 오프셋을 달고 오는 상황을 재현한다.
    idx = [start + timedelta(minutes=i) for i in range(n)]
    rows = []
    for i in range(n):
        c = open_ + step * i
        rows.append({"open": c, "high": c + high_boost, "low": c - low_cut, "close": c, "volume": 1000})
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, tz=tz))


# ------------------------------------------------------------------ 파싱

def test_parse_entry_reason_scalp_1m():
    reason = "1분봉 스캘프 패턴B 진입: 105560 w=0.50 손절=173,877 (기준=173,857) [구조손절:swing_low] [게이트:통과] [W%R:통과]"
    parsed = parse_entry_reason(reason)
    assert parsed["pattern"] == "1분봉 스캘프 패턴B 진입"
    assert parsed["weight"] == 0.50
    assert parsed["stop_price"] == 173877.0
    assert parsed["basis_price"] == 173857.0
    assert parsed["gates"] == ["구조손절:swing_low", "게이트:통과", "W%R:통과"]


def test_parse_entry_reason_vol_breakout():
    reason = "변동성 돌파 진입: 122630 w=0.50 트리거=112,110 현재=112,120 손절=111,128 [k=0.5 전일범위=1,200 시가=112,000]"
    parsed = parse_entry_reason(reason)
    assert parsed["trigger_price"] == 112110.0
    assert parsed["stop_price"] == 111128.0
    assert parsed["target_price"] is None  # vol_breakout엔 고정 목표가 없다 — EoD/손절만


def test_parse_entry_reason_close_bet_percent():
    reason = "종가배팅: 마감강도 0.85 양봉 · 내일 시초 갭 노림 (손절 -1% / 익절 +2%)"
    parsed = parse_entry_reason(reason)
    assert parsed["stop_pct"] == 1.0
    assert parsed["target_pct"] == 2.0
    assert parsed["stop_price"] is None


def test_parse_entry_reason_news_momentum_percent():
    reason = "뉴스 모멘텀 개장진입(EVENT): 042660 w=0.08 손절 -3%(KR 확인 매매)"
    parsed = parse_entry_reason(reason)
    assert parsed["stop_pct"] == 3.0


def test_parse_entry_reason_frgn_accumulate_has_no_band():
    reason = "적립 매수(FRGN): 003490 1주 @ 30,200"
    parsed = parse_entry_reason(reason)
    assert parsed["stop_price"] is None
    assert parsed["stop_pct"] is None
    assert parsed["target_price"] is None


def test_parse_exit_reason_stop_loss():
    parsed = parse_exit_reason("손절: entry=21,300 stop=21,211 현재=21,200")
    assert parsed["kind"] == "손절"
    assert parsed["entry_price"] == 21300.0
    assert parsed["price"] == 21200.0


# ------------------------------------------------------------------ 밴드

def test_compute_band_from_reason_price():
    parsed = parse_entry_reason("1분봉 스캘프 패턴B 진입: 105560 w=0.50 손절=173,877 (기준=173,857)")
    band = compute_band("scalp_1m", 174000.0, parsed, {})
    assert band["stop"] == 173877.0
    assert "reason:손절" in band["band_source"]
    assert band["target"] is None


def test_compute_band_from_reason_percent():
    parsed = parse_entry_reason("종가배팅: ... (손절 -1% / 익절 +2%)")
    band = compute_band("close_bet", 10000.0, parsed, {})
    assert band["stop"] == pytest.approx(9900.0)
    assert band["target"] == pytest.approx(10200.0)
    assert "reason:손절%" in band["band_source"]


def test_compute_band_from_params_when_reason_lacks_it():
    parsed = parse_entry_reason("종가배팅: 마감강도 0.85 양봉")  # 비율 표기가 없는 경우
    strategy_params = {"close_bet": {"params": {"stop_pct": 1.0, "take_profit_pct": 2.0}}}
    band = compute_band("close_bet", 10000.0, parsed, strategy_params)
    assert band["stop"] == pytest.approx(9900.0)
    assert band["target"] == pytest.approx(10200.0)
    assert "params:stop_pct" in band["band_source"]
    assert "params:take_profit_pct" in band["band_source"]


def test_compute_band_frgn_accumulate_uses_risk_max_loss():
    parsed = parse_entry_reason("적립 매수(FRGN): 003490 1주 @ 30,200")
    band = compute_band("frgn_accumulate", 30200.0, parsed, {}, {"accumulate_max_loss_pct": 15.0})
    assert band["stop"] == pytest.approx(30200.0 * 0.85)
    assert "risk.accumulate_max_loss_pct" in band["band_source"]
    assert band["target"] is None


def test_compute_band_unknown_when_nothing_available():
    parsed = parse_entry_reason("이유 없음")
    band = compute_band("mystery_strategy", 100.0, parsed, {})
    assert band["band_source"] == "unknown"
    assert band["stop"] is None and band["target"] is None


# ------------------------------------------------------------------ build_trade_review

def test_build_trade_review_closed_trip_mfe_mae_and_fees():
    entry_ts = f"2026-09-07T00:30:00{'+00:00'}"
    exit_ts = f"2026-09-07T00:40:00{'+00:00'}"
    fills = [
        _fill("scalp_1m", "105560", "buy", 10, 100.0, entry_ts,
              reason="1분봉 스캘프 패턴B 진입: 105560 w=0.50 손절=95 (기준=99)"),
        _fill("scalp_1m", "105560", "sell", 10, 110.0, exit_ts,
              reason="60선 이탈(잔량 트레일): 종가=110 MA60=108", pnl=100.0, fee=2.0),
    ]
    bars = _bars("105560", datetime(2026, 9, 7, 0, 30), 15, open_=100.0, step=1.0, high_boost=2.0, low_cut=1.0)
    review = build_trade_review(fills, {"105560": bars}, {}, "KR", date(2026, 9, 7))
    assert len(review["groups"]) == 1
    g = review["groups"][0]
    assert g["status"] == "closed"
    assert g["pnl_known"] is True
    assert g["pnl"] == pytest.approx(98.0)  # 100 realized - 2 fee
    assert g["pnl_bp"] == pytest.approx(98.0 / 1000.0 * 1e4)
    assert g["holding_minutes"] == pytest.approx(10.0)
    assert g["mfe_bp"] is not None and g["mae_bp"] is not None
    assert g["band"]["stop"] == 95.0
    assert g["thinking"]["entry_parsed"]["pattern"] == "1분봉 스캘프 패턴B 진입"
    assert len(g["bars_window"]) > 0
    assert set(g["bars_window"][0].keys()) == {"ts", "open", "high", "low", "close", "ma60"}


def test_build_trade_review_open_trip_has_no_exit():
    entry_ts = "2026-09-07T05:50:00+00:00"
    fills = [
        _fill("frgn_accumulate", "003490", "buy", 1, 30200.0, entry_ts,
              reason="적립 매수(FRGN): 003490 1주 @ 30,200"),
    ]
    bars = _bars("003490", datetime(2026, 9, 7, 5, 50), 5, open_=30200.0)
    review = build_trade_review(fills, {"003490": bars}, {}, "KR", date(2026, 9, 7),
                                 risk_params={"accumulate_max_loss_pct": 15.0})
    g = review["groups"][0]
    assert g["status"] == "open"
    assert g["exits"] == []
    assert g["pnl_known"] is False
    assert g["pnl"] is None
    assert g["band"]["stop"] == pytest.approx(30200.0 * 0.85)


def test_build_trade_review_skips_paper_epoch_marker():
    fills = [
        {"ts": "2026-09-07T00:00:00+09:00", "strategy_id": "epoch", "symbol": "_EPOCH_",
         "side": "buy", "qty": 0, "price": 0, "fee": 0, "realized_pnl": None,
         "reason": "페이퍼 에폭 리셋 — capital_policy=fixed_dual", "market": "US"},
        _fill("scalp_1m", "105560", "buy", 10, 100.0, "2026-09-07T00:30:00+00:00",
              reason="1분봉 스캘프 패턴B 진입: 105560 w=0.50 손절=95"),
    ]
    review = build_trade_review(fills, {}, {}, "KR", date(2026, 9, 7))
    assert all(g["strategy_id"] != "epoch" for g in review["groups"])
    assert len(review["groups"]) == 1


def test_build_trade_review_missing_bars_yields_no_mfe_mae():
    fills = [
        _fill("scalp_1m", "999999", "buy", 10, 100.0, "2026-09-07T00:30:00+00:00", reason="진입"),
        _fill("scalp_1m", "999999", "sell", 10, 105.0, "2026-09-07T00:40:00+00:00", reason="청산", pnl=50.0),
    ]
    review = build_trade_review(fills, {}, {}, "KR", date(2026, 9, 7))
    g = review["groups"][0]
    assert g["bars_available"] is False
    assert g["mfe_bp"] is None and g["mae_bp"] is None


def test_build_trade_review_skips_orphan_sell_without_prior_buy():
    # 전날 진입 후 오늘 아침 청산된 오버나이트 포지션 — 오늘 "진입"이 아니므로 스킵.
    fills = [
        _fill("close_bet", "005930", "sell", 5, 71000.0, "2026-09-07T00:05:00+00:00",
              reason="종가배팅 익절: 시초 갭 실현 (진입 70,000 → 71,000)", pnl=5000.0),
    ]
    review = build_trade_review(fills, {}, {}, "KR", date(2026, 9, 7))
    assert review["groups"] == []


def test_build_trade_review_filters_by_market_and_date():
    fills = [
        _fill("scalp_1m", "105560", "buy", 10, 100.0, "2026-09-06T00:30:00+00:00", reason="진입", market="KR"),
        _fill("scalp_1m", "105560", "sell", 10, 105.0, "2026-09-06T00:40:00+00:00", reason="청산", pnl=50.0, market="KR"),
        _fill("gap_fade", "AAPL", "buy", 1, 200.0, "2026-09-07T14:35:00+00:00", reason="진입", market="US"),
    ]
    review = build_trade_review(fills, {}, {}, "KR", date(2026, 9, 7))
    assert review["groups"] == []  # 다른 날짜/다른 시장 — 둘 다 안 잡혀야 한다


def test_day_summary_win_rate_and_best_worst():
    fills = [
        _fill("scalp_1m", "AAA", "buy", 10, 100.0, "2026-09-07T00:30:00+00:00", reason="진입 w=0.5 손절=90"),
        _fill("scalp_1m", "AAA", "sell", 10, 110.0, "2026-09-07T00:40:00+00:00", reason="익절", pnl=100.0),
        _fill("scalp_1m", "BBB", "buy", 10, 100.0, "2026-09-07T01:30:00+00:00", reason="진입 w=0.5 손절=90"),
        _fill("scalp_1m", "BBB", "sell", 10, 95.0, "2026-09-07T01:40:00+00:00", reason="손절", pnl=-50.0),
    ]
    review = build_trade_review(fills, {}, {}, "KR", date(2026, 9, 7))
    per = review["summary"]["per_strategy"]["scalp_1m"]
    assert per["n"] == 2
    assert per["win_rate"] == pytest.approx(0.5)
    assert per["best"] == "AAA"
    assert per["worst"] == "BBB"
    totals = review["summary"]["totals"]
    assert totals["n"] == 2
    assert totals["net_bp"] == pytest.approx(per["net_bp"])


def test_day_summary_excludes_pnl_unknown_trips():
    fills = [
        _fill("scalp_1m", "AAA", "buy", 10, 100.0, "2026-09-07T00:30:00+00:00", reason="진입"),
        _fill("scalp_1m", "AAA", "sell", 10, 110.0, "2026-09-07T00:40:00+00:00", reason="익절", pnl=None),
    ]
    review = build_trade_review(fills, {}, {}, "KR", date(2026, 9, 7))
    g = review["groups"][0]
    assert g["pnl_known"] is False
    per = review["summary"]["per_strategy"]["scalp_1m"]
    assert per["n"] == 0
    assert per["net_bp"] is None


def test_format_telegram_line_empty_when_no_trades():
    review = build_trade_review([], {}, {}, "KR", date(2026, 9, 7))
    assert format_telegram_line(review) == ""


def test_format_telegram_line_matches_expected_shape():
    fills = [
        _fill("scalp_1m", "105560", "buy", 10, 100.0, "2026-09-07T00:30:00+00:00", reason="진입"),
        _fill("scalp_1m", "105560", "sell", 10, 110.0, "2026-09-07T00:40:00+00:00", reason="익절", pnl=100.0),
    ]
    review = build_trade_review(fills, {}, {}, "KR", date(2026, 9, 7))
    line = format_telegram_line(review)
    assert line.startswith("📈 매매 리뷰 KR 09-07: 1트립")
    assert "승률 100%" in line
    assert "최고 scalp_1m 105560" in line


def test_format_telegram_line_appends_url():
    fills = [
        _fill("scalp_1m", "105560", "buy", 10, 100.0, "2026-09-07T00:30:00+00:00", reason="진입"),
        _fill("scalp_1m", "105560", "sell", 10, 110.0, "2026-09-07T00:40:00+00:00", reason="익절", pnl=100.0),
    ]
    review = build_trade_review(fills, {}, {}, "KR", date(2026, 9, 7))
    line = format_telegram_line(review, url="https://example.com/x.html")
    assert line.endswith("https://example.com/x.html")


# ------------------------------------------------------------------ 시간대 (2026-09-07 수리)

def test_to_market_local_iso_converts_utc_to_kst_and_strips_offset():
    dt = datetime(2026, 9, 7, 0, 30, 32, tzinfo=UTC)
    assert to_market_local_iso(dt, "KR") == "2026-09-07T09:30:32"


def test_to_market_local_iso_converts_utc_to_et_and_strips_offset():
    # 2026-09-07은 미 동부 서머타임(EDT, UTC-4) 구간.
    dt = datetime(2026, 9, 7, 13, 30, 0, tzinfo=UTC)
    assert to_market_local_iso(dt, "US") == "2026-09-07T09:30:00"


def test_market_tz_label_kr_and_us():
    assert MARKET_TZ_LABEL["KR"] == "KST"
    assert MARKET_TZ_LABEL["US"] == "ET"


def test_build_trade_review_entries_shown_in_kst_not_raw_utc():
    # 실측 버그: 원장 ts는 UTC 오프셋("...+00:00")으로 기록되는데 그 화면
    # 표시가 그 리터럴 숫자를 그대로 보여줘 09:30 KST 체결이 "00:30"으로
    # 보였다. entries[].ts는 이제 오프셋 없는 KST 벽시계여야 한다.
    fills = [
        _fill("scalp_1m", "105560", "buy", 10, 100.0, "2026-09-07T00:30:32+00:00",
              reason="1분봉 스캘프 패턴B 진입: 105560 w=0.50 손절=95"),
        _fill("scalp_1m", "105560", "sell", 10, 110.0, "2026-09-07T00:40:00+00:00",
              reason="60선 이탈", pnl=100.0),
    ]
    review = build_trade_review(fills, {}, {}, "KR", date(2026, 9, 7))
    g = review["groups"][0]
    assert g["entries"][0]["ts"] == "2026-09-07T09:30:32"
    assert g["exits"][0]["ts"] == "2026-09-07T09:40:00"
    assert g["tz_label"] == "KST"
    assert "+00:00" not in g["entries"][0]["ts"]


def test_build_trade_review_bars_window_ts_is_kst_local_matching_entries():
    entry_ts = "2026-09-07T00:30:00+00:00"  # = 09:30 KST
    exit_ts = "2026-09-07T00:40:00+00:00"   # = 09:40 KST
    fills = [
        _fill("scalp_1m", "105560", "buy", 10, 100.0, entry_ts, reason="진입 손절=95"),
        _fill("scalp_1m", "105560", "sell", 10, 110.0, exit_ts, reason="청산", pnl=100.0),
    ]
    # 야후 KR 봉은 거래소 로컬(KST)로 이미 와 있다고 가정 — 실제 관측과 동일.
    bars = _bars("105560", datetime(2026, 9, 7, 9, 25, tzinfo=None), 20,
                 open_=100.0, step=0.5, tz="Asia/Seoul")
    review = build_trade_review(fills, {"105560": bars}, {}, "KR", date(2026, 9, 7))
    g = review["groups"][0]
    assert len(g["bars_window"]) > 1
    bar_ts = [b["ts"] for b in g["bars_window"]]
    # 봉·체결 둘 다 "2026-09-07T09:.." 로컬 KST 벽시계로 같은 자릿수 체계다 —
    # 하나가 UTC(00:xx)로 남아 있으면 이 assert가 깨진다.
    assert all(t.startswith("2026-09-07T09:") or t.startswith("2026-09-07T10:") for t in bar_ts)
    assert g["entries"][0]["ts"] in bar_ts or (min(bar_ts) <= g["entries"][0]["ts"] <= max(bar_ts))


def test_coordinator_regression_kst_marker_falls_inside_kst_bar_range():
    """오너 지적 재현 테스트 — KR 09:30 KST 체결이 09:00~10:00 KST 봉 범위
    "안"에 찍혀야 한다(전엔 봉이 09:00대, 마커가 00:30대로 서로 다른 위치에
    찍혔다). 데이터(JSON) 레벨과 손그림 SVG 렌더 레벨 둘 다 검증한다."""
    entry_ts = "2026-09-07T00:30:00+00:00"  # = 09:30 KST
    exit_ts = "2026-09-07T00:45:00+00:00"   # = 09:45 KST
    fills = [
        _fill("scalp_1m", "105560", "buy", 10, 100.0, entry_ts, reason="진입 손절=95"),
        _fill("scalp_1m", "105560", "sell", 10, 102.0, exit_ts, reason="청산", pnl=20.0),
    ]
    bars = _bars("105560", datetime(2026, 9, 7, 9, 0, tzinfo=None), 60,
                 open_=100.0, step=0.05, tz="Asia/Seoul")  # 09:00~10:00 KST
    review = build_trade_review(fills, {"105560": bars}, {}, "KR", date(2026, 9, 7))
    g = review["groups"][0]
    bar_ts = [b["ts"] for b in g["bars_window"]]

    # ① JSON 레벨: 체결 시각이 봉 시각 범위 "안"에 있어야 한다.
    assert min(bar_ts) <= g["entries"][0]["ts"] <= max(bar_ts)
    assert min(bar_ts) <= g["exits"][0]["ts"] <= max(bar_ts)

    # ② 렌더 레벨: daily_wrap의 손그림 SVG에서 BUY 마커의 x좌표가 캔들 x축
    # 범위(0~차트폭) 안, 그것도 가장자리(0 또는 w)에 붙지 않아야 한다 — 그게
    # 바로 실측 버그의 증상이었다(캔들 오른쪽 끝, 마커 왼쪽 끝).
    from quant.control.daily_wrap import _svg_candle

    card = {
        "symbol": g["symbol"], "market": g["market"], "entry_price": g["entry_price"], "band": g["band"],
        "bars_window": g["bars_window"], "entry_ts": g["entries"][0]["ts"],
        "exit_ts": g["exits"][0]["ts"], "exit_price": g["exits"][0]["price"],
        "pnl_known": g["pnl_known"], "pnl": g["pnl"], "pnl_bp": g["pnl_bp"],
        "y_range": g["y_range"], "r": g["r"],
    }
    svg = _svg_candle(card, w=300.0, h=130.0)
    assert svg, "봉이 있는데 SVG가 비었다"
    m = re.search(r'<circle cx="([\d.]+)" cy="[\d.]+" r="5" fill="#0052FF"', svg)
    assert m is not None, "BUY 마커를 SVG에서 못 찾음"
    buy_x = float(m.group(1))
    assert 5.0 < buy_x < 295.0, f"BUY 마커가 가장자리에 붙음(x={buy_x}) — 시간대 정렬 결함 재발"


# ------------------------------------------------------------------ 목표가 파생 (2026-09-07 추가)

def test_compute_band_derives_target_from_partial_take_r_when_reason_has_none():
    parsed = parse_entry_reason("1분봉 스캘프 패턴B 진입: 105560 w=0.50 손절=90")
    strategy_params = {"scalp_1m": {"params": {"partial_take_r": 1.5, "take_profit_bps": 0}}}
    band = compute_band("scalp_1m", 100.0, parsed, strategy_params)
    assert band["stop"] == 90.0
    # R = entry - stop = 10, target = entry + 1.5R = 115
    assert band["target"] == pytest.approx(115.0)
    assert "params:partial_take_r" in band["band_source"]
    assert band["target_note"] == "+1.5R 부분익절"


def test_compute_band_derives_target_from_take_profit_bps_before_partial_take_r():
    parsed = parse_entry_reason("진입 손절=90")
    strategy_params = {"scalp_1m": {"params": {"partial_take_r": 1.5, "take_profit_bps": 50}}}
    band = compute_band("scalp_1m", 100.0, parsed, strategy_params)
    assert band["target"] == pytest.approx(100.5)  # +50bp = +0.5%
    assert "params:take_profit_bps" in band["band_source"]
    assert "partial_take_r" not in band["band_source"]


def test_compute_band_does_not_fabricate_target_from_trail_bp_alone():
    parsed = parse_entry_reason("진입 손절=90")
    strategy_params = {"scalp_1m": {"params": {"trail_bp": 70, "take_profit_bps": 0, "partial_take_r": 0}}}
    band = compute_band("scalp_1m", 100.0, parsed, strategy_params)
    assert band["target"] is None
    assert band["target_note"] is None


def test_compute_band_partial_take_r_skipped_without_a_stop():
    # 손절 자체를 못 구했으면 R을 정의할 수 없다 — 목표를 지어내지 않는다.
    parsed = parse_entry_reason("진입")
    strategy_params = {"scalp_1m": {"params": {"partial_take_r": 1.5}}}
    band = compute_band("scalp_1m", 100.0, parsed, strategy_params)
    assert band["target"] is None
    assert band["stop"] is None


def test_compute_band_stop_note_present_for_derived_stop():
    parsed = parse_entry_reason("종가배팅: 마감강도 0.85 양봉")
    strategy_params = {"close_bet": {"params": {"stop_pct": 1.0, "take_profit_pct": 2.0}}}
    band = compute_band("close_bet", 10000.0, parsed, strategy_params)
    assert band["stop_note"] == "-1% 손절"
    assert band["target_note"] == "+2% 익절"


# ------------------------------------------------------------------ 차트 y축/R-배수 (2026-09-07 가시성 수리)

from quant.control.trade_review import _chart_y_range, _r_multiples  # noqa: E402


def test_chart_y_range_expands_to_cover_untouched_target_and_stop():
    # 창 안 캔들은 99~101 사이만 오갔지만 목표(110)/손절(80)은 훨씬 밖이다 —
    # 실측 버그: 자동스케일에 맡기면 이 범위가 화면 밖으로 잘렸다.
    bars_window = [{"high": 101.0, "low": 99.0}, {"high": 100.5, "low": 99.5}]
    y_range = _chart_y_range(bars_window, stop=80.0, target=110.0)
    assert y_range[0] == pytest.approx(80.0 * 0.997)
    assert y_range[1] == pytest.approx(110.0 * 1.003)


def test_chart_y_range_falls_back_to_window_when_no_band():
    bars_window = [{"high": 101.0, "low": 99.0}]
    y_range = _chart_y_range(bars_window, stop=None, target=None)
    assert y_range[0] == pytest.approx(99.0 * 0.997)
    assert y_range[1] == pytest.approx(101.0 * 1.003)


def test_chart_y_range_none_without_bars():
    assert _chart_y_range([], stop=90.0, target=110.0) is None


def test_r_multiples_basic():
    r = _r_multiples(entry_price=100.0, stop=90.0, target=115.0, exit_price=95.0, mfe_bp=140.0, mae_bp=-300.0)
    assert r["unit_r_bp"] == pytest.approx(1000.0)  # R=10 on entry 100 = 1000bp
    assert r["stop_r"] == -1.0
    assert r["target_r"] == pytest.approx(1.5)
    assert r["exit_r"] == pytest.approx(-0.5)
    assert r["mfe_r"] == pytest.approx(0.14)
    assert r["mae_r"] == pytest.approx(-0.3)


def test_r_multiples_none_without_stop():
    r = _r_multiples(entry_price=100.0, stop=None, target=115.0, exit_price=95.0, mfe_bp=10.0, mae_bp=-10.0)
    assert all(v is None for v in r.values())


def test_ma60_does_not_leak_across_local_day_boundary():
    # 전날 마감(172,000대)에 이어 다음날 개장(175,000대)이 붙은 2일치 1분봉 —
    # 실측 버그였던 "개장 직후 MA60이 172.5k에서 시작해 치솟는" 워밍업 꼬리가
    # 재발하면 이 테스트가 잡는다: 다음날 첫 분의 MA60은 **그 분 자신의 종가와
    # 같아야 한다**(전날 값이 하나도 안 섞임).
    day1 = pd.date_range("2026-09-06 09:00", periods=5, freq="1min", tz="Asia/Seoul")
    day2 = pd.date_range("2026-09-07 09:00", periods=5, freq="1min", tz="Asia/Seoul")
    idx = day1.append(day2)
    closes = [172000.0] * 5 + [175000.0, 175010.0, 175020.0, 175030.0, 175040.0]
    bars = pd.DataFrame(
        {"open": closes, "high": [c + 5 for c in closes], "low": [c - 5 for c in closes], "close": closes, "volume": 1000},
        index=idx,
    )
    fills = [
        _fill("scalp_1m", "105560", "buy", 10, 175000.0, "2026-09-07T00:00:30+00:00", reason="진입 손절=174000"),
        _fill("scalp_1m", "105560", "sell", 10, 175020.0, "2026-09-07T00:02:00+00:00", reason="청산", pnl=200.0),
    ]
    review = build_trade_review(fills, {"105560": bars}, {}, "KR", date(2026, 9, 7))
    g = review["groups"][0]
    first_bar_of_day2 = next(b for b in g["bars_window"] if b["ts"].startswith("2026-09-07"))
    assert first_bar_of_day2["ma60"] == pytest.approx(first_bar_of_day2["close"])


def test_build_trade_review_group_exposes_y_range_and_r():
    entry_ts = "2026-09-07T00:30:00+00:00"
    exit_ts = "2026-09-07T00:40:00+00:00"
    fills = [
        _fill("scalp_1m", "105560", "buy", 10, 100.0, entry_ts, reason="진입 손절=90"),
        _fill("scalp_1m", "105560", "sell", 10, 95.0, exit_ts, reason="청산", pnl=-50.0),
    ]
    bars = _bars("105560", datetime(2026, 9, 7, 9, 20), 30, open_=100.0, step=0.1, tz="Asia/Seoul")
    review = build_trade_review(fills, {"105560": bars}, {}, "KR", date(2026, 9, 7))
    g = review["groups"][0]
    assert g["y_range"] is not None and g["y_range"][0] < g["y_range"][1]
    assert g["r"]["stop_r"] == -1.0
    assert g["r"]["exit_r"] == pytest.approx(-0.5)


# ------------------------------------------------------------------ 렌더 스모크 (2026-09-07 가시성 수리)

def test_jinja_template_js_block_has_no_comment_swallowing_hazard():
    """실측 결함 재발 방지 — JS `//` 주석 줄 바로 다음이 좌측 공백을 지우는
    `{%- if/for %}` 태그면, Jinja가 주석 줄의 개행을 삼켜 그 태그의 본문(진짜
    코드)까지 주석에 먹힌다(2026-09-06 실측: node --check로 "Unexpected token
    ':'" 확인). 템플릿 소스에서 이 패턴 자체가 없는지 정적으로 검사한다 —
    렌더링 결과 문자열만 봐서는(주석 안이든 밖이든 글자는 똑같이 있다) 못
    잡는 결함이라 소스 라인 인접성을 직접 본다."""
    import re as _re
    from pathlib import Path

    path = Path("quant/analyze/templates/trade_review.html.j2")
    lines = path.read_text(encoding="utf-8").splitlines()
    comment_re = _re.compile(r"^\s*//")
    hazard_re = _re.compile(r"^\s*\{%-\s*(if|for|endif|endfor)\b")
    hits = [
        i for i in range(len(lines) - 1)
        if comment_re.match(lines[i]) and hazard_re.match(lines[i + 1])
    ]
    assert hits == [], f"주석 직후 좌측 공백 제거 태그 — 줄 {hits}에서 코드가 주석에 먹힐 위험"


def test_render_trade_review_js_is_syntactically_valid_with_full_band():
    """목표/손절/청산이 전부 있는(가장 복잡한 분기 조합) 카드를 렌더해 실제
    브라우저에서 실행될 <script> 블록을 뽑아 Node로 문법 검사한다. Node가 이
    환경에 없으면(CI 등) 스킵 — 이 테스트의 목적은 "있으면 잡는" 안전망이지
    Node 설치를 강제하는 게 아니다."""
    import re as _re
    import shutil
    import subprocess
    import tempfile

    if shutil.which("node") is None:
        pytest.skip("node 없음 — 이 환경에서는 JS 문법 검사를 건너뜀")

    fills = [
        _fill("scalp_1m", "105560", "buy", 10, 100.0, "2026-09-07T00:30:32+00:00",
              reason="1분봉 스캘프 패턴B 진입: 105560 w=0.50 손절=95"),
        _fill("scalp_1m", "105560", "sell", 10, 98.0, "2026-09-07T00:40:19+00:00",
              reason="60선 이탈(잔량 트레일): 종가=98 MA60=99", pnl=-20.0),
    ]
    bars = _bars("105560", datetime(2026, 9, 7, 9, 0), 90, open_=100.0, step=0.02, tz="Asia/Seoul")
    strategy_params = {"scalp_1m": {"params": {"partial_take_r": 1.5, "take_profit_bps": 0}}}
    review = build_trade_review(fills, {"105560": bars}, strategy_params, "KR", date(2026, 9, 7))

    from quant.report.render.trade_review import render_trade_review
    html = render_trade_review(review, "KR", date(2026, 9, 7))
    scripts = _re.findall(r"<script>(.*?)</script>", html, _re.S)
    assert scripts, "차트 <script> 블록을 못 찾음 — 밴드가 있으니 차트가 그려져야 한다"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(scripts[0])
        path = f.name
    result = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    assert result.returncode == 0, f"생성된 JS 문법 오류:\n{result.stderr}"


def test_render_strategy_table_best_worst_use_names():
    """전략별 요약표의 최고/최저 셀도 '이름(코드)'로 나온다(2026-09-07 소유자: 종목번호만 오지 않게 —
    EC2 재측정에서 이 셀만 코드 그대로 남아 있었다)."""
    fills = [
        _fill("scalp_1m", "105560", "buy", 10, 100.0, "2026-09-07T00:30:32+00:00",
              reason="1분봉 스캘프 패턴B 진입: 105560 w=0.50 손절=95"),
        _fill("scalp_1m", "105560", "sell", 10, 98.0, "2026-09-07T00:40:19+00:00",
              reason="60선 이탈(잔량 트레일): 종가=98 MA60=99", pnl=-20.0),
    ]
    bars = _bars("105560", datetime(2026, 9, 7, 9, 0), 90, open_=100.0, step=0.02, tz="Asia/Seoul")
    review = build_trade_review(fills, {"105560": bars}, {"scalp_1m": {"params": {}}}, "KR", date(2026, 9, 7),
                                names={"105560": "KB금융"})
    from quant.report.render.trade_review import render_trade_review
    html = render_trade_review(review, "KR", date(2026, 9, 7))
    assert html.count("KB금융(105560)") >= 2, "카드 제목과 요약표 최고/최저 셀 모두 이름(코드)여야 한다"
