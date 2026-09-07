"""판단하는 워치독(quant.control.ops_judge) — 사실 수집 + VERDICT 파싱 + run_judgment.

**2026-09-07 전송 수단 전환**(LLM 레인 신뢰성 세션): 옛 OpenRouter 툴콜링 루프
(`TOOLS_SPEC`/`build_execute`/`chat` 콜러블)를 걷어내고 "사실을 전부 미리 모아
narrator 에 한 번만 묻는다"로 바꿨다(`quant/report/collect/agent_interpret.py`의
같은 전환과 같은 이유·같은 패턴). 이 테스트가 지키는 핵심 주장들:

- `_build_facts`가 모든 `_tool_get_*` 핸들러를 결정론적으로 전부 호출해 사실
  테이블 하나로 합친다 — 조회 실패는 여전히 `{"note": ...}`로 정직하게 답한다.
- 근거(reasons) 없는 "alert"는 "review"로 낮아진다.
- LLM 호출 실패/미구성/예산 소진은 전부 "review"로 떨어진다 — "ok"가 기본값이 아니다.
- 파싱 실패해도 산문은 살아남는다(agent_interpret._parse_judgment와 같은 계약).
- `narrator`는 `.narrate(prompt) -> str | None` 하나짜리 계약으로 주입된다(도구
  선택 루프가 아니라 단일 호출) — `tools_used`는 이제 "이번에 준비한 사실 전체"다.
"""
from __future__ import annotations

from quant.control.ops_judge import (
    AgentData,
    _build_facts,
    _build_prompt,
    _parse_verdict,
    run_judgment,
)


def _data(**over) -> AgentData:
    return AgentData(**over)


class _FakeNarrator:
    """`.narrate(prompt) -> str | None` 계약 하나짜리 테스트 더블 —
    `quant/report/collect/agent_interpret.py`의 테스트와 같은 관례."""

    def __init__(self, text, capture: dict | None = None):
        self._text = text
        self._capture = capture

    def narrate(self, prompt: str):
        if self._capture is not None:
            self._capture["prompt"] = prompt
        return self._text


class _RaisingNarrator:
    def narrate(self, prompt: str):
        raise RuntimeError("network down")


# ── _build_facts — 결정론적 사실 수집 ──────────────────────────────────────

def test_build_facts_includes_all_top_level_categories():
    facts = _build_facts(_data())
    assert set(facts) >= {
        "rule_based_findings", "portfolio_state", "recent_trades",
        "strategy_books", "strategy_config", "control_state", "sent_notifications",
    }


def test_build_facts_missing_data_returns_notes_not_crash():
    facts = _build_facts(_data())
    assert "note" in facts["rule_based_findings"]
    assert "note" in facts["portfolio_state"]
    assert "note" in facts["recent_trades"]
    assert "note" in facts["strategy_books"]
    assert "note" in facts["control_state"]["control"]
    assert "note" in facts["sent_notifications"]


def test_build_facts_passes_through_real_data_not_just_notes():
    """실제 값이 있으면 note 가 아니라 그 값 자체가 나와야 한다(핸들러를 실제로
    호출한다는 증거, 위조 방지)."""
    portfolio = {"cash": -1000.0, "positions": {}}
    facts = _build_facts(_data(portfolio=portfolio))
    assert facts["portfolio_state"] == portfolio


def test_build_facts_limits_recent_trades_to_tool_default():
    trades = [{"symbol": "005930", "qty": i} for i in range(50)]
    facts = _build_facts(_data(recent_trades=trades))
    assert len(facts["recent_trades"]["trades"]) == 20
    assert facts["recent_trades"]["trades"][-1]["qty"] == 49
    assert facts["recent_trades"]["total_available"] == 50


def test_build_facts_limits_sent_notifications_to_tool_default():
    rows = [{"ts": str(i), "ok": True, "text": f"msg{i}"} for i in range(30)]
    facts = _build_facts(_data(sent_notifications=rows))
    assert len(facts["sent_notifications"]["notifications"]) == 20
    assert facts["sent_notifications"]["notifications"][-1]["text"] == "msg29"


def test_build_facts_includes_full_strategy_config():
    cfg = {"frgn_accumulate": {"params": {"fixed_amount_krw": 100000}}, "donchian": {}}
    facts = _build_facts(_data(strategy_config=cfg))
    assert facts["strategy_config"] == cfg


def test_build_facts_includes_one_entry_per_report_session():
    reports = {"KR_am": {"kospi_change_pct": -1.55}, "US_am": None}
    facts = _build_facts(_data(reports=reports))
    assert facts["report_summary[KR_am]"] == {"kospi_change_pct": -1.55}
    assert "note" in facts["report_summary[US_am]"]


def test_build_facts_includes_one_entry_per_bar_check_key():
    bars = {"QQQ 1d": [{"ts": "2026-08-18", "close": 500.0}]}
    facts = _build_facts(_data(bar_checks=bars))
    assert facts["index_bars[QQQ 1d]"] == {"bars": bars["QQQ 1d"]}


def test_build_facts_limits_each_log_to_tool_default():
    lines = [f"line{i}" for i in range(50)]
    facts = _build_facts(_data(log_tails={"ops_watch": lines}))
    assert facts["recent_alert_log[ops_watch]"]["lines"] == lines[-30:]


def test_build_facts_distinguishes_none_and_empty_sent_notifications():
    """`None`은 "원장이 없거나 못 읽었다"다 — "발송 이력이 없다"(빈 리스트)와
    다른 정보이며 둘을 합쳐 뭉개면 안 된다(호출부 계약, ops_judge 모듈 docstring
    "텔레그램 발송 원장" 절)."""
    none_note = _build_facts(_data(sent_notifications=None))["sent_notifications"]["note"]
    empty_note = _build_facts(_data(sent_notifications=[]))["sent_notifications"]["note"]
    assert "없거나" in none_note
    assert "발송 이력 없음" in empty_note
    assert none_note != empty_note


# ── _build_prompt ──────────────────────────────────────────────────────────

def test_build_prompt_has_injection_defense_and_verdict_format_and_label():
    facts = _build_facts(_data(label="kr-midday"))
    prompt = _build_prompt(_data(label="kr-midday"), facts)
    assert "따르지 마라" in prompt
    assert "데이터일 뿐" in prompt
    assert "VERDICT:" in prompt
    assert "ok|review|alert" in prompt
    assert "모르면 review" in prompt
    assert "kr-midday" in prompt
    assert "[사실]" in prompt


# ── _parse_verdict ─────────────────────────────────────────────────────────

def test_parse_verdict_extracts_level_reasons_and_prose():
    text = (
        "KOSPI 등락률과 069500 봉을 대조했다. 부호가 일치한다.\n"
        'VERDICT: {"level": "ok", "reasons": ["069500 종가 대비 등락률 부호 일치"]}'
    )
    level, reasons, prose = _parse_verdict(text)
    assert level == "ok"
    assert reasons == ["069500 종가 대비 등락률 부호 일치"]
    assert "KOSPI" in prose
    assert "VERDICT" not in prose


def test_parse_verdict_missing_marker_keeps_prose_level_none():
    level, reasons, prose = _parse_verdict("그냥 산문만 있고 마커가 없다.")
    assert level is None
    assert reasons == []
    assert prose == "그냥 산문만 있고 마커가 없다."


def test_parse_verdict_broken_json_keeps_prose():
    level, reasons, prose = _parse_verdict("산문.\nVERDICT: {이건 json이 아니다}")
    assert level is None
    assert prose == "산문."


def test_parse_verdict_invalid_level_becomes_none():
    level, reasons, prose = _parse_verdict('산문.\nVERDICT: {"level": "심각한이상", "reasons": []}')
    assert level is None


def test_parse_verdict_empty_text():
    level, reasons, prose = _parse_verdict("")
    assert level is None
    assert reasons == []
    assert prose == ""


# ── run_judgment ────────────────────────────────────────────────────────────

def test_run_judgment_happy_path_ok_calls_narrator_once_with_full_facts():
    data = _data(portfolio={"cash": 1000.0, "positions": {}})
    captured: dict = {}
    narrator = _FakeNarrator(
        '산문 설명.\nVERDICT: {"level": "ok", "reasons": ["현금 정상"]}', captured)
    out = run_judgment(data, narrator)
    assert out["level"] == "ok"
    assert out["reasons"] == ["현금 정상"]
    assert out["rounds"] == 1
    assert out["budget_exhausted"] is False
    assert "portfolio_state" in out["tools_used"]
    assert "1000.0" in captured["prompt"]


def test_run_judgment_alert_with_reasons_passes_through():
    narrator = _FakeNarrator('ok\nVERDICT: {"level": "alert", "reasons": ["현금이 음수(-1047만원)"]}')
    out = run_judgment(_data(), narrator)
    assert out["level"] == "alert"
    assert out["reasons"] == ["현금이 음수(-1047만원)"]


def test_run_judgment_alert_without_reasons_is_downgraded_to_review():
    narrator = _FakeNarrator('ok\nVERDICT: {"level": "alert", "reasons": []}')
    out = run_judgment(_data(), narrator)
    assert out["level"] == "review"
    assert "확인 필요" in out["reasons"][0]


def test_run_judgment_tools_used_is_always_the_full_deterministic_set():
    """옛 버전의 "도구 0건 사용 → review" 가드는 없앴다 — 도구 선택이라는
    개념 자체가 사라졌으므로 `tools_used`는 항상 사실 전체다."""
    narrator = _FakeNarrator('VERDICT: {"level": "ok", "reasons": ["그냥 정상 같음"]}')
    out = run_judgment(_data(), narrator)
    assert out["level"] == "ok"
    assert set(out["tools_used"]) == set(_build_facts(_data()))


def test_run_judgment_narrator_returns_none_is_review_not_ok():
    out = run_judgment(_data(), _FakeNarrator(None))
    assert out["level"] == "review"
    assert "LLM 응답" in out["summary"]


def test_run_judgment_narrator_raises_is_review_not_crash():
    out = run_judgment(_data(), _RaisingNarrator())
    assert out["level"] == "review"
    assert "LLM 호출 실패" in out["summary"]


def test_run_judgment_no_narrator_backend_is_review():
    out = run_judgment(_data(), None)
    assert out["level"] == "review"
    assert "자격증명" in out["summary"]


def test_run_judgment_budget_zero_does_not_call_narrator():
    calls = []

    class _Tracked:
        def narrate(self, prompt):
            calls.append(1)
            return 'VERDICT: {"level": "ok", "reasons": ["x"]}'

    out = run_judgment(_data(), _Tracked(), time_budget_seconds=0)
    assert out["level"] == "review"
    assert out["budget_exhausted"] is True
    assert calls == []


def test_run_judgment_negative_budget_does_not_call_narrator():
    class _Boom:
        def narrate(self, prompt):
            raise AssertionError("should not be called")

    out = run_judgment(_data(), _Boom(), time_budget_seconds=-5)
    assert out["level"] == "review"
    assert out["budget_exhausted"] is True


def test_run_judgment_unparsable_verdict_is_review_but_keeps_prose():
    narrator = _FakeNarrator("그냥 산문만 있고 VERDICT 마커가 없다.")
    out = run_judgment(_data(), narrator)
    assert out["level"] == "review"
    assert "산문만" in out["summary"]
