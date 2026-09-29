"""A strategy's explicit market scope survives watchlist/session rebuilds."""
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from quant.apps.assembly import rebuild_strategies
from quant.trade.loop import _roll_universe
from quant.trade.strategy import build_strategies


def _config():
    return {
        "universe": {},
        "strategies": {
            "vol_breakout_cat": {
                "enabled": True, "class": "vol_breakout", "universe": "watchlist",
                "symbols": ["TQQQ", "005930"], "params": {},
                "capital_fraction": {"KR": 0.07, "US": 0.05},
            },
        },
    }


def test_explicit_kr_scope_filters_us_without_changing_capital_history():
    cfg = _config()
    cfg["strategies"]["vol_breakout_cat"]["markets"] = ["KR"]
    before = deepcopy(cfg)
    strategy, = build_strategies(cfg)
    assert strategy.symbols == ["005930"]
    assert strategy.inner.market == "KR"
    assert cfg == before


def test_omitted_market_scope_keeps_existing_universe():
    strategy, = build_strategies(_config())
    assert strategy.symbols == ["TQQQ", "005930"]


@pytest.mark.parametrize("scope", [None, [], "KR", ["JP"], ["kr"], ["KR", None], [1], {}])
def test_invalid_explicit_scope_rejects_configuration(scope):
    cfg = _config()
    cfg["strategies"]["vol_breakout_cat"]["markets"] = scope
    with pytest.raises(ValueError, match="markets"):
        build_strategies(cfg)


def test_held_us_symbol_cannot_bypass_kr_scope_and_empty_scope_stays_kr():
    cfg = _config()
    block = cfg["strategies"]["vol_breakout_cat"]
    block.update(markets=["KR"], symbols=["TQQQ"])
    strategy, = build_strategies(cfg, held_symbols=["TQQQ"])
    assert strategy.symbols == []
    assert strategy.inner.market == "KR"


class Watchlist:
    def __init__(self):
        self.names = ["TQQQ", "005930"]

    def symbols(self):
        return self.names

    def refresh(self):
        pass

    def tags(self):
        return {symbol: ["FRGN", "EVENT", "TREND"] for symbol in self.names}


def test_deployed_policy_builds_only_kr_cat_without_erasing_historical_accounts():
    cfg = yaml.safe_load((Path(__file__).parents[1] / "config/settings.yaml").read_text())
    before = deepcopy(cfg)
    strategies, _, active = rebuild_strategies(cfg, Watchlist(), inbox_reader=lambda: [])
    assert [strategy.id for strategy in strategies] == ["vol_breakout_cat"]
    assert strategies[0].symbols == ["005930"]
    assert active == {"KR"}
    # Account participation remains independent of current trading enablement.
    # Zeroing these would delete historical P&L/capital from the epoch report.
    for sid in ("vol_breakout", "vol_breakout_cat"):
        assert cfg["strategies"][sid]["capital_fraction"] == {"KR": 0.07, "US": 0.05}
    assert cfg == before


def test_session_rebuilds_never_restore_us_even_from_held_symbols():
    cfg = _config()
    block = cfg["strategies"]["vol_breakout_cat"]
    block.update(markets=["KR"], universe_filter={"KR": {"require_any": ["FRGN"]}})
    universe = Watchlist()

    def rebuild():
        strategies, _, active = rebuild_strategies(
            {**cfg, "_held_symbols": ["TQQQ"]}, universe, held_symbols=["TQQQ"],
            inbox_reader=lambda: [],
        )
        assert active <= {"KR"}
        return strategies

    strategies = rebuild()
    for names, expected in [
        (["AAPL", "000660"], ["000660"]),
        (["TQQQ"], []),
        (["TQQQ", "005930"], ["005930"]),
        (["TSLA", "005930"], ["005930"]),
    ]:
        universe.names = names
        strategies = _roll_universe(universe, rebuild, strategies)
        assert len(strategies) == 1
        assert strategies[0].symbols == expected
        assert strategies[0].inner.market == "KR"
