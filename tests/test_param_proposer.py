"""전략 파라미터 제안자(`quant/analyze/param_proposer.py`) — 3단계 (2026-08-26).

계약:
① **제안만 한다** — settings 를 건드리지 않고, 출력에 "자동 반영 안 됨 —
   사람이 바꾸면 16:30 자동 판정 루프(이중차분)가 실측 판정" 고지가 붙는다.
② **사이징 계열 제안 금지** — capital_fraction/max_position 등 거버너 층 0
   영역 파라미터는 파싱 단계에서 버린다(LLM 이 제안해도).
③ **없는 전략·표본 부족이면 침묵** — 지어낸 제안이 원장에 남는 것보다 무제안.
④ 주간 멱등 — 같은 (주, 전략, 파라미터) 제안은 한 번만 적재.
"""
from __future__ import annotations

import json

from quant.analyze.param_proposer import (
    append_proposals, build_prompt, parse_proposals, propose, render_note,
)


def _prop(strategy="scalp_1m", param="take_profit_bps", cur=100, new=150):
    return {"strategy": strategy, "param": param, "current": cur, "proposed": new,
            "rationale": "익절이 MFE 를 다 못 먹는다", "risk": "승률 하락",
            "verify": "2주 내 평균 bps 개선 없으면 철회"}


def test_prompt_grounds_on_given_data_and_forbids_sizing():
    p = build_prompt("주간 리뷰 텍스트", "params: yaml", ["scalp_1m", "close_bet"])
    assert "주간 리뷰 텍스트" in p and "params: yaml" in p
    assert "capital_fraction" in p and "금지" in p, "사이징 제안 금지를 프롬프트에도 명시"
    assert "표본" in p, "표본 부족 시 제안하지 말라는 지시"


def test_parse_filters_forbidden_and_unknown():
    text = json.dumps({"proposals": [
        _prop(),
        _prop(param="capital_fraction", cur=0.3, new=0.5),      # ② 사이징 — 버림
        _prop(strategy="ghost_strategy"),                        # ③ 없는 전략 — 버림
    ]})
    out = parse_proposals(text, valid_strategies={"scalp_1m", "close_bet"})
    assert len(out) == 1 and out[0]["param"] == "take_profit_bps"


def test_parse_caps_at_three_and_requires_fields():
    props = [_prop(param=f"p{i}") for i in range(5)]
    out = parse_proposals(json.dumps({"proposals": props}), valid_strategies={"scalp_1m"})
    assert len(out) == 3, "제안은 최대 3건 — 산탄총 제안은 다중검정 낭비"
    assert parse_proposals("쓰레기", valid_strategies={"scalp_1m"}) is None
    missing = json.dumps({"proposals": [{"strategy": "scalp_1m", "param": "x"}]})
    assert parse_proposals(missing, valid_strategies={"scalp_1m"}) == []


def test_note_carries_no_auto_apply_disclaimer():
    note = render_note([_prop()])
    assert "scalp_1m" in note and "take_profit_bps" in note
    assert "자동" in note and "이중차분" in note, "① 고지 필수"
    assert render_note([]) is None


def test_propose_absent_on_llm_failure():
    assert propose("리뷰", "yaml", {"scalp_1m"}, lambda p: None) is None


def test_propose_end_to_end_with_fake_llm():
    reply = json.dumps({"proposals": [_prop()]})
    out = propose("리뷰", "yaml", {"scalp_1m"}, lambda p: reply)
    assert out is not None and len(out["proposals"]) == 1
    assert "자동" in out["note"]


def test_append_idempotent_per_week(tmp_path):
    p = tmp_path / "props.jsonl"
    rows = [_prop()]
    assert append_proposals(rows, p, week="2026-W35") == 1
    assert append_proposals(rows, p, week="2026-W35") == 0
    assert append_proposals(rows, p, week="2026-W36") == 1, "다음 주엔 같은 제안도 새 표본"
