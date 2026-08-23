"""유니버스 프로바이더: StaticUniverse, TossRankingUniverse(캐시/폴백), CompositeUniverse(합집합)."""
from __future__ import annotations

from datetime import date

from quant.trade.universe import CompositeUniverse, StaticUniverse, TossRankingUniverse


class _FakeRankingClient:
    def __init__(self, symbols: list[str] | None = None, error: Exception | None = None):
        self._symbols = symbols
        self._error = error
        self.calls = 0

    def rankings(self, type, market_country, duration, count):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return {"rankedAt": "2026-06-10T14:30:00+09:00", "rankings": [
            {"rank": i + 1, "symbol": sym} for i, sym in enumerate(self._symbols or [])
        ]}


# ------------------------------------------------------------- StaticUniverse

def test_static_universe_always_returns_its_list():
    u = StaticUniverse(["TQQQ", "SQQQ"])
    assert u.symbols() == ["TQQQ", "SQQQ"]
    # 반환값을 변형해도 내부 상태에 영향 없어야 한다
    u.symbols().append("XXX")
    assert u.symbols() == ["TQQQ", "SQQQ"]


# ------------------------------------------------------- TossRankingUniverse

def test_ranking_universe_fetches_once_per_day():
    client = _FakeRankingClient(["005930", "000660"])
    u = TossRankingUniverse(client, market="KR", type="MARKET_TRADING_AMOUNT", count=20)

    day = date(2026, 6, 10)
    assert u.symbols(today=day) == ["005930", "000660"]
    assert u.symbols(today=day) == ["005930", "000660"]
    assert client.calls == 1, "같은 거래일 재호출은 캐시를 써야 한다"


def test_ranking_universe_refetches_on_a_new_day():
    client = _FakeRankingClient(["005930"])
    u = TossRankingUniverse(client, market="KR", type="MARKET_TRADING_AMOUNT", count=20)

    u.symbols(today=date(2026, 6, 10))
    u.symbols(today=date(2026, 6, 11))
    assert client.calls == 2


def test_ranking_universe_falls_back_to_previous_days_cache_on_failure():
    client = _FakeRankingClient(["005930"])
    u = TossRankingUniverse(client, market="KR", type="MARKET_TRADING_AMOUNT", count=20)
    u.symbols(today=date(2026, 6, 10))  # 정상 스냅샷으로 캐시 채움

    client._error = RuntimeError("503 maintenance")
    result = u.symbols(today=date(2026, 6, 11))
    assert result == ["005930"], "실패 시 전일 캐시를 반환해야 한다"


def test_ranking_universe_returns_empty_list_when_no_cache_and_fetch_fails():
    client = _FakeRankingClient(error=RuntimeError("403 access_denied"))
    u = TossRankingUniverse(client, market="KR", type="MARKET_TRADING_AMOUNT", count=20)
    assert u.symbols(today=date(2026, 6, 10)) == []


def test_ranking_universe_handles_empty_rankings_response():
    client = _FakeRankingClient([])
    u = TossRankingUniverse(client, market="KR", type="MARKET_TRADING_AMOUNT", count=20)
    assert u.symbols(today=date(2026, 6, 10)) == []


# ----------------------------------------------------------- CompositeUniverse

def test_composite_universe_unions_and_dedupes_preserving_order():
    a = StaticUniverse(["TQQQ", "SQQQ"])
    b = StaticUniverse(["SQQQ", "005930"])
    composite = CompositeUniverse([a, b])
    assert composite.symbols() == ["TQQQ", "SQQQ", "005930"]


def test_composite_universe_with_no_sources_is_empty():
    assert CompositeUniverse([]).symbols() == []
