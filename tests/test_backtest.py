"""run_backtest 스모크 + 회계 검산(reconciliation) 테스트.

회계 검산이 여기 있는 이유: 2026-08 실측에서 같은 10년 백테스트가 서로 모순되는
세 가지 답을 냈다 — 자산곡선은 -29%, 체결 로그 기반 손익 합계는 +$31,839(부호도
자릿수도 다름), 현금흐름 재구성은 불가능한 잔여 포지션. 셋 중 어느 것도 다른 둘과
대조되지 않았기 때문에 몇 달간 아무도 몰랐다. 이 파일의 테스트들은 그 대조를
영구적으로 강제한다.
"""
from __future__ import annotations

import pandas as pd
import pytest

from quant.backtest import BacktestResult, run_backtest
from quant.backtest.engine import ReconciliationError, _reconcile
from quant.core.fx import FixedFxProvider
from quant.adapters.data.stub import StubDataFeed
from quant.core.models import Position


# ------------------------------------------------------- watchlist 전략(symbols: [])


def test_run_backtest_raises_a_clear_error_when_a_watchlist_strategy_has_no_symbols():
    """orb_scan/intraday_scan/cross_momentum/confluence는 settings.yaml에
    symbols: []로 선언돼 있다(관심종목 유니버스는 라이브 세션 롤이 채운다). --symbols
    없이 백테스트를 돌리면 symbols[0] 접근이 IndexError로 죽는 대신, 무엇을 어떻게
    고쳐야 하는지 알 수 있는 명확한 에러로 멈춰야 한다."""
    with pytest.raises(ValueError, match="관심종목 유니버스"):
        run_backtest(strategy_id="orb_scan", days=10, interval="15m", source="stub")


def test_run_backtest_symbols_override_lets_a_watchlist_strategy_run():
    """symbols= 오버라이드로 settings.yaml의 빈 symbols: []를 채우면 watchlist
    전략도 정상적으로 리플레이가 완주해야 한다."""
    result = run_backtest(
        strategy_id="orb_scan", days=10, interval="15m", source="stub", symbols=["TQQQ"],
    )
    assert isinstance(result, BacktestResult)
    assert len(result.equity_curve) > 0
    assert result.strategy_errors == {}, "정상 완주라면 스킵된 사이클이 없어야 한다"


def test_run_backtest_rejects_kr_symbols_on_stub_source():
    """stub은 심볼과 무관하게 US 세션(09:30-16:00 America/New_York) 봉만 합성한다
    (data/stub.py) — KR 심볼로 stub을 돌리면 세션 캘린더가 실제 KR 개장 시간과
    무관해져 결과가 의미 없다. 되는 척 조용히 돌지 않고 여기서 멈춰야 한다.

    이 테스트는 동시에 markets 백필도 검증한다: "000660"은 config/settings.yaml의
    universe.kr 목록(빈 리스트)에 없으므로, market_of_symbol로 채우지 않으면 기본값
    "US"로 떨어져 이 가드를 통과해버린다(2026-08-11 058610 사고와 같은 부류의 결함)."""
    with pytest.raises(ValueError, match="000660"):
        run_backtest(
            strategy_id="orb_scan", days=10, interval="15m", source="stub",
            symbols=["000660"],
        )


def test_run_backtest_mean_reversion_kr_etf_pair_no_longer_silently_swallows_errors():
    """settings.yaml 기본 mean_reversion.symbols는 KR ETF 페어("069500"/"229200")다.
    이 전략은 심볼별로 market_of_symbol()을 추론해 개별적으로 is_market_open을
    묻는데, 예전에는 markets 백필이 없어 캘린더가 "US"로 만들어지고 조회는 "KR"로
    들어와 매 사이클 ValueError가 나며 run_cycle이 그걸 삼켜 "OK, n_trades 0"이라는
    가짜 성공을 냈다. 지금은 이 명확한 stub/KR 가드에 걸려야 한다 — 조용한 실패가
    아니라 시끄러운 실패로 바뀌었다는 것이 이 테스트의 요점이다."""
    with pytest.raises(ValueError, match="069500"):
        run_backtest(strategy_id="mean_reversion", days=10, interval="15m", source="stub")


def test_run_backtest_stub_completes():
    result = run_backtest(strategy_id="donchian", days=20, interval="15m", source="stub")

    assert isinstance(result, BacktestResult)
    assert len(result.equity_curve) > 0
    assert not result.equity_curve.isna().any()
    assert (result.equity_curve > 0).all()

    expected_cols = {
        "ts", "symbol", "side", "qty", "price", "fee",
        "fee_krw", "realized_pnl_krw", "pnl", "reason",
    }
    assert expected_cols.issubset(set(result.trades.columns))

    for key in ("total_return_pct", "cagr_pct", "mdd_pct", "sharpe", "win_rate", "n_trades"):
        assert key in result.metrics


# ------------------------------------------------------------------ 벤치마크(단순 매수보유)
#
# 이 저장소 전체에 "benchmark"/"buy&hold" 문자열이 하나도 없었던 적이 있다 — 그
# 결과 10년 백테스트 -34%를 같은 기간 TQQQ 단순보유 +2,941%와 한 번도 나란히
# 못 봤다. 아래 테스트들은 엔진 내부 값을 그대로 믿지 않고 독립적으로 재계산해
# 대조한다.


def test_backtest_result_includes_benchmark():
    result = run_backtest(strategy_id="donchian", days=15, interval="15m", source="stub")

    assert result.benchmark
    for key in ("buy_hold", "buy_hold_50pct"):
        assert key in result.benchmark
        for metric in ("total_return_pct", "cagr_pct", "mdd_pct", "sharpe"):
            assert metric in result.benchmark[key]


def test_benchmark_buy_hold_matches_direct_price_calc():
    """buy_hold 총수익률은 리플레이와 정확히 같은 구간에서 symbols[0](TQQQ) 가격이
    움직인 비율과 일치해야 한다 — 엔진 내부 값을 믿지 않고 독립적으로 재조회한
    가격으로 대조한다."""
    result = run_backtest(strategy_id="donchian", days=20, interval="15m", source="stub")

    # equity_curve.index[0]은 "아무 것도 하기 전"의 가상 시점, [1]이 리플레이 첫
    # 봉 마감(진입가), [-1]이 마지막 봉 마감(청산가)이다.
    entry_ts = result.equity_curve.index[1]
    exit_ts = result.equity_curve.index[-1]

    # StubDataFeed는 seed=42로 결정론적이라, days를 더 크게 잡아 독립적으로
    # 재생성해도 같은 날짜의 봉은 동일하다.
    feed = StubDataFeed(["TQQQ", "SQQQ"], days=200)
    feed.set_now(entry_ts)
    entry_price = feed.quote("TQQQ").price
    feed.set_now(exit_ts)
    exit_price = feed.quote("TQQQ").price

    expected_return_pct = (exit_price / entry_price - 1) * 100
    assert result.benchmark["buy_hold"]["total_return_pct"] == pytest.approx(expected_return_pct, abs=0.01)


def test_benchmark_50pct_is_half_of_full_buy_hold():
    """buy_hold_50pct는 자본의 50%만 심볼에 넣고 나머지는 무이자 현금이므로,
    총수익률은 buy_hold의 정확히 절반이어야 한다."""
    result = run_backtest(strategy_id="donchian", days=20, interval="15m", source="stub")

    full = result.benchmark["buy_hold"]["total_return_pct"]
    half = result.benchmark["buy_hold_50pct"]["total_return_pct"]
    assert half == pytest.approx(full / 2, abs=1e-6)


def test_equity_curve_starts_at_initial_capital():
    """첫 사이클에서 이미 체결이 날 수 있으므로 자산곡선의 첫 점은 거래 전 시작
    자본이어야 한다 — 리플레이 첫 봉의 사후 자산을 시작점으로 삼으면 그 거래의
    손익이 총수익률에서 통째로 빠진다."""
    result = run_backtest(strategy_id="donchian", days=20, interval="15m", source="stub")

    assert result.equity_curve.iloc[0] == pytest.approx(5_000_000.0)
    assert result.reconciliation["initial_equity"] == pytest.approx(5_000_000.0)


# ------------------------------------------------------------------ 회계 항등식

def test_stub_backtest_satisfies_equity_identity():
    """최종자산 - 초기자산 == Σ실현손익 + 미실현평가손익 - Σ수수료 (전부 KRW).

    run_backtest이 이미 내부에서 강제하지만(위반 시 ReconciliationError), 여기서
    다시 검산해 "예외가 안 났으니 맞겠지"가 아니라 실제 숫자로 확인한다."""
    result = run_backtest(strategy_id="donchian", days=60, interval="15m", source="stub")
    rec = result.reconciliation

    assert rec["currency"] == "KRW"
    assert result.metrics["n_trades"] > 0, "거래가 0건이면 이 검산은 공허하다"

    lhs = rec["final_equity"] - rec["initial_equity"]
    rhs = rec["realized_pnl"] + rec["unrealized_pnl"] - rec["fees"]
    assert lhs == pytest.approx(rhs, abs=rec["tolerance"])
    assert abs(rec["residual"]) <= rec["tolerance"]


def test_stub_backtest_satisfies_per_symbol_quantity_identity():
    """종목별 Σ매수수량 - Σ매도수량 == 최종 보유수량.

    금액 항등식은 체결 로그와 포트폴리오가 같이 틀리면 통과할 수 있지만 이쪽은
    통과하지 못한다 — 로그에서 사라진 체결/유령 수량을 잡는 독립 레일이다."""
    result = run_backtest(strategy_id="donchian", days=60, interval="15m", source="stub")
    trades = result.trades

    assert result.reconciliation["positions"], "검산할 종목이 없다"
    for symbol, check in result.reconciliation["positions"].items():
        rows = trades[trades["symbol"] == symbol]
        buy = rows[rows["side"] == "buy"]["qty"].sum()
        sell = rows[rows["side"] == "sell"]["qty"].sum()
        assert check["buy_qty"] == pytest.approx(buy)
        assert check["sell_qty"] == pytest.approx(sell)
        assert buy - sell == pytest.approx(check["final_qty"], abs=1e-6)


def test_realized_pnl_column_is_krw_not_usd():
    """통화 규약 회귀 방지: 실현손익은 KRW다. 예전에는 손익이 USD, 자산곡선이 KRW라
    부호와 자릿수가 다른 두 숫자가 아무 경고 없이 공존했다."""
    result = run_backtest(strategy_id="donchian", days=60, interval="15m", source="stub")
    sells = result.trades[result.trades["side"] == "sell"]
    assert len(sells) > 0

    fx = FixedFxProvider().usd_krw()
    # 표시 통화(USD) 기준 실현손익 x 환율 == KRW 실현손익.
    assert result.trades["fee_krw"].sum() == pytest.approx(result.trades["fee"].sum() * fx)
    # pnl = 실현손익 - 그 체결의 수수료 (전부 KRW)
    expected = result.trades["realized_pnl_krw"] - result.trades["fee_krw"]
    pd.testing.assert_series_equal(result.trades["pnl"], expected, check_names=False)


# ------------------------------------------------- 검산이 실제로 잡는지(음성 테스트)

def _clean_book() -> tuple[pd.Series, pd.DataFrame, dict]:
    """수기로 만든, 항등식이 정확히 성립하는 최소 장부.

    TQQQ 10주를 $100에 사고(수수료 $1) $110에 전량 매도(수수료 $1.1), 환율 1500원.
    실현손익 $100 = 150,000원, 수수료 $2.1 = 3,150원 -> 자산 +146,850원.
    """
    fx = FixedFxProvider()
    equity = pd.Series(
        [5_000_000.0, 5_146_850.0],
        index=pd.DatetimeIndex(["2026-01-05T14:30:00Z", "2026-01-05T15:30:00Z"], name="ts"),
    )
    trades = pd.DataFrame([
        {"ts": equity.index[0], "symbol": "TQQQ", "side": "buy", "qty": 10.0, "price": 100.0,
         "fee": 1.0, "fee_krw": 1500.0, "realized_pnl_krw": 0.0, "pnl": -1500.0, "reason": ""},
        {"ts": equity.index[1], "symbol": "TQQQ", "side": "sell", "qty": 10.0, "price": 110.0,
         "fee": 1.1, "fee_krw": 1650.0, "realized_pnl_krw": 150_000.0, "pnl": 148_350.0, "reason": ""},
    ])
    kwargs = {"positions": {}, "last_prices": {}, "market_of": {"TQQQ": "US"}, "fx": fx}
    return equity, trades, kwargs


def test_reconcile_passes_on_a_hand_checked_book():
    equity, trades, kwargs = _clean_book()
    rec = _reconcile(equity_curve=equity, trades=trades, **kwargs)

    assert rec["realized_pnl"] == pytest.approx(150_000.0)
    assert rec["fees"] == pytest.approx(3_150.0)
    assert rec["residual"] == pytest.approx(0.0, abs=rec["tolerance"])


def test_reconcile_raises_when_a_fill_is_missing_from_the_log():
    """체결 하나가 로그에서 빠지면 금액·수량 양쪽 항등식이 동시에 깨져야 한다 —
    이것이 실제로 터졌던 결함(로그에 없는 수량 758주)의 모양이다."""
    equity, trades, kwargs = _clean_book()
    truncated = trades.iloc[:1]  # 매도 체결 소실

    with pytest.raises(ReconciliationError) as exc:
        _reconcile(equity_curve=equity, trades=truncated, **kwargs)
    assert "자산 항등식 불일치" in str(exc.value)
    assert "TQQQ" in str(exc.value)


def test_reconcile_raises_when_realized_pnl_is_in_the_wrong_currency():
    """손익만 USD로 남겨두면(예전 동작) 검산이 반드시 실패해야 한다."""
    equity, trades, kwargs = _clean_book()
    usd_pnl = trades.copy()
    usd_pnl.loc[1, "realized_pnl_krw"] = 100.0  # USD 원값 그대로

    with pytest.raises(ReconciliationError, match="자산 항등식 불일치"):
        _reconcile(equity_curve=equity, trades=usd_pnl, **kwargs)


def test_reconcile_raises_on_phantom_open_position():
    """로그상 남아 있는 수량과 실제 보유 수량이 다르면 잡아야 한다."""
    equity, trades, kwargs = _clean_book()
    kwargs = {**kwargs, "positions": {"TQQQ": Position(symbol="TQQQ", qty=5.0, avg_cost=100.0)}}

    with pytest.raises(ReconciliationError, match="최종 보유"):
        _reconcile(equity_curve=equity, trades=trades, **kwargs)


def test_reconcile_counts_open_position_as_unrealized():
    """청산되지 않은 포지션은 미실현 평가손익으로 항등식에 들어가야 한다."""
    fx = FixedFxProvider()
    equity = pd.Series(
        [5_000_000.0, 5_000_000.0 - 1500.0 + 150_000.0],  # 수수료 1500원, 평가익 15만원
        index=pd.DatetimeIndex(["2026-01-05T14:30:00Z", "2026-01-05T15:30:00Z"], name="ts"),
    )
    trades = pd.DataFrame([
        {"ts": equity.index[0], "symbol": "TQQQ", "side": "buy", "qty": 10.0, "price": 100.0,
         "fee": 1.0, "fee_krw": 1500.0, "realized_pnl_krw": 0.0, "pnl": -1500.0, "reason": ""},
    ])
    rec = _reconcile(
        equity_curve=equity, trades=trades,
        positions={"TQQQ": Position(symbol="TQQQ", qty=10.0, avg_cost=100.0)},
        last_prices={"TQQQ": 110.0}, market_of={"TQQQ": "US"}, fx=fx,
    )

    assert rec["unrealized_pnl"] == pytest.approx(150_000.0)
    assert rec["realized_pnl"] == pytest.approx(0.0)
    assert rec["residual"] == pytest.approx(0.0, abs=rec["tolerance"])


# ── 봉 간격 파싱 (2026-08-15) ─────────────────────────────────────────────
#
# 회귀: 엔진이 `int(interval.rstrip("m"))` 로 파싱해 `1d` 에서 ValueError 로 죽었다.
# HistoryDataFeed 는 이미 `_interval_minutes` 로 1d 를 처리하는데 엔진만 못 했다.
#
# 왜 중요한가: 원장 실측에서 US 수수료가 **정률 20bp**(명목 $201~$1,411 전 구간에서
# 19.85~20.56bp)로 확인됐다. 정률이면 명목을 키워도 bp 가 안 줄고, **보유기간을
# 늘려 gross 를 키우는 것만이 비용을 넘는 길**이다. 그 가설을 시험하려면 굵은 봉으로
# 백테스트를 돌려야 하는데, 이 버그가 정확히 그걸 막고 있었다.


def test_daily_interval_is_accepted_by_the_engine():
    from quant.backtest.engine import _interval_to_minutes

    assert _interval_to_minutes("1d") == 24 * 60
    assert _interval_to_minutes("15m") == 15
    assert _interval_to_minutes("5m") == 5


def test_unknown_interval_fails_loudly():
    """조용히 기본값으로 떨어지면 다른 간격의 백테스트를 돌린 줄 모른다."""
    import pytest as _pytest

    from quant.backtest.engine import _interval_to_minutes

    with _pytest.raises(ValueError, match="봉 간격"):
        _interval_to_minutes("1w")


# ── interval="1d" 캐던스가 조용히 거래 0건을 내던 결함 (2026-08-19) ────────────
#
# 증상: interval="1d"로 cross_momentum을 돌리면 거래가 0건이었다(같은 조건에서
# interval="15m"은 정상 거래). 처음엔 "판단 주기가 15:45 마감 전 강제청산을
# 건너뛰던 버그"(test_clock.py의 배경)와 같은 부류 — is_market_open이
# now==session.close에서 항상 False가 되는 것 — 로 의심됐지만, 실측 결과 그건
# 아니었다: `SimClock.is_market_open`은 대부분의 거래일에서 True였다(금요일 봉만
# 다음날인 토요일로 밀려 세션이 없어 False — 그마저도 전체 거래 차단과는 무관).
#
# 진짜 원인은 Clock.now()에 넘기는 시각이었다. 리플레이는 봉 **마감**
# (bar_close = open + interval)을 `clock.set()`에 그대로 넘겼는데, interval이
# 하루 이상이면 bar_close가 봉이 속한 날의 자정을 넘어 **다음 날 자정**이 된다
# (월요일 봉의 마감 = 화요일 00:00). cross_momentum의 주간 리밸런싱 게이트는
# `ctx.clock.now().astimezone(tz).date().weekday() == rebalance_weekday`
# (기본값 0=월요일)로 판정하는데, 거래일 봉의 마감 시각은 절대 월요일 날짜에
# 걸리지 않는다(월+1일=화, 화+1일=수, ..., 금+1일=토 — 주말엔 봉이 없어 그 사이를
# 못 메운다). 조건이 수학적으로 성립 불가능해 거래 0건이 "결과"처럼 보였다.
#
# 수정(`_clock_now_for`, quant/backtest/engine.py)은 cadence >= 1일일 때만
# Clock에 봉의 **시가**(그 봉이 실제로 속한 날)를 넘긴다. look-ahead 방지가 걸린
# DataFeed(`data.set_now`)는 건드리지 않는다 — 시세/이력 조회는 여전히 봉 마감
# 기준으로 완성봉만 본다. intraday(cadence < 1일)에서는 이 오프셋이 하루를 넘지
# 않으므로 보정이 꺼진 채(no-op) 그대로 봉 마감을 쓴다 — 15분봉/5분봉 백테스트
# 결과가 이 수정 전후로 완전히 동일함을 별도로 확인했다(donchian 15m n_trades=53,
# cross_momentum 15m n_trades=15, orb_scan 5m n_trades=0 — 전부 불변).
#
# `quant/core/clock.py`(SimClock/WallClock)는 이 수정에서 단 한 줄도 바뀌지
# 않았다 — 라이브(WallClock)가 이 버그의 영향을 받을 수 없다는 것이 코드
# 자체로 증명된다(같은 파일을 건드리지 않았으므로 회귀 테스트가 필요 없다).


def test_daily_interval_backtest_is_not_silently_empty():
    """이 결함이 위험했던 이유는 에러가 아니라 그럴듯한 숫자("거래 0건")로
    나왔기 때문이다 — 장타(일봉) 전략 검증이 전부 조용히 무효화됐었다."""
    result = run_backtest(
        strategy_id="cross_momentum", days=90, interval="1d", source="stub",
        symbols=["TQQQ", "SQQQ", "SOXL", "SOXS"],
    )
    assert result.metrics["n_trades"] > 0


def test_daily_and_intraday_replay_both_produce_trades_for_the_same_strategy():
    """1d만 결함이고 15m은 정상이던 비대칭이 사라졌는지 직접 대조한다."""
    daily = run_backtest(
        strategy_id="cross_momentum", days=90, interval="1d", source="stub",
        symbols=["TQQQ", "SQQQ", "SOXL", "SOXS"],
    )
    intraday = run_backtest(
        strategy_id="cross_momentum", days=90, interval="15m", source="stub",
        symbols=["TQQQ", "SQQQ", "SOXL", "SOXS"],
    )
    assert daily.metrics["n_trades"] > 0
    assert intraday.metrics["n_trades"] > 0


def test_clock_now_for_keeps_intraday_bar_close_unchanged():
    """cadence < 1일이면 Clock에 넘기는 시각은 봉 마감 그대로다 — 이 보정이
    기존 intraday 백테스트에 아무 영향도 주지 않는다는 것을 직접 증명한다."""
    from quant.backtest.engine import _clock_now_for

    bar_close = pd.Timestamp("2026-01-05 09:45:00", tz="America/New_York")
    assert _clock_now_for(bar_close, minutes=15) == bar_close


def test_clock_now_for_rewinds_to_bar_open_for_daily_cadence():
    """cadence >= 1일이면 Clock에는 봉의 시가(그 봉이 실제로 속한 날)를 넘긴다 —
    봉 마감(다음 날 자정)을 그대로 넘기면 날짜 판정이 하루 밀린다."""
    from quant.backtest.engine import _clock_now_for

    bar_open = pd.Timestamp("2026-01-05 00:00:00", tz="America/New_York")  # 월요일
    bar_close = bar_open + pd.Timedelta(days=1)  # 화요일 00:00 — 월요일 봉의 "마감"
    assert bar_close.date().weekday() != bar_open.date().weekday()  # 전제: 실제로 날짜가 넘어간다
    assert _clock_now_for(bar_close, minutes=24 * 60) == bar_open


def test_clock_now_for_boundary_is_exactly_one_day():
    """경계값(정확히 1440분)에서도 보정이 켜지고, 1439분에서는 꺼진 채 유지된다 —
    향후 실수로 >= 를 > 로 바꾸면 1440분 캐던스(interval="1d")에서 이 테스트가
    깨진다."""
    from quant.backtest.engine import _clock_now_for

    bar_open = pd.Timestamp("2026-01-05 00:00:00", tz="America/New_York")
    bar_close_1440 = bar_open + pd.Timedelta(minutes=1440)
    assert _clock_now_for(bar_close_1440, minutes=1440) == bar_open

    bar_close_1439 = bar_open + pd.Timedelta(minutes=1439)
    assert _clock_now_for(bar_close_1439, minutes=1439) == bar_close_1439
