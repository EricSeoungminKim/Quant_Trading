"""전략 배정표(TAG_ASSIGNMENT) 대조 — 소유자 조직도 역할 3, 2026-08-26.

중심 주장: **배정표는 그럴싸한 문서가 아니라 실제 소비 코드와 일치해야 한다.**
특정 전략에 배정된 태그는 그 전략 생성자가 진짜 `tags_of`를 받아야 하고, 그
전략의 태그 상수도 배정표와 같아야 한다. "*"(전체감시) 버킷에 배정된 전략은
반대로 `tags_of`를 아예 몰라야 한다 — 태그를 읽지도 않는 전략에 배정만
그럴싸하게 적어두는 사고, 혹은 실제로는 태그를 읽는데 "전체감시"로 잘못
적어두는 사고를 둘 다 잡는다.
"""
from __future__ import annotations

import inspect

from quant.trade.strategy import STRATEGY_REGISTRY, TAG_ASSIGNMENT, assignment_for, describe_tags
from quant.trade.strategy.close_bet import CloseBetStrategy
from quant.trade.strategy.frgn_accumulate import FrgnAccumulateStrategy
from quant.trade.strategy.news_momentum import NewsMomentumStrategy
from quant.trade.strategy.news_scalp import NewsScalpStrategy


def _accepts_tags_of(strategy_id: str) -> bool:
    cls = STRATEGY_REGISTRY[strategy_id]
    return "tags_of" in inspect.signature(cls.__init__).parameters


def test_all_assigned_ids_exist_in_registry():
    for tag, ids in TAG_ASSIGNMENT.items():
        for sid in ids:
            assert sid in STRATEGY_REGISTRY, f"{tag} → {sid}: STRATEGY_REGISTRY에 없는 전략 id"


def test_specific_tag_consumers_accept_tags_of():
    """EVENT/EVENT_SCALP/FRGN/FRGN_EXIT/CLOSE_BET처럼 특정 전략에 배정된
    태그는, 그 전략 클래스 생성자가 실제로 `tags_of`를 받아야 한다."""
    for tag, ids in TAG_ASSIGNMENT.items():
        if tag == "*":
            continue
        for sid in ids:
            assert _accepts_tags_of(sid), (
                f"{sid}는 생성자가 tags_of를 받지 않는데 {tag} 소비자로 배정됨"
            )


def test_catchall_consumers_do_not_gate_on_tags():
    """"*"(전체감시) 버킷에 배정된 전략은 tags_of를 아예 모른다 — 유니버스에
    들어온 심볼이면 태그와 무관하게 감시 대상이라는 뜻과 일치해야 한다."""
    for sid in TAG_ASSIGNMENT["*"]:
        assert not _accepts_tags_of(sid), (
            f"{sid}는 tags_of를 받는데 '*'(전체감시, 태그 무관) 버킷으로 배정됨"
        )


def test_no_strategy_id_in_both_specific_and_catchall():
    specific_ids = {sid for tag, ids in TAG_ASSIGNMENT.items() if tag != "*" for sid in ids}
    catchall_ids = set(TAG_ASSIGNMENT["*"])
    overlap = specific_ids & catchall_ids
    assert not overlap, f"특정 태그 소비자이면서 동시에 전체감시로도 배정됨: {overlap}"


def test_tag_constants_match_assignment_table_core_four():
    """핵심 4전략(2026-08-26 기준 config/settings.yaml enabled: true, "전략 4종
    체제")의 실제 태그 상수가 배정표와 일치하는지 소스에서 직접 확인한다."""
    assert NewsMomentumStrategy is STRATEGY_REGISTRY["news_momentum"]
    assert FrgnAccumulateStrategy is STRATEGY_REGISTRY["frgn_accumulate"]
    assert CloseBetStrategy is STRATEGY_REGISTRY["close_bet"]

    from quant.trade.strategy import news_momentum as _news_momentum
    from quant.trade.strategy import frgn_accumulate as _frgn_accumulate
    from quant.trade.strategy import close_bet as _close_bet

    assert _news_momentum._EVENT_TAG == "EVENT"
    assert TAG_ASSIGNMENT["EVENT"] == ["news_momentum"]

    assert _frgn_accumulate._FRGN_TAG == "FRGN"
    assert _frgn_accumulate._FRGN_EXIT_TAG == "FRGN_EXIT"
    assert TAG_ASSIGNMENT["FRGN"] == ["frgn_accumulate"]
    assert TAG_ASSIGNMENT["FRGN_EXIT"] == ["frgn_accumulate"]

    assert _close_bet._TAG == "CLOSE_BET"
    assert TAG_ASSIGNMENT["CLOSE_BET"] == ["close_bet"]


def test_news_scalp_tag_constant_matches_even_though_disabled():
    """news_scalp는 2026-08-26 기준 config/settings.yaml에서 enabled: false지만
    (paper 등록만), 코드 경로는 여전히 EVENT_SCALP만 읽는다 — 배정표도 그래야
    한다(비활성 전략을 배정표에서 지우면, 활성화하는 순간 그 근거를 다시
    찾아야 한다)."""
    assert NewsScalpStrategy is STRATEGY_REGISTRY["news_scalp"]
    from quant.trade.strategy import news_scalp as _news_scalp
    assert _news_scalp._SCALP_TAG == "EVENT_SCALP"
    assert TAG_ASSIGNMENT["EVENT_SCALP"] == ["news_scalp"]


def test_assignment_for_untagged_falls_back_to_catchall():
    assert assignment_for([]) == TAG_ASSIGNMENT["*"]


def test_assignment_for_specific_tag():
    assert assignment_for(["EVENT"]) == ["news_momentum"]


def test_assignment_for_unknown_tag_falls_back_to_catchall():
    # TREND/REBOUND(watch_scorer._VALID_TAGS 소속)처럼 배정표에 없는 태그는
    # 게이트가 없는 태그라 "*" 버킷으로 떨어진다.
    assert assignment_for(["TREND"]) == TAG_ASSIGNMENT["*"]
    assert assignment_for(["REBOUND"]) == TAG_ASSIGNMENT["*"]


def test_assignment_for_dedupes_across_multiple_tags():
    result = assignment_for(["TREND", "REBOUND"])
    assert result == TAG_ASSIGNMENT["*"]  # 둘 다 같은 버킷 — 중복 없이 한 번만


def test_describe_tags_matches_own_brief_example():
    # own_brief.sh 자동 편입 메시지 예시(2026-08-26 소유자 지시) 그대로.
    assert describe_tags(["TREND", "EVENT"]) == "news_momentum·전체감시"
    assert describe_tags([]) == "전체감시"
    assert describe_tags(["CLOSE_BET"]) == "close_bet"
