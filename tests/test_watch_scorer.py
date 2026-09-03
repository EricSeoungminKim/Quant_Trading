"""watch_scorer v2 순수 함수 테스트. 전부 오프라인 — 합성 DataFrame + fake client만
쓴다. v2는 프리퍼시티(하드 게이트) 통과 후 테마별(TREND/REBOUND/EVENT) 증거 점수를
매기는 2계층 모델이다 — score_symbol 자체는 통과/불통과를 판단하지 않는다
(passed는 항상 False로 나오고, run_watch_score가 prereq_ok와 threshold를 함께 봐서
최종 판단한다)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd

from quant.analyze.watch_scorer import (
    ScoreResult,
    _VALID_TAGS,
    _check_prerequisites,
    _event_score,
    _market_cap_krw,
    _rebound_score,
    _trend_score,
    effective_threshold,
    macro_sector_adjustment,
    resolve_regime_label,
    run_watch_score,
    score_symbol,
    sector_daily_adjustment,
)

_DEFAULT_THRESHOLD = 50


# --------------------------------------------------------------- fixtures / fakes

def _make_daily(n: int, start_price: float, pct_change: float, volume: float,
                 last_volume: float | None = None, range_pct: float = 0.04,
                 start: str | None = None) -> pd.DataFrame:
    """일봉 합성 데이터. open은 전일 종가로 잡아 종가>시가(양봉)/종가<시가(음봉)가
    추세 방향과 일치하게 만든다 — 시가=종가로 두면 v1 테스트처럼 양봉/음봉 판정이
    항상 동률로 무너진다.

    날짜는 기본적으로 **어제(직전 영업일)에서 끝난다** — score_symbol이 오늘 이후의
    미완성 행을 잘라내므로(개장 전후 RVOL 붕괴 방지), 오늘을 포함한 픽스처는
    마지막 행(엔지니어링된 RVOL)이 잘려 테스트 의도가 무너진다."""
    if start is not None:
        dates = pd.bdate_range(start=start, periods=n)
    else:
        end = pd.Timestamp.today().normalize() - pd.tseries.offsets.BDay(1)
        dates = pd.bdate_range(end=end, periods=n)
    closes = [start_price * (1 + pct_change) ** i for i in range(n)]
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) * (1 + range_pct / 2) for o, c in zip(opens, closes)]
    lows = [min(o, c) * (1 - range_pct / 2) for o, c in zip(opens, closes)]
    volumes = [volume] * n
    if last_volume is not None:
        volumes[-1] = last_volume
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=dates,
    )


def _uptrend_kr_daily() -> pd.DataFrame:
    """강한 상승추세 + 높은 RVOL(마지막날) — TREND 프로필이 만점 나오는 데이터."""
    return _make_daily(n=60, start_price=20000, pct_change=0.01, volume=500_000, last_volume=1_000_000)


def _downtrend_daily() -> pd.DataFrame:
    return _make_daily(n=60, start_price=20000, pct_change=-0.01, volume=500_000, last_volume=300_000)


def _build_rebound(confirmed: bool) -> pd.DataFrame:
    """20일 고점 대비 -15~-40% 낙폭 구간에서 마지막날 반등(또는 반등 실패)."""
    n_pad = 15
    dates = pd.bdate_range(start="2026-05-01", periods=n_pad + 20)
    pad_dates, window_dates = dates[:n_pad], dates[n_pad:]

    pad_close = [9000.0] * n_pad
    pad_open = [9000.0] * n_pad
    pad_high = [9050.0] * n_pad
    pad_low = [8950.0] * n_pad
    pad_vol = [400_000] * n_pad

    high_day_close = 10000.0  # 20일 윈도우의 고점
    decline_days = 18
    end_close = 7550.0
    closes = [high_day_close]
    for i in range(1, decline_days + 1):
        frac = i / decline_days
        closes.append(high_day_close + (end_close - high_day_close) * frac)
    opens = [high_day_close] + closes[:-1][:decline_days]
    highs = [max(o, c) * 1.01 for o, c in zip(opens, closes)]
    lows = [min(o, c) * 0.99 for o, c in zip(opens, closes)]
    vols = [400_000] * (decline_days + 1)

    prev_close = closes[-1]
    if confirmed:
        last_open, last_close = prev_close * 0.98, prev_close * 1.06
        last_low, last_high = last_open * 0.99, last_close * 1.01
        last_vol = 400_000 * 3
    else:
        last_open, last_close = prev_close * 1.05, prev_close * 1.02  # 음봉(시가>종가)
        last_low, last_high = last_close * 0.99, last_open * 1.01
        last_vol = 400_000 * 1.2

    closes.append(last_close)
    opens.append(last_open)
    highs.append(last_high)
    lows.append(last_low)
    vols.append(last_vol)

    idx = list(pad_dates) + list(window_dates)
    return pd.DataFrame({
        "open": pad_open + opens, "high": pad_high + highs, "low": pad_low + lows,
        "close": pad_close + closes, "volume": pad_vol + vols,
    }, index=idx)


def _build_event(rvol_mult: float, gap_pct: float) -> pd.DataFrame:
    n = 34
    dates = pd.bdate_range(start="2026-06-01", periods=n)
    base = 10000.0
    closes = [base] * (n - 1)
    opens = [base] * (n - 1)
    highs = [base * 1.01] * (n - 1)
    lows = [base * 0.99] * (n - 1)
    vols = [400_000] * (n - 1)

    prev_close = closes[-1]
    last_open = prev_close * (1 + gap_pct)
    last_close = last_open * 1.01
    closes.append(last_close)
    opens.append(last_open)
    highs.append(last_close * 1.01)
    lows.append(last_open * 0.99)
    vols.append(400_000 * rvol_mult)
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols}, index=dates)


class _FakeClient:
    def __init__(self, daily: pd.DataFrame, stock_info: dict | None = None):
        self._daily = daily
        # sharesOutstanding(2026-09-03, 시총 게이트 A) — 기본 픽스처(_uptrend_kr_daily
        # 등, 마지막 종가 ~35,880)에 10,000,000주를 곱하면 ~359억이 아니라
        # ~3,598억원 > 3,000억 기준을 통과한다. 개별 테스트가 시총 게이트 자체를
        # 검증할 땐 이 필드를 명시적으로 빼거나 작은 값으로 덮어써야 한다.
        self._stock_info = (
            stock_info if stock_info is not None
            else {"securityType": "ETF", "sharesOutstanding": "10000000"}
        )

    def candles(self, symbol: str, interval: str = "day", count: int = 90) -> pd.DataFrame:
        return self._daily

    def stock_info(self, symbol: str) -> dict:
        return self._stock_info


class _RaisingCandlesClient:
    def candles(self, symbol: str, interval: str = "day", count: int = 90) -> pd.DataFrame:
        raise RuntimeError("network down")

    def stock_info(self, symbol: str) -> dict:
        return {"securityType": "ETF"}


class _RaisingStockInfoClient:
    def __init__(self, daily: pd.DataFrame):
        self._daily = daily

    def candles(self, symbol: str, interval: str = "day", count: int = 90) -> pd.DataFrame:
        return self._daily

    def stock_info(self, symbol: str) -> dict:
        raise RuntimeError("stock-info down")


# --------------------------------------------------------------- TREND profile

def test_trend_uptrend_passes_at_50():
    d = _uptrend_kr_daily()
    r = score_symbol(d, "005930", ["TREND"], None, _FakeClient(d), today=d.index[-1].date())
    assert r.score >= 50
    assert r.prereq_ok is True


def test_trend_downtrend_fails():
    d = _downtrend_daily()
    r = score_symbol(d, "005930", ["TREND"], None, _FakeClient(d), today=d.index[-1].date())
    assert r.score < effective_threshold(_DEFAULT_THRESHOLD, "neutral")


# --------------------------------------------------------------- REBOUND profile

def test_rebound_with_confirmation_candle_passes():
    d = _build_rebound(confirmed=True)
    score, reasons, _bd = _rebound_score(d)
    assert score >= 50
    assert any("확인캔들" in r and "충족" in r for r in reasons)


def test_rebound_without_confirmation_candle_capped_and_fails():
    """마지막날이 확인캔들(양봉+RVOL>=2)이 아니면 30점 상한 — falling-knife guard."""
    d = _build_rebound(confirmed=False)
    score, _r, _bd = _rebound_score(d)
    assert score <= 30
    assert score < effective_threshold(_DEFAULT_THRESHOLD, "neutral")


# --------------------------------------------------------------- EVENT profile

def test_event_gapup_high_rvol_fresh_report_passes():
    d = _build_event(rvol_mult=1.5, gap_pct=0.02)
    last_bar_date = d.index[-1].date()
    score, reasons, _bd = _event_score(d, last_bar_date)
    assert score >= 50
    assert any("신선도" in r for r in reasons)


def test_event_stale_report_date_fails():
    """동일 데이터라도 리포트 발행일이 마지막 봉보다 2일 넘게 뒤처지면(뒷북) 신선도
    가산이 빠져 threshold 밑으로 떨어진다."""
    d = _build_event(rvol_mult=1.5, gap_pct=0.02)
    last_bar_date = d.index[-1].date()
    score, reasons, _bd = _event_score(d, last_bar_date - timedelta(days=10))
    assert score < effective_threshold(_DEFAULT_THRESHOLD, "neutral")
    assert any("뒷북" in r for r in reasons)


# --------------------------------------------------------------- 무태그(best-of)

def test_untagged_takes_max_across_profiles():
    d = _uptrend_kr_daily()
    today = d.index[-1].date()
    client = _FakeClient(d)
    trend_only = score_symbol(d, "005930", ["TREND"], None, client, today=today)
    untagged = score_symbol(d, "005930", [], None, client, today=today)
    assert untagged.score == trend_only.score
    assert any(reason.startswith("[TREND]") for reason in untagged.reasons)


# --------------------------------------------------------------- 프리퍼시티(하드 게이트)

def test_fewer_than_30_rows_fails_prereq():
    d = _make_daily(n=10, start_price=20000, pct_change=0.01, volume=500_000)
    r = score_symbol(d, "005930", ["TREND"], None, _FakeClient(d), today=d.index[-1].date())
    assert r.prereq_ok is False
    assert r.score == 0
    assert r.reasons == ["데이터 부족(30행 미만)"]


def test_stale_last_bar_fails_prereq_but_still_reports_score():
    d = _uptrend_kr_daily()
    stale_today = d.index[-1].date() + timedelta(days=10)
    r = score_symbol(d, "005930", ["TREND"], None, _FakeClient(d), today=stale_today)
    assert r.prereq_ok is False
    assert r.score > 0  # 실패해도 점수는 계속 보고한다
    assert any("데이터 최신성 부족" in reason for reason in r.reasons)


def test_kr_non_etf_fails_with_cost_reason():
    d = _uptrend_kr_daily()
    client = _FakeClient(d, stock_info={"securityType": "STOCK"})
    r = score_symbol(d, "005930", ["TREND"], None, client, today=d.index[-1].date())
    assert r.prereq_ok is False
    assert any("왕복 23bp" in reason for reason in r.reasons)


def test_kr_etf_passes_product_prereq():
    d = _uptrend_kr_daily()
    client = _FakeClient(d, stock_info={"securityType": "ETF", "sharesOutstanding": "10000000"})
    r = score_symbol(d, "005930", ["TREND"], None, client, today=d.index[-1].date())
    assert r.prereq_ok is True


def test_stock_info_failure_does_not_block_product_check_but_market_cap_gate_now_fails():
    """stock_info 조회 자체가 실패하면(예: rate limit) 상품유형 판정은 여전히
    비차단(미확인 표기만)이지만, 시총도 같은 stock_info 응답에서 나오므로
    (sharesOutstanding, 2026-09-03 시총 게이트 A) 시총도 미확인이 되어 이번엔
    이 게이트가 막는다 — "모르는 시총을 조용히 통과시키지 않는다"는 정책이라
    ETF 판정 실패의 관대함(상품유형 미확인은 비차단)과는 다르다."""
    d = _uptrend_kr_daily()
    client = _RaisingStockInfoClient(d)
    r = score_symbol(d, "005930", ["TREND"], None, client, today=d.index[-1].date())
    assert r.prereq_ok is False
    assert any("상품유형 미확인" in reason for reason in r.reasons)
    assert any("시총 미확인" in reason for reason in r.reasons)


# --------------------------------------------------------------- effective_threshold / regime

def test_defensive_regime_raises_effective_threshold():
    assert effective_threshold(50, "defensive") == 60


def test_aggressive_regime_lowers_effective_threshold():
    assert effective_threshold(50, "aggressive") == 45


def test_neutral_regime_leaves_threshold_unchanged():
    assert effective_threshold(50, "neutral") == 50


def test_us_symbol_needs_5_more_boundary():
    assert effective_threshold(50, "neutral", is_us=True) == 55


def test_resolve_regime_label_passes_through_fresh_state():
    now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    state = {"label": "aggressive", "computed_at": (now - timedelta(hours=1)).isoformat()}
    label, reason = resolve_regime_label(state, now=now)
    assert label == "aggressive"
    assert reason is None


def test_resolve_regime_label_stale_falls_back_to_neutral():
    now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    state = {"label": "aggressive", "computed_at": (now - timedelta(hours=25)).isoformat()}
    label, reason = resolve_regime_label(state, now=now)
    assert label == "neutral"
    assert reason is not None


def test_resolve_regime_label_missing_state_defaults_neutral_without_reason():
    label, reason = resolve_regime_label(None)
    assert label == "neutral"
    assert reason is None


# --------------------------------------------------------------- run_watch_score

def test_run_watch_score_marks_passing_symbol():
    d = _uptrend_kr_daily()
    client = _FakeClient(d)
    results = run_watch_score(["005930:TREND"], client, threshold=_DEFAULT_THRESHOLD, regime_label="neutral")
    assert len(results) == 1
    assert results[0].passed is True
    assert results[0].tags == ["TREND"]


def test_run_watch_score_catches_per_symbol_candle_exception_without_raising():
    results = run_watch_score(["TQQQ"], _RaisingCandlesClient(), threshold=_DEFAULT_THRESHOLD, regime_label="neutral")
    assert len(results) == 1
    r = results[0]
    assert r.score == 0
    assert r.passed is False
    assert r.reasons and "조회 실패" in r.reasons[0]


def test_run_watch_score_disabled_returns_all_fail_without_network():
    results = run_watch_score(
        ["TQQQ", "005930:REBOUND"], client=None, threshold=_DEFAULT_THRESHOLD,
        regime_label="neutral", enabled=False,
    )
    assert len(results) == 2
    assert all(r.passed is False for r in results)
    assert all(r.reasons == ["auto_score 비활성"] for r in results)


def test_run_watch_score_unknown_tag_falls_back_to_untagged_best_of():
    d = _uptrend_kr_daily()
    client = _FakeClient(d)
    # today는 마지막 봉의 "다음날" — score_symbol이 today 이후 미완성 행을 잘라내므로
    # 마지막 봉이 완성일로 남으려면 today가 그보다 뒤여야 한다.
    results = run_watch_score(["005930:BOGUS"], client, threshold=_DEFAULT_THRESHOLD, regime_label="neutral",
                               today=d.index[-1].date() + timedelta(days=1))
    r = results[0]
    assert r.tags == []
    assert "알 수 없는 태그" in r.reasons
    assert r.score == 100  # TREND가 best-of로 선택됨


# ---------------------------------------------------------------------------
# 시장 수급 조류 (market_flow_adjustment) — 외국인+기관 순매수 기반 임계 조정
# ---------------------------------------------------------------------------
def _flow_client(base: "_FakeClient", net_per_market: int) -> "_FakeClient":
    """FakeClient에 investor_trading을 얹는다 — 시장당 순매수 net_per_market원."""
    def investor_trading(symbol="KOSPI", interval="1d", count=2):
        buy = max(net_per_market, 0)
        sell = max(-net_per_market, 0)
        return {"records": [{
            "date": "2026-08-08",
            "foreigner": {"buyAmount": str(buy), "sellAmount": str(sell)},
            "institution": {"buyAmount": "0", "sellAmount": "0"},
        }]}
    base.investor_trading = investor_trading
    return base


def test_market_flow_tailwind_lowers_kr_threshold():
    from quant.analyze.watch_scorer import market_flow_adjustment
    client = _flow_client(_FakeClient(_uptrend_kr_daily()), net_per_market=int(5e11))
    adj, reason = market_flow_adjustment(client)
    assert adj == -5
    assert "순풍" in reason


def test_market_flow_headwind_raises_kr_threshold():
    from quant.analyze.watch_scorer import market_flow_adjustment
    client = _flow_client(_FakeClient(_uptrend_kr_daily()), net_per_market=int(-5e11))
    adj, reason = market_flow_adjustment(client)
    assert adj == +5
    assert "역풍" in reason


def test_market_flow_missing_api_degrades_to_zero_adjustment():
    """investor_trading이 없는 클라이언트(구버전/페이크) → 조정 0, 채점은 계속된다."""
    from quant.analyze.watch_scorer import market_flow_adjustment
    adj, reason = market_flow_adjustment(_FakeClient(_uptrend_kr_daily()))
    assert adj == 0
    assert "실패" in reason


def test_market_flow_applies_to_kr_but_not_us_symbols():
    """역풍(+5)에서 KR 임계는 55가 되지만 US 임계는 수급 조정 없이 그대로다."""
    d = _uptrend_kr_daily()
    client = _flow_client(_FakeClient(d), net_per_market=int(-5e11))
    results = run_watch_score(
        ["005930:TREND", "TQQQ:TREND"], client,
        threshold=_DEFAULT_THRESHOLD, regime_label="neutral",
    )
    kr, us = results[0], results[1]
    assert any("역풍" in r for r in kr.reasons), "KR 결과에 수급 사유가 붙어야 한다"
    assert not any("역풍" in r for r in us.reasons), "US 결과에는 수급 조정이 없어야 한다"


# ---------------------------------------------------------------------------
# 종목별 수급 (symbol_flow_adjustment, 키움 ka10059) — 시장 조류보다 우선
# ---------------------------------------------------------------------------
class _FakeKiwoom:
    def __init__(self, frgn: str, orgn: str):
        self._row = {"dt": "20260807", "frgnr_invsr": frgn, "orgn": orgn}

    def investor_flow_daily(self, symbol, date_yyyymmdd):
        return [self._row]


class _RaisingKiwoom:
    def investor_flow_daily(self, symbol, date_yyyymmdd):
        raise RuntimeError("Request Blocked")


def test_symbol_flow_net_buy_gives_tailwind():
    from datetime import date as _date

    from quant.analyze.watch_scorer import symbol_flow_adjustment
    adj, reason = symbol_flow_adjustment(_FakeKiwoom("+577807", "-517404"), "005930", _date(2026, 8, 10))
    assert adj == -5 and "순풍" in reason


def test_symbol_flow_net_sell_gives_headwind():
    from datetime import date as _date

    from quant.analyze.watch_scorer import symbol_flow_adjustment
    adj, reason = symbol_flow_adjustment(_FakeKiwoom("-398687", "8288"), "005930", _date(2026, 8, 10))
    assert adj == +5 and "역풍" in reason


def test_symbol_flow_failure_returns_none_for_market_fallback():
    from datetime import date as _date

    from quant.analyze.watch_scorer import symbol_flow_adjustment
    assert symbol_flow_adjustment(_RaisingKiwoom(), "005930", _date(2026, 8, 10)) is None


def test_symbol_flow_overrides_market_flow_in_run():
    """키움 종목별 수급이 있으면 시장 조류 대신 그것이 사유에 붙는다."""
    d = _uptrend_kr_daily()
    client = _flow_client(_FakeClient(d), net_per_market=int(-5e11))  # 시장은 역풍
    results = run_watch_score(
        ["005930:TREND"], client, threshold=_DEFAULT_THRESHOLD, regime_label="neutral",
        kiwoom_client=_FakeKiwoom("100000", "50000"),  # 종목은 순풍
    )
    r = results[0]
    assert any("종목 수급 순풍" in x for x in r.reasons)
    assert not any("시장 수급" in x for x in r.reasons), "종목별 수급이 있으면 시장 조류는 안 쓴다"


def test_symbol_flow_failure_falls_back_to_market_flow_in_run():
    d = _uptrend_kr_daily()
    client = _flow_client(_FakeClient(d), net_per_market=int(-5e11))
    results = run_watch_score(
        ["005930:TREND"], client, threshold=_DEFAULT_THRESHOLD, regime_label="neutral",
        kiwoom_client=_RaisingKiwoom(),
    )
    assert any("시장 수급 역풍" in x for x in results[0].reasons)


# ---------------------------------------------------------------------------
# p5 종목 경고 게이트 (Toss stocks/{symbol}/warnings)
# ---------------------------------------------------------------------------
class _WarningClient(_FakeClient):
    def __init__(self, daily, warnings):
        super().__init__(daily)
        self._warnings = warnings

    def stock_warnings(self, symbol):
        if isinstance(self._warnings, Exception):
            raise self._warnings
        return self._warnings


def test_dangerous_warning_blocks_prerequisite():
    d = _uptrend_kr_daily()
    client = _WarningClient(d, [{"warningType": "INVESTMENT_RISK"}])
    results = run_watch_score(["069500:TREND"], client, threshold=0, regime_label="neutral")
    r = results[0]
    assert r.prereq_ok is False and r.passed is False
    assert any("매수 유의 지정" in x for x in r.reasons)


def test_overheated_blocks_but_vi_passes_with_note():
    d = _uptrend_kr_daily()
    blocked = run_watch_score(
        ["069500:TREND"], _WarningClient(d, [{"warningType": "OVERHEATED"}]),
        threshold=0, regime_label="neutral",
    )[0]
    assert blocked.prereq_ok is False

    mild = run_watch_score(
        ["069500:TREND"], _WarningClient(d, [{"warningType": "VI_STATIC"}]),
        threshold=0, regime_label="neutral",
    )[0]
    assert mild.prereq_ok is True
    assert any("경고 표기" in x for x in mild.reasons)


def test_warning_fetch_failure_is_non_blocking():
    d = _uptrend_kr_daily()
    r = run_watch_score(
        ["069500:TREND"], _WarningClient(d, RuntimeError("api down")),
        threshold=0, regime_label="neutral",
    )[0]
    assert r.prereq_ok is True
    assert any("경고 조회 실패" in x for x in r.reasons)


def test_unknown_warning_code_passes_with_note():
    d = _uptrend_kr_daily()
    r = run_watch_score(
        ["069500:TREND"], _WarningClient(d, [{"warningType": "FUTURE_NEW_CODE"}]),
        threshold=0, regime_label="neutral",
    )[0]
    assert r.prereq_ok is True, "스펙: unknown code는 허용해야 한다"


# ---------------------------------------------------------------------------
# discover_candidates — 거래대금 랭킹 기반 후보 발굴
# ---------------------------------------------------------------------------
class _RankingClient:
    def __init__(self, symbols):
        self._symbols = symbols

    def rankings(self, type, market_country, *, duration="realtime", count=20,
                 exclude_investment_caution=False):
        return {"rankings": [{"rank": i + 1, "symbol": s} for i, s in enumerate(self._symbols)]}


def test_discover_candidates_returns_tagged_kr_symbols():
    from quant.analyze.watch_scorer import discover_candidates
    client = _RankingClient(["069500", "TQQQ", "005930", "005930", "122630"])
    out = discover_candidates(client, market="KR", top=3)
    assert out == ["069500:TREND", "005930:TREND", "122630:TREND"], "US 제외·중복 제거·top 캡"


def test_discover_candidates_failure_returns_empty():
    from quant.analyze.watch_scorer import discover_candidates

    class _Boom:
        def rankings(self, *a, **k):
            raise RuntimeError("down")

    assert discover_candidates(_Boom()) == []


# ---------------------------------------------------------------------------
# allow_kr_stocks — 개별주 자동 편입 허용(매도세는 paper 수수료 모델이 반영)
# ---------------------------------------------------------------------------
def test_allow_kr_stocks_converts_block_to_pass_with_note():
    d = _uptrend_kr_daily()
    client = _FakeClient(
        d, stock_info={"securityType": "STOCK", "sharesOutstanding": "10000000"},
    )
    blocked = run_watch_score(["005930:TREND"], client, threshold=0, regime_label="neutral")[0]
    assert blocked.prereq_ok is False, "기본값은 여전히 차단"

    allowed = run_watch_score(
        ["005930:TREND"], client, threshold=0, regime_label="neutral", allow_kr_stocks=True,
    )[0]
    assert allowed.prereq_ok is True
    assert any("왕복 23bp paper 반영" in x for x in allowed.reasons)


# ================================ US 자동 편입 발굴 (2026-08-11 사용자 요청)

def test_discover_candidates_us_keeps_us_tickers_and_drops_kr_codes():
    """US 발굴은 US 티커만 — 6자리 숫자(KR)가 섞이면 시장 필터가 깨진 것이다.

    실측 배경: 자동 편입이 KR만 되던 이유가 발굴 경로가 KR 전용이었기 때문
    (2026-08-10~11 자동 등록 7종목 전부 KR).
    """
    from quant.analyze.watch_scorer import discover_candidates

    class _Client:
        def __init__(self):
            self.calls = []

        def rankings(self, **kw):
            self.calls.append(kw)
            return {"rankings": [
                {"symbol": "NVDA"}, {"symbol": "TSLA"}, {"symbol": "005930"},
                {"symbol": "NVDA"},  # 중복
                {"symbol": ""},      # 빈 값
            ]}

    c = _Client()
    out = discover_candidates(c, market="US", top=10)
    assert out == ["NVDA:TREND", "TSLA:TREND", "005930:TREND"], (
        "US 랭킹은 응답을 그대로 신뢰한다(KR 필터는 market='KR'일 때만 적용) — "
        "중복·빈 값만 제거"
    )
    assert c.calls[0]["market_country"] == "US", "요청이 US 시장으로 나가야 한다"


def test_discover_candidates_kr_filters_out_non_kr_symbols():
    from quant.analyze.watch_scorer import discover_candidates

    class _Client:
        def rankings(self, **kw):
            return {"rankings": [{"symbol": "005930"}, {"symbol": "NVDA"}]}

    out = discover_candidates(_Client(), market="KR", top=10)
    assert out == ["005930:TREND"], "KR 발굴에 US 티커가 섞이면 안 된다"


def test_discover_candidates_returns_empty_on_api_failure_and_never_raises():
    """발굴은 보너스 경로 — 실패가 본 채점을 죽이면 안 된다."""
    from quant.analyze.watch_scorer import discover_candidates

    class _Broken:
        def rankings(self, **kw):
            raise RuntimeError("api down")

    assert discover_candidates(_Broken(), market="US") == []


# ---------------------------------------------------------------------------
# ATR 게이트 레버리지 정규화 (_check_prerequisites) — 레버리지/인버스 ETF는
# 태생적으로 ATR이 배수만큼 커지므로, 기초자산 기준(ATR ÷ |배수|)으로 판정한다.
# ---------------------------------------------------------------------------

class _LeverageClient:
    """stock_info만 있으면 되는 최소 페이크. warnings/candles는 _check_prerequisites가
    직접 부르지 않으므로(그건 run_watch_score 레벨) 필요 없다."""

    def __init__(self, leverage_factor=None, security_type="ETF", raise_on_info=False):
        self._leverage_factor = leverage_factor
        self._security_type = security_type
        self._raise = raise_on_info
        self.stock_info_calls = 0

    def stock_info(self, symbol):
        self.stock_info_calls += 1
        if self._raise:
            raise RuntimeError("stock-info down")
        info = {"securityType": self._security_type}
        if self._leverage_factor is not None:
            info["leverageFactor"] = self._leverage_factor
        return info

    def stock_warnings(self, symbol):
        return []


def _flat_daily_with_atr_pct(atr_pct: float, n: int = 40, price: float = 100.0,
                              volume: float = 1_000_000.0) -> pd.DataFrame:
    """레인지(고가-저가)가 종가의 atr_pct%가 되도록 만든 평평한(가격 불변) 일봉.
    가격이 안 움직이므로 True Range == high-low가 되고, ATR(14) ≈ atr_pct%."""
    return _make_daily(n=n, start_price=price, pct_change=0.0, volume=volume,
                        range_pct=atr_pct / 100)


def test_atr_gate_normalizes_leveraged_etf_atr_and_passes():
    """3배 ETF의 raw ATR 19.05%는 원래 게이트(0.5~15%)를 벗어나지만, 3으로 나눈
    6.35%는 정상 범위 — 레버리지를 알면 정상 종목이 탈락하지 않아야 한다."""
    d = _flat_daily_with_atr_pct(19.05)
    client = _LeverageClient(leverage_factor=3.0)
    today = d.index[-1].date() + timedelta(days=1)
    failures, info = _check_prerequisites(d, "SOXS", is_kr=False, client=client, today=today)
    assert not any("변동성" in f for f in failures)
    assert any("변동성 정상" in i and "3배" in i for i in info)


def test_atr_gate_without_leverage_info_uses_raw_gate_and_fails():
    """같은 raw ATR(19.05%)이라도 레버리지 배수를 모르면 정규화하지 않고 기존
    0.5~15% 게이트를 그대로 적용 — 모르는 것을 안전하다고 가정하지 않는다."""
    d = _flat_daily_with_atr_pct(19.05)
    client = _LeverageClient(leverage_factor=None)  # leverageFactor 필드 없음
    today = d.index[-1].date() + timedelta(days=1)
    failures, _info = _check_prerequisites(d, "SOXS", is_kr=False, client=client, today=today)
    assert any("변동성 비정상" in f for f in failures)
    assert any("레버리지 미상" in f for f in failures)


def test_atr_gate_leveraged_etf_still_fails_when_truly_excessive():
    """정규화해도 기초자산 기준 변동성이 15%를 넘으면 여전히 탈락해야 한다."""
    d = _flat_daily_with_atr_pct(50.0)  # 3으로 나눠도 16.67% > 15%
    client = _LeverageClient(leverage_factor=3.0)
    today = d.index[-1].date() + timedelta(days=1)
    failures, _info = _check_prerequisites(d, "SOXS", is_kr=False, client=client, today=today)
    assert any("변동성 비정상" in f and "3배" in f for f in failures)


def test_atr_gate_stock_info_failure_falls_back_to_raw_gate():
    """stock_info 조회 자체가 실패해도(예: rate limit) 채점이 죽지 않고 원래
    게이트로 판정한다."""
    d = _flat_daily_with_atr_pct(2.0)  # 원래 게이트로도 정상
    client = _LeverageClient(raise_on_info=True)
    today = d.index[-1].date() + timedelta(days=1)
    failures, _info = _check_prerequisites(d, "XYZ", is_kr=False, client=client, today=today)
    assert not any("변동성" in f for f in failures)


def test_atr_gate_reuses_single_stock_info_call_for_kr_symbol():
    """KR 종목은 ETF 판정과 레버리지 정규화가 같은 stock_info 응답을 쓴다 —
    중복 조회하면 안 된다(호출 횟수 = rate limit 비용)."""
    d = _flat_daily_with_atr_pct(6.0, price=20000.0)
    client = _LeverageClient(leverage_factor=2.0, security_type="ETF")
    today = d.index[-1].date() + timedelta(days=1)
    failures, info = _check_prerequisites(d, "122630", is_kr=True, client=client, today=today)
    assert client.stock_info_calls == 1
    assert not any("변동성" in f for f in failures)
    assert not any("매도세" in f for f in failures)  # ETF로 정상 판정됐다는 방증


# ============ 상품 구조 배제 (2026-08-12 레버리지 유니버스 확대와 함께)

def test_structural_exclusions_reject_even_when_volatility_passes():
    """ATR 게이트를 통과해도 상품 구조가 위험하면 거래하지 않는다.

    실측(2026-08-12): UVXY는 ATR 5.87%/1.5배 = 3.91%로 게이트를 **통과**한다.
    최근 시장이 조용해서일 뿐이고, VIX 선물 콘탱고 감쇠(상장 이후 -99.9%)는
    변동성 지표로 잡히지 않는다. 자동 발굴이 랭킹 상위에서 이걸 집어오는 날이
    반드시 오므로 문서가 아니라 코드로 막는다.
    """
    import pandas as pd

    from quant.analyze.watch_scorer import _check_prerequisites

    # 게이트를 전부 통과할 정상 일봉 (유동성·신선도·변동성 모두 정상)
    idx = pd.date_range("2026-07-01", periods=30, freq="D")
    daily = pd.DataFrame({
        "open": [100.0] * 30, "high": [101.0] * 30, "low": [99.0] * 30,
        "close": [100.0] * 30, "volume": [1_000_000.0] * 30,
    }, index=idx)
    today = idx[-1].date()

    class _Client:
        def stock_info(self, symbol):
            return {"securityType": "ETF", "leverageFactor": 1.5, "name": symbol}

    for sym, hint in [("UVXY", "VIX"), ("SVXY", "VIX"), ("BOIL", "천연가스"),
                      ("KOLD", "천연가스"), ("225130", "합성")]:
        failures, _ = _check_prerequisites(daily, sym, sym.isdigit(), _Client(), today)
        joined = " ".join(failures)
        assert "상품 구조 배제" in joined, f"{sym}이 배제되지 않음: {failures}"
        assert hint in joined, f"{sym} 사유에 근거가 없음: {failures}"


def test_normal_leveraged_etf_is_not_structurally_excluded():
    """배제는 특정 상품 구조에만 적용된다 — 일반 레버리지 ETF는 통과해야 한다.
    (SOXL/TQQQ/122630까지 막으면 이번 유니버스 확대 자체가 무의미해진다.)"""
    import pandas as pd

    from quant.analyze.watch_scorer import _check_prerequisites

    idx = pd.date_range("2026-07-01", periods=30, freq="D")
    daily = pd.DataFrame({
        "open": [100.0] * 30, "high": [104.0] * 30, "low": [96.0] * 30,
        "close": [100.0] * 30, "volume": [1_000_000.0] * 30,
    }, index=idx)
    today = idx[-1].date()

    class _Client:
        def stock_info(self, symbol):
            return {"securityType": "ETF", "leverageFactor": 3, "name": symbol}

    for sym in ("SOXL", "TQQQ", "SOXS"):
        failures, _ = _check_prerequisites(daily, sym, False, _Client(), today)
        assert not any("상품 구조 배제" in f for f in failures), f"{sym}: {failures}"


def test_exclusion_is_case_insensitive_and_trims():
    import pandas as pd

    from quant.analyze.watch_scorer import _check_prerequisites

    idx = pd.date_range("2026-07-01", periods=30, freq="D")
    daily = pd.DataFrame({
        "open": [100.0] * 30, "high": [101.0] * 30, "low": [99.0] * 30,
        "close": [100.0] * 30, "volume": [1_000_000.0] * 30,
    }, index=idx)

    class _Client:
        def stock_info(self, symbol):
            return {"securityType": "ETF", "leverageFactor": 1.5}

    failures, _ = _check_prerequisites(daily, " uvxy ", False, _Client(), idx[-1].date())
    assert any("상품 구조 배제" in f for f in failures)


# --------------------------------------------------------------- 서브프로젝트 T
# (2026-08-17) — EVENT_SCALP(news_scalp)/FRGN(frgn_accumulate) 태그 배선.
# 둘 다 새 채점식이 아니라 기존 프로필의 별칭이다("기존 임계 그대로 태우되 태그만
# EVENT_SCALP로" — spec §5/T 지시) — 아래는 그 별칭이 실제로 동일 점수를 내는지,
# 그리고 두 태그가 _VALID_TAGS에 등록돼 "알 수 없는 태그"로 무태그 강등되지 않는지
# 검증한다.

def test_event_scalp_and_frgn_are_registered_as_valid_tags():
    assert "EVENT_SCALP" in _VALID_TAGS
    assert "FRGN" in _VALID_TAGS


def test_unknown_tag_token_is_not_registered_as_the_bare_symbol_tags():
    """대조군 — 모르는 태그는 여전히 파싱 단계에서 무태그로 강등돼야 한다(회귀 방지:
    EVENT_SCALP/FRGN을 등록하면서 검증 자체를 느슨하게 풀지 않았는지 확인)."""
    from quant.analyze.watch_scorer import _parse_token

    symbol, tags, _, reasons = _parse_token("005930:NOT_A_REAL_TAG")
    assert tags == []
    assert any("알 수 없는 태그" in r for r in reasons)


def test_event_scalp_tag_scores_identically_to_event_profile():
    """"기존 임계 그대로 태우되 태그만 EVENT_SCALP로" — EVENT 프로필의 완전한 별칭."""
    d = _build_event(rvol_mult=1.5, gap_pct=0.02)
    today = d.index[-1].date()
    client = _FakeClient(d)

    event = score_symbol(d, "005930", ["EVENT"], today, client, today=today)
    event_scalp = score_symbol(d, "005930", ["EVENT_SCALP"], today, client, today=today)

    assert event_scalp.score == event.score
    assert event_scalp.profile == "EVENT_SCALP"
    assert event_scalp.tags == ["EVENT_SCALP"], "PASS 토큰에 실리는 태그는 채점 프로필이 아니라 입력 태그 그대로"


def test_frgn_tag_scores_identically_to_trend_profile():
    d = _uptrend_kr_daily()
    today = d.index[-1].date()
    client = _FakeClient(d)

    trend = score_symbol(d, "005930", ["TREND"], None, client, today=today)
    frgn = score_symbol(d, "005930", ["FRGN"], None, client, today=today)

    assert frgn.score == trend.score
    assert frgn.profile == "FRGN"
    assert frgn.tags == ["FRGN"]


def test_frgn_and_event_scalp_run_through_run_watch_score_end_to_end():
    """PASS 토큰 조립까지 포함한 회귀 — run_watch_score가 EVENT_SCALP/FRGN 토큰을
    받아도 KeyError 없이 채점하고, 통과 시 원래 태그를 그대로 돌려준다."""
    d = _build_event(rvol_mult=3.0, gap_pct=0.03)
    today = d.index[-1].date()
    yyyymmdd = today.strftime("%Y%m%d")
    client = _FakeClient(d)

    results = run_watch_score(
        [f"005930:EVENT_SCALP:{yyyymmdd}", "005930:FRGN"], client, _DEFAULT_THRESHOLD, "neutral",
    )
    assert [r.tags for r in results] == [["EVENT_SCALP"], ["FRGN"]]


# ---------------------------------------------------------------------------
# 자금 흐름 섹터 기울기 (macro_sector_adjustment) — §4, 2026-08-31 소유자 지시
# ---------------------------------------------------------------------------

def test_macro_sector_adjustment_known_sector_returns_score_and_reason():
    sector_map = {"096770": "석유와가스"}
    sector_tilt = {"석유와가스": {"score": 2, "why": ["정제마진 확대"]}}
    result = macro_sector_adjustment("096770", sector_map, sector_tilt)
    assert result == (2, "매크로: 석유와가스 +2 (정제마진 확대)")


def test_macro_sector_adjustment_unmapped_symbol_returns_none():
    """sector_map에 없는 종목(섹터 미상)은 None — 불이익 없음."""
    assert macro_sector_adjustment("005930", {"096770": "석유와가스"},
                                    {"석유와가스": {"score": 2, "why": []}}) is None


def test_macro_sector_adjustment_sector_not_in_tilt_table_returns_none():
    """섹터는 알지만 오늘 활성화된 매크로 드라이버가 그 섹터를 안 건드리면 None."""
    sector_map = {"005930": "반도체와반도체장비"}
    assert macro_sector_adjustment("005930", sector_map, {"석유와가스": {"score": 2, "why": []}}) is None


def test_macro_sector_adjustment_missing_inputs_returns_none():
    assert macro_sector_adjustment("096770", None, {"석유와가스": {"score": 2, "why": []}}) is None
    assert macro_sector_adjustment("096770", {"096770": "석유와가스"}, None) is None


def test_macro_sector_adjustment_clips_to_plus_minus_2():
    """money_flow.sector_tilt이 이미 -2..2로 자르지만, 이 함수도 방어적으로
    한 번 더 자른다(계약이 깨져도 채점이 ±2를 넘지 않게)."""
    sector_map = {"096770": "석유와가스"}
    sector_tilt = {"석유와가스": {"score": 5, "why": []}}
    score, _ = macro_sector_adjustment("096770", sector_map, sector_tilt)
    assert score == 2


def test_score_symbol_applies_macro_sector_adj_to_score_and_breakdown():
    d = _uptrend_kr_daily()
    client = _FakeClient(d)
    today = d.index[-1].date()
    base = score_symbol(d, "096770", ["TREND"], None, client, today=today)
    boosted = score_symbol(d, "096770", ["TREND"], None, client, today=today,
                            macro_sector_adj=(2, "매크로: 석유와가스 +2 (정제마진 확대)"))
    assert boosted.score == base.score + 2
    assert "매크로: 석유와가스 +2 (정제마진 확대)" in boosted.reasons
    assert ("매크로 섹터 기울기", 2, 2, "매크로: 석유와가스 +2 (정제마진 확대)") in boosted.breakdown


def test_run_watch_score_applies_macro_adjustment_for_kr_symbol():
    d = _uptrend_kr_daily()
    client = _FakeClient(d)
    sector_map = {"096770": "석유와가스"}
    sector_tilt = {"석유와가스": {"score": 2, "why": ["정제마진 확대"]}}

    plain = run_watch_score(["096770:TREND"], client, _DEFAULT_THRESHOLD, "neutral")
    boosted = run_watch_score(
        ["096770:TREND"], client, _DEFAULT_THRESHOLD, "neutral",
        sector_map=sector_map, sector_tilt=sector_tilt,
    )
    assert boosted[0].score == plain[0].score + 2
    assert any("매크로: 석유와가스" in r for r in boosted[0].reasons)


def test_run_watch_score_us_symbol_unaffected_even_if_sector_data_present():
    """US 종목은 섹터 매핑이 없다(모듈 docstring 조사 결과) — sector_map/
    sector_tilt이 주어져도 US 심볼 채점에는 영향이 없다."""
    d = _uptrend_kr_daily()
    client = _FakeClient(d)
    sector_map = {"TQQQ": "XLK(기술/성장주)"}  # 가정: 매핑이 있더라도
    sector_tilt = {"XLK(기술/성장주)": {"score": -1, "why": ["할인율 부담"]}}

    plain = run_watch_score(["TQQQ:TREND"], client, _DEFAULT_THRESHOLD, "neutral")
    with_sector_data = run_watch_score(
        ["TQQQ:TREND"], client, _DEFAULT_THRESHOLD, "neutral",
        sector_map=sector_map, sector_tilt=sector_tilt,
    )
    assert with_sector_data[0].score == plain[0].score
    assert not any("매크로:" in r for r in with_sector_data[0].reasons)


def test_run_watch_score_unknown_sector_symbol_not_penalized():
    """sector_map에 없는 KR 종목은 매크로 조정 없이 기존 점수 그대로다."""
    d = _uptrend_kr_daily()
    client = _FakeClient(d)
    sector_tilt = {"석유와가스": {"score": 2, "why": ["정제마진 확대"]}}

    plain = run_watch_score(["005930:TREND"], client, _DEFAULT_THRESHOLD, "neutral")
    unmapped = run_watch_score(
        ["005930:TREND"], client, _DEFAULT_THRESHOLD, "neutral",
        sector_map={"096770": "석유와가스"}, sector_tilt=sector_tilt,
    )
    assert unmapped[0].score == plain[0].score


# ---------------------------------------------------------------------------
# 시가총액 게이트 (소유자 철학 지시 A, 2026-09-03) — KR 후보 시총 3,000억 미만/
# 미확인은 자동등록 차단. US는 KRW 표시 기준 규칙이라 적용하지 않는다.
# ---------------------------------------------------------------------------

def test_market_cap_krw_computes_shares_times_close():
    assert _market_cap_krw({"sharesOutstanding": "1000"}, 100.0) == 100_000


def test_market_cap_krw_missing_shares_returns_none():
    assert _market_cap_krw({"securityType": "STOCK"}, 100.0) is None
    assert _market_cap_krw(None, 100.0) is None


def test_market_cap_krw_unparseable_shares_returns_none():
    assert _market_cap_krw({"sharesOutstanding": "abc"}, 100.0) is None


def test_market_cap_above_threshold_passes_with_confirmation_reason():
    d = _uptrend_kr_daily()  # 마지막 종가 ~35,974원
    client = _FakeClient(d, stock_info={"securityType": "STOCK", "sharesOutstanding": "10000000"})
    r = score_symbol(d, "005930", ["TREND"], None, client, today=d.index[-1].date())
    assert not any(reason.startswith("시총 미확인") or reason.startswith("시총 <") for reason in r.reasons)
    assert any(reason.startswith("시총 확인") for reason in r.reasons)


def test_market_cap_below_threshold_fails_prereq():
    d = _uptrend_kr_daily()
    # 1,000주 × 마지막 종가(~35,974원) ≈ 3,600만원 << 3,000억 기준.
    client = _FakeClient(d, stock_info={"securityType": "STOCK", "sharesOutstanding": "1000"})
    r = score_symbol(d, "005930", ["TREND"], None, client, today=d.index[-1].date())
    assert r.prereq_ok is False
    assert any(reason.startswith("시총 <3,000억") for reason in r.reasons)


def test_market_cap_unknown_fails_prereq_with_reason():
    d = _uptrend_kr_daily()
    client = _FakeClient(d, stock_info={"securityType": "STOCK"})  # sharesOutstanding 없음
    r = score_symbol(d, "005930", ["TREND"], None, client, today=d.index[-1].date())
    assert r.prereq_ok is False
    assert any(reason.startswith("시총 미확인") for reason in r.reasons)


def test_market_cap_gate_not_applied_to_us_symbols():
    d = _uptrend_kr_daily()
    client = _FakeClient(d, stock_info={"securityType": "STOCK"})  # sharesOutstanding 없음
    failures, _info = _check_prerequisites(
        d, "TQQQ", is_kr=False, client=client, today=d.index[-1].date(),
    )
    assert not any("시총" in f for f in failures)


# ---------------------------------------------------------------------------
# 전일 상한가 게이트 (소유자 결정, 2026-09-03 — L2, risk/manager.py의
# prev_limit_up_block(L1 회로차단기)와 짝을 이루는 자동등록 단계 방어선). 근거는
# 모듈 상단 `_PREV_LIMIT_UP_THRESHOLD_PCT` 주석의 실측 수치와 같은 원장이다.
# ---------------------------------------------------------------------------

def _kr_daily_with_prev_session_return(pct: float) -> pd.DataFrame:
    """`_uptrend_kr_daily()` 기반, 마지막 완성 세션의 종가만 전날 종가 대비 pct%로
    덮어쓴다 — 다른 프리퍼시티(ATR/유동성/시총)에 쓰이는 나머지 행은 그대로 둬서
    이 게이트만 격리해서 검증한다."""
    d = _uptrend_kr_daily().copy()
    close_col, open_col = d.columns.get_loc("close"), d.columns.get_loc("open")
    high_col, low_col = d.columns.get_loc("high"), d.columns.get_loc("low")
    prev_close = float(d.iloc[-2, close_col])
    new_close = prev_close * (1 + pct / 100)
    d.iloc[-1, open_col] = prev_close
    d.iloc[-1, close_col] = new_close
    d.iloc[-1, high_col] = max(new_close, prev_close) * 1.01
    d.iloc[-1, low_col] = min(new_close, prev_close) * 0.99
    return d


def test_prev_limit_up_blocks_prereq_at_exactly_threshold():
    d = _kr_daily_with_prev_session_return(29.5)
    client = _FakeClient(d)  # 기본 stock_info = ETF + 시총 통과(모듈 상단 주석 참고)
    failures, _info = _check_prerequisites(d, "005930", is_kr=True, client=client, today=d.index[-1].date())
    assert any(reason.startswith("전일 상한가") for reason in failures)


def test_prev_limit_up_blocks_above_threshold():
    d = _kr_daily_with_prev_session_return(35.0)
    client = _FakeClient(d)
    failures, _info = _check_prerequisites(d, "005930", is_kr=True, client=client, today=d.index[-1].date())
    assert any(reason.startswith("전일 상한가") for reason in failures)


def test_prev_limit_up_allows_just_below_threshold():
    d = _kr_daily_with_prev_session_return(29.4)
    client = _FakeClient(d)
    failures, _info = _check_prerequisites(d, "005930", is_kr=True, client=client, today=d.index[-1].date())
    assert not any(reason.startswith("전일 상한가") for reason in failures)


def test_prev_limit_up_gate_not_applied_to_us_symbols():
    d = _kr_daily_with_prev_session_return(50.0)
    client = _FakeClient(d)
    failures, _info = _check_prerequisites(d, "TQQQ", is_kr=False, client=client, today=d.index[-1].date())
    assert not any("상한가" in f for f in failures)


def test_prev_limit_up_fails_prereq_via_score_symbol():
    """`_check_prerequisites` 단위가 아니라 `score_symbol` 경유로도 prereq_ok가
    False로 떨어지는지 확인 — cmd_watch_score의 자동등록 차단이 실제로 이 실패에
    묶여 있다."""
    d = _kr_daily_with_prev_session_return(40.0)
    client = _FakeClient(d)
    # today는 마지막 봉 날짜의 **다음날**이어야 한다 — score_symbol은 `today` 당일
    # (및 이후) 행을 미완성으로 간주해 잘라낸다(개장 전후 RVOL 붕괴 방지). 마지막
    # 봉 날짜를 그대로 today로 주면 방금 조작한 마지막 행 자체가 잘려나간다.
    today = d.index[-1].date() + timedelta(days=1)
    r = score_symbol(d, "005930", ["TREND"], None, client, today=today)
    assert r.prereq_ok is False
    assert any(reason.startswith("전일 상한가") for reason in r.reasons)


# ---------------------------------------------------------------------------
# 주도 섹터 보너스 (sector_daily_adjustment) — 소유자 철학 지시 B, 2026-09-03
# ---------------------------------------------------------------------------

def test_sector_daily_adjustment_top3_positive_returns_plus8():
    sector_map = {"096770": "석유와가스"}
    ctx = {"top3_positive": {"석유와가스"}, "negative_streak3": set()}
    result = sector_daily_adjustment("096770", sector_map, ctx)
    assert result == (8, "주도섹터: 석유와가스 거래대금 top3 + 외국인 순매수 (+8)")


def test_sector_daily_adjustment_negative_streak_returns_minus4():
    sector_map = {"096770": "석유와가스"}
    ctx = {"top3_positive": set(), "negative_streak3": {"석유와가스"}}
    result = sector_daily_adjustment("096770", sector_map, ctx)
    assert result == (-4, "주도섹터: 석유와가스 외국인 순매수 3일 연속 이탈 (-4)")


def test_sector_daily_adjustment_unmapped_symbol_returns_none():
    ctx = {"top3_positive": {"석유와가스"}, "negative_streak3": set()}
    assert sector_daily_adjustment("005930", {"096770": "석유와가스"}, ctx) is None


def test_sector_daily_adjustment_missing_inputs_returns_none():
    assert sector_daily_adjustment("096770", None, {"top3_positive": {"석유와가스"}}) is None
    assert sector_daily_adjustment("096770", {"096770": "석유와가스"}, None) is None


def test_sector_daily_adjustment_neither_bucket_returns_none():
    sector_map = {"096770": "석유와가스"}
    ctx = {"top3_positive": {"반도체"}, "negative_streak3": {"화학"}}
    assert sector_daily_adjustment("096770", sector_map, ctx) is None


def test_score_symbol_applies_sector_daily_adj_to_score_and_breakdown():
    d = _uptrend_kr_daily()
    client = _FakeClient(d)
    today = d.index[-1].date()
    base = score_symbol(d, "096770", ["TREND"], None, client, today=today)
    boosted = score_symbol(
        d, "096770", ["TREND"], None, client, today=today,
        sector_daily_adj=(8, "주도섹터: 석유와가스 거래대금 top3 + 외국인 순매수 (+8)"),
    )
    assert boosted.score == base.score + 8
    assert "주도섹터: 석유와가스 거래대금 top3 + 외국인 순매수 (+8)" in boosted.reasons
    assert (
        "주도 섹터 보너스", 8, 8, "주도섹터: 석유와가스 거래대금 top3 + 외국인 순매수 (+8)",
    ) in boosted.breakdown


def test_run_watch_score_applies_sector_daily_bonus_for_kr_symbol():
    d = _uptrend_kr_daily()
    client = _FakeClient(d)
    sector_map = {"096770": "석유와가스"}
    ctx = {"top3_positive": {"석유와가스"}, "negative_streak3": set()}

    plain = run_watch_score(["096770:TREND"], client, _DEFAULT_THRESHOLD, "neutral")
    boosted = run_watch_score(
        ["096770:TREND"], client, _DEFAULT_THRESHOLD, "neutral",
        sector_map=sector_map, sector_daily_ctx=ctx,
    )
    assert boosted[0].score == plain[0].score + 8
    assert any("주도섹터:" in r for r in boosted[0].reasons)


def test_run_watch_score_us_symbol_unaffected_by_sector_daily_ctx():
    d = _uptrend_kr_daily()
    client = _FakeClient(d)
    sector_map = {"TQQQ": "XLK(기술/성장주)"}
    ctx = {"top3_positive": {"XLK(기술/성장주)"}, "negative_streak3": set()}

    plain = run_watch_score(["TQQQ:TREND"], client, _DEFAULT_THRESHOLD, "neutral")
    with_ctx = run_watch_score(
        ["TQQQ:TREND"], client, _DEFAULT_THRESHOLD, "neutral",
        sector_map=sector_map, sector_daily_ctx=ctx,
    )
    assert with_ctx[0].score == plain[0].score
    assert not any("주도섹터:" in r for r in with_ctx[0].reasons)
