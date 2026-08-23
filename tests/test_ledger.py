"""거래 원장(TradeLedgerSink) + 라운드트립 + 스코어보드 테스트 — 전부 오프라인."""
from datetime import date, datetime, timezone

import pytest

from quant.control.ledger import (
    TradeLedgerSink,
    frgn_accumulate_promotion_verdict,
    load_trades,
    news_scalp_promotion_verdict,
    round_trips,
    scoreboard_text,
    session_cash_delta_krw,
    session_pnl_summary,
    session_pnl_text,
    session_window,
    strategy_trading_days,
    trades_in_session,
)
from quant.core.models import Fill, Side


class _InnerSink:
    def __init__(self):
        self.fills = []
        self.signals = []

    def on_signal(self, s):
        self.signals.append(s)

    def on_fill(self, f):
        self.fills.append(f)


def _fill(symbol, side, qty, price, *, pnl=None, fee=0.0, strategy="orb_scan", ts=None):
    return Fill(
        symbol=symbol, side=side, qty=qty, price=price,
        ts=ts or datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc),
        strategy_id=strategy, fee=fee, realized_pnl=pnl,
    )


def test_sink_appends_jsonl_and_passes_through(tmp_path):
    inner = _InnerSink()
    sink = TradeLedgerSink(inner, path=tmp_path / "trades.jsonl")
    sink.on_fill(_fill("069500", Side.BUY, 10, 10000.0, fee=15.0))
    assert len(inner.fills) == 1, "래퍼는 내부 sink로 반드시 전달"
    rows = load_trades(tmp_path / "trades.jsonl")
    assert rows[0]["symbol"] == "069500" and rows[0]["market"] == "KR"


def test_sink_write_failure_never_blocks_fill(tmp_path):
    inner = _InnerSink()
    bad = tmp_path / "no_dir_allowed"
    bad.write_text("파일이라 디렉토리 생성이 불가")
    sink = TradeLedgerSink(inner, path=bad / "trades.jsonl")
    sink.on_fill(_fill("TQQQ", Side.BUY, 1, 70.0))
    assert len(inner.fills) == 1, "원장 기록 실패해도 체결 처리는 계속"


def test_round_trip_win_and_loss_math(tmp_path):
    p = tmp_path / "t.jsonl"
    sink = TradeLedgerSink(_InnerSink(), path=p)
    t0 = datetime(2026, 8, 10, 9, 5, tzinfo=timezone.utc)
    # 승리 트립: 069500 매수 10@10000 → 매도 10@10200 (pnl 2000, fee 3+3)
    sink.on_fill(_fill("069500", Side.BUY, 10, 10000.0, fee=3.0, ts=t0))
    sink.on_fill(_fill("069500", Side.SELL, 10, 10200.0, pnl=2000.0, fee=3.0, ts=t0))
    # 패배 트립: 122630 매수 5@20000 → 매도 5@19800 (pnl -1000, fee 3+3)
    sink.on_fill(_fill("122630", Side.BUY, 5, 20000.0, fee=3.0, ts=t0))
    sink.on_fill(_fill("122630", Side.SELL, 5, 19800.0, pnl=-1000.0, fee=3.0, ts=t0))
    # 미종결: 여전히 보유 중 → 트립 아님
    sink.on_fill(_fill("005930", Side.BUY, 1, 230000.0, ts=t0))

    trips = round_trips(load_trades(p))
    assert len(trips) == 2, "미종결 포지션은 트립으로 세지 않는다"
    win = next(t for t in trips if t["symbol"] == "069500")
    assert win["pnl"] == 2000.0 - 6.0
    assert abs(win["bps"] - (win["pnl"] / 100000.0 * 1e4)) < 1e-9
    board = scoreboard_text(trips)
    assert "승률 50%" in board


def test_scale_in_trip_groups_three_fills(tmp_path):
    p = tmp_path / "t.jsonl"
    sink = TradeLedgerSink(_InnerSink(), path=p)
    sink.on_fill(_fill("069500", Side.BUY, 10, 10000.0))
    sink.on_fill(_fill("069500", Side.BUY, 5, 10100.0))
    sink.on_fill(_fill("069500", Side.SELL, 15, 10300.0, pnl=3500.0))
    trips = round_trips(load_trades(p))
    assert len(trips) == 1 and trips[0]["n_fills"] == 3
    assert trips[0]["notional"] == 10 * 10000.0 + 5 * 10100.0


def test_unknown_pnl_excluded_from_win_rate(tmp_path):
    p = tmp_path / "t.jsonl"
    sink = TradeLedgerSink(_InnerSink(), path=p)
    sink.on_fill(_fill("TQQQ", Side.BUY, 1, 70.0))
    sink.on_fill(_fill("TQQQ", Side.SELL, 1, 71.0, pnl=None))  # 브로커가 원가 모름
    trips = round_trips(load_trades(p))
    assert trips[0]["pnl_known"] is False
    assert "손익미상" in scoreboard_text(trips)


def test_corrupt_ledger_line_is_skipped(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"symbol": "TQQQ", "side": "BUY", "qty": 1, "price": 70, "ts": "2026-08-10T10:00:00+00:00", "strategy_id": "x", "fee": 0}\n깨진 줄{{{\n', encoding="utf-8")
    assert len(load_trades(p)) == 1


def _trip(strategy="s", symbol="X", market="KR", pnl=1000.0, notional=100_000.0, pnl_known=True):
    """round_trips()가 만드는 것과 동일한 형태의 트립 dict — 스코어보드 표시 계층만
    테스트하기 위해 fill 왕복 없이 직접 구성한다."""
    return {
        "strategy": strategy, "symbol": symbol, "market": market,
        "entry_ts": "2026-08-10T09:00:00+00:00", "exit_ts": "2026-08-10T10:00:00+00:00",
        "pnl": pnl, "fees": 0.0, "notional": notional,
        "bps": (pnl / notional * 1e4) if notional else 0.0,
        "pnl_known": pnl_known, "n_fills": 2,
    }


def test_small_sample_win_rate_ci_is_undetermined():
    """2026-08-10 첫 dry run 실측 회귀 가드: intraday_scan 6건(4승2패, 승률 67%)은
    95% CI가 50%를 포함해 '판단 불가'가 나와야 한다 — 승률만 보고 단정하면 안 된다."""
    trips = (
        [_trip(strategy="intraday_scan", symbol=f"W{i}", pnl=9820.0, notional=1_000_000.0) for i in range(4)]
        + [_trip(strategy="intraday_scan", symbol=f"L{i}", pnl=-33070.0, notional=1_000_000.0) for i in range(2)]
    )
    board = scoreboard_text(trips)
    assert "승률 67%" in board
    assert "판단 불가" in board
    assert "⚠️ 표본 부족 (6/30건) — 이 숫자로 자본 배분을 결정하지 마라" in board


def test_large_sample_high_win_rate_is_significant_positive():
    trips = (
        [_trip(strategy="donchian", symbol=f"W{i}", pnl=10_000.0, notional=1_000_000.0) for i in range(35)]
        + [_trip(strategy="donchian", symbol=f"L{i}", pnl=-5_000.0, notional=1_000_000.0) for i in range(5)]
    )
    board = scoreboard_text(trips)
    assert "유의(양)" in board
    assert "표본 부족" not in board


def test_min_sample_warning_boundary_29_vs_30():
    trips29 = [_trip(strategy="s", symbol=f"T{i}", pnl=1000.0) for i in range(29)]
    trips30 = [_trip(strategy="s", symbol=f"T{i}", pnl=1000.0) for i in range(30)]
    assert "⚠️ 표본 부족 (29/30건)" in scoreboard_text(trips29)
    assert "표본 부족" not in scoreboard_text(trips30)


def test_dust_notice_kr_threshold():
    below = [
        _trip(strategy="s", symbol="A", market="KR", pnl=1000.0, notional=29_999.0),
        _trip(strategy="s", symbol="B", market="KR", pnl=1000.0, notional=100_000.0),
    ]
    at_threshold = [
        _trip(strategy="s", symbol="A", market="KR", pnl=1000.0, notional=30_000.0),
        _trip(strategy="s", symbol="B", market="KR", pnl=1000.0, notional=100_000.0),
    ]
    assert "명목 30,000원 미만 1건 포함 — 표본 왜곡 주의" in scoreboard_text(below)
    assert "명목" not in scoreboard_text(at_threshold)


def test_dust_notice_us_threshold():
    below = [
        _trip(strategy="s", symbol="TQQQ", market="US", pnl=10.0, notional=19.99),
        _trip(strategy="s", symbol="SQQQ", market="US", pnl=10.0, notional=1000.0),
    ]
    at_threshold = [
        _trip(strategy="s", symbol="TQQQ", market="US", pnl=10.0, notional=20.0),
        _trip(strategy="s", symbol="SQQQ", market="US", pnl=10.0, notional=1000.0),
    ]
    assert "명목 $20.00 미만 1건 포함 — 표본 왜곡 주의" in scoreboard_text(below)
    assert "명목" not in scoreboard_text(at_threshold)


# --- 세션 손익 리포트 (run session-pnl) --------------------------------------

def _row(symbol, side, qty, price, ts, *, pnl=None, fee=0.0, strategy="orb_scan",
         market=None, cash_after=None):
    """원장 raw dict를 직접 구성 — sink를 거치지 않고 세션 경계/통화 로직만 테스트."""
    return {
        "ts": ts, "strategy_id": strategy, "symbol": symbol, "side": side,
        "qty": qty, "price": price, "fee": fee, "realized_pnl": pnl,
        "cash_after": cash_after,
        "market": market or ("KR" if symbol.isdigit() and len(symbol) == 6 else "US"),
    }


def test_session_window_kr_boundaries():
    start, end = session_window("KR", date(2026, 8, 12))
    assert start.isoformat() == "2026-08-12T09:00:00+09:00"
    assert end.isoformat() == "2026-08-12T15:30:00+09:00"


def test_session_window_us_dst_summer_vs_winter():
    """서머타임(EDT/EST)을 America/New_York으로 계산 — KST 고정 클럭이면 여기서 어긋난다."""
    summer_start, summer_end = session_window("US", date(2026, 7, 15))  # EDT, UTC-4
    assert summer_start.astimezone(timezone.utc).isoformat() == "2026-07-15T13:30:00+00:00"
    assert summer_end.astimezone(timezone.utc).isoformat() == "2026-07-15T20:00:00+00:00"

    winter_start, winter_end = session_window("US", date(2026, 1, 15))  # EST, UTC-5
    assert winter_start.astimezone(timezone.utc).isoformat() == "2026-01-15T14:30:00+00:00"
    assert winter_end.astimezone(timezone.utc).isoformat() == "2026-01-15T21:00:00+00:00"


def test_trades_in_session_excludes_other_market():
    rows = [
        _row("069500", "BUY", 10, 10000, "2026-08-12T02:00:00+00:00"),  # 11:00 KST, KR 세션 중
        _row("TQQQ", "BUY", 1, 70, "2026-08-12T02:00:00+00:00", market="US"),  # 같은 시각, US
    ]
    kr = trades_in_session(rows, "KR", date(2026, 8, 12))
    assert len(kr) == 1 and kr[0]["symbol"] == "069500"


def test_session_boundary_excludes_just_before_and_after():
    on = date(2026, 8, 12)
    just_before = _row("069500", "BUY", 1, 10000, "2026-08-11T23:59:59+00:00")  # 08:59:59 KST
    at_open = _row("069500", "BUY", 1, 10000, "2026-08-12T00:00:00+00:00")  # 09:00:00 KST
    at_close = _row("069500", "SELL", 1, 10000, "2026-08-12T06:30:00+00:00", pnl=0.0)  # 15:30:00 KST
    just_after = _row("069500", "SELL", 1, 10000, "2026-08-12T06:30:01+00:00", pnl=0.0)  # 15:30:01 KST
    session = trades_in_session([just_before, at_open, at_close, just_after], "KR", on)
    assert just_before not in session
    assert just_after not in session
    assert at_open in session and at_close in session


def test_session_pnl_summary_us_dst_summer():
    rows = [
        _row("TQQQ", "BUY", 10, 70.0, "2026-07-15T13:30:00+00:00", fee=0.5),
        _row("TQQQ", "SELL", 10, 71.0, "2026-07-15T19:00:00+00:00", pnl=10.0, fee=0.5),
    ]
    summary = session_pnl_summary(rows, "US", date(2026, 7, 15))
    assert summary["has_trades"] is True
    assert summary["gross_realized"] == 10.0
    assert summary["fees"] == 1.0
    assert summary["net_realized"] == 9.0
    assert "$9.00" in session_pnl_text(summary)


def test_session_pnl_summary_us_dst_winter():
    rows = [
        _row("TQQQ", "BUY", 10, 70.0, "2026-01-15T14:30:00+00:00", fee=0.5),
        _row("TQQQ", "SELL", 10, 71.0, "2026-01-15T20:30:00+00:00", pnl=10.0, fee=0.5),
    ]
    summary = session_pnl_summary(rows, "US", date(2026, 1, 15))
    assert summary["gross_realized"] == 10.0
    assert summary["net_realized"] == 9.0


def test_session_pnl_null_realized_pnl_excluded_not_zeroed():
    rows = [
        _row("069500", "BUY", 10, 10000.0, "2026-08-12T00:30:00+00:00"),
        _row("069500", "SELL", 10, 10100.0, "2026-08-12T01:00:00+00:00", pnl=None, fee=3.0),
    ]
    summary = session_pnl_summary(rows, "KR", date(2026, 8, 12))
    assert summary["unknown_sells"] == 1
    assert summary["gross_realized"] == 0.0  # 손익미상 매도는 0으로 위장하지 않고 합산 제외
    assert "손익미상" in session_pnl_text(summary)


def test_session_pnl_empty_session_says_no_trades():
    summary = session_pnl_summary([], "KR", date(2026, 8, 12))
    assert summary["has_trades"] is False
    assert "거래 없음" in session_pnl_text(summary)


def test_session_pnl_per_strategy_and_symbol_breakdown():
    on = date(2026, 8, 12)
    rows = [
        _row("069500", "BUY", 10, 10000.0, "2026-08-12T00:30:00+00:00", strategy="orb_scan"),
        _row("069500", "SELL", 10, 10200.0, "2026-08-12T01:00:00+00:00", pnl=2000.0, strategy="orb_scan"),
        _row("122630", "BUY", 5, 20000.0, "2026-08-12T02:00:00+00:00", strategy="intraday_scan"),
        _row("122630", "SELL", 5, 19800.0, "2026-08-12T03:00:00+00:00", pnl=-1000.0, strategy="intraday_scan"),
    ]
    summary = session_pnl_summary(rows, "KR", on)
    assert set(summary["by_strategy"].keys()) == {"orb_scan", "intraday_scan"}
    assert set(summary["by_symbol"].keys()) == {"069500", "122630"}
    assert summary["by_strategy"]["orb_scan"]["gross"] == 2000.0
    assert summary["by_strategy"]["intraday_scan"]["gross"] == -1000.0


def test_session_cash_delta_krw_attributes_only_session_fills():
    all_trades = [
        _row("TQQQ", "BUY", 1, 70.0, "2026-08-11T14:00:00+00:00", cash_after=9_902_000.0),  # 전날 US 체결
        _row("069500", "BUY", 10, 10000.0, "2026-08-12T00:30:00+00:00", cash_after=9_802_000.0),  # 오늘 KR
        _row("069500", "SELL", 10, 10200.0, "2026-08-12T01:00:00+00:00", pnl=2000.0, cash_after=9_904_000.0),
    ]
    kr_session = trades_in_session(all_trades, "KR", date(2026, 8, 12))
    delta, unknown = session_cash_delta_krw(all_trades, kr_session)
    assert unknown == 0
    assert delta == (9_802_000.0 - 9_902_000.0) + (9_904_000.0 - 9_802_000.0)


def test_session_cash_delta_krw_unknown_when_cash_after_missing():
    """라이브(Toss) 체결은 cash_after가 없다 — 0으로 위장하지 않고 계산 불가로 잡는다."""
    all_trades = [_row("TQQQ", "BUY", 1, 70.0, "2026-07-15T13:30:00+00:00", cash_after=None)]
    us_session = trades_in_session(all_trades, "US", date(2026, 7, 15))
    delta, unknown = session_cash_delta_krw(all_trades, us_session)
    assert delta is None
    assert unknown == 1


# ============================================================ 갈래 A/B 승격 게이트 (spec §5/§4)

def test_strategy_trading_days_counts_distinct_days_not_fills():
    """같은 날 여러 번 산 적립 매수는 거래일 1일로 센다 — 트립/체결 건수가 아니다."""
    trades = [
        _row("005930", "BUY", 1, 70_000.0, "2026-01-05T00:30:00+00:00", strategy="frgn_accumulate"),
        _row("005930", "BUY", 1, 71_000.0, "2026-01-05T02:00:00+00:00", strategy="frgn_accumulate"),
        _row("005930", "BUY", 1, 72_000.0, "2026-01-06T00:30:00+00:00", strategy="frgn_accumulate"),
    ]
    assert strategy_trading_days(trades, "frgn_accumulate") == 2


def test_strategy_trading_days_only_counts_the_given_strategy():
    trades = [
        _row("005930", "BUY", 1, 70_000.0, "2026-01-05T00:30:00+00:00", strategy="frgn_accumulate"),
        _row("069500", "BUY", 1, 10_000.0, "2026-01-05T00:30:00+00:00", strategy="orb_scan"),
    ]
    assert strategy_trading_days(trades, "frgn_accumulate") == 1
    assert strategy_trading_days(trades, "orb_scan") == 1
    assert strategy_trading_days(trades, "nonexistent") == 0


def test_frgn_accumulate_promotion_verdict_below_threshold():
    trades = [
        _row("005930", "BUY", 1, 70_000.0, f"2026-01-{d:02d}T00:30:00+00:00", strategy="frgn_accumulate")
        for d in range(5, 15)  # 10 거래일 — 20 미만
    ]
    v = frgn_accumulate_promotion_verdict(trades, min_days=20)
    assert v["promote"] is False
    assert v["n_trading_days"] == 10


def test_frgn_accumulate_promotion_verdict_meets_threshold():
    trades = [
        _row("005930", "BUY", 1, 70_000.0, f"2026-01-{d:02d}T00:30:00+00:00", strategy="frgn_accumulate")
        for d in range(1, 21)  # 20 거래일
    ]
    v = frgn_accumulate_promotion_verdict(trades, min_days=20)
    assert v["promote"] is True
    assert v["n_trading_days"] == 20
    assert "승격 판정 가능" in v["reason"]


def test_news_scalp_promotion_verdict_insufficient_sample():
    v = news_scalp_promotion_verdict({"n_symbol_days": 10, "avg_open_close_bp": 50.0},
                                      round_trip_fee_bps=18.0, min_n_symbol_days=30)
    assert v["promote"] is False
    assert "표본 부족" in v["reason"]


def test_news_scalp_promotion_verdict_net_positive_after_fees():
    v = news_scalp_promotion_verdict({"n_symbol_days": 40, "avg_open_close_bp": 30.0},
                                      round_trip_fee_bps=18.0, min_n_symbol_days=30)
    assert v["promote"] is True
    assert v["net_bp"] == pytest.approx(12.0)
    assert "승격 판정 가능" in v["reason"]


def test_news_scalp_promotion_verdict_fees_eat_the_edge():
    """수수료를 빼면 음수가 되는 경우 — 표본은 충분해도 승격 근거 없음."""
    v = news_scalp_promotion_verdict({"n_symbol_days": 40, "avg_open_close_bp": 10.0},
                                      round_trip_fee_bps=18.0, min_n_symbol_days=30)
    assert v["promote"] is False
    assert v["net_bp"] == pytest.approx(-8.0)


def test_news_scalp_promotion_verdict_missing_avg_bp_is_not_promoted():
    v = news_scalp_promotion_verdict({"n_symbol_days": 40, "avg_open_close_bp": None},
                                      round_trip_fee_bps=18.0, min_n_symbol_days=30)
    assert v["promote"] is False
