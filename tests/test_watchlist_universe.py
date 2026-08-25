"""관심종목(watchlist) 유니버스: 파일 파싱, 스냅샷 시맨틱, 조립 opt-in, 세션 롤 재조립.

전부 오프라인 — 네트워크도 실 파일시스템 상태도 건드리지 않는다(tmp_path만 쓴다).

여기서 고정하는 불변식 두 가지:
1. 유니버스는 **신규 진입 후보**를 고르는 장치다. 관심종목에서 빠졌다고 이미 열린
   포지션이 전략의 시야에서 사라지면 안 된다(손절/청산 로직이 통째로 증발한다).
2. `universe.watchlist.enabled: false`면 이 기능은 흔적을 남기지 않는다 — 조립
   결과도 루프 동작도 기존과 100% 동일해야 한다.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from quant.apps.assembly import build_universe, rebuild_strategies
from quant.trade.universe import (
    CompositeUniverse,
    FileWatchlistUniverse,
    StaticUniverse,
)


_DONCHIAN_PARAMS = {
    "interval_minutes": 15, "lookback_bars": 40, "volume_mult": 1.5,
    "stop_fallback_pct": 1.5, "risk_reward": 2.0, "max_concurrent_names": 1,
    "flatten_before_close_minutes": 10,
}


def _write(path, text: str):
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------- 파일 파싱

def test_parses_simple_symbol_list(tmp_path):
    path = _write(tmp_path / "watchlist.yaml", "symbols: [TQQQ, SQQQ, '005930']\n")
    assert FileWatchlistUniverse(path).symbols() == ["TQQQ", "SQQQ", "005930"]


def test_parses_rich_entry_list(tmp_path):
    """텔레그램 브리지가 쓰는 형식 — 메모/추가시각이 붙은 dict 항목."""
    path = _write(tmp_path / "watchlist.yaml", """
symbols:
  - {symbol: TQQQ, note: "돌파 감시", added_at: "2026-08-08T09:00:00+09:00"}
  - symbol: SOXL
    note: 반도체
""")
    assert FileWatchlistUniverse(path).symbols() == ["TQQQ", "SOXL"]


def test_dedupes_and_strips_preserving_order(tmp_path):
    path = _write(tmp_path / "watchlist.yaml", """
symbols:
  - "  TQQQ  "
  - {symbol: TQQQ}
  - {note: "심볼 키가 없는 깨진 항목"}
  - ""
  - SQQQ
""")
    assert FileWatchlistUniverse(path).symbols() == ["TQQQ", "SQQQ"]


def test_missing_file_is_empty_list_not_an_exception(tmp_path):
    u = FileWatchlistUniverse(tmp_path / "does-not-exist.yaml")
    assert u.symbols() == []


def test_broken_file_is_empty_list(tmp_path):
    path = _write(tmp_path / "watchlist.yaml", "symbols: [TQQQ, SQQQ\n  bad: yaml: [\n")
    assert FileWatchlistUniverse(path).symbols() == []


def test_wrong_shape_file_is_empty_list(tmp_path):
    """유효한 YAML이지만 우리가 기대하는 구조가 아닌 경우도 빈 목록."""
    assert FileWatchlistUniverse(_write(tmp_path / "a.yaml", "just a string\n")).symbols() == []
    assert FileWatchlistUniverse(_write(tmp_path / "b.yaml", "symbols: TQQQ\n")).symbols() == []
    assert FileWatchlistUniverse(_write(tmp_path / "c.yaml", "symbols:\n")).symbols() == []


def test_warns_only_once_for_the_same_failure(tmp_path, caplog):
    """세션마다 refresh를 부르는데 같은 경고를 매번 찍으면 로그가 무의미해진다."""
    u = FileWatchlistUniverse(tmp_path / "nope.yaml")
    with caplog.at_level("WARNING", logger="quant.trade.universe"):
        u.refresh()
        u.refresh()
        u.refresh()
    assert len([r for r in caplog.records if "관심종목 파일 없음" in r.getMessage()]) == 1


def test_warns_again_after_the_failure_reason_changes(tmp_path):
    path = tmp_path / "watchlist.yaml"
    u = FileWatchlistUniverse(path)
    u.refresh()  # 없음 → 경고
    _write(path, "symbols: [TQQQ]\n")
    assert u.refresh() == ["TQQQ"]
    path.unlink()
    u.refresh()
    assert u._last_warning is not None, "정상 복구 후 다시 실패하면 경고가 되살아나야 한다"


# ------------------------------------------------------- 스냅샷 시맨틱

def test_intraday_file_change_is_not_reflected_until_refresh(tmp_path):
    path = _write(tmp_path / "watchlist.yaml", "symbols: [TQQQ]\n")
    u = FileWatchlistUniverse(path)
    assert u.symbols() == ["TQQQ"]

    _write(path, "symbols: [TQQQ, SOXL]\n")  # 장중에 폰으로 추가
    assert u.symbols() == ["TQQQ"], "장중에 유니버스가 흔들리면 안 된다"

    assert u.refresh() == ["TQQQ", "SOXL"]  # 다음 세션 롤
    assert u.symbols() == ["TQQQ", "SOXL"]


def test_symbols_returns_a_copy(tmp_path):
    u = FileWatchlistUniverse(_write(tmp_path / "w.yaml", "symbols: [TQQQ]\n"))
    u.symbols().append("XXX")
    assert u.symbols() == ["TQQQ"]


# ------------------------------------------------------- CompositeUniverse 조합

def test_composite_unions_static_basket_and_watchlist(tmp_path):
    path = _write(tmp_path / "w.yaml", "symbols: [SQQQ, SOXL]\n")
    composite = CompositeUniverse([StaticUniverse(["TQQQ", "SQQQ"]), FileWatchlistUniverse(path)])
    assert composite.symbols() == ["TQQQ", "SQQQ", "SOXL"]


def test_composite_refresh_propagates_to_file_source(tmp_path):
    path = _write(tmp_path / "w.yaml", "symbols: [SOXL]\n")
    composite = CompositeUniverse([StaticUniverse(["TQQQ"]), FileWatchlistUniverse(path)])
    assert composite.symbols() == ["TQQQ", "SOXL"]

    _write(path, "symbols: [SOXL, TSLL]\n")
    assert composite.symbols() == ["TQQQ", "SOXL"], "refresh 전에는 그대로"
    assert composite.refresh() == ["TQQQ", "SOXL", "TSLL"]


def test_composite_refresh_tolerates_sources_without_refresh():
    """StaticUniverse/TossRankingUniverse에는 refresh가 없다 — 조용히 건너뛰어야 한다."""
    composite = CompositeUniverse([StaticUniverse(["TQQQ"])])
    assert composite.refresh() == ["TQQQ"]


# ------------------------------------------------------- build_universe (조립)

def _cfg(watchlist_enabled: bool, path=None, **strategy_over) -> dict:
    return {
        "universe": {
            "us": ["TQQQ", "SQQQ"],
            "kr": [],
            "watchlist": {"enabled": watchlist_enabled, **({"path": str(path)} if path else {})},
        },
        "strategies": {
            "donchian": {
                "class": "donchian", "enabled": True, "symbols": ["TQQQ", "SQQQ"],
                "params": dict(_DONCHIAN_PARAMS), **strategy_over,
            },
        },
    }


def test_build_universe_returns_none_when_disabled():
    assert build_universe(_cfg(False)) is None


def test_build_universe_combines_config_basket_and_watchlist(tmp_path):
    path = _write(tmp_path / "w.yaml", "symbols: [SOXL]\n")
    universe = build_universe(_cfg(True, path))
    assert universe is not None
    assert universe.symbols() == ["TQQQ", "SQQQ", "SOXL"]


def test_build_universe_survives_a_missing_watchlist_file(tmp_path):
    """파일이 없어도 설정의 고정 바스켓 덕분에 유니버스가 완전히 비지 않는다."""
    universe = build_universe(_cfg(True, tmp_path / "nope.yaml"))
    assert universe.symbols() == ["TQQQ", "SQQQ"]


# ------------------------------------------------------- rebuild_strategies (opt-in)

def test_disabled_watchlist_is_a_perfect_regression_guard(tmp_path):
    """watchlist.enabled=false → 기존 build_strategies와 동일한 결과."""
    from quant.trade.strategy import build_strategies

    cfg = _cfg(False)
    strategies, markets, active_markets = rebuild_strategies(cfg, build_universe(cfg))
    baseline = build_strategies(cfg)

    assert [s.id for s in strategies] == [s.id for s in baseline]
    assert [list(s.symbols) for s in strategies] == [list(s.symbols) for s in baseline]
    assert active_markets == frozenset({"US"})
    assert markets == {"TQQQ": "US", "SQQQ": "US"}


def test_fixed_pair_strategies_never_receive_watchlist_symbols(tmp_path):
    """opt-in이 없는 전략은 관심종목이 아무리 많아도 심볼이 바뀌지 않는다.

    donchian(TQQQ/SQQQ 역방향 쌍)에 임의 종목을 부으면 에러 없이 다른 전략이 된다.
    """
    path = _write(tmp_path / "w.yaml", "symbols: [SOXL, TSLL]\n")
    cfg = _cfg(True, path)
    strategies, _markets, _active = rebuild_strategies(cfg, build_universe(cfg))
    assert [list(s.symbols) for s in strategies] == [["TQQQ", "SQQQ"]]


def test_no_consumer_is_logged_honestly(tmp_path, caplog):
    path = _write(tmp_path / "w.yaml", "symbols: [SOXL]\n")
    cfg = _cfg(True, path)
    with caplog.at_level("INFO", logger="quant.apps.assembly"):
        rebuild_strategies(cfg, build_universe(cfg))
    assert any("소비하는 전략 없음" in r.getMessage() for r in caplog.records)


def test_opted_in_strategy_receives_universe_symbols(tmp_path):
    """`universe: watchlist`를 명시한 전략만 심볼을 갈아끼운다.

    (여기서 donchian을 쓰는 것은 조립 배선을 검증하기 위한 것이지 donchian을
    다종목으로 돌리라는 뜻이 아니다 — 실제 소비자는 임의 종목을 받도록 설계된
    전략이어야 한다.)
    """
    path = _write(tmp_path / "w.yaml", "symbols: [SOXL]\n")
    cfg = _cfg(True, path, universe="watchlist")
    strategies, _markets, active_markets = rebuild_strategies(cfg, build_universe(cfg))
    assert list(strategies[0].symbols) == ["TQQQ", "SQQQ", "SOXL"]
    assert active_markets == frozenset({"US"})


def test_open_position_survives_removal_from_the_watchlist(tmp_path):
    """관심종목에서 빠져도 **열린 포지션이 있는 종목**은 전략 심볼에 남는다.

    유니버스는 신규 진입 후보를 고르는 장치다. 여기가 깨지면 관심종목을 지우는
    순간 그 포지션의 손절/청산 로직이 사라져 포지션이 영원히 방치된다.
    """
    path = _write(tmp_path / "w.yaml", "symbols: [SOXL]\n")
    cfg = {
        "universe": {"us": [], "kr": [], "watchlist": {"enabled": True, "path": str(path)}},
        "strategies": {
            "scan": {
                "class": "donchian", "enabled": True, "universe": "watchlist",
                "symbols": [], "params": dict(_DONCHIAN_PARAMS),
            },
        },
    }
    strategies, _markets, _active = rebuild_strategies(
        cfg, build_universe(cfg), held_symbols=["TSLL"],
    )
    assert list(strategies[0].symbols) == ["SOXL", "TSLL"]


def test_held_symbol_is_not_duplicated_when_also_in_the_watchlist(tmp_path):
    path = _write(tmp_path / "w.yaml", "symbols: [SOXL]\n")
    cfg = _cfg(True, path, universe="watchlist")
    strategies, _m, _a = rebuild_strategies(cfg, build_universe(cfg), held_symbols=["SOXL"])
    assert list(strategies[0].symbols) == ["TQQQ", "SQQQ", "SOXL"]


def test_empty_universe_keeps_the_configured_symbols(tmp_path):
    """유니버스가 비었다고 전략을 심볼 0개로 만들지 않는다(조용한 거래 정지 방지)."""
    cfg = {
        "universe": {"us": [], "kr": [], "watchlist": {"enabled": True, "path": str(tmp_path / "nope.yaml")}},
        "strategies": {
            "scan": {
                "class": "donchian", "enabled": True, "universe": "watchlist",
                "symbols": ["TQQQ"], "params": dict(_DONCHIAN_PARAMS),
            },
        },
    }
    strategies, _m, _a = rebuild_strategies(cfg, build_universe(cfg))
    assert list(strategies[0].symbols) == ["TQQQ"]


# ------------------------------------------------------- 세션 롤 (run_paper_loop)

from tests.test_loop_resilience import (  # noqa: E402  (fakes 재사용)
    FakeBroker,
    FakeClock,
    FakeDataFeed,
    FakeRisk,
    FakeSink,
    FakeStrategy,
    _drive_n_cycles,
    make_settings,
)

from quant.core.ports import Context  # noqa: E402
from quant.core.models import Position, Signal, SignalAction  # noqa: E402


class _FakeUniverse:
    def __init__(self, symbols: list[str]):
        self._symbols = list(symbols)
        self.refresh_calls = 0

    def set(self, symbols: list[str]) -> None:
        self._symbols = list(symbols)

    def refresh(self) -> list[str]:
        self.refresh_calls += 1
        return list(self._symbols)

    def symbols(self) -> list[str]:
        return list(self._symbols)


def _loop_kwargs(**over):
    base = {
        "strategies": [FakeStrategy()],
        "ctx": Context(clock=FakeClock(), data=FakeDataFeed(), broker=FakeBroker()),
        "risk": FakeRisk(),
        "sinks": FakeSink(),
        "notifier": None,
    }
    base.update(over)
    return base


def test_universe_refreshes_once_per_kst_day(tmp_path):
    universe = _FakeUniverse(["TQQQ"])
    asyncio.run(_drive_n_cycles(
        5, settings=make_settings(tmp_path), universe=universe, **_loop_kwargs(),
    ))
    assert universe.refresh_calls == 1, "같은 거래일 안에서는 1회만 다시 읽어야 한다"


def test_universe_refreshes_again_after_the_day_rolls(tmp_path):
    universe = _FakeUniverse(["TQQQ"])
    days = iter(["2026-08-08", "2026-08-08", "2026-08-09", "2026-08-09"])
    with patch("quant.trade.loop._universe_roll_bucket", lambda now=None: next(days)):
        asyncio.run(_drive_n_cycles(
            4, settings=make_settings(tmp_path), universe=universe, **_loop_kwargs(),
        ))
    assert universe.refresh_calls == 2


def test_universe_roll_bucket_adds_preopen_boundary_at_0827_kst():
    """08:12 브리핑의 자동 /watch가 그날 KR 동시호가(08:30)부터 반영되려면 자정 외에
    동시호가 직전 경계가 하나 더 있어야 한다 — 08:26과 08:27은 다른 버킷
    (2026-08-17 사용자 세션 표에 맞춰 08:57에서 전진)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from quant.trade.loop import _universe_roll_bucket

    kst = ZoneInfo("Asia/Seoul")
    before = _universe_roll_bucket(datetime(2026, 8, 10, 8, 26, tzinfo=kst))
    after = _universe_roll_bucket(datetime(2026, 8, 10, 8, 27, tzinfo=kst))
    midnight = _universe_roll_bucket(datetime(2026, 8, 10, 0, 0, tzinfo=kst))
    next_day = _universe_roll_bucket(datetime(2026, 8, 11, 0, 0, tzinfo=kst))

    assert before == midnight, "자정~08:26은 같은 버킷(그날 첫 리로드 1회뿐)"
    assert after != before, "08:27에 두 번째 리로드 경계가 지나야 한다"
    assert next_day != after, "다음날 자정에 다시 리로드"
    # KR 오전 세션 내내(~13:59)는 08:27 버킷이 유지된다 — 장중 유니버스 스냅샷 안정성.
    assert _universe_roll_bucket(datetime(2026, 8, 10, 13, 59, tzinfo=kst)) == after
    # 14:00: 오후 마감 포지션 리포트(EVENT_SCALP) 흡수 경계(서브프로젝트 T Task 2) —
    # 08:27 버킷과는 달라야 하고, KR 세션 마감(15:30)과 그 뒤 22:10 전까지 유지된다.
    afternoon = _universe_roll_bucket(datetime(2026, 8, 10, 14, 53, tzinfo=kst))
    assert afternoon != after, "14:53에 세 번째 리로드 경계가 지나야 한다(2026-08-25 종가배팅 체인)"
    assert _universe_roll_bucket(datetime(2026, 8, 10, 15, 30, tzinfo=kst)) == afternoon
    assert _universe_roll_bucket(datetime(2026, 8, 10, 22, 9, tzinfo=kst)) == afternoon
    # 2026-08-11: US 개장 직전(22:10) 경계 추가 — 21:50 US 자동 편입분이 그날 US
    # 세션 개장부터 반영되게 한다. 상세는 test_loop_resilience의 전용 테스트 참고.
    late_evening = _universe_roll_bucket(datetime(2026, 8, 10, 23, 59, tzinfo=kst))
    assert late_evening != afternoon, "22:10에 US 개장 직전 리로드 경계가 지나야 한다"
    assert late_evening != next_day, "자정 전이므로 다음날 버킷과도 달라야 한다"


def test_no_universe_means_no_new_code_path(tmp_path):
    """universe=None이면 재조립 콜백은 절대 불리지 않는다(기존 경로 100% 보존)."""
    calls = []
    asyncio.run(_drive_n_cycles(
        3, settings=make_settings(tmp_path),
        rebuild_strategies=lambda: calls.append(1) or [FakeStrategy()],
        **_loop_kwargs(),
    ))
    assert calls == []


def test_rebuild_failure_keeps_the_existing_strategies(tmp_path):
    """재조립이 터져도 거래는 계속돼야 한다 — 기존 전략으로 사이클을 계속 돈다."""
    original = FakeStrategy(id="orig")

    def boom():
        raise RuntimeError("설정이 깨졌다")

    asyncio.run(_drive_n_cycles(
        3, settings=make_settings(tmp_path), universe=_FakeUniverse(["TQQQ"]),
        rebuild_strategies=boom, **_loop_kwargs(strategies=[original]),
    ))
    assert original.calls == 3, "재조립 실패 후에도 기존 전략이 계속 돌아야 한다"


def test_empty_rebuild_result_keeps_the_existing_strategies(tmp_path):
    original = FakeStrategy(id="orig")
    asyncio.run(_drive_n_cycles(
        3, settings=make_settings(tmp_path), universe=_FakeUniverse([]),
        rebuild_strategies=lambda: [], **_loop_kwargs(strategies=[original]),
    ))
    assert original.calls == 3


def test_strategy_instance_is_reused_when_its_symbols_are_unchanged(tmp_path):
    """KST 자정은 US 세션(22:30~05:00 KST) 한가운데다. 심볼이 그대로인 전략까지
    새로 만들면 orb의 `_entered_today` 같은 세션 상태가 장중에 리셋되어 하루 1회
    진입 제한이 풀린다 — 실제로 심볼이 바뀐 전략만 교체해야 한다."""
    original = FakeStrategy(id="pair")
    replacement = FakeStrategy(id="pair")  # 같은 id, 같은 심볼 → 교체되면 안 된다
    asyncio.run(_drive_n_cycles(
        3, settings=make_settings(tmp_path), universe=_FakeUniverse(["TQQQ"]),
        rebuild_strategies=lambda: [replacement], **_loop_kwargs(strategies=[original]),
    ))
    assert original.calls == 3 and replacement.calls == 0


def test_symbol_dropped_from_the_watchlist_can_still_be_exited(tmp_path):
    """관심종목에서 빠진 종목의 열린 포지션은 계속 관리된다 — 재조립 후에도
    그 종목의 청산 시그널이 주문까지 도달해야 한다."""
    positions = {"TSLL": Position(symbol="TSLL", qty=10.0, avg_cost=100.0)}
    broker = FakeBroker(positions=positions)
    exit_signal = Signal(
        strategy_id="scan", symbol="TSLL", action=SignalAction.EXIT_LONG,
        target_weight=0.0, exit_fraction=1.0, reason="손절",
    )
    # 재조립된 전략은 유니버스에서 빠진 TSLL을 여전히 보유 종목으로 들고 있다.
    rebuilt = FakeStrategy(id="scan", signals_fn=lambda: [exit_signal])
    rebuilt.symbols = ["SOXL", "TSLL"]
    before = FakeStrategy(id="scan")
    before.symbols = ["TSLL"]

    universe = _FakeUniverse(["SOXL"])  # TSLL은 관심종목에서 빠졌다
    asyncio.run(_drive_n_cycles(
        2, settings=make_settings(tmp_path), universe=universe,
        rebuild_strategies=lambda: [rebuilt],
        **_loop_kwargs(strategies=[before], ctx=Context(
            clock=FakeClock(), data=FakeDataFeed(), broker=broker,
        )),
    ))
    assert rebuilt.calls == 2, "재조립된 전략이 다음 사이클부터 돌아야 한다"
    # 두 번째 사이클에도 같은 청산 시그널이 나오지만 이미 수량이 0이라 리스크에서
    # 막힌다 — 주문은 1건이 정상이다.
    assert [o.symbol for o in broker.orders] == ["TSLL"]
    assert positions["TSLL"].qty == 0.0, "빠진 종목의 포지션도 청산까지 관리돼야 한다"


@pytest.mark.parametrize("bad", [RuntimeError("디스크 오류")])
def test_universe_refresh_failure_does_not_stop_the_loop(tmp_path, bad):
    class _BoomUniverse:
        def refresh(self):
            raise bad

        def symbols(self):
            return []

    strategy = FakeStrategy(id="orig")
    asyncio.run(_drive_n_cycles(
        3, settings=make_settings(tmp_path), universe=_BoomUniverse(),
        rebuild_strategies=lambda: [], **_loop_kwargs(strategies=[strategy]),
    ))
    assert strategy.calls == 3


def test_kr_watchlist_symbol_gets_kr_market_not_us(tmp_path):
    """관심종목으로 들어온 KR 6자리 코드는 시장 매핑에서 KR로 추론돼야 한다.

    settings의 us/kr 목록에 없는 심볼은 하위 계층의 `.get(sym, "US")` 폴백으로
    US가 되는데, KR 종목이 US로 매핑되면 KRW 표시가격에 환율(1500)을 곱해
    **명목가치가 1,500배 부풀고** 세션 판정도 미국 시간으로 어긋난다 — 사이징이
    통째로 무너지는 돈 결함이다. KR 심볼은 6자리 숫자로 구조 식별이 가능하다
    (docs/api/toss/QUICKREF, brokers/toss/broker.py 동일 정규식).
    """
    path = _write(tmp_path / "w.yaml", "symbols: [AAPL, '005930']\n")
    cfg = _cfg(True, path)
    cfg["strategies"]["orb_scan"] = {
        "class": "orb_scan", "enabled": True, "universe": "watchlist", "symbols": [],
        "params": {"bar_interval_minutes": 5, "opening_range_minutes": 5},
    }

    _strategies, markets, active_markets = rebuild_strategies(cfg, build_universe(cfg))

    assert markets["005930"] == "KR"
    assert markets["AAPL"] == "US"
    assert active_markets == frozenset({"US", "KR"})


# ========== 표시명 갱신 (2026-08-12: 부팅 후 편입 종목이 코드로만 표시되던 문제)

def test_watchlist_universe_exposes_names(tmp_path):
    import yaml as _yaml

    from quant.trade.universe import FileWatchlistUniverse

    p = tmp_path / "wl.yaml"
    p.write_text(_yaml.safe_dump({"symbols": [
        {"symbol": "192820", "name": "코스맥스"},
        {"symbol": "TQQQ"},          # 이름 없음
        "SPY",                        # 문자열 형식
    ]}, allow_unicode=True), encoding="utf-8")

    u = FileWatchlistUniverse(p)
    assert u.symbols() == ["192820", "TQQQ", "SPY"]
    assert u.names() == {"192820": "코스맥스"}, "이름 없는 항목은 빠져야 한다"


def test_roll_universe_refreshes_display_names(tmp_path, monkeypatch):
    """실측 배경: 엔진 02:50 부팅 → 08:41 자동 편입된 192820이 세션 롤 후에도
    '192820'으로만 표시됐다. 이름은 브리지가 파일에 이미 적어뒀는데 엔진이
    부팅 스냅샷만 봤기 때문 — market_of 스테일과 같은 부류."""
    import yaml as _yaml

    from quant.trade import loop as loop_mod
    from quant.trade.universe import CompositeUniverse, FileWatchlistUniverse, StaticUniverse

    p = tmp_path / "wl.yaml"
    p.write_text(_yaml.safe_dump({"symbols": [
        {"symbol": "192820", "name": "코스맥스"},
    ]}, allow_unicode=True), encoding="utf-8")

    monkeypatch.setattr(loop_mod, "_SYMBOL_NAMES", {}, raising=False)
    universe = CompositeUniverse([StaticUniverse(["TQQQ"]), FileWatchlistUniverse(p)])

    assert loop_mod._display("192820") == "192820", "갱신 전에는 코드만"
    loop_mod._roll_universe(universe, None, [])
    assert loop_mod._display("192820") == "코스맥스 (192820)", "롤 후에는 이름이 붙는다"


def test_name_refresh_survives_a_source_without_names():
    """이름을 제공하지 않는 소스가 섞여도 갱신이 죽지 않는다."""
    from quant.trade import loop as loop_mod
    from quant.trade.universe import CompositeUniverse, StaticUniverse

    universe = CompositeUniverse([StaticUniverse(["TQQQ", "SQQQ"])])
    assert loop_mod._refresh_symbol_names(universe) == 0


# ========== 태그 영속화 (2026-08-12: news_momentum이 EVENT 태그를 읽는다)

def test_watchlist_universe_exposes_tags(tmp_path):
    """tags 필드가 있는 항목만 tags()에 나타난다 — 기존 항목(필드 없음)은 하위호환."""
    import yaml as _yaml

    from quant.trade.universe import FileWatchlistUniverse

    p = tmp_path / "wl.yaml"
    p.write_text(_yaml.safe_dump({"symbols": [
        {"symbol": "NVDA", "tags": ["EVENT"]},
        {"symbol": "TQQQ"},               # 태그 없음
        {"symbol": "005930", "tags": []},  # 빈 태그 리스트도 매핑에서 빠져야 한다
        "SPY",                             # 문자열 형식
    ]}, allow_unicode=True), encoding="utf-8")

    u = FileWatchlistUniverse(p)
    assert u.symbols() == ["NVDA", "TQQQ", "005930", "SPY"]
    assert u.tags() == {"NVDA": ["EVENT"]}


def test_composite_universe_merges_tags_from_sources_with_tags():
    """StaticUniverse/TossRankingUniverse는 tags()가 없다 — 조용히 건너뛴다."""
    p_tags = {"NVDA": ["EVENT"]}

    class _TaggedSource:
        def symbols(self):
            return ["NVDA"]

        def tags(self):
            return dict(p_tags)

    composite = CompositeUniverse([StaticUniverse(["TQQQ"]), _TaggedSource()])
    assert composite.tags() == {"NVDA": ["EVENT"]}


def test_rebuild_strategies_wires_tags_of_from_universe_into_news_momentum(tmp_path):
    """assembly.rebuild_strategies가 명시적으로 넘기지 않아도 universe.tags()를
    읽어 news_momentum에 주입한다(leverage_of와 달리 매번 새로 읽는다 — 근거는
    rebuild_strategies의 docstring)."""
    path = _write(tmp_path / "w.yaml", "symbols: [{symbol: NVDA, tags: [EVENT]}, TQQQ]\n")
    cfg = {
        "universe": {"us": [], "kr": [], "watchlist": {"enabled": True, "path": str(path)}},
        "strategies": {
            "news_momentum": {
                "class": "news_momentum", "enabled": True, "universe": "watchlist",
                "symbols": [], "params": {},
            },
        },
    }
    strategies, _m, _a = rebuild_strategies(cfg, build_universe(cfg))
    [strat] = strategies
    assert strat.tags_of == {"NVDA": ["EVENT"]}
    assert set(strat.symbols) == {"NVDA", "TQQQ"}


def test_watchlist_tags_snapshot_semantics_match_names(tmp_path):
    """tags()도 names()와 같은 세션 롤 스냅샷 시맨틱 — refresh() 전에는 갱신 안 됨."""
    import yaml as _yaml

    from quant.trade.universe import FileWatchlistUniverse

    p = tmp_path / "wl.yaml"
    p.write_text(_yaml.safe_dump({"symbols": [{"symbol": "NVDA", "tags": ["EVENT"]}]},
                                  allow_unicode=True), encoding="utf-8")
    u = FileWatchlistUniverse(p)
    assert u.tags() == {"NVDA": ["EVENT"]}

    p.write_text(_yaml.safe_dump({"symbols": [{"symbol": "NVDA", "tags": ["TREND"]}]},
                                  allow_unicode=True), encoding="utf-8")
    assert u.tags() == {"NVDA": ["EVENT"]}, "refresh 전에는 그대로"
    u.refresh()
    assert u.tags() == {"NVDA": ["TREND"]}


# ── 유니버스 롤이 전략의 당일 상태를 지우는 결함 (2026-08-21) ──────────────
# 유니버스는 KST 자정 + 08:27(KR 동시호가 전) + 22:10(US 개장 전)에 리로드되고
# /watch 추가 때마다도 돈다. `_roll_universe`는 심볼이 같으면 기존 인스턴스를
# 유지하지만, 관심종목이 **하나라도** 바뀌면 watchlist 소비 전략 전부가 새
# 인스턴스로 교체돼 인메모리 당일 상태가 통째로 리셋된다 — frgn_accumulate 의
# `_evaluated_date`(오늘 이미 평가함)가 지워지면 **같은 날 같은 종목을 다시
# 산다**. scalp_1m 의 `_pattern_a_used`, orb 계열의 `_entries_today` 도 같은
# 부류다.
#
# 수정: 교체는 하되(핫 리로드된 params·tags_of 는 새 인스턴스가 옳다) 이전
# 인스턴스의 **비어 있지 않은 상태 컨테이너를, 새 인스턴스에서 아직 비어 있는
# 경우에만** 이월한다. "빈 컨테이너만 채운다"는 조건이 안전핀이다 — 새 인스턴스가
# 생성자에서 뭔가를 채웠다면 그건 설계된 초기 상태라 덮지 않는다.

class _RollUniverse:
    def __init__(self, symbols):
        self._symbols = list(symbols)

    def refresh(self):
        pass

    def symbols(self):
        return list(self._symbols)


def _frgn(symbols):
    from quant.trade.strategy.frgn_accumulate import FrgnAccumulateStrategy

    return FrgnAccumulateStrategy(
        symbols=symbols, params={"buy_qty": 1}, id="frgn_accumulate",
        tags_of={s: ["FRGN"] for s in symbols},
    )


def test_roll_universe_carries_day_state_into_replacement_instance():
    from datetime import date as _date

    from quant.trade import loop as loop_mod

    old = _frgn(["005930"])
    old._evaluated_date["KR"] = _date(2026, 8, 21)
    old._exit_streak["005930"] = 2

    fresh = _frgn(["005930", "042700"])  # 관심종목이 하나 늘었다 → 심볼 불일치 → 교체
    merged = loop_mod._roll_universe(_RollUniverse(["005930", "042700"]), lambda: [fresh], [old])

    assert merged == [fresh], "심볼이 바뀌었으니 새 인스턴스(핫 리로드된 설정)가 이긴다"
    assert fresh._evaluated_date == {"KR": _date(2026, 8, 21)}, "오늘 이미 평가한 기록이 이월돼야 같은 날 재매수를 막는다"
    assert fresh._exit_streak == {"005930": 2}, "이탈 연속 카운터도 이월"


def test_roll_universe_does_not_clobber_nonempty_fresh_state():
    """새 인스턴스가 생성자에서 이미 채운 컨테이너는 설계된 초기 상태 — 덮지 않는다."""
    from quant.trade import loop as loop_mod

    old = _frgn(["005930"])
    old.last_reject["005930"] = "낡은 사유"

    fresh = _frgn(["005930", "042700"])
    fresh.last_reject["042700"] = "새 사유"
    loop_mod._roll_universe(_RollUniverse(["005930", "042700"]), lambda: [fresh], [old])

    assert fresh.last_reject == {"042700": "새 사유"}


def test_roll_universe_same_symbols_still_keeps_old_instance():
    """기존 동작 보존: 심볼이 그대로면 옛 인스턴스 유지(이월 로직 미개입)."""
    from quant.trade import loop as loop_mod

    old = _frgn(["005930"])
    fresh = _frgn(["005930"])
    merged = loop_mod._roll_universe(_RollUniverse(["005930"]), lambda: [fresh], [old])
    assert merged == [old]
