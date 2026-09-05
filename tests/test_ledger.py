"""거래 원장(TradeLedgerSink) + 라운드트립 + 스코어보드 테스트 — 전부 오프라인."""
from datetime import date, datetime, timezone

import pytest

from quant.control.ledger import (
    TradeLedgerSink,
    daily_benchmark_series_by_market,
    daily_equity_series_by_market,
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


def test_fill_row_persists_the_traders_entry_reason(tmp_path):
    """트레이더의 시그널 기록(reason)이 원장에 남는다 (2026-08-26 소유자 조직도
    역할 4·5): "당시 들어갈때 트레이더가 남겨둔 진입 시그널이 어디에서 나온건지
    당시 상황을 트레이더가 기록해줘야 하고, 그걸 보면서 장이 종료됐을 때 5번
    직원이 피드백을 준다." 그전엔 reason 이 엔진 로그(journalctl)에만 있어
    로그 로테이션과 함께 사라졌다 — 패턴A/B·구조손절 근거·게이트 판정이 전부
    이 문자열에 실려 있는데도."""
    import json

    from quant.control.ledger import TradeLedgerSink

    path = tmp_path / "trades.jsonl"
    sink = TradeLedgerSink(_InnerSink(), path)
    f = _fill("005930", Side.BUY, 10, 81300.0)
    f = Fill(symbol=f.symbol, side=f.side, qty=f.qty, price=f.price, ts=f.ts,
             strategy_id=f.strategy_id, fee=f.fee, realized_pnl=f.realized_pnl,
             reason="1분봉 스캘프 패턴A 진입: 005930 w=0.50 손절=81,000 [구조손절:swing_low]")
    sink.on_fill(f)

    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert "구조손절" in row["reason"]


def test_fill_row_without_reason_writes_empty_string(tmp_path):
    """reason 이 빈 체결(구버전 Fill·수동 조정)도 행 구조는 같다 — 스키마 계약
    (필드 추가는 자유, 과거 행과 섞여도 읽기가 안 죽는다)."""
    import json

    from quant.control.ledger import TradeLedgerSink

    path = tmp_path / "trades.jsonl"
    sink = TradeLedgerSink(_InnerSink(), path)
    sink.on_fill(_fill("005930", Side.SELL, 10, 81300.0))

    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert row["reason"] == ""


# --- 자본 곡선(equity_curve.jsonl) → quantstats 티어시트용 일별 시리즈 어댑터 ---
# 행 형식은 quant/apps/cli.py::cmd_equity_snapshot 이 실제로 쓰는 그대로다
# (tests/test_alpha.py::_equity_row 와 동일한 관례).

def _eq_row(day: str, market: str, total: float, *, recorded_at: str,
            bench_symbol: str | None = None, bench_close: float | None = None) -> dict:
    row = {
        "date": day, "market": market, "total_krw": total,
        "books": {"donchian": total}, "marked": 1, "degraded": [],
        "recorded_at": recorded_at,
    }
    if bench_symbol is not None:
        row["benchmark_symbol"] = bench_symbol
        row["benchmark_close"] = bench_close
    return row


def test_daily_equity_series_takes_last_mark_of_the_day(tmp_path):
    """하루 여러 번 마크(장중 재실행) → 그날의 마지막 값만 시리즈에 남는다."""
    import json

    path = tmp_path / "equity_curve.jsonl"
    rows = [
        _eq_row("2026-08-24", "KR", 7_722_657.8, recorded_at="2026-08-24T03:20:45+09:00"),
        _eq_row("2026-08-24", "KR", 7_382_105.34, recorded_at="2026-08-24T15:40:01+09:00"),
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    out = daily_equity_series_by_market(path)
    assert list(out.keys()) == ["KR"]
    s = out["KR"]
    assert len(s) == 1
    assert s.iloc[0] == pytest.approx(7_382_105.34)


def test_daily_equity_series_splits_by_market(tmp_path):
    """같은 날짜라도 시장이 다르면 서로 다른 시리즈로 분리된다 — 섞으면 안 된다."""
    import json

    path = tmp_path / "equity_curve.jsonl"
    rows = [
        _eq_row("2026-08-24", "KR", 7_382_105.34, recorded_at="2026-08-24T15:40:01+09:00"),
        _eq_row("2026-08-24", "US", 7_353_040.09, recorded_at="2026-08-25T06:15:01+09:00"),
        _eq_row("2026-08-25", "KR", 7_402_050.48, recorded_at="2026-08-25T15:40:02+09:00"),
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    out = daily_equity_series_by_market(path)
    assert set(out.keys()) == {"KR", "US"}
    assert len(out["KR"]) == 2
    assert len(out["US"]) == 1
    # 결측일을 채우지 않는다 — US 시리즈에는 08-25가 없다(휴장/미기록이라 지어내지 않음).
    assert list(out["US"].index.date.astype(str)) == ["2026-08-24"]


def test_daily_benchmark_series_reads_companion_field(tmp_path):
    """benchmark_close 동반 기록이 있는 행에서만 벤치마크 시리즈를 뽑는다."""
    import json

    path = tmp_path / "equity_curve.jsonl"
    rows = [
        # 구버전 행(벤치마크 동반 기록 없음) — 벤치마크 시리즈에 기여하지 않는다.
        _eq_row("2026-08-24", "US", 7_353_040.09, recorded_at="2026-08-25T06:15:01+09:00"),
        _eq_row("2026-08-28", "US", 7_224_272.21, recorded_at="2026-08-29T06:15:02+09:00",
                bench_symbol="QQQ", bench_close=716.72),
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    out = daily_benchmark_series_by_market(path)
    assert list(out.keys()) == ["US"]
    s = out["US"]
    assert len(s) == 1
    assert s.iloc[0] == pytest.approx(716.72)


def test_daily_equity_series_empty_file_returns_empty_dict(tmp_path):
    """파일이 없거나 비어 있으면 에러가 아니라 빈 dict — 표본 없음은 정상 상태다."""
    missing = tmp_path / "does_not_exist.jsonl"
    assert daily_equity_series_by_market(missing) == {}
    assert daily_benchmark_series_by_market(missing) == {}

    empty = tmp_path / "equity_curve.jsonl"
    empty.write_text("", encoding="utf-8")
    assert daily_equity_series_by_market(empty) == {}
    assert daily_benchmark_series_by_market(empty) == {}


# --- 실계좌 이식(2026-09-01)이 만든 결함 3건 (2026-09-02 수정) -----------------
#
# 프로덕션 원장(EC2 data/state/trades.jsonl) 2026-09-01 US 세션의 실제 행들로
# 고정한다 — 합성 데이터로는 이 결함들이 재현되지 않았다.

SEEDING_REASON = "실계좌 이식 정리 — 소유자 지시 2026-09-01: 005930만 보유 유지, 나머지 정리"

# 그날 US 세션에서 프로그램이 실제로 낸 손익(수수료 전 -77.77 / 수수료 25.13).
_PROGRAM_ROWS_2026_09_01 = [
    _row("QQQ", "SELL", 5.0, 707.4330975, "2026-09-01T13:30:00+00:00",
         pnl=-49.43093750000003, fee=3.610861096542501, strategy="overnight_drift"),
    _row("TQQQ", "BUY", 9, 69.397345, "2026-09-01T13:50:30+00:00",
         fee=0.624576105, strategy="gap_fade"),
    # --- 이식 경계(14:01:08) ---
    _row("TQQQ", "BUY", 1, 69.20729750000001, "2026-09-01T14:06:20+00:00",
         fee=0.0692072975, strategy="gap_fade"),
    _row("TQQQ", "SELL", 1.0, 70.4923725, "2026-09-01T15:47:26+00:00",
         pnl=1.285074999999992, fee=0.08065837249999999, strategy="gap_fade"),
]
_SEEDING_ROWS_2026_09_01 = [
    _row("GOOGL", "SELL", 1.0, 334.07, "2026-09-01T14:01:08.315330+00:00",
         pnl=-54.639999999999986, fee=0.344236, strategy="legacy"),
    _row("SOXL", "SELL", 13.0, 105.67, "2026-09-01T14:01:08.315330+00:00",
         pnl=-706.4199999999998, fee=1.4041664260000002, strategy="legacy"),
]
for _r in _SEEDING_ROWS_2026_09_01:
    _r["reason"] = SEEDING_REASON


def test_session_pnl_excludes_seeding_liquidation_and_reports_it_separately():
    """F1: 이식 정리 매도가 프로그램 손익으로 발송됐다(실측: 프로그램 -$102.90 인데
    -$1,214.65 로 나갔다). 이제 빼되 조용히 빼지 않는다 — 제외 줄이 반드시 보인다."""
    from quant.control.ledger import session_pnl_summary, session_pnl_text

    trades = _PROGRAM_ROWS_2026_09_01 + _SEEDING_ROWS_2026_09_01
    s = session_pnl_summary(trades, "US", date(2026, 9, 1))

    program_gross = sum(float(r["realized_pnl"]) for r in _PROGRAM_ROWS_2026_09_01
                        if r["realized_pnl"] is not None)
    program_fees = sum(float(r["fee"]) for r in _PROGRAM_ROWS_2026_09_01)
    assert s["gross_realized"] == pytest.approx(program_gross)
    assert s["net_realized"] == pytest.approx(program_gross - program_fees)
    assert s["n_fills"] == len(_PROGRAM_ROWS_2026_09_01)      # 정리 매도는 체결 수에서도 빠진다
    assert "legacy" not in s["by_strategy"]

    assert s["excluded_seeding"]["n"] == 2
    assert s["excluded_seeding"]["gross"] == pytest.approx(-761.06, abs=0.01)

    text = session_pnl_text(s)
    assert "이식 정리 2건 제외" in text


def test_session_pnl_excluded_line_shows_even_when_no_program_trades():
    """정리 매도만 있는 세션은 "거래 없음"이지만, 원장 총액과 왜 안 맞는지는 밝힌다."""
    from quant.control.ledger import session_pnl_summary, session_pnl_text

    s = session_pnl_summary(_SEEDING_ROWS_2026_09_01, "US", date(2026, 9, 1))
    assert s["has_trades"] is False
    text = session_pnl_text(s)
    assert "이 세션에 체결된 거래 없음" in text
    assert "이식 정리 2건 제외" in text


def test_round_trips_treats_transplant_as_epoch_boundary():
    """F2: 이식 시점에 열려 있던 lot(gap_fade TQQQ 9주)은 상계 행 없이 사라졌다.
    그걸 안 버리면 이식 이후의 정상 왕복(+$1.13)이 트립으로 안 세진다."""
    from quant.control.ledger import round_trips

    trades = _PROGRAM_ROWS_2026_09_01 + _SEEDING_ROWS_2026_09_01
    trips = round_trips(trades)

    gap = [t for t in trips if t["strategy"] == "gap_fade" and t["symbol"] == "TQQQ"]
    assert len(gap) == 1
    assert gap[0]["entry_ts"] == "2026-09-01T14:06:20+00:00"
    assert gap[0]["pnl"] == pytest.approx(1.135209, abs=1e-5)
    # 정리 매도 행 자체는 트립 재료가 아니다
    assert not [t for t in trips if t["strategy"] == "legacy"]


def test_round_trips_never_pairs_a_sell_across_the_transplant_boundary():
    """이식으로 물려받은 주식을 나중에 팔면 모의 시대 매수와 짝지어져 **없던
    트립**이 만들어진다 — 경계 이후 재고 없는 매도는 아무것도 열지 않는다."""
    from quant.control.ledger import round_trips

    trades = [
        _row("005930", "BUY", 1, 70000.0, "2026-08-20T01:00:00+00:00",
             fee=10.0, strategy="frgn_accumulate"),
        _SEEDING_ROWS_2026_09_01[0],
        # 이식으로 받은 6주를 이식 이후에 매도 — 8/20 매수와 짝지으면 안 된다
        _row("005930", "SELL", 6.0, 80000.0, "2026-09-02T01:00:00+00:00",
             pnl=60000.0, fee=100.0, strategy="frgn_accumulate"),
    ]
    assert round_trips(trades) == []


_CARRY_REASON = "실계좌 이식 이월 — 소유자 지시 2026-09-01: 005930 이월 보유"


def test_round_trips_ignores_carry_row_for_pnl():
    """D3: `cmd_seed_real`이 유지 종목에 남기는 캐리오버 합성 buy
    (`SEEDING_CARRY_MARKER`)는 실제 매수가 아니다 — 정리 매도와 같은 대우로
    트립 재료에서 빠져야 한다. 안 빠지면 이월 buy가 그 뒤 매도와 짝지어져
    '가짜 트립'(원가·손익 없는데 트립으로 집계됨)을 만든다."""
    from quant.control.ledger import round_trips

    carry = _row("005930", "BUY", 6.0, 263416.666666, "2026-09-01T14:01:08.315330+00:00",
                  strategy="seed")
    carry["reason"] = _CARRY_REASON
    sell = _row("005930", "SELL", 6.0, 260000.0, "2026-09-02T01:00:00+00:00",
                 pnl=-20500.0, fee=100.0, strategy="seed")

    trips = round_trips([carry, sell])

    assert trips == []  # 캐리 buy가 걸러지므로 매도와 짝지어 가짜 트립을 만들지 않는다


def test_session_cash_delta_uses_usd_pool_for_us_market():
    """F3: US 체결은 KRW 풀을 안 건드린다 — 예전엔 "계좌 현금 변화 +0원"을 찍었다."""
    from quant.control.ledger import session_pnl_summary, session_pnl_text

    buy = _row("TQQQ", "BUY", 1, 70.0, "2026-09-02T14:00:00+00:00", fee=0.07)
    sell = _row("TQQQ", "SELL", 1.0, 72.0, "2026-09-02T15:00:00+00:00", pnl=2.0, fee=0.072)
    prior = _row("TQQQ", "BUY", 1, 70.0, "2026-09-01T14:00:00+00:00", fee=0.07)
    for row, usd in ((prior, 1000.0), (buy, 929.93), (sell, 1001.86)):
        row["cash_after"] = 5_000_000.0   # KRW 풀은 US 체결로 안 변한다
        row["cash_after_usd"] = usd

    s = session_pnl_summary([prior, buy, sell], "US", date(2026, 9, 2))
    assert s["cash_delta_krw"] == pytest.approx(0.0)
    assert s["cash_delta_usd"] == pytest.approx(1.86)
    text = session_pnl_text(s)
    assert "계좌 USD 현금 변화 $+1.86" in text
    assert "계좌 현금 변화(KRW" not in text


def test_us_session_says_unavailable_when_cash_after_usd_missing():
    """구 형식 원장(필드 없음)은 0으로 위장하지 않고 "집계 불가"라고 말한다."""
    from quant.control.ledger import session_pnl_summary, session_pnl_text

    rows = [
        _row("TQQQ", "BUY", 1, 70.0, "2026-09-02T14:00:00+00:00", fee=0.07, cash_after=1.0),
        _row("TQQQ", "SELL", 1.0, 72.0, "2026-09-02T15:00:00+00:00", pnl=2.0, fee=0.07,
             cash_after=2.0),
    ]
    s = session_pnl_summary(rows, "US", date(2026, 9, 2))
    assert s["cash_delta_usd"] is None
    assert "집계 불가(구 형식" in session_pnl_text(s)


def test_kr_session_cash_line_is_unchanged():
    """KR 라인은 예전 그대로(KRW 풀) — 이 수정이 건드리는 건 US 쪽뿐이다."""
    from quant.control.ledger import session_pnl_summary, session_pnl_text

    rows = [
        _row("069500", "BUY", 10, 10000.0, "2026-08-12T00:30:00+00:00", cash_after=9_802_000.0),
        _row("069500", "SELL", 10, 10200.0, "2026-08-12T01:00:00+00:00", pnl=2000.0,
             cash_after=9_904_000.0),
    ]
    text = session_pnl_text(session_pnl_summary(rows, "KR", date(2026, 8, 12)))
    assert "계좌 현금 변화(KRW, paper 브로커 체결시점 환산 반영) +102,000원" in text


# ── A/B 갈래 비교 (2026-09-03) ────────────────────────────────────────────────
# 중심 주장: **표본이 얇으면 아무 말도 하지 않는다.** 한쪽만 30건이어도 안 된다 —
# 차이의 신뢰구간은 얇은 쪽이 지배한다. 그리고 같은 원장에서 같은 p 값이 나와야
# 한다(리포트가 실행마다 흔들리면 판단 근거로 못 쓴다).

def _ab_trip(strategy: str, bps: float, market: str = "US", pnl: float | None = None) -> dict:
    return {
        "strategy": strategy, "symbol": "X", "market": market,
        "pnl": bps if pnl is None else pnl, "fees": 0.0, "notional": 1000.0,
        "bps": bps, "pnl_known": True, "n_fills": 2,
    }


def test_base_strategy_id_strips_catalyst_and_pure_suffixes():
    """2026-09-03 부채 상환: 이 함수는 이제 `quant.core.strategy_ids`를 그대로
    가리킨다 — 예전엔 `_cat`만 벗기고 `_pure`는 벗기지 않아 `quant.trade.loop`/
    `quant.trade.risk.manager`의 같은 이름 함수와 갈라져 있었다
    (`tests/test_strategy_ids.py`가 세 곳의 일치를 직접 대조한다)."""
    from quant.control.ledger import base_strategy_id

    assert base_strategy_id("scalp_1m_cat") == "scalp_1m"
    assert base_strategy_id("scalp_1m") == "scalp_1m"
    assert base_strategy_id("donchian_pure") == "donchian"
    assert base_strategy_id("") == ""


def test_ab_pairs_from_config_needs_both_arms_present():
    from quant.control.ledger import ab_pairs_from_config

    cfg = {"strategies": {"a": {}, "a_cat": {}, "b": {}, "c_cat": {}}}
    assert ab_pairs_from_config(cfg) == ["a"]


def test_ab_compare_reports_judgement_impossible_below_the_sample_floor():
    from quant.control.ledger import MIN_TRIPS_FOR_JUDGEMENT, ab_compare

    trips = ([_ab_trip("s", 10.0)] * MIN_TRIPS_FOR_JUDGEMENT) + [_ab_trip("s_cat", 50.0)] * 5
    (row,) = ab_compare(trips)
    assert row["judgeable"] is False
    assert row["reason"] == f"판단 불가(n<{MIN_TRIPS_FOR_JUDGEMENT})"
    assert row["delta_expectancy_bp"] is None and row["p_value"] is None
    # 판정을 못 해도 각 갈래의 표본 수·기대값은 보여준다(그게 다음 판단의 재료다)
    assert row["baseline"]["n"] == MIN_TRIPS_FOR_JUDGEMENT and row["catalyst"]["n"] == 5


def test_ab_compare_with_no_trips_still_lists_configured_pairs():
    from quant.control.ledger import ab_compare

    (row,) = ab_compare([], bases=["scalp_1m"])
    assert row["base"] == "scalp_1m" and row["market"] is None
    assert row["baseline"]["n"] == 0 and not row["judgeable"]


def test_ab_compare_separates_markets():
    from quant.control.ledger import ab_compare

    trips = [_ab_trip("s", 1.0, "KR"), _ab_trip("s_cat", 2.0, "US")]
    assert [r["market"] for r in ab_compare(trips)] == ["KR", "US"]


def test_ab_compare_measures_the_difference_when_both_arms_are_thick_enough():
    from quant.control.ledger import ab_compare

    # 촉매가 명백히 낫다: -20bp 대 +30bp, 각 40건(잡음 섞어 분산이 0이 아니게).
    base = [_ab_trip("s", -20.0 + (i % 5) - 2) for i in range(40)]
    cat = [_ab_trip("s_cat", 30.0 + (i % 5) - 2) for i in range(40)]
    (row,) = ab_compare(base + cat, permutations=400)
    assert row["judgeable"] is True and row["reason"] == ""
    assert row["delta_expectancy_bp"] == pytest.approx(50.0, abs=0.5)
    lo, hi = row["delta_ci"]
    assert lo > 0, "차이가 명백한데 신뢰구간이 0을 포함한다"
    assert row["p_value"] < 0.01


def test_ab_compare_p_value_is_deterministic():
    """시드 고정 — 같은 원장이면 같은 p 값. 리포트가 실행마다 흔들리면 안 된다."""
    from quant.control.ledger import ab_compare

    trips = ([_ab_trip("s", (i % 7) - 3.0) for i in range(35)]
             + [_ab_trip("s_cat", (i % 5) - 1.0) for i in range(35)])
    first = ab_compare(trips, permutations=300)[0]["p_value"]
    second = ab_compare(trips, permutations=300)[0]["p_value"]
    assert first == second


def test_ab_compare_ignores_trips_with_unknown_pnl():
    """손익미상은 승패도 기대값도 모른다 — 0으로 위장하지 않고 표본에서 뺀다."""
    from quant.control.ledger import ab_compare

    unknown = {**_ab_trip("s_cat", 0.0), "pnl_known": False}
    (row,) = ab_compare([_ab_trip("s", 5.0), unknown])
    assert row["catalyst"]["n"] == 0 and row["catalyst"]["n_unknown"] == 1
    assert row["catalyst"]["expectancy_bp"] is None


# ── session_pnl_text HTML 서식 (2026-09-04, tgfmt) ────────────────────────

import re as _re


def _assert_balanced_html(text: str) -> None:
    stack: list[str] = []
    for m in _re.finditer(r"<(/?)([a-z]+)[^>]*>", text):
        closing, name = m.group(1), m.group(2)
        if not closing:
            stack.append(name)
        else:
            assert stack and stack[-1] == name, f"짝이 안 맞는 태그 </{name}> in: {text!r}"
            stack.pop()
    assert not stack, f"닫히지 않은 태그 {stack} in: {text!r}"


def test_session_pnl_text_html_is_balanced_with_and_without_trades():
    empty = session_pnl_summary([], "KR", date(2026, 8, 12))
    _assert_balanced_html(session_pnl_text(empty))

    rows = [
        _row("069500", "BUY", 10, 10000.0, "2026-08-12T00:30:00+00:00", strategy="orb_scan"),
        _row("069500", "SELL", 10, 10200.0, "2026-08-12T01:00:00+00:00", pnl=2000.0, strategy="orb_scan"),
    ]
    summary = session_pnl_summary(rows, "KR", date(2026, 8, 12))
    text = session_pnl_text(summary)
    _assert_balanced_html(text)
    assert "<pre>" in text and "<blockquote expandable>" in text


def test_session_pnl_text_escapes_strategy_and_symbol_and_report_link():
    rows = [
        _row("A&B<x>", "BUY", 10, 10000.0, "2026-08-12T00:30:00+00:00", strategy="s<1>", market="KR"),
        _row("A&B<x>", "SELL", 10, 10200.0, "2026-08-12T01:00:00+00:00", pnl=2000.0, strategy="s<1>", market="KR"),
    ]
    summary = session_pnl_summary(rows, "KR", date(2026, 8, 12))
    text = session_pnl_text(summary, report_url="https://example.com/r?a=1&b=2")
    _assert_balanced_html(text)
    assert "s<1>" not in text and "s&lt;1&gt;" in text
    assert "A&B<x>" not in text and "A&amp;B&lt;x&gt;" in text
    assert '<a href="https://example.com/r?a=1&amp;b=2">전체 리포트</a>' in text
