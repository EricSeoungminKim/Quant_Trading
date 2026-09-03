"""`universe_filter` — A/B 갈래를 가르는 설정 레벨 유니버스 필터 (2026-09-03).

중심 주장: **필터는 감시 대상만 자르고 진입 규칙은 건드리지 않는다**, 그리고
**모르면(태그 없음) 촉매 갈래는 아무것도 사지 않는다**. 태그 배선이 끊겼을 때
촉매 갈래가 유니버스 전체를 사 버리면 그건 A/B 가 아니라 다른 실험이고, 그
사고는 원장에 영구히 남는다.
"""
from __future__ import annotations

import logging

from quant.trade.strategy import build_strategies, filter_universe

TAGS = {"A": ["EVENT", "FRGN"], "B": ["EVENT"], "C": ["FRGN"], "D": []}


def test_require_all_keeps_only_symbols_carrying_every_tag():
    kept, dropped = filter_universe(["A", "B", "C", "D"], {"require_all": ["EVENT", "FRGN"]}, TAGS)
    assert kept == ["A"]
    assert dropped == ["B", "C", "D"]


def test_require_any_keeps_symbols_carrying_at_least_one_tag():
    kept, _ = filter_universe(["A", "B", "C", "D"], {"require_any": ["FRGN"]}, TAGS)
    assert kept == ["A", "C"]


def test_exclude_all_drops_only_symbols_carrying_both_tags():
    """`exclude_all: [EVENT, FRGN]` = "둘 **다** 가진 것만 버린다" — 하나만
    가진 종목은 남는다. require_all 의 정확한 여집합이라 두 갈래가 딱 맞물린다."""
    kept, dropped = filter_universe(["A", "B", "C", "D"], {"exclude_all": ["EVENT", "FRGN"]}, TAGS)
    assert kept == ["B", "C", "D"]
    assert dropped == ["A"]


def test_exclude_any_drops_symbols_carrying_any_tag():
    kept, _ = filter_universe(["A", "B", "C", "D"], {"exclude_any": ["FRGN"]}, TAGS)
    assert kept == ["B", "D"]


def test_require_and_exclude_arms_partition_the_universe():
    """두 갈래의 합집합 = 원래 유니버스, 교집합 = 공집합. A/B 의 전제."""
    symbols = ["A", "B", "C", "D"]
    cat, _ = filter_universe(symbols, {"require_any": ["FRGN"]}, TAGS)
    base, _ = filter_universe(symbols, {"exclude_any": ["FRGN"]}, TAGS)
    assert set(cat) | set(base) == set(symbols)
    assert not (set(cat) & set(base))


def test_market_specific_clause_applies_per_symbol_market():
    """`{KR: {...}, US: {...}}` — 6자리 숫자면 KR, 아니면 US(저장소 전역 규칙)."""
    tags = {"005930": ["FRGN"], "000660": [], "TQQQ": ["EVENT", "TREND"], "SOXL": ["EVENT"]}
    spec = {"KR": {"require_any": ["FRGN"]}, "US": {"require_all": ["EVENT", "TREND"]}}
    kept, _ = filter_universe(list(tags), spec, tags)
    assert kept == ["005930", "TQQQ"]


def test_market_without_a_clause_is_not_filtered():
    """KR 절만 준 시장별 필터가 US 심볼을 조용히 전멸시키지 않는다."""
    tags = {"005930": [], "TQQQ": []}
    kept, _ = filter_universe(list(tags), {"KR": {"require_any": ["FRGN"]}}, tags)
    assert kept == ["TQQQ"]


def test_missing_tags_select_nothing_for_require_and_drop_nothing_for_exclude():
    symbols = ["A", "B"]
    assert filter_universe(symbols, {"require_all": ["EVENT"]}, None)[0] == []
    assert filter_universe(symbols, {"exclude_all": ["EVENT"]}, None)[0] == symbols


def test_held_symbols_always_survive_the_filter():
    kept, dropped = filter_universe(["A", "B"], {"require_all": ["NOPE"]}, TAGS, held_symbols={"B"})
    assert kept == ["B"] and dropped == ["A"]


# ── build_strategies 배선 ────────────────────────────────────────────────────

def _cfg(**over) -> dict:
    base = {
        "class": "scalp_1m", "enabled": True, "symbols": ["A", "B", "C", "D"],
        "params": {"target_weight": 0.5},
    }
    return {"universe": {"us": []}, "strategies": {"s": {**base, **over}}}


def test_build_strategies_applies_the_filter_before_construction():
    (built,) = build_strategies(_cfg(universe_filter={"require_all": ["EVENT", "FRGN"]}), tags_of=TAGS)
    assert built.symbols == ["A"]


def test_build_strategies_without_filter_is_unchanged():
    (built,) = build_strategies(_cfg(), tags_of=TAGS)
    assert built.symbols == ["A", "B", "C", "D"]


def test_build_strategies_keeps_held_symbols_passed_through_cfg():
    """`cfg["_held_symbols"]` 경로(cli._rebuild 가 쓰는 길)도 kwarg 와 동일하게 먹는다."""
    cfg = {**_cfg(universe_filter={"require_all": ["NOPE"]}), "_held_symbols": ["C"]}
    (built,) = build_strategies(cfg, tags_of=TAGS)
    assert built.symbols == ["C"]

    (built2,) = build_strategies(
        _cfg(universe_filter={"require_all": ["NOPE"]}), tags_of=TAGS, held_symbols=["D"])
    assert built2.symbols == ["D"]


def test_missing_tags_warns_once_per_assembly(caplog):
    with caplog.at_level(logging.WARNING, logger="quant.trade.strategy"):
        build_strategies(_cfg(universe_filter={"require_all": ["EVENT"]}), tags_of=None)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "tags_of" in warnings[0].getMessage()


def test_filtered_strategy_keeps_its_market_even_when_the_filter_empties_it():
    """필터가 심볼을 다 버려도 시장 판정은 필터 **전** 심볼로 정해진다 —
    KR 전략이 조용히 US 로 미끄러지면 예산이 환율로 나뉜다(2026-08-11 사고)."""
    cfg = {
        "universe": {"kr": ["005930"]},
        "strategies": {"s": {
            "class": "scalp_1m", "enabled": True, "symbols": ["005930"],
            "params": {}, "universe_filter": {"require_all": ["NOPE"]},
        }},
    }
    (built,) = build_strategies(cfg, tags_of={})
    assert built.symbols == [] and built.market == "KR"
