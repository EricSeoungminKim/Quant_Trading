"""MarketDataService(Anti-Corruption Layer) 테스트. 페이크 소스만 사용 — 네트워크 없음.

커버리지: 우선순위 라우팅, 실패 시 폴백+degraded 가시성, provenance, 심볼/interval
불일치 소스 스킵, look-ahead 필터링(마지막 방어선), naive 타임스탬프 정규화,
전체 실패 시 None/빈 프레임, health()의 소스별 실패 상태 반영."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from quant.adapters.data.service import Capability, MarketDataService, SourceRoute
from quant.core.models import Quote
from quant.core.ports import DataSourceError

_OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


# --------------------------------------------------------------------- helpers

class FakeClock:
    def __init__(self, now: datetime, cadence_minutes: float = 15.0):
        self._now = now
        self._cadence = cadence_minutes

    def now(self) -> datetime:
        return self._now

    def is_market_open(self, market: str) -> bool:
        return True

    def minutes_to_close(self, market: str) -> float | None:
        return 120.0

    def cadence_minutes(self) -> float:
        return self._cadence

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        mtc = self.minutes_to_close(market)
        return mtc is not None and mtc - self._cadence < flatten_minutes


class FakeSource:
    """quote()/history() 호출을 기록하고, 설정에 따라 값을 반환하거나 예외를 던진다."""

    def __init__(
        self,
        quote_result: Quote | None = None,
        quote_error: Exception | None = None,
        history_df: pd.DataFrame | None = None,
        history_error: Exception | None = None,
    ):
        self._quote_result = quote_result
        self._quote_error = quote_error
        self._history_df = history_df if history_df is not None else pd.DataFrame(columns=_OHLCV_COLUMNS)
        self._history_error = history_error
        self.quote_calls: list[str] = []
        self.history_calls: list[tuple[str, str, int]] = []

    def quote(self, symbol: str) -> Quote | None:
        self.quote_calls.append(symbol)
        if self._quote_error is not None:
            raise self._quote_error
        return self._quote_result

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        self.history_calls.append((symbol, interval, n))
        if self._history_error is not None:
            raise self._history_error
        return self._history_df


def _bars(start: str, periods: int, freq: str = "15min", tz: str | None = "UTC") -> pd.DataFrame:
    idx = pd.date_range(start, periods=periods, freq=freq, tz=tz)
    prices = [100.0 + i for i in range(periods)]
    return pd.DataFrame({
        "open": prices, "high": [p + 1 for p in prices], "low": [p - 1 for p in prices],
        "close": prices, "volume": [10.0] * periods,
    }, index=idx)


# ------------------------------------------------------------------- routing

def test_routing_picks_declared_priority_source():
    primary = FakeSource(quote_result=Quote(symbol="TQQQ", ts=datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc), price=51.0))
    secondary = FakeSource(quote_result=Quote(symbol="TQQQ", ts=datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc), price=99.0))
    svc = MarketDataService(
        routes=[
            SourceRoute(name="primary", source=primary, capabilities=frozenset({Capability.QUOTE})),
            SourceRoute(name="secondary", source=secondary, capabilities=frozenset({Capability.QUOTE})),
        ],
        clock=FakeClock(datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)),
    )

    q = svc.quote("TQQQ")

    assert q is not None
    assert q.price == 51.0  # primary 응답이 사용됨
    assert secondary.quote_calls == []  # fallback 없이 primary만 호출됨


# ------------------------------------------------------------------- fallback

def test_falls_back_when_primary_raises_and_records_degraded_state(caplog):
    primary = FakeSource(quote_error=RuntimeError("network down"))
    secondary = FakeSource(quote_result=Quote(symbol="TQQQ", ts=datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc), price=99.0))
    svc = MarketDataService(
        routes=[
            SourceRoute(name="primary", source=primary, capabilities=frozenset({Capability.QUOTE})),
            SourceRoute(name="secondary", source=secondary, capabilities=frozenset({Capability.QUOTE})),
        ],
        clock=FakeClock(datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)),
    )

    with caplog.at_level("DEBUG"):
        q = svc.quote("TQQQ")

    assert q is not None
    assert q.price == 99.0  # secondary로 폴백됨
    # 폴백이 결국 성공했으므로(D5/D6, 2026-09-05) 로그는 WARNING이 아니라 DEBUG다
    # — 설계대로 동작한 폴백은 소음이지 경고가 아니다. 그래도 기록 자체는 남는다.
    debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert any("primary" in rec.message for rec in debug_records)
    assert not any("primary" in rec.message for rec in caplog.records if rec.levelname == "WARNING")

    health = svc.health()
    # degraded는 2026-08-12부터 "끝내 못 받았다"는 뜻이다 — 폴백이 성공했으므로 False.
    # 소스별 실패는 아래처럼 그대로 관측 가능하다(관측 가능성은 유지, 알람 기준만 변경).
    assert health.degraded is False
    assert health.sources["primary"].healthy is False
    assert health.sources["primary"].consecutive_failures == 1
    assert health.sources["secondary"].healthy is True


# ------------------------------------------------------ symbol/interval skip

def test_source_not_supporting_symbol_or_interval_is_skipped():
    wrong_symbol = FakeSource(history_df=_bars("2024-01-02T09:30", 4))
    wrong_interval = FakeSource(history_df=_bars("2024-01-02T09:30", 4))
    matching = FakeSource(history_df=_bars("2024-01-02T09:30", 4))
    svc = MarketDataService(
        routes=[
            SourceRoute(name="wrong_symbol", source=wrong_symbol, capabilities=frozenset({Capability.BARS}), symbols=frozenset({"SQQQ"})),
            SourceRoute(name="wrong_interval", source=wrong_interval, capabilities=frozenset({Capability.BARS}), intervals=frozenset({"1d"})),
            SourceRoute(name="matching", source=matching, capabilities=frozenset({Capability.BARS})),
        ],
        clock=FakeClock(datetime(2024, 1, 3, 0, 0, tzinfo=timezone.utc)),
    )

    svc.history("TQQQ", "15m", 10)

    assert wrong_symbol.history_calls == []
    assert wrong_interval.history_calls == []
    # 서비스는 형성봉 보정을 위해 소스에 n+1을 요청한다(2026-08-24 결함 수리).
    assert matching.history_calls == [("TQQQ", "15m", 11)]


# -------------------------------------------------------------- look-ahead

def test_lookahead_bars_from_sloppy_source_are_filtered():
    bars = _bars("2024-01-02T09:30", 6)  # 09:30, 09:45, 10:00, 10:15, 10:30, 10:45
    now = bars.index[3]  # 10:15 — 09:30/09:45/10:00 bin만 마감됨(open+15m<=now)
    sloppy = FakeSource(history_df=bars)  # 미래 봉(10:15~10:45)까지 그대로 반환하는 sloppy 소스
    svc = MarketDataService(
        routes=[SourceRoute(name="sloppy", source=sloppy, capabilities=frozenset({Capability.BARS}))],
        clock=FakeClock(now),
    )

    out = svc.history("TQQQ", "15m", 10)

    assert len(out) == 3
    assert (out.index + pd.Timedelta(minutes=15) <= now).all()


# ---------------------------------------------------------- tz normalization

def test_naive_timestamps_get_normalized():
    naive_bars = _bars("2024-01-02T09:30", 3, tz=None)  # naive index
    now = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
    bars_source = FakeSource(history_df=naive_bars)
    quote_source = FakeSource(quote_result=Quote(symbol="TQQQ", ts=datetime(2024, 1, 2, 9, 45), price=51.0))
    svc = MarketDataService(
        routes=[
            SourceRoute(name="bars", source=bars_source, capabilities=frozenset({Capability.BARS})),
            SourceRoute(name="quotes", source=quote_source, capabilities=frozenset({Capability.QUOTE})),
        ],
        clock=FakeClock(now),
    )

    out = svc.history("TQQQ", "15m", 10)
    q = svc.quote("TQQQ")

    assert out.index.tz is not None
    assert str(out.index.tz) == "UTC"
    assert q is not None
    assert q.ts.tzinfo is not None
    assert q.ts.utcoffset() == timedelta(0)


# ----------------------------------------------------------- all sources fail

def test_all_sources_fail_quote_returns_none_and_history_returns_empty_frame():
    failing_quote = FakeSource(quote_error=RuntimeError("down"))
    failing_bars = FakeSource(history_error=RuntimeError("down"))
    svc = MarketDataService(
        routes=[
            SourceRoute(name="q", source=failing_quote, capabilities=frozenset({Capability.QUOTE})),
            SourceRoute(name="b", source=failing_bars, capabilities=frozenset({Capability.BARS})),
        ],
        clock=FakeClock(datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)),
    )

    assert svc.quote("TQQQ") is None

    out = svc.history("TQQQ", "15m", 10)
    assert out.empty
    assert list(out.columns) == _OHLCV_COLUMNS

    health = svc.health()
    assert health.degraded is True
    assert health.sources["q"].healthy is False
    assert health.sources["b"].healthy is False


# ------------------------------------------------------------- 사이클 내 quote 캐시


class _CountingQuoteSource:
    def __init__(self, price: float = 100.0):
        self.calls = 0
        self.price = price

    def quote(self, symbol: str):
        self.calls += 1
        return Quote(symbol=symbol, ts=datetime(2024, 6, 3, 14, 0, tzinfo=timezone.utc),
                     price=self.price)

    def history(self, symbol: str, interval: str, n: int):
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def _svc(source, clock, **kw):
    return MarketDataService(
        routes=[SourceRoute(name="src", source=source,
                            capabilities=frozenset({Capability.QUOTE, Capability.BARS}))],
        clock=clock, **kw,
    )


def test_quote_cache_collapses_repeat_calls_within_a_cycle():
    """한 사이클에서 quote()는 전략·리스크·체결이 각각 부른다(최소 3회).

    캐시가 없으면 셋이 서로 다른 HTTP 응답을 본다 — 승인 시점 가격으로 수량을
    계산해 놓고 다른 가격에 체결되므로 주문금액 상한 검증이 무의미해진다.
    """
    src = _CountingQuoteSource()
    svc = _svc(src, FakeClock(datetime(2024, 6, 3, 14, 0, tzinfo=timezone.utc)), quote_cache_seconds=5.0)

    prices = [svc.quote("TQQQ").price for _ in range(3)]
    assert src.calls == 1
    assert prices == [100.0, 100.0, 100.0], "같은 사이클은 같은 스냅샷을 봐야 한다"


def test_quote_cache_is_off_by_default():
    src = _CountingQuoteSource()
    svc = _svc(src, FakeClock(datetime(2024, 6, 3, 14, 0, tzinfo=timezone.utc)))
    for _ in range(3):
        svc.quote("TQQQ")
    assert src.calls == 3, "기본값은 캐시 없음 — 기존 동작이 바뀌면 안 된다"


def test_quote_cache_expires_so_next_cycle_gets_fresh_data():
    src = _CountingQuoteSource()
    svc = _svc(src, FakeClock(datetime(2024, 6, 3, 14, 0, tzinfo=timezone.utc)), quote_cache_seconds=0.01)
    svc.quote("TQQQ")
    time.sleep(0.02)
    svc.quote("TQQQ")
    assert src.calls == 2, "TTL이 지나면 반드시 새로 받아야 한다(시세가 굳으면 안 된다)"


def test_quote_cache_is_per_symbol():
    src = _CountingQuoteSource()
    svc = _svc(src, FakeClock(datetime(2024, 6, 3, 14, 0, tzinfo=timezone.utc)), quote_cache_seconds=5.0)
    svc.quote("TQQQ")
    svc.quote("SQQQ")
    svc.quote("TQQQ")
    assert src.calls == 2


# ============ degraded 정의 (2026-08-12): 폴백 성공은 장애가 아니다
# 배경: 예전 정의 any(not healthy)는 US 세션 내내 오보를 냈다. 키움 웹소켓은 해외
# 틱을 구조적으로 못 주므로 US 심볼에 **항상** 실패 상태인데, Toss 폴백이 정상
# 서빙해도 degraded=True가 되어 "시세 조회 연속 3회 실패"가 5분마다 떴다.
# 항상 울리는 경고는 없는 경고보다 나쁘다 — 진짜 장애를 무시하게 만든다.

_NOW = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)


def test_all_sources_exhausted_is_degraded():
    """전 소스 소진 = 진짜 데이터 손실 = 알람 대상."""
    svc = MarketDataService(
        routes=[
            SourceRoute(name="a", source=FakeSource(quote_error=RuntimeError("down")),
                        capabilities=frozenset({Capability.QUOTE})),
            SourceRoute(name="b", source=FakeSource(quote_error=RuntimeError("down")),
                        capabilities=frozenset({Capability.QUOTE})),
        ],
        clock=FakeClock(_NOW),
    )
    assert svc.quote("TQQQ") is None
    assert svc.health().degraded is True


def test_degraded_clears_once_data_flows_again():
    """장애가 회복되면 알람도 내려가야 한다 — 안 내려가면 그것도 오보다."""
    flaky = FakeSource(quote_error=RuntimeError("down"))
    svc = MarketDataService(
        routes=[SourceRoute(name="only", source=flaky,
                            capabilities=frozenset({Capability.QUOTE}))],
        clock=FakeClock(_NOW),
        quote_cache_seconds=0,
    )
    svc.quote("TQQQ")
    assert svc.health().degraded is True

    flaky._quote_error = None
    flaky._quote_result = Quote(symbol="TQQQ", ts=_NOW, price=1.0)
    svc.quote("TQQQ")
    assert svc.health().degraded is False


def test_history_exhaustion_also_marks_degraded():
    svc = MarketDataService(
        routes=[SourceRoute(name="only", source=FakeSource(history_error=RuntimeError("down")),
                            capabilities=frozenset({Capability.BARS}))],
        clock=FakeClock(_NOW),
    )
    assert svc.history("TQQQ", "5m", 10).empty
    assert svc.health().degraded is True


# ---------------------------------------------------- 형성봉 보정 (2026-08-24)

class _TailingSource(FakeSource):
    """실제 어댑터처럼 요청 n개로 잘라 반환한다 — 이 tail 이 결함의 절반이다."""

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        super().history(symbol, interval, n)
        return self._history_df.tail(n)


def test_history_compensates_for_forming_bar_dropped_by_lookahead_filter():
    """실측 결함(2026-08-24): 어댑터는 요청 n개로 잘라 주고, 서비스의 완성봉
    필터가 형성 중인 마지막 봉을 버리면 소비자는 n-1개를 받는다. cross_momentum
    (월요일 **장중** 리밸런스)은 일봉 21개를 요구하는데 항상 20개를 받아
    '랭킹봉부족: 21' — 태어나서 한 번도 랭킹하지 못했다. 서비스는 소스에
    여유분을 요청해 필터 후에도 n개를 채운다. 형성봉 제외 자체(look-ahead
    계약)는 불변이다."""
    df = _bars("2024-01-01", 30, freq="1D")
    src = _TailingSource(history_df=df)
    now = (df.index[-1] + timedelta(hours=2)).to_pydatetime()  # 마지막 일봉 형성 중
    svc = MarketDataService(
        routes=[SourceRoute(name="s", source=src, capabilities=frozenset({Capability.BARS}))],
        clock=FakeClock(now),
    )
    out = svc.history("A", "1d", 21)
    assert len(out) == 21, "형성봉이 잘려도 요청한 개수는 채워져야 한다"
    assert out.index[-1] == df.index[-2], "형성 중인 봉은 여전히 제외(look-ahead 불변)"


def test_history_when_market_closed_still_returns_n():
    """장 마감 후(형성봉 없음)에는 보정 여유분이 결과를 부풀리면 안 된다."""
    df = _bars("2024-01-01", 30, freq="1D")
    src = _TailingSource(history_df=df)
    now = (df.index[-1] + timedelta(days=2)).to_pydatetime()  # 전부 완성
    svc = MarketDataService(
        routes=[SourceRoute(name="s", source=src, capabilities=frozenset({Capability.BARS}))],
        clock=FakeClock(now),
    )
    out = svc.history("A", "1d", 21)
    assert len(out) == 21
    assert out.index[-1] == df.index[-1]


# --------------------------------------------------- 사이클당 콜드 페치 예산

_BUDGET_NOW = datetime(2024, 6, 3, 14, 0, tzinfo=timezone.utc)


def _budget_svc(cold_fetch_budget_per_cycle, src=None):
    src = src or FakeSource(history_df=_bars("2024-06-03T13:00", 4))
    svc = MarketDataService(
        routes=[SourceRoute(name="s", source=src, capabilities=frozenset({Capability.BARS}))],
        clock=FakeClock(_BUDGET_NOW),
        cold_fetch_budget_per_cycle=cold_fetch_budget_per_cycle,
    )
    return svc, src


def test_cold_fetch_within_budget_succeeds():
    """예산 내 콜드 페치(캐시 미스)는 정상적으로 소스를 때리고 결과를 받는다."""
    svc, src = _budget_svc(cold_fetch_budget_per_cycle=2)

    out_a = svc.history("A", "15m", 3)
    out_b = svc.history("B", "15m", 3)

    assert not out_a.empty
    assert not out_b.empty
    assert len(src.history_calls) == 2


def test_cold_fetch_over_budget_raises_without_calling_source():
    """예산을 넘는 캐시 미스는 DataSourceError를 던지고 소스는 아예 호출하지 않는다."""
    svc, src = _budget_svc(cold_fetch_budget_per_cycle=2)
    svc.history("A", "15m", 3)
    svc.history("B", "15m", 3)
    assert len(src.history_calls) == 2  # 예산 소진 직전 상태

    with pytest.raises(DataSourceError, match="콜드 페치 예산 초과"):
        svc.history("C", "15m", 3)

    assert len(src.history_calls) == 2, "예산 초과 시 소스를 때리면 안 된다"


def test_cold_fetch_cache_hit_does_not_consume_budget():
    """이미 캐시에 있는 심볼(히트)은 예산과 무관하게 계속 응답한다."""
    svc, src = _budget_svc(cold_fetch_budget_per_cycle=1)

    svc.history("A", "15m", 3)  # 미스 1회 — 예산 소진
    assert len(src.history_calls) == 1

    # 같은 (symbol, interval, 봉 경계)에 대한 반복 요청은 캐시 히트라 예산을 안 쓴다.
    for _ in range(3):
        out = svc.history("A", "15m", 3)
        assert not out.empty
    assert len(src.history_calls) == 1, "캐시 히트는 소스를 다시 때리면 안 된다"

    # 새 심볼(B)은 예산이 이미 0이라 여전히 거부된다.
    with pytest.raises(DataSourceError):
        svc.history("B", "15m", 3)


def test_cold_fetch_budget_recovers_after_cycle_reset():
    """reset_cycle_budget() 이후엔 예산이 회복되어 이전엔 거부된 심볼도 다시 시도된다."""
    svc, src = _budget_svc(cold_fetch_budget_per_cycle=1)

    svc.history("A", "15m", 3)
    with pytest.raises(DataSourceError):
        svc.history("B", "15m", 3)
    assert len(src.history_calls) == 1

    svc.reset_cycle_budget()

    out_b = svc.history("B", "15m", 3)
    assert not out_b.empty
    assert len(src.history_calls) == 2, "리셋 후엔 B도 소스를 때려야 한다"


def test_cold_fetch_budget_unset_is_unlimited_by_default():
    """cold_fetch_budget_per_cycle을 안 주면(기본 None) 기존 동작 그대로 무제한이다."""
    svc, src = _budget_svc(cold_fetch_budget_per_cycle=None)
    for i in range(10):
        svc.history(f"SYM{i}", "15m", 3)
    assert len(src.history_calls) == 10


@pytest.mark.parametrize("bad_budget", [0, -1, -5])
def test_cold_fetch_budget_rejects_non_positive(bad_budget):
    """0/음수 예산은 "무제한"과 혼동되기 쉬운 값이라 생성자가 명시적으로 거부한다."""
    with pytest.raises(ValueError):
        MarketDataService(
            routes=[SourceRoute(name="s", source=FakeSource(),
                                capabilities=frozenset({Capability.BARS}))],
            clock=FakeClock(_BUDGET_NOW),
            cold_fetch_budget_per_cycle=bad_budget,
        )


def test_reset_cycle_budget_is_harmless_without_budget_configured():
    """예산 미설정(None)이어도 reset_cycle_budget() 호출은 안전해야 한다(loop가 항상 부른다)."""
    svc, src = _budget_svc(cold_fetch_budget_per_cycle=None)
    svc.reset_cycle_budget()  # 예외 없이 통과해야 함
    out = svc.history("A", "15m", 3)
    assert not out.empty


# ---------------------------------------------- 실패 로그 스로틀 (D5/D6, 2026-09-05)
# 4일간 WARNING 이상 146,451줄의 대부분이 키움 실시간(stale 틱)/미국(쿨다운)
# 라우트가 매 quote() 호출마다 "실패, 폴백 시도"를 WARNING으로 찍은 것이었는데,
# 두 경우 다 다음 라우트(toss)가 정상 응답해 실제로는 데이터 손실이 아니었다.
# 여기서는 fake clock(time.monotonic 몽키패치)으로 (route,symbol)별 스로틀
# 상태머신만 고정한다 — stale 임계값이나 라우팅 자체는 건드리지 않는다.

_THROTTLE_NOW = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)


def test_total_failure_first_occurrence_warns_repeat_within_window_is_debug(monkeypatch, caplog):
    """끝내 아무 라우트도 못 살렸을 때(진짜 데이터 손실)만 스로틀 대상이다 —
    첫 발생은 WARNING, 10분 안의 재발은 DEBUG로 눌러 담는다."""
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake_now["t"])

    only = FakeSource(quote_error=RuntimeError("down"))
    svc = MarketDataService(
        routes=[SourceRoute(name="only", source=only, capabilities=frozenset({Capability.QUOTE}))],
        clock=FakeClock(_THROTTLE_NOW),
    )

    with caplog.at_level("DEBUG"):
        svc.quote("TQQQ")  # 1차 — 첫 발생
        fake_now["t"] += 60.0  # 1분 후(10분 미만)
        svc.quote("TQQQ")  # 2차 — 스로틀 창 안

    warn = [r for r in caplog.records if r.levelname == "WARNING"]
    debug = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert len(warn) == 1
    assert len(debug) == 1


def test_total_failure_warns_again_after_ten_minutes_elapse(monkeypatch, caplog):
    """10분(스로틀 창)이 지나 여전히 실패 중이면 재발도 다시 WARNING — 지속되는
    장애를 DEBUG 뒤에 영원히 숨기지 않는다."""
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake_now["t"])

    only = FakeSource(quote_error=RuntimeError("down"))
    svc = MarketDataService(
        routes=[SourceRoute(name="only", source=only, capabilities=frozenset({Capability.QUOTE}))],
        clock=FakeClock(_THROTTLE_NOW),
    )

    with caplog.at_level("DEBUG"):
        svc.quote("TQQQ")  # 첫 발생 — WARNING
        fake_now["t"] += 601.0  # 10분(600초) 초과 경과
        svc.quote("TQQQ")  # 재발 — 다시 WARNING

    warn = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warn) == 2


def test_recovery_from_total_failure_logs_one_warning(monkeypatch, caplog):
    """그 라우트가 데이터 손실 상태에서 회복되면(다시 성공) WARNING 한 줄로
    알린다 — 안 그러면 장애가 언제 끝났는지 로그만 보고는 알 수 없다."""
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake_now["t"])

    flaky = FakeSource(quote_error=RuntimeError("down"))
    svc = MarketDataService(
        routes=[SourceRoute(name="only", source=flaky, capabilities=frozenset({Capability.QUOTE}))],
        clock=FakeClock(_THROTTLE_NOW),
    )

    with caplog.at_level("DEBUG"):
        svc.quote("TQQQ")  # 실패 — WARNING(첫 발생)
        flaky._quote_error = None
        flaky._quote_result = Quote(symbol="TQQQ", ts=_THROTTLE_NOW, price=1.0)
        svc.quote("TQQQ")  # 회복

    recovery = [r for r in caplog.records if "정상 회복" in r.message]
    assert len(recovery) == 1
    assert "only" in recovery[0].message


def test_fallback_saved_route_never_needs_a_recovery_warning(monkeypatch, caplog):
    """폴백이 계속 대신 응답해준 라우트는 애초에 WARNING을 낸 적이 없으므로,
    나중에 그 라우트가 직접 성공해도 '회복' 알림이 뜨지 않는다(WARNING을 낸
    적 없는 상태의 '회복'은 의미가 없다)."""
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake_now["t"])

    primary = FakeSource(quote_error=RuntimeError("down"))
    secondary = FakeSource(quote_result=Quote(symbol="TQQQ", ts=_THROTTLE_NOW, price=99.0))
    svc = MarketDataService(
        routes=[
            SourceRoute(name="primary", source=primary, capabilities=frozenset({Capability.QUOTE})),
            SourceRoute(name="secondary", source=secondary, capabilities=frozenset({Capability.QUOTE})),
        ],
        clock=FakeClock(_THROTTLE_NOW),
    )

    with caplog.at_level("DEBUG"):
        svc.quote("TQQQ")  # primary 실패하지만 secondary가 살려줌 — DEBUG만
        primary._quote_error = None
        primary._quote_result = Quote(symbol="TQQQ", ts=_THROTTLE_NOW, price=50.0)
        svc.quote("TQQQ")  # primary가 이제 직접 성공

    assert not any(r.levelname == "WARNING" for r in caplog.records)
    assert not any("정상 회복" in r.message for r in caplog.records)
