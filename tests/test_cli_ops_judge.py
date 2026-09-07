"""`quant.apps.cli cmd_ops_judge` 배선 — 파일 I/O → `AgentData` → narrator 조립.

판단 로직 자체(사실 수집/VERDICT 파싱/근거 없는 alert 강등 등)는
`tests/test_ops_judge.py`가 순수 함수로 이미 다룬다. 여기는 apps 계층의 배선만
검증한다.

**2026-09-07 전송 수단 전환**: 옛 `chat_with_tools`(OpenRouter 툴콜링 루프)를
몽키패치하던 방식은 더 이상 통하지 않는다 — 이제 LLM 백엔드는 `_ops_judge_narrator()`
가 조립하는 Claude CLI 1순위 + OpenRouter 폴백이다. `CLAUDE_BIN`이 실제 설치된
개발 머신에서 `chat_with_tools`만 가짜로 바꾸면 Claude CLI 쪽 서브프로세스가 그대로
실행돼 버리므로(`quant/report/collect/agent_interpret.py`의 같은 테스트가 이미 잡은
구멍), `quant.apps.cli._ops_judge_narrator` 자체를 몽키패치해 진짜 네트워크·서브
프로세스를 절대 타지 않게 한다(그 모듈의 `_agent_interpret_narrator` 테스트와
같은 관례)."""
from __future__ import annotations

import argparse
import json

import pytest


def _args(tmp_path, **over) -> argparse.Namespace:
    base = dict(root=str(tmp_path), rule_based_json=None, label="test", time_budget=30.0)
    base.update(over)
    return argparse.Namespace(**base)


class _FakeNarrator:
    """`.narrate(prompt) -> str | None` 계약 하나짜리 테스트 더블."""

    def __init__(self, text, capture: dict | None = None):
        self._text = text
        self._capture = capture

    def narrate(self, prompt: str):
        if self._capture is not None:
            self._capture["prompt"] = prompt
        return self._text


def _patch_narrator(monkeypatch, factory):
    """`quant.apps.cli._ops_judge_narrator`를 가짜로 바꾼다 — 진짜 Claude
    CLI/서브프로세스나 OpenRouter 네트워크를 절대 타지 않는다."""
    import quant.apps.cli as cli_mod

    monkeypatch.setattr(cli_mod, "_ops_judge_narrator", factory)


def test_degrades_to_review_without_llm_backend(tmp_path, monkeypatch, capsys):
    """narrator 를 아예 구성하지 못하면(자격증명 없음 등) LLM 을 부르지 않고
    review 로 떨어진다 — "정상"이 기본값이 아니다. 파일이 하나도 없는 빈
    저장소에서도 죽지 않는다(모든 사실 소스가 결측 상태로 조립된다)."""
    _patch_narrator(monkeypatch, lambda claude_timeout: None)

    from quant.apps.cli import cmd_ops_judge

    with pytest.raises(SystemExit) as exc_info:
        cmd_ops_judge(_args(tmp_path))

    assert exc_info.value.code == 2  # review
    out = json.loads(capsys.readouterr().out)
    assert out["level"] == "review"
    assert "자격증명" in out["summary"]


def test_wires_portfolio_file_into_facts_and_relays_verdict(tmp_path, monkeypatch, capsys):
    """`data/state/portfolio.json`을 실제로 써두면 그 값이 사실 테이블에 실려
    프롬프트에 그대로 나가고, LLM(가짜)이 낸 판정(레벨·근거)이 최종 출력·
    종료코드에 그대로 실린다."""
    state_dir = tmp_path / "data" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "portfolio.json").write_text(
        json.dumps({"cash": -500.0, "positions": {}}), encoding="utf-8")

    captured: dict = {}
    _patch_narrator(
        monkeypatch,
        lambda claude_timeout: _FakeNarrator(
            'ok\nVERDICT: {"level": "alert", "reasons": ["현금 음수"]}', captured),
    )

    from quant.apps.cli import cmd_ops_judge

    with pytest.raises(SystemExit) as exc_info:
        cmd_ops_judge(_args(tmp_path))

    assert exc_info.value.code == 1  # alert
    assert "-500.0" in captured["prompt"]
    out = json.loads(capsys.readouterr().out)
    assert out["level"] == "alert"
    assert out["reasons"] == ["현금 음수"]
    assert out["narrator"] in ("claude", "openrouter")


def test_notifications_ledger_missing_file_is_none_not_empty(tmp_path, monkeypatch, capsys):
    """`data/ledger/notifications.jsonl`이 아예 없으면 사실은 "없거나 못
    읽었다"고 답해야 한다 — "발송 이력이 없다"(빈 리스트)와는 다른 문구."""
    captured: dict = {}
    _patch_narrator(
        monkeypatch,
        lambda claude_timeout: _FakeNarrator(
            'ok\nVERDICT: {"level": "ok", "reasons": ["x"]}', captured),
    )

    from quant.apps.cli import cmd_ops_judge

    with pytest.raises(SystemExit):
        cmd_ops_judge(_args(tmp_path))

    assert "없거나" in captured["prompt"]


def test_notifications_ledger_present_but_empty_file(tmp_path, monkeypatch, capsys):
    """원장 파일은 존재하지만(빈 파일) 유효한 줄이 없으면 "발송 이력 없음"이지
    "못 읽었다"가 아니다."""
    ledger_dir = tmp_path / "data" / "ledger"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "notifications.jsonl").write_text("", encoding="utf-8")

    captured: dict = {}
    _patch_narrator(
        monkeypatch,
        lambda claude_timeout: _FakeNarrator(
            'ok\nVERDICT: {"level": "ok", "reasons": ["x"]}', captured),
    )

    from quant.apps.cli import cmd_ops_judge

    with pytest.raises(SystemExit):
        cmd_ops_judge(_args(tmp_path))

    assert "발송 이력 없음" in captured["prompt"]


def test_notifications_ledger_with_rows_is_wired_through_with_exact_text(tmp_path, monkeypatch, capsys):
    """실제 원장 파일에 쓴 행이 사실 테이블을 통해 정확한 문자열 그대로
    프롬프트에 나온다 — `TelegramNotifier._record`가 쓰는 것과 같은 스키마
    (ts/ok/text[/error])."""
    ledger_dir = tmp_path / "data" / "ledger"
    ledger_dir.mkdir(parents=True)
    rows = [
        {"ts": "2026-08-19T00:00:00+00:00", "ok": True, "text": "🎯 목표가 없음 (장 마감까지 보유)"},
        {"ts": "2026-08-19T00:05:00+00:00", "ok": False, "text": "실패", "error": "RuntimeError: x"},
    ]
    (ledger_dir / "notifications.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    captured: dict = {}
    _patch_narrator(
        monkeypatch,
        lambda claude_timeout: _FakeNarrator(
            'ok\nVERDICT: {"level": "ok", "reasons": ["x"]}', captured),
    )

    from quant.apps.cli import cmd_ops_judge

    with pytest.raises(SystemExit):
        cmd_ops_judge(_args(tmp_path))

    assert "🎯 목표가 없음" in captured["prompt"]
    assert "RuntimeError: x" in captured["prompt"]


def test_notifications_ledger_read_cap_applies_before_facts_clamp(tmp_path, monkeypatch, capsys):
    """cli.py 의 배선 자체도 원장 전체를 무제한으로 들고 있지 않는다 — 최근 300건만
    `AgentData`에 실리고, 사실 수집은 그 안에서 다시 기본 한도(20건)만큼만
    싣는다."""
    ledger_dir = tmp_path / "data" / "ledger"
    ledger_dir.mkdir(parents=True)
    rows = [{"ts": str(i), "ok": True, "text": f"msg{i}"} for i in range(350)]
    (ledger_dir / "notifications.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    captured: dict = {}
    _patch_narrator(
        monkeypatch,
        lambda claude_timeout: _FakeNarrator(
            'ok\nVERDICT: {"level": "ok", "reasons": ["x"]}', captured),
    )

    from quant.apps.cli import cmd_ops_judge

    with pytest.raises(SystemExit):
        cmd_ops_judge(_args(tmp_path))

    assert "msg349" in captured["prompt"]  # 최신 순 유지
    assert "msg329" not in captured["prompt"]  # 도구 기본 한도(20건) 밖


def test_rule_based_json_from_stdin_marker_is_not_read_as_file(tmp_path, monkeypatch, capsys):
    """`--rule-based-json -`는 stdin 을 읽으라는 뜻이다 — 파일 시스템에서
    `-`라는 이름의 파일을 찾지 않는다. narrator 가 없어 review 로 조기
    반환되는 경로라 stdin 을 실제로 소비하진 않지만(narrator 미구성 체크가
    먼저다), 최소한 크래시하지 않는지 확인한다."""
    _patch_narrator(monkeypatch, lambda claude_timeout: None)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"verdict": "ok"}'))

    from quant.apps.cli import cmd_ops_judge

    with pytest.raises(SystemExit) as exc_info:
        cmd_ops_judge(_args(tmp_path, rule_based_json="-"))

    assert exc_info.value.code == 2
