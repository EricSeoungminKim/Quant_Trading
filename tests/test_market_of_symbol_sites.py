"""19곳에 흩어져 있던 KR/US 판정(`isdigit() and len() == 6`)을
`quant.core.models.market_of_symbol`에 위임하도록 고친 뒤의 회귀 테스트.

이 판정 규칙이 저장소 곳곳에 따로 구현돼 있다가 하나가 어긋나 2026-08-11
058610을 US로 잘못 분류해 0.0015주를 매수한 사고로 이어졌다(quant/core/models.py
market_of_symbol docstring 참고). 위임 이후에도 KR 6자리·US 티커·KR ETF류
6자리·이상 입력(빈 문자열/5자리/7자리/점 포함 티커)에서 결과가 흔들리지 않는지,
그리고 각 사이트의 None/비-str 가드가 그대로인지 이 파일이 고정한다.
"""
from __future__ import annotations

import pytest

from quant.analyze import watch_scorer
from quant.apps.assembly import build_universe, rebuild_strategies
from quant.control import daily_wrap, ledger, tca, warehouse
from quant.trade import loop
from quant.trade.strategy import llm_trader

KR_DIGIT = "005930"  # 삼성전자
US_TICKER = "AAPL"
KR_ETF = "069500"  # KODEX200 — ETF도 일반 종목과 같은 6자리 규칙
EMPTY = ""
FIVE_DIGIT = "12345"
SEVEN_DIGIT = "1234567"
DOTTED_US = "AAPL.US"

# (심볼, 기대 시장) — market_of_symbol의 정의 그대로: 6자리 전부 숫자만 KR.
_STR_CASES: list[tuple[str, str]] = [
    (KR_DIGIT, "KR"),
    (US_TICKER, "US"),
    (KR_ETF, "KR"),
    (EMPTY, "US"),
    (FIVE_DIGIT, "US"),
    (SEVEN_DIGIT, "US"),
    (DOTTED_US, "US"),
]


@pytest.mark.parametrize("symbol,expected", _STR_CASES)
def test_ledger_market_of(symbol, expected):
    assert ledger._market_of(symbol) == expected


@pytest.mark.parametrize("symbol,expected", _STR_CASES)
def test_tca_market_of(symbol, expected):
    assert tca._market_of(symbol) == expected


@pytest.mark.parametrize("symbol,expected", _STR_CASES)
def test_daily_wrap_market_of(symbol, expected):
    assert daily_wrap._market_of(symbol) == expected


@pytest.mark.parametrize("symbol,expected", _STR_CASES)
def test_watch_scorer_is_kr_symbol(symbol, expected):
    assert watch_scorer._is_kr_symbol(symbol) == (expected == "KR")


@pytest.mark.parametrize("symbol,expected", _STR_CASES)
def test_loop_is_kr(symbol, expected):
    assert loop._is_kr(symbol) == (expected == "KR")


@pytest.mark.parametrize("symbol,expected", _STR_CASES)
def test_llm_trader_is_kr_symbol(symbol, expected):
    assert llm_trader._is_kr_symbol(symbol) == (expected == "KR")


def test_llm_trader_is_kr_symbol_tolerates_non_str():
    """시그니처가 `symbol: object`다 — isinstance 가드가 None/비-str에서도
    예외 없이 False를 돌려줘야 한다(위임 전과 동일해야 하는 지점)."""
    assert llm_trader._is_kr_symbol(None) is False
    assert llm_trader._is_kr_symbol(12345) is False


# ------------------------------------------------------------- warehouse.trade_row


@pytest.mark.parametrize("symbol,expected", [c for c in _STR_CASES if c[0]])
def test_warehouse_trade_row_infers_market_when_missing(symbol, expected):
    rec = {"ts": "2026-09-01T00:00:00+00:00", "symbol": symbol, "side": "buy"}
    row = warehouse.trade_row(rec)
    assert row is not None
    assert row[warehouse.TRADE_COLS.index("market")] == expected


def test_warehouse_trade_row_empty_symbol_yields_none():
    """빈 심볼은 market 추론 이전에 이미 None을 반환한다(위임과 무관한 기존 가드)."""
    rec = {"ts": "2026-09-01T00:00:00+00:00", "symbol": "", "side": "buy"}
    assert warehouse.trade_row(rec) is None


def test_warehouse_trade_row_keeps_explicit_market_over_inference():
    """market 필드가 이미 유효하면(정상 원장) 심볼 추론으로 덮어쓰지 않는다 —
    이 사이트는 market이 없거나 KR/US 밖일 때만 market_of_symbol로 폴백한다."""
    rec = {
        "ts": "2026-09-01T00:00:00+00:00", "symbol": US_TICKER, "side": "buy",
        "market": "KR",  # 심볼과 모순되는 값이라도 명시값이 우선
    }
    row = warehouse.trade_row(rec)
    assert row[warehouse.TRADE_COLS.index("market")] == "KR"


# ------------------------------------------------------------- assembly.py: markets 매핑

_DONCHIAN_PARAMS = {
    "interval_minutes": 15, "lookback_bars": 40, "volume_mult": 1.5,
    "stop_fallback_pct": 1.5, "risk_reward": 2.0, "max_concurrent_names": 1,
    "flatten_before_close_minutes": 10,
}


def test_assembly_rebuild_strategies_infers_market_for_unmapped_symbols(tmp_path):
    """관심종목으로 새로 들어와 universe.us/kr 목록(설정 기반 매핑)에 없는 심볼도
    markets 딕셔너리에 올바른 시장으로 채워져야 한다 — assembly.py의
    `markets[sym] = market_of_symbol(sym)` 대입부."""
    watchlist_path = tmp_path / "w.yaml"
    watchlist_path.write_text(
        f"symbols: ['{KR_DIGIT}', '{US_TICKER}']\n", encoding="utf-8"
    )
    cfg = {
        "universe": {"us": [], "kr": [], "watchlist": {"enabled": True, "path": str(watchlist_path)}},
        "strategies": {
            "scan": {
                "class": "donchian", "enabled": True, "universe": "watchlist",
                "symbols": [], "params": dict(_DONCHIAN_PARAMS),
            },
        },
    }
    universe = build_universe(cfg)
    _strategies, markets, _active = rebuild_strategies(cfg, universe)

    assert markets[KR_DIGIT] == "KR"
    assert markets[US_TICKER] == "US"
