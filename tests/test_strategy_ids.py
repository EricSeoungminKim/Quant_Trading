"""`quant.core.strategy_ids` 단일 정의 회귀(2026-09-03).

`quant/trade/loop.py`·`quant/trade/risk/manager.py`·`quant/control/ledger.py`가
각자 베껴 쓰던 `_base_strategy_id`/`base_strategy_id`를 이 모듈 하나로 합쳤다.
`ledger` 쪽 구현은 `_pure` 접미사를 벗기지 않아 갈라져 있었다 — 이 테스트는
그 회귀를 세 호출부 전부에서 직접 대조해 잡는다.
"""
from __future__ import annotations

from quant.core.strategy_ids import CATALYST_ARM_SUFFIX, PURE_ARM_SUFFIX, base_strategy_id, is_catalyst_arm


def test_strips_catalyst_suffix():
    assert base_strategy_id("scalp_1m_cat") == "scalp_1m"


def test_strips_pure_suffix():
    assert base_strategy_id("donchian_pure") == "donchian"


def test_strips_catalyst_then_pure_in_that_order():
    """`_cat`을 먼저 벗기고 그다음 `_pure`를 벗긴다 — 순서를 바꾸면
    `scalp_1m_pure_cat`이 `scalp_1m_cat`(틀림)이 돼버린다.
    2026-09-03 기준 `config/settings.yaml`에 실제 등록된 조합은 아니지만,
    형태 자체가 나타나면 다뤄야 하므로 순서를 문서화하고 고정한다."""
    assert base_strategy_id("scalp_1m_pure_cat") == "scalp_1m"


def test_no_suffix_returns_unchanged():
    assert base_strategy_id("scalp_1m") == "scalp_1m"


def test_empty_and_none_return_empty_string():
    assert base_strategy_id("") == ""
    assert base_strategy_id(None) == ""


def test_is_catalyst_arm():
    assert is_catalyst_arm("scalp_1m_cat") is True
    assert is_catalyst_arm("scalp_1m") is False
    assert is_catalyst_arm("scalp_1m_pure") is False
    assert is_catalyst_arm(None) is False


def test_suffix_constants():
    assert CATALYST_ARM_SUFFIX == "_cat"
    assert PURE_ARM_SUFFIX == "_pure"


def test_all_three_call_sites_agree():
    """`loop._base_strategy_id`/`manager._base_strategy_id`/`ledger.base_strategy_id`
    가 전부 `quant.core.strategy_ids.base_strategy_id`를 가리키는 같은 함수인지
    — 별칭이 끊기면(예: 셋 중 하나가 로컬로 재구현되면) 여기서 잡는다."""
    from quant.trade.loop import _base_strategy_id as loop_fn
    from quant.trade.risk.manager import _base_strategy_id as manager_fn
    from quant.control.ledger import base_strategy_id as ledger_fn

    cases = ["scalp_1m", "scalp_1m_cat", "donchian_pure", "scalp_1m_pure_cat", "", None]
    for sid in cases:
        expected = base_strategy_id(sid)
        assert loop_fn(sid) == expected
        assert manager_fn(sid) == expected
        assert ledger_fn(sid) == expected
