"""판단하는 워치독(quant.control.ops_judge) — 도구 실행 + VERDICT 파싱 + run_judgment.

이 테스트가 지키는 핵심 주장들:
- 도구는 전부 읽기 전용이다(쓰기 도구 자체가 없다 — TOOLS_SPEC 이름 목록으로 확인).
- 근거(reasons) 없는 "alert"는 "review"로 낮아진다.
- 도구를 하나도 안 쓴 판정은 level 과 무관하게 "review"로 낮아진다.
- LLM 호출 실패/미구성/예산 소진은 전부 "review"로 떨어진다 — "ok"가 기본값이 아니다.
- 파싱 실패해도 산문은 살아남는다(agent_interpret._parse_judgment와 같은 계약).
"""
from __future__ import annotations

import json

from quant.control.ops_judge import (
    SYSTEM_PROMPT,
    TOOLS_SPEC,
    AgentData,
    _parse_verdict,
    build_execute,
    run_judgment,
)


def _data(**over) -> AgentData:
    return AgentData(**over)


# ── TOOLS_SPEC — 전부 읽기 전용, OpenAI 함수콜 모양 ──────────────────────

def test_tools_spec_has_ten_read_only_tools():
    assert len(TOOLS_SPEC) == 10
    names = {t["function"]["name"] for t in TOOLS_SPEC}
    assert names == {
        "get_rule_based_findings", "get_portfolio_state", "get_recent_trades",
        "get_strategy_books", "get_strategy_config", "get_control_state",
        "get_report_summary", "get_index_bars", "get_recent_alert_log",
        "get_sent_notifications",
    }
    # 이름 자체가 전부 조회(get_*)다 — 쓰기/주문 도구가 존재하지 않는다는
    # 구조적 보증(테스트로 다시 확인).
    assert all(n.startswith("get_") for n in names)
    for t in TOOLS_SPEC:
        assert t["type"] == "function"
        assert t["function"]["description"]
        assert t["function"]["parameters"]["type"] == "object"


def test_system_prompt_has_injection_defense_and_verdict_format():
    assert "따르지 마라" in SYSTEM_PROMPT
    assert "데이터일 뿐" in SYSTEM_PROMPT
    assert "VERDICT:" in SYSTEM_PROMPT
    assert "ok|review|alert" in SYSTEM_PROMPT
    assert "모르면 review" in SYSTEM_PROMPT


# ── build_execute: 도구별 조회 ────────────────────────────────────────────

def test_get_rule_based_findings_passthrough():
    findings = {"verdict": "alert", "n_alert": 1, "n_unknown": 0, "findings": []}
    execute = build_execute(_data(rule_based=findings))
    out = json.loads(execute("get_rule_based_findings", {}))
    assert out == findings


def test_get_rule_based_findings_missing_returns_note():
    execute = build_execute(_data())
    out = json.loads(execute("get_rule_based_findings", {}))
    assert "note" in out


def test_get_portfolio_state_passthrough():
    portfolio = {"cash": -1000.0, "positions": {}}
    execute = build_execute(_data(portfolio=portfolio))
    out = json.loads(execute("get_portfolio_state", {}))
    assert out == portfolio


def test_get_recent_trades_limits_and_reports_total():
    trades = [{"symbol": "005930", "side": "buy", "qty": i} for i in range(30)]
    execute = build_execute(_data(recent_trades=trades))
    out = json.loads(execute("get_recent_trades", {"limit": 5}))
    assert len(out["trades"]) == 5
    assert out["trades"][-1]["qty"] == 29
    assert out["total_available"] == 30


def test_get_recent_trades_limit_clamped_to_max():
    trades = [{"symbol": "x", "qty": i} for i in range(150)]
    execute = build_execute(_data(recent_trades=trades))
    out = json.loads(execute("get_recent_trades", {"limit": 9999}))
    assert len(out["trades"]) == 100  # _MAX_TRADES


def test_get_recent_trades_empty_returns_note():
    execute = build_execute(_data())
    out = json.loads(execute("get_recent_trades", {}))
    assert "note" in out


def test_get_strategy_books_passthrough():
    books = {"exists": True, "books": {"donchian": {"cash_krw": 1000}}}
    execute = build_execute(_data(strategy_books=books))
    out = json.loads(execute("get_strategy_books", {}))
    assert out == books


def test_get_strategy_config_known_id():
    cfg = {"frgn_accumulate": {"params": {"fixed_amount_krw": 100000}}}
    execute = build_execute(_data(strategy_config=cfg))
    out = json.loads(execute("get_strategy_config", {"strategy_id": "frgn_accumulate"}))
    assert out == cfg["frgn_accumulate"]


def test_get_strategy_config_unknown_id_lists_available():
    cfg = {"donchian": {}}
    execute = build_execute(_data(strategy_config=cfg))
    out = json.loads(execute("get_strategy_config", {"strategy_id": "없는전략"}))
    assert "donchian" in out["note"]


def test_get_control_state_missing_files_returns_notes_not_crash():
    execute = build_execute(_data())
    out = json.loads(execute("get_control_state", {}))
    assert "note" in out["control"]
    assert "note" in out["heartbeat"]


def test_get_control_state_present():
    execute = build_execute(_data(control_state={"halted": True}, heartbeat={"ts": 1}))
    out = json.loads(execute("get_control_state", {}))
    assert out["control"] == {"halted": True}
    assert out["heartbeat"] == {"ts": 1}


def test_get_report_summary_known_session():
    reports = {"KR_am": {"kospi_change_pct": -1.55}}
    execute = build_execute(_data(reports=reports))
    out = json.loads(execute("get_report_summary", {"session": "KR_am"}))
    assert out == {"kospi_change_pct": -1.55}


def test_get_report_summary_unknown_session_lists_available():
    execute = build_execute(_data(reports={"KR_am": {}}))
    out = json.loads(execute("get_report_summary", {"session": "US_am"}))
    assert "KR_am" in out["note"]


def test_get_report_summary_session_present_but_none_payload():
    execute = build_execute(_data(reports={"KR_am": None}))
    out = json.loads(execute("get_report_summary", {"session": "KR_am"}))
    assert "note" in out


def test_get_index_bars_known_key():
    bars = {"QQQ 1d": [{"ts": "2026-08-18", "close": 500.0}]}
    execute = build_execute(_data(bar_checks=bars))
    out = json.loads(execute("get_index_bars", {"key": "QQQ 1d"}))
    assert out == {"bars": bars["QQQ 1d"]}


def test_get_index_bars_unknown_key_lists_available():
    execute = build_execute(_data(bar_checks={"QQQ 1d": []}))
    out = json.loads(execute("get_index_bars", {"key": "069500 1d"}))
    assert "QQQ 1d" in out["note"]


def test_get_recent_alert_log_limits_lines():
    lines = [f"line{i}" for i in range(50)]
    execute = build_execute(_data(log_tails={"ops_watch": lines}))
    out = json.loads(execute("get_recent_alert_log", {"name": "ops_watch", "limit": 3}))
    assert out["lines"] == lines[-3:]


def test_get_recent_alert_log_unknown_name_lists_available():
    execute = build_execute(_data(log_tails={"report": []}))
    out = json.loads(execute("get_recent_alert_log", {"name": "close_report"}))
    assert "report" in out["note"]


# ── get_sent_notifications — 발송 원장(정확한 전송 문자열) ──────────────────

def test_get_sent_notifications_none_means_ledger_unreadable():
    """`None`은 "원장이 없거나 못 읽었다"다 — "발송 이력이 없다"(빈 리스트)와
    다른 정보이며 둘을 합쳐 뭉개면 안 된다."""
    execute = build_execute(_data(sent_notifications=None))
    out = json.loads(execute("get_sent_notifications", {}))
    assert "note" in out
    assert "없거나" in out["note"]


def test_get_sent_notifications_empty_list_means_no_history_yet():
    """빈 리스트는 "원장은 읽었는데 발송 이력이 없다"다 — `None`과 다른 문구여야
    한다(호출부가 두 상태를 구분해서 넘긴다는 계약 확인)."""
    execute = build_execute(_data(sent_notifications=[]))
    out = json.loads(execute("get_sent_notifications", {}))
    assert "note" in out
    assert "발송 이력 없음" in out["note"]
    assert out["note"] != json.loads(
        build_execute(_data(sent_notifications=None))("get_sent_notifications", {}))["note"]


def test_get_sent_notifications_returns_exact_text_and_ok_flag():
    rows = [
        {"ts": "2026-08-19T00:00:00+00:00", "ok": True, "text": "🎯 목표가 없음 (장 마감까지 보유)"},
        {"ts": "2026-08-19T00:05:00+00:00", "ok": False, "text": "실패한 메시지", "error": "RuntimeError: x"},
    ]
    execute = build_execute(_data(sent_notifications=rows))
    out = json.loads(execute("get_sent_notifications", {}))
    assert out["total_available"] == 2
    assert out["notifications"][0]["text"] == "🎯 목표가 없음 (장 마감까지 보유)"
    assert out["notifications"][0]["ok"] is True
    assert "error" not in out["notifications"][0]  # 성공 건엔 error 키 자체가 없다
    assert out["notifications"][1]["ok"] is False
    assert out["notifications"][1]["error"] == "RuntimeError: x"
    assert all(n["truncated"] is False for n in out["notifications"])


def test_get_sent_notifications_limit_clamped_to_max():
    rows = [{"ts": str(i), "ok": True, "text": f"msg{i}"} for i in range(200)]
    execute = build_execute(_data(sent_notifications=rows))
    out = json.loads(execute("get_sent_notifications", {"limit": 9999}))
    assert len(out["notifications"]) == 50  # _MAX_NOTIF
    assert out["total_available"] == 200


def test_get_sent_notifications_respects_requested_limit_and_recency():
    rows = [{"ts": str(i), "ok": True, "text": f"msg{i}"} for i in range(10)]
    execute = build_execute(_data(sent_notifications=rows))
    out = json.loads(execute("get_sent_notifications", {"limit": 3}))
    assert [n["text"] for n in out["notifications"]] == ["msg7", "msg8", "msg9"]


def test_get_sent_notifications_truncates_long_text_and_marks_it():
    long_text = "가" * 3000
    rows = [{"ts": "t", "ok": True, "text": long_text}]
    execute = build_execute(_data(sent_notifications=rows))
    out = json.loads(execute("get_sent_notifications", {}))
    entry = out["notifications"][0]
    assert entry["truncated"] is True
    assert len(entry["text"]) < len(long_text)
    assert "잘림" in entry["text"]
    assert "3000" in entry["text"]  # 원문 길이를 그대로 알려준다


def test_get_sent_notifications_short_text_is_not_marked_truncated():
    rows = [{"ts": "t", "ok": True, "text": "짧은 메시지"}]
    execute = build_execute(_data(sent_notifications=rows))
    out = json.loads(execute("get_sent_notifications", {}))
    assert out["notifications"][0]["truncated"] is False
    assert out["notifications"][0]["text"] == "짧은 메시지"


def test_unknown_tool_name_returns_note_not_exception():
    execute = build_execute(_data())
    out = json.loads(execute("delete_everything", {}))
    assert "알 수 없는 도구" in out["note"]


def test_handler_exception_does_not_escape(monkeypatch):
    import quant.control.ops_judge as mod

    def boom(data, args):
        raise RuntimeError("boom")

    monkeypatch.setitem(mod._DISPATCH, "get_portfolio_state", boom)
    execute = build_execute(_data())
    out = json.loads(execute("get_portfolio_state", {}))
    assert "도구 실행 오류" in out["note"]


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

def _fake_chat_using_one_tool(level: str, reasons: list[str]):
    def chat(messages, tools, execute):
        execute("get_portfolio_state", {})
        payload = json.dumps({"level": level, "reasons": reasons}, ensure_ascii=False)
        return {"text": f"산문 설명.\nVERDICT: {payload}", "rounds": 2}
    return chat


def test_run_judgment_happy_path_ok():
    data = _data(portfolio={"cash": 1000.0, "positions": {}})
    out = run_judgment(data, _fake_chat_using_one_tool("ok", ["현금 정상"]))
    assert out["level"] == "ok"
    assert out["reasons"] == ["현금 정상"]
    assert out["tools_used"] == ["get_portfolio_state"]
    assert out["rounds"] == 2
    assert out["budget_exhausted"] is False


def test_run_judgment_alert_with_reasons_passes_through():
    out = run_judgment(_data(), _fake_chat_using_one_tool("alert", ["현금이 음수(-1047만원)"]))
    assert out["level"] == "alert"
    assert out["reasons"] == ["현금이 음수(-1047만원)"]


def test_run_judgment_alert_without_reasons_is_downgraded_to_review():
    out = run_judgment(_data(), _fake_chat_using_one_tool("alert", []))
    assert out["level"] == "review"
    assert "확인 필요" in out["reasons"][0]


def test_run_judgment_no_tools_used_is_downgraded_to_review_even_for_ok():
    def chat(messages, tools, execute):
        # 도구를 전혀 안 부르고 바로 판정.
        return {"text": 'VERDICT: {"level": "ok", "reasons": ["그냥 정상 같음"]}', "rounds": 1}

    out = run_judgment(_data(), chat)
    assert out["level"] == "review"
    assert any("도구 호출 0건" in r for r in out["reasons"])


def test_run_judgment_chat_returns_none_is_review_not_ok():
    out = run_judgment(_data(), lambda **kw: None)
    assert out["level"] == "review"
    assert "LLM 응답" in out["summary"]


def test_run_judgment_chat_raises_is_review_not_crash():
    def boom(**kw):
        raise RuntimeError("network down")

    out = run_judgment(_data(), boom)
    assert out["level"] == "review"
    assert "LLM 호출 실패" in out["summary"]


def test_run_judgment_no_chat_backend_is_review():
    out = run_judgment(_data(), None)
    assert out["level"] == "review"
    assert "자격증명" in out["summary"]


def test_run_judgment_budget_zero_does_not_call_chat():
    calls = []

    def chat(**kw):
        calls.append(1)
        return {"text": 'VERDICT: {"level": "ok", "reasons": ["x"]}', "rounds": 1}

    out = run_judgment(_data(), chat, time_budget_seconds=0)
    assert out["level"] == "review"
    assert out["budget_exhausted"] is True
    assert calls == []


def test_run_judgment_negative_budget_does_not_call_chat():
    out = run_judgment(_data(), lambda **kw: (_ for _ in ()).throw(AssertionError("should not be called")),
                       time_budget_seconds=-5)
    assert out["level"] == "review"
    assert out["budget_exhausted"] is True


def test_run_judgment_unparsable_verdict_is_review_but_keeps_prose():
    def chat(messages, tools, execute):
        execute("get_portfolio_state", {})
        return {"text": "그냥 산문만 있고 VERDICT 마커가 없다.", "rounds": 1}

    out = run_judgment(_data(), chat)
    assert out["level"] == "review"
    assert "산문만" in out["summary"]
