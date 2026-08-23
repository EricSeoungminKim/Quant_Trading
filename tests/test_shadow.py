"""LLM 섀도우 판단 — Phase 7.4. 순수 파싱만(네트워크 없음).

## 섀도우의 계약

LLM 은 **주문을 내지 않는다.** 판단만 `judgments.jsonl` 에 남기고, 실현 수익으로
채점돼 이겨야만 승격한다. 구조적으로 보장된다 — 판단을 읽어 주문을 내는 코드가
없고, 아키텍처 테스트가 `analyze`/`collect` → `trade` 임포트를 금지한다.

## 공정한 비교의 전제

LLM 이 결정론적 스코어러보다 **더 많은 정보를 보면** 비교가 무의미해진다(정보량
비교가 된다). 그래서 프롬프트는 선정 원장의 `attributes` 만으로 만들고, `input_hash`
도 그 같은 dict 로 계산한다.

## 파싱 실패를 점수로 위장하지 않는다

모델이 형식을 어기면 그 종목은 **판단이 없는 것**이다. 0점을 주면 "최하위로
평가했다"가 되어 IC 를 오염시킨다.
"""
from __future__ import annotations

import argparse

from quant.control.shadow import build_prompt, parse_scores


def test_parses_symbol_score_lines():
    got = parse_scores("005930 72\n000660 41\n", allowed={"005930", "000660"})

    assert got == {"005930": 72.0, "000660": 41.0}


def test_ignores_symbols_we_did_not_ask_about():
    """**모델이 지어낸 종목을 받아들이면 안 된다.** 우리가 준 목록 밖은 환각이다."""
    got = parse_scores("005930 72\nAAPL 99\n", allowed={"005930"})

    assert got == {"005930": 72.0}


def test_unparseable_lines_are_skipped_not_scored_zero():
    got = parse_scores("005930 72\n헛소리 한 줄\n000660 없음\n", allowed={"005930", "000660"})

    assert got == {"005930": 72.0}
    assert "000660" not in got


def test_scores_outside_range_are_rejected():
    """척도를 벗어난 값은 모델이 지시를 안 따른 것이다 — 순위를 오염시킨다."""
    got = parse_scores("005930 720\n000660 -5\n", allowed={"005930", "000660"})

    assert got == {}


def test_empty_output_yields_nothing():
    assert parse_scores("", allowed={"005930"}) == {}
    assert parse_scores(None, allowed={"005930"}) == {}


def test_prompt_contains_only_the_shared_attributes():
    """**LLM 이 스코어러보다 더 보면 비교가 정보량 비교가 된다.**

    프롬프트에 심볼과 속성만 들어가고, 결정론적 판정(is_candidate)이나 점수는
    들어가지 않는다 — 그걸 보여주면 베끼기만 해도 이긴다.
    """
    rows = [{"symbol": "005930", "is_candidate": True,
             "attributes": {"trending_score100": 68, "news_articles_today": 13}}]

    prompt = build_prompt(rows)

    assert "005930" in prompt
    assert "trending_score100" in prompt
    assert "is_candidate" not in prompt


# ============ input_hash 배선 — 2026-08-15 채점 루프 최종 리뷰 발견 (P1-6) ============
#
# `selections.build_rows` 는 속성을 최상위에 펼쳐 넣는다(`**attrs`) — 중첩
# `attributes` 키가 없다. 결정론 판단(`selection_judgment`)은 이미
# `judgment.selection_attributes()` 로 이걸 알고 있지만, `cmd_shadow_judge` 는
# `r.get("attributes") or {}` 를 그대로 읽어 **항상 빈 dict** 를 해싱했다 — 리더보드가
# "같은 입력을 본 판단끼리만 비교"할 수 있다는 전제가 LLM 쪽에서는 성립한 적이 없다.

def test_shadow_judge_input_hash_matches_deterministic_judgment(tmp_path, monkeypatch):
    """섀도우 경로와 결정론 경로가 **같은 선정 행**에서 **같은 input_hash** 를 내야
    리더보드 비교가 실력 비교가 된다. `selections.build_rows` 로 실제 평평한 행을
    만들어 재현한다 — 스키마를 추측하면 이 버그가 또 난다."""
    from quant.apps.cli import cmd_shadow_judge
    from quant.control import selections
    from quant.control.judgment import selection_judgment

    payload = {
        "session_date": "2026-08-14",
        "market": "KR",
        "symbols": [{
            "symbol": "005930", "name": "삼성전자",
            "trending_score100": 68, "baseline_score100": 68,
            "news_articles_today": 13, "close": 71_000.0,
        }],
    }
    rows = selections.build_rows(payload, candidate_symbols={"005930"})
    sel_path = tmp_path / "data" / "ledger" / "selections.jsonl"
    selections.append(rows, sel_path)

    expected_hash = selection_judgment(rows[0], producer_version="3").input_hash

    class _FakeNarrator:
        def narrate(self, prompt: str) -> str:
            return "005930 72\n"

    monkeypatch.setattr("quant.adapters.narrate.make_narrator", lambda: _FakeNarrator())

    args = argparse.Namespace(
        root=str(tmp_path), date="2026-08-14", market="KR",
        producer="nemotron-3-ultra", producer_version="free", limit=None,
    )
    cmd_shadow_judge(args)

    written = selections.load(tmp_path / "data" / "ledger" / "judgments.jsonl")
    shadow = next(j for j in written if j["producer"] == "nemotron-3-ultra")

    assert shadow["input_hash"] == expected_hash
