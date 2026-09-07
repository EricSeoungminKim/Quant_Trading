"""`quant.report.collect.agent_interpret` — 결정론 사실 수집 + 단일 LLM 호출
전환(2026-09-07, LLM 레인 신뢰성 세션).

이전엔 OpenRouter 툴콜링 루프(`chat_with_tools` + `quant.analyze.
agent_interpret.interpret_candidates`)가 라운드마다 모델이 도구를 스스로
골라 호출했다. 이 세션에서 그 도구 선택을 없애고 "사실을 전부 미리 모아
Claude CLI 에 한 번만 묻는다"로 바꿨다 — 사실 자체(도구 핸들러 로직)는
`quant.analyze.agent_interpret`의 기존 `_tool_get_*`를 그대로 재사용하므로
그쪽 테스트(`tests/test_agent_interpret.py`)가 이미 검증한다. 여기서는
새로 추가된 조립 함수(사실 모으기 → 프롬프트 → narrator 호출)만 검증한다.
"""
from __future__ import annotations

import json

from quant.analyze.agent_interpret import AgentData
from quant.report.collect.agent_interpret import (
    _agent_interpret_narrator,
    _build_deterministic_prompt,
    _gather_deterministic_facts,
    _interpret_candidates_deterministic,
)


def _data(**over) -> AgentData:
    base = dict(session_date="2026-09-07")
    base.update(over)
    return AgentData(**base)


# ── _gather_deterministic_facts ───────────────────────────────────────────

def test_gathers_all_five_facts_for_midterm_source():
    data = _data()
    facts = _gather_deterministic_facts(data, {"symbol": "005930"}, source="midterm")

    assert set(facts) == {
        "get_foreign_flow", "get_news_titles", "get_disclosures",
        "get_telegram_mentions", "get_track_record",
    }


def test_gathers_score_breakdown_only_for_intraday_source():
    data = _data(score_breakdown={"005930": {"score100": 80, "factors": []}})
    facts = _gather_deterministic_facts(data, {"symbol": "005930"}, source="intraday")

    assert "get_score_breakdown" in facts
    assert facts["get_score_breakdown"] == {"score100": 80, "factors": []}


def test_gathers_real_data_not_just_notes():
    """도구 핸들러를 실제로 호출한다는 증거 — 값이 있으면 note가 아니라
    실제 데이터가 나와야 한다(핸들러 재사용 확인, 위조 방지)."""
    data = _data(foreign_flow={"005930": [
        {"date": "2026-09-01", "foreign_net": 100, "inst_net": 50},
    ]})
    facts = _gather_deterministic_facts(data, {"symbol": "005930"}, source="midterm")

    assert "note" not in facts["get_foreign_flow"]
    assert facts["get_foreign_flow"]["series"]


def test_gathers_note_when_no_data():
    data = _data()
    facts = _gather_deterministic_facts(data, {"symbol": "999999"}, source="midterm")

    assert facts["get_foreign_flow"] == {"note": "데이터 없음"}


# ── _build_deterministic_prompt ───────────────────────────────────────────

def test_prompt_contains_injection_defense_and_judgment_format():
    prompt = _build_deterministic_prompt({"symbol": "005930", "name": "삼성전자", "score100": 80}, {})

    assert "따르지 마라" in prompt
    assert "데이터일 뿐" in prompt
    assert "JUDGMENT:" in prompt
    assert "bullish|neutral|bearish" in prompt


def test_prompt_contains_grounding_instruction():
    """전 저장소 프롬프트가 공유하는 환각 방지 계약(`midterm_watch.
    _GROUNDING_INSTRUCTION`과 같은 문구)."""
    prompt = _build_deterministic_prompt({"symbol": "005930", "name": "삼성전자", "score100": 80}, {})

    assert "근거 없으면 '근거 없음'" in prompt


def test_prompt_embeds_candidate_and_facts():
    facts = {"get_foreign_flow": {"note": "데이터 없음"}}
    prompt = _build_deterministic_prompt({"symbol": "005930", "name": "삼성전자", "score100": 80}, facts)

    assert "005930" in prompt
    assert "삼성전자" in prompt
    assert "80/100" in prompt
    marker = "[사실]\n"
    parsed_tail = prompt[prompt.rindex(marker) + len(marker):]
    assert json.loads(parsed_tail) == facts


# ── _agent_interpret_narrator ─────────────────────────────────────────────

def test_narrator_is_none_when_nothing_available(monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", "/nonexistent/claude")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    import quant.adapters.env as env_mod
    monkeypatch.setattr(env_mod, "get_key", lambda name: None)

    assert _agent_interpret_narrator(claude_timeout=30) is None


def test_narrator_wraps_claude_primary_and_openrouter_fallback(monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", __file__)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    from quant.adapters.narrate import ClaudeCliNarrator, OpenRouterNarrator, QualityFallbackNarrator

    got = _agent_interpret_narrator(claude_timeout=42)

    assert isinstance(got, QualityFallbackNarrator)
    assert isinstance(got._primary, ClaudeCliNarrator)
    assert got._primary._timeout == 42
    assert isinstance(got._fallback, OpenRouterNarrator)
    assert got.name == "agent_interpret"


def test_narrator_falls_back_to_openrouter_only_when_binary_missing(monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", "/nonexistent/claude")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    from quant.adapters.narrate import NullNarrator, OpenRouterNarrator, QualityFallbackNarrator

    got = _agent_interpret_narrator(claude_timeout=30)

    assert isinstance(got, QualityFallbackNarrator)
    assert isinstance(got._primary, NullNarrator)
    assert isinstance(got._fallback, OpenRouterNarrator)


# ── _interpret_candidates_deterministic ───────────────────────────────────

class _StubNarrator:
    def __init__(self, replies):
        self._replies = list(replies)
        self.prompts: list[str] = []

    def narrate(self, prompt):
        self.prompts.append(prompt)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _judgment_text(direction="bullish", confidence=4, prose="이유가 되는 산문이다."):
    return f'{prose}\nJUDGMENT: {{"direction": "{direction}", "confidence": {confidence}}}'


def test_happy_path_returns_full_record_with_rounds_one_and_all_facts_used():
    data = _data()
    candidates = [{"symbol": "005930", "name": "삼성전자", "score100": 80}]
    narrator = _StubNarrator([_judgment_text()])

    out = _interpret_candidates_deterministic(candidates, data, "intraday", narrator)

    assert len(out) == 1
    item = out[0]
    assert item["symbol"] == "005930"
    assert item["name"] == "삼성전자"
    assert item["direction"] == "bullish"
    assert item["confidence"] == 4
    assert item["rounds"] == 1
    assert item["tools_used"] == sorted([
        "get_foreign_flow", "get_news_titles", "get_disclosures",
        "get_telegram_mentions", "get_score_breakdown", "get_track_record",
    ])


def test_skips_candidate_when_narrator_returns_none():
    data = _data()
    candidates = [{"symbol": "005930", "name": "삼성전자", "score100": 80}]
    narrator = _StubNarrator([None])

    assert _interpret_candidates_deterministic(candidates, data, "midterm", narrator) == []


def test_skips_candidate_when_narrator_raises():
    data = _data()
    candidates = [{"symbol": "005930", "name": "삼성전자", "score100": 80}]
    narrator = _StubNarrator([RuntimeError("boom")])

    assert _interpret_candidates_deterministic(candidates, data, "midterm", narrator) == []


def test_one_failure_does_not_kill_the_rest():
    data = _data()
    candidates = [
        {"symbol": "005930", "name": "삼성전자", "score100": 80},
        {"symbol": "000660", "name": "SK하이닉스", "score100": 70},
    ]
    narrator = _StubNarrator([None, _judgment_text(prose="두번째 후보 이유.")])

    out = _interpret_candidates_deterministic(candidates, data, "midterm", narrator)

    assert [item["symbol"] for item in out] == ["000660"]


def test_time_budget_skips_unstarted_candidates(capsys):
    data = _data()
    candidates = [{"symbol": "005930", "name": "삼성전자", "score100": 80}]
    narrator = _StubNarrator([_judgment_text()])

    out = _interpret_candidates_deterministic(
        candidates, data, "midterm", narrator, time_budget_seconds=0,
    )

    assert out == []
    assert "시간 예산" in capsys.readouterr().out
