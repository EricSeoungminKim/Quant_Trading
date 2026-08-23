"""섹션 AI 해석이 조용히 사라지지 않는가.

2026-08-16~20 실장애(5일 이상): 리포트의 `section_advice`/`exec_summary` 가 계속
비어 있었는데 **아무 로그도 없었다.** 소유자가 "원래 유동성 금리, 기술적지표,
시장심리, 한국 수급 체력을 요약했는데 왜 요약을 안 하냐"고 물어서야 발견됐다.

원인 두 겹:
1. `run_report.sh` 가 `OPENROUTER_MAX_TOKENS` 를 설정하지 않아 기본 700 으로 돌았다.
   한국어 4개 섹션을 쓰다 두 번째에서 잘렸다(deepdive·close_report 크론은 이미
   4000 을 명시하고 있었는데 아침/오후 리포트만 빠졌다).
2. all-or-nothing 파서가 마커를 못 찾으면 **말없이** None 을 돌려줬다 — 성공한
   두 섹션까지 버리면서.

LLM 은 제대로 요약하고 있었다. 우리가 그 결과를 조용히 버린 것이다.
"""
from __future__ import annotations

import logging

import pytest

from quant.analyze.section_advice import _MARKERS, _SECTION_ORDER, _parse, advise


def _full_text() -> str:
    return "\n\n".join(f"{_MARKERS[k]}\n{k} 섹션 서술 내용." for k in _SECTION_ORDER)


def test_full_response_parses():
    out = _parse(_full_text(), list(_SECTION_ORDER))
    assert out is not None
    assert set(out) == set(_SECTION_ORDER)


def test_truncated_response_logs_which_markers_are_missing(caplog):
    """이번 장애의 직접 회귀 — 잘린 응답이 조용히 사라지면 안 된다."""
    truncated = f"{_MARKERS['supply']}\n수급 서술.\n\n{_MARKERS['sentiment']}\n심리 서술."

    with caplog.at_level(logging.WARNING, logger="quant.analyze.section_advice"):
        assert _parse(truncated, list(_SECTION_ORDER)) is None

    msgs = [r.getMessage() for r in caplog.records]
    assert msgs, "파싱 실패가 조용히 지나갔다 — 5일간 아무도 모른 이유가 이것이다"
    joined = " ".join(msgs)
    assert _MARKERS["technical"] in joined and _MARKERS["liquidity"] in joined, (
        "어느 마커가 없었는지 알려주지 않으면 진단이 불가능하다"
    )
    assert "OPENROUTER_MAX_TOKENS" in joined, "잘림이 의심될 때 볼 곳을 알려줘야 한다"


def test_empty_section_body_also_logs(caplog):
    text = f"{_MARKERS['supply']}\n\n{_MARKERS['sentiment']}\n심리 서술."
    with caplog.at_level(logging.WARNING, logger="quant.analyze.section_advice"):
        assert _parse(text, ["supply", "sentiment"]) is None
    assert any("비었다" in r.getMessage() for r in caplog.records)


def test_advise_returns_none_without_calling_llm_when_no_sections():
    """전 섹션 결측이면 LLM 을 부르지 않는다(기존 동작 보존)."""
    class _Boom:
        def narrate(self, prompt):  # pragma: no cover
            raise AssertionError("빈 입력으로 LLM 을 부르면 안 된다")

    assert advise({}, _Boom()) is None


def test_run_report_sets_token_limit():
    """만든 것과 배선된 것은 다르다 — 스크립트가 실제로 상한을 올리는지 고정한다."""
    from quant.adapters.env import REPO_ROOT

    src = (REPO_ROOT / "server" / "scripts" / "run_report.sh").read_text(encoding="utf-8")
    assert "OPENROUTER_MAX_TOKENS" in src, (
        "아침/오후 리포트가 기본 700 토큰으로 돌면 섹션 해석이 또 잘린다"
    )
