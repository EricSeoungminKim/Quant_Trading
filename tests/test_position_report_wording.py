"""포지션 리포트 문구가 사실과 어긋나지 않는지.

2026-08-19: 소유자가 텔레그램에서 이걸 발견했다 — 한국장이 마감됐는데 KR 종목
2개(현대해상·DN오토모티브)가 살아 있고, 메시지는 "🎯 목표가 없음 (장 마감까지
보유)"라고 했다. 동작은 정상이었다(`frgn_accumulate` 는 docstring 첫 줄이
"오버나이트 보유가 전략의 본질"이고 EoD 강제청산 레일이 아예 없다). **문구가
틀린 것이었고**, 소유자는 그걸 버그로 의심할 수밖에 없었다.

문구가 상태를 잘못 설명하면 사람이 정상을 장애로, 장애를 정상으로 읽는다.
"""
from __future__ import annotations

import re
from pathlib import Path

from quant.adapters.env import REPO_ROOT
from quant.trade.loop import _OVERNIGHT_STRATEGIES

_STRATEGY_DIR = REPO_ROOT / "quant" / "trade" / "strategy"
# 전략이 아닌 모듈(계약/껍질/레지스트리)은 제외한다.
_NOT_STRATEGIES = {"__init__", "shell"}


def _strategy_modules() -> dict[str, str]:
    out: dict[str, str] = {}
    for f in sorted(_STRATEGY_DIR.glob("*.py")):
        if f.stem in _NOT_STRATEGIES:
            continue
        out[f.stem] = f.read_text(encoding="utf-8")
    return out


def test_overnight_set_matches_strategies_without_eod_flatten():
    """`should_flatten` 을 호출하지 않는 전략 = EoD 청산이 없는 전략 = 오버나이트 보유.

    이 대조가 없으면 목록이 조용히 낡는다 — 새 오버나이트 전략이 들어와도 메시지는
    계속 "장 마감까지 보유"라고 거짓말한다.
    """
    mods = _strategy_modules()
    assert mods, "전략 모듈을 하나도 못 찾았다 — 경로가 바뀌었는지 확인할 것"

    no_flatten = {name for name, src in mods.items() if "should_flatten" not in src}

    assert no_flatten == set(_OVERNIGHT_STRATEGIES), (
        "오버나이트 전략 목록이 실제 코드와 어긋난다.\n"
        f"  should_flatten 미호출(실제): {sorted(no_flatten)}\n"
        f"  _OVERNIGHT_STRATEGIES(선언): {sorted(_OVERNIGHT_STRATEGIES)}\n"
        "loop.py 의 _OVERNIGHT_STRATEGIES 를 갱신할 것."
    )


def test_no_target_wording_does_not_claim_a_horizon_it_cannot_know():
    """소유 전략을 모를 때 보유 기한을 단정하면 안 된다."""
    src = (REPO_ROOT / "quant" / "trade" / "loop.py").read_text(encoding="utf-8")
    block = src[src.index("🎯 목표가 없음") - 400 : src.index("rail: list[str] = []")]

    assert "오버나이트 보유" in block, "오버나이트 전략용 문구가 없다"
    assert "장 마감까지 보유" in block, "일중 전략용 문구가 없다"
    # sid 가 없을 때(소유 전략 불명)는 기한을 붙이지 않은 문구가 있어야 한다.
    assert re.search(r'lines\.append\("\s*🎯 목표가 없음"\)', block), (
        "소유 전략을 모를 때 기한을 단정하지 않는 문구가 없다"
    )


def test_missing_quote_distinguishes_closed_market_from_fetch_failure():
    """장이 닫혀 시세가 없는 것과 조회가 실패한 것은 다르다 — 같은 문구면 장애로 읽힌다."""
    src = (REPO_ROOT / "quant" / "trade" / "loop.py").read_text(encoding="utf-8")
    assert "장 마감 — 현재가 없음" in src, "장 마감 케이스 문구가 없다"
    assert "현재가 조회 실패" in src, "진짜 조회 실패 문구가 사라졌다"
