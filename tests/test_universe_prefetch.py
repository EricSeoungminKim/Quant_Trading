"""유니버스 롤 직후 신규 편입 심볼 프리페치(2026-09-06 안정성 감사 P1-2b) —
`quant/trade/loop.py`의 `_roll_universe`가 `market_data.prefetch(...)`를
새로 생긴 심볼에만, 시간 예산 안에서 호출하는지 검증한다.

배경: EC2 5일 로그 실측에서 유니버스 롤(08:27 KR 경계 등) 직후 첫 사이클이
신규 편입 심볼 수십 개의 캐시 미스가 몰려 최대 30초까지 늘어졌다. 이 프리페치는
그 롤 경계에서 미리 `MarketDataService.history()`를 불러 캐시를 데워 둔다."""
from __future__ import annotations

from quant.trade.loop import _PREFETCH_DEFAULT_NEEDS, _roll_universe


class _FakeUniverse:
    def __init__(self, symbols: list[str]):
        self._symbols = list(symbols)

    def refresh(self) -> None:
        pass

    def symbols(self) -> list[str]:
        return list(self._symbols)


class _FakeStrategy:
    def __init__(self, id: str, symbols: list[str]):
        self.id = id
        self.symbols = list(symbols)


class _FakeMarketData:
    def __init__(self):
        self.calls: list[tuple[tuple[str, ...], tuple, float | None]] = []

    def prefetch(self, symbols, needs, time_budget_seconds=None) -> int:
        self.calls.append((tuple(symbols), tuple(needs), time_budget_seconds))
        return len(symbols) * len(needs)


def test_prefetch_called_with_only_newly_added_symbols():
    """기존 전략에 없던 심볼만 프리페치 대상이다 — 이미 캐시가 데워져 있을
    기존 심볼까지 다시 프리페치하면 낭비다."""
    market_data = _FakeMarketData()
    before = [_FakeStrategy("s1", ["AAA", "BBB"])]

    def rebuild():
        return [_FakeStrategy("s1", ["AAA", "BBB", "CCC"])]  # CCC만 신규

    _roll_universe(
        _FakeUniverse(["AAA", "BBB", "CCC"]), rebuild, before,
        market_data=market_data,
    )

    assert len(market_data.calls) == 1
    symbols, needs, budget = market_data.calls[0]
    assert symbols == ("CCC",)
    assert needs == _PREFETCH_DEFAULT_NEEDS


def test_prefetch_not_called_when_no_symbols_are_new():
    """심볼이 그대로면(전략 재사용) 프리페치도 호출되지 않는다 — 데울 게 없다."""
    market_data = _FakeMarketData()
    before = [_FakeStrategy("s1", ["AAA", "BBB"])]

    def rebuild():
        return [_FakeStrategy("s1", ["AAA", "BBB"])]

    _roll_universe(
        _FakeUniverse(["AAA", "BBB"]), rebuild, before,
        market_data=market_data,
    )

    assert market_data.calls == []


def test_prefetch_covers_brand_new_strategy_all_symbols():
    """이전에 없던(id가 새로운) 전략은 그 심볼 전부가 신규 취급된다."""
    market_data = _FakeMarketData()

    def rebuild():
        return [_FakeStrategy("s2", ["XXX", "YYY"])]  # s2는 이전에 없던 전략

    _roll_universe(
        _FakeUniverse(["XXX", "YYY"]), rebuild, [],
        market_data=market_data,
    )

    assert len(market_data.calls) == 1
    symbols, _needs, _budget = market_data.calls[0]
    assert set(symbols) == {"XXX", "YYY"}


def test_prefetch_time_budget_is_passed_through():
    market_data = _FakeMarketData()

    def rebuild():
        return [_FakeStrategy("s1", ["AAA"])]

    _roll_universe(
        _FakeUniverse(["AAA"]), rebuild, [],
        market_data=market_data, prefetch_time_budget_seconds=1.5,
    )

    assert market_data.calls[0][2] == 1.5


def test_prefetch_failure_does_not_break_the_roll():
    """프리페치가 예외를 던져도 유니버스 롤 자체(전략 재조립 결과)는 정상
    반환돼야 한다 — 프리페치는 최선 노력이지 필수 경로가 아니다."""
    class _BoomMarketData:
        def prefetch(self, symbols, needs, time_budget_seconds=None):
            raise RuntimeError("network boom")

    def rebuild():
        return [_FakeStrategy("s1", ["AAA"])]

    result = _roll_universe(
        _FakeUniverse(["AAA"]), rebuild, [],
        market_data=_BoomMarketData(),
    )

    assert [s.id for s in result] == ["s1"]


def test_market_data_without_prefetch_method_is_skipped_silently():
    """market_data가 prefetch()를 노출하지 않아도(구형 더블/미지원) 예외 없이
    넘어간다 — duck-typing으로 조용히 생략(다른 market_data 훅과 같은 원칙)."""
    class _NoPrefetch:
        pass

    def rebuild():
        return [_FakeStrategy("s1", ["AAA"])]

    result = _roll_universe(
        _FakeUniverse(["AAA"]), rebuild, [],
        market_data=_NoPrefetch(),
    )

    assert [s.id for s in result] == ["s1"]


def test_no_market_data_is_skipped_silently():
    def rebuild():
        return [_FakeStrategy("s1", ["AAA"])]

    result = _roll_universe(_FakeUniverse(["AAA"]), rebuild, [], market_data=None)

    assert [s.id for s in result] == ["s1"]
