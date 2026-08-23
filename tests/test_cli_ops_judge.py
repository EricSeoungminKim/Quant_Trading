"""`quant.apps.cli cmd_ops_judge` 배선 — 파일 I/O → `AgentData` → LLM 콜러블 조립.

판단 로직 자체(도구 실행/VERDICT 파싱/근거 없는 alert 강등 등)는
`tests/test_ops_judge.py`가 순수 함수로 이미 다룬다. 여기는 apps 계층의 배선만
검증한다 — 진짜 네트워크는 절대 타지 않는다(chat_with_tools 는 항상 가짜로 바꾼다,
`test_agent_interpret.py`/`test_report_cli_agent_interpret.py` 분리와 같은 관례).
"""
from __future__ import annotations

import argparse
import json

import pytest


def _args(tmp_path, **over) -> argparse.Namespace:
    base = dict(root=str(tmp_path), rule_based_json=None, label="test", time_budget=30.0)
    base.update(over)
    return argparse.Namespace(**base)


def test_degrades_to_review_without_llm_credentials(tmp_path, monkeypatch, capsys):
    """OPENROUTER_API_KEY 가 어디에도 없으면(env·.env.local 둘 다) LLM 을 아예
    부르지 않고 review 로 떨어진다 — "정상"이 기본값이 아니다. 파일이 하나도
    없는 빈 저장소에서도 죽지 않는다(모든 도구 소스가 결측 상태로 조립된다)."""
    # `load_settings()`(cmd_ops_judge 내부에서 부른다)는 `.env.local`을
    # `override=True`로 읽되, **호출 전에 이미 명시적으로 준 프로세스 환경변수는
    # 항상 이긴다**(quant.apps.config.load_settings 문서 — 파일 로드 전 스냅샷 후
    # 되돌린다). 그래서 `delenv`(키 자체를 지움)가 아니라 `setenv("", ...)`(빈
    # 문자열로 명시)를 써야 실제 `.env.local`의 진짜 키가 다시 채워지지 않는다 —
    # 처음엔 delenv를 썼다가 이 함수가 실제 OpenRouter 에 네트워크 호출을 내는
    # 것을 실측으로 잡았다.
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setattr("quant.adapters.env.get_key", lambda name: None)

    from quant.apps.cli import cmd_ops_judge

    with pytest.raises(SystemExit) as exc_info:
        cmd_ops_judge(_args(tmp_path))

    assert exc_info.value.code == 2  # review
    out = json.loads(capsys.readouterr().out)
    assert out["level"] == "review"
    assert out["narrator"] == "none"
    assert "자격증명" in out["summary"]


def test_wires_portfolio_file_into_tool_and_relays_verdict(tmp_path, monkeypatch, capsys):
    """`data/state/portfolio.json`을 실제로 써두면 그 값이 도구 결과로 그대로
    나가고, LLM(가짜)이 낸 판정(레벨·근거)이 최종 출력·종료코드에 그대로 실린다.
    `chat_with_tools`는 네트워크를 타지 않는 가짜로 바꾼다."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key-for-test")

    state_dir = tmp_path / "data" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "portfolio.json").write_text(
        json.dumps({"cash": -500.0, "positions": {}}), encoding="utf-8")

    captured = {}

    def fake_chat_with_tools(messages, tools, execute, api_key, model, timeout):
        captured["portfolio"] = json.loads(execute("get_portfolio_state", {}))
        captured["api_key"] = api_key
        return {"text": 'ok\nVERDICT: {"level": "alert", "reasons": ["현금 음수"]}', "rounds": 1}

    monkeypatch.setattr("quant.adapters.narrate.chat_with_tools", fake_chat_with_tools)

    from quant.apps.cli import cmd_ops_judge

    with pytest.raises(SystemExit) as exc_info:
        cmd_ops_judge(_args(tmp_path))

    assert exc_info.value.code == 1  # alert
    assert captured["portfolio"]["cash"] == -500.0
    assert captured["api_key"] == "fake-key-for-test"
    out = json.loads(capsys.readouterr().out)
    assert out["level"] == "alert"
    assert out["reasons"] == ["현금 음수"]
    assert out["narrator"].startswith("openrouter:")


def _fake_chat_capturing_tool(tool_name, tool_args, captured, level="ok", reasons=None):
    """`get_<tool_name>`을 한 번 호출해 그 결과를 `captured`에 담고, 고정된
    VERDICT를 내는 가짜 `chat_with_tools`."""
    reasons = reasons if reasons is not None else ["x"]

    def fake_chat_with_tools(messages, tools, execute, api_key, model, timeout):
        captured["result"] = json.loads(execute(tool_name, tool_args))
        payload = json.dumps({"level": level, "reasons": reasons}, ensure_ascii=False)
        return {"text": f"ok\nVERDICT: {payload}", "rounds": 1}

    return fake_chat_with_tools


def test_notifications_ledger_missing_file_is_none_not_empty(tmp_path, monkeypatch, capsys):
    """`data/ledger/notifications.jsonl`이 아예 없으면 도구는 "없거나 못 읽었다"고
    답해야 한다 — "발송 이력이 없다"(빈 리스트)와는 다른 문구."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key-for-test")
    captured: dict = {}
    monkeypatch.setattr(
        "quant.adapters.narrate.chat_with_tools",
        _fake_chat_capturing_tool("get_sent_notifications", {}, captured),
    )

    from quant.apps.cli import cmd_ops_judge

    with pytest.raises(SystemExit):
        cmd_ops_judge(_args(tmp_path))

    assert "note" in captured["result"]
    assert "없거나" in captured["result"]["note"]


def test_notifications_ledger_present_but_empty_file(tmp_path, monkeypatch, capsys):
    """원장 파일은 존재하지만(빈 파일) 유효한 줄이 없으면 "발송 이력 없음"이지
    "못 읽었다"가 아니다."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key-for-test")
    ledger_dir = tmp_path / "data" / "ledger"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "notifications.jsonl").write_text("", encoding="utf-8")

    captured: dict = {}
    monkeypatch.setattr(
        "quant.adapters.narrate.chat_with_tools",
        _fake_chat_capturing_tool("get_sent_notifications", {}, captured),
    )

    from quant.apps.cli import cmd_ops_judge

    with pytest.raises(SystemExit):
        cmd_ops_judge(_args(tmp_path))

    assert "note" in captured["result"]
    assert "발송 이력 없음" in captured["result"]["note"]


def test_notifications_ledger_with_rows_is_wired_through_with_exact_text(tmp_path, monkeypatch, capsys):
    """실제 원장 파일에 쓴 행이 도구를 통해 정확한 문자열 그대로 나온다 —
    `TelegramNotifier._record`가 쓰는 것과 같은 스키마(ts/ok/text[/error])."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key-for-test")
    ledger_dir = tmp_path / "data" / "ledger"
    ledger_dir.mkdir(parents=True)
    rows = [
        {"ts": "2026-08-19T00:00:00+00:00", "ok": True, "text": "🎯 목표가 없음 (장 마감까지 보유)"},
        {"ts": "2026-08-19T00:05:00+00:00", "ok": False, "text": "실패", "error": "RuntimeError: x"},
    ]
    (ledger_dir / "notifications.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    captured: dict = {}
    monkeypatch.setattr(
        "quant.adapters.narrate.chat_with_tools",
        _fake_chat_capturing_tool("get_sent_notifications", {}, captured),
    )

    from quant.apps.cli import cmd_ops_judge

    with pytest.raises(SystemExit):
        cmd_ops_judge(_args(tmp_path))

    out = captured["result"]
    assert out["total_available"] == 2
    assert out["notifications"][0]["text"] == "🎯 목표가 없음 (장 마감까지 보유)"
    assert out["notifications"][1]["error"] == "RuntimeError: x"


def test_notifications_ledger_read_cap_applies_before_tool_clamp(tmp_path, monkeypatch, capsys):
    """cli.py 의 배선 자체도 원장 전체를 무제한으로 들고 있지 않는다 — 최근 300건만
    `AgentData`에 실리고, 도구는 그 안에서 다시 요청한 만큼(최대 50)만 돌려준다."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key-for-test")
    ledger_dir = tmp_path / "data" / "ledger"
    ledger_dir.mkdir(parents=True)
    rows = [{"ts": str(i), "ok": True, "text": f"msg{i}"} for i in range(350)]
    (ledger_dir / "notifications.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    captured: dict = {}
    monkeypatch.setattr(
        "quant.adapters.narrate.chat_with_tools",
        _fake_chat_capturing_tool("get_sent_notifications", {"limit": 999}, captured),
    )

    from quant.apps.cli import cmd_ops_judge

    with pytest.raises(SystemExit):
        cmd_ops_judge(_args(tmp_path))

    out = captured["result"]
    assert out["total_available"] == 300  # cli.py 읽기 상한(최근 300건)
    assert len(out["notifications"]) == 50  # 도구 자체 상한(_MAX_NOTIF)
    assert out["notifications"][-1]["text"] == "msg349"  # 최신 순 유지


def test_rule_based_json_from_stdin_marker_is_not_read_as_file(tmp_path, monkeypatch, capsys):
    """`--rule-based-json -`는 stdin 을 읽으라는 뜻이다 — 파일 시스템에서
    `-`라는 이름의 파일을 찾지 않는다. LLM 자격증명이 없어 review 로 조기
    반환되는 경로라 stdin 을 실제로 소비하진 않지만(sys.stdin.read 호출 전에
    자격증명 체크가 없다는 점 확인은 별도), 최소한 크래시하지 않는지 확인한다."""
    # `load_settings()`(cmd_ops_judge 내부에서 부른다)는 `.env.local`을
    # `override=True`로 읽되, **호출 전에 이미 명시적으로 준 프로세스 환경변수는
    # 항상 이긴다**(quant.apps.config.load_settings 문서 — 파일 로드 전 스냅샷 후
    # 되돌린다). 그래서 `delenv`(키 자체를 지움)가 아니라 `setenv("", ...)`(빈
    # 문자열로 명시)를 써야 실제 `.env.local`의 진짜 키가 다시 채워지지 않는다 —
    # 처음엔 delenv를 썼다가 이 함수가 실제 OpenRouter 에 네트워크 호출을 내는
    # 것을 실측으로 잡았다.
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setattr("quant.adapters.env.get_key", lambda name: None)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"verdict": "ok"}'))

    from quant.apps.cli import cmd_ops_judge

    with pytest.raises(SystemExit) as exc_info:
        cmd_ops_judge(_args(tmp_path, rule_based_json="-"))

    assert exc_info.value.code == 2
