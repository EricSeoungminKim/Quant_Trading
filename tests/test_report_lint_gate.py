"""`_lint_and_gate`(quant/apps/report_cli.py)의 REPORT_LINT_GATE 우회 지침
(2026-09-07, 리포트 QA 세션) — 오탐으로 게이트가 정상 발행을 막을 때 빠르게
재발행할 수 있는 길이 필요하다는 소유자 지시.

두 모드:
  - 기본("block", 미지정 포함) — error 등급이 있으면 RuntimeError를 던져
    발행을 멈춘다(기존 동작 그대로).
  - "warn" — error 등급이 있어도 절대 멈추지 않는다. warn+error 등급 모두
    `data/ledger/report_lint.jsonl`에 남고, NOTIFY_LANE=ops 알림은 문구만
    바뀐 채 그대로 나간다.

네트워크는 절대 타지 않는다 — `TelegramNotifier.send`를 몽키패치해 실제
전송 대신 호출 인자만 기록한다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from quant.adapters.notify.telegram import TelegramNotifier
from quant.analyze.briefing import STANCE_LABEL
from quant.apps.report_cli import _lint_and_gate
from quant.report.model import ReportModel


def _model_with_one_error_and_one_warn() -> ReportModel:
    """`quant.report.lint.lint_report`가 반드시 error 1건 + warn 1건을 내는
    최소 payload — `symbols: []`(거래일에 후보 완전 공백, error)와
    `stance.label`이 옛 방향 문구(옛 스키마, warn)를 함께 심는다."""
    payload = {
        "schema": 1, "market": "KR", "session_date": "2026-09-07",
        "generated_at": "2026-09-07T08:00:00+09:00",
        "missing": [],
        "auto_watch": "AUTO_WATCH: 없음",
        "symbols": [],  # → 후보 완전 공백 error
        "stance": {
            "label": "중립",  # 옛 방향 문구 → warn(STANCE_LABEL 고정 문구가 아님)
            "tier": "중립", "score": 0, "score100": 50, "line": "...",
        },
    }
    return ReportModel(payload=payload)


@pytest.fixture
def sent(monkeypatch):
    """실제 전송 대신 (text, lane) 튜플만 기록 — 네트워크 없음."""
    calls: list[tuple[str, str | None]] = []

    def _fake_send(self, text: str, lane: str | None = None) -> None:
        calls.append((text, lane))

    monkeypatch.setattr(TelegramNotifier, "send", _fake_send)
    return calls


def test_block_mode_is_default_and_raises_on_error(tmp_path: Path, sent, monkeypatch):
    monkeypatch.delenv("REPORT_LINT_GATE", raising=False)
    model = _model_with_one_error_and_one_warn()
    with pytest.raises(RuntimeError, match="발행 중단"):
        _lint_and_gate(model, tmp_path)
    # 알림은 여전히 나간다(기존 동작) — 문구는 "발행 중단".
    assert len(sent) == 1
    assert "발행 중단" in sent[0][0]
    assert sent[0][1] == "ops"


def test_block_mode_explicit_matches_default(tmp_path: Path, sent, monkeypatch):
    monkeypatch.setenv("REPORT_LINT_GATE", "block")
    model = _model_with_one_error_and_one_warn()
    with pytest.raises(RuntimeError):
        _lint_and_gate(model, tmp_path)


def test_warn_mode_never_raises_even_with_errors(tmp_path: Path, sent, monkeypatch):
    monkeypatch.setenv("REPORT_LINT_GATE", "warn")
    model = _model_with_one_error_and_one_warn()
    _lint_and_gate(model, tmp_path)  # 예외 없이 반환돼야 한다


def test_warn_mode_still_sends_ops_alert_with_different_wording(tmp_path: Path, sent, monkeypatch):
    monkeypatch.setenv("REPORT_LINT_GATE", "warn")
    model = _model_with_one_error_and_one_warn()
    _lint_and_gate(model, tmp_path)
    assert len(sent) == 1
    text, lane = sent[0]
    assert lane == "ops"
    assert "발행 계속" in text
    assert "발행 중단" not in text


def _ledger_rows(root: Path) -> list[dict]:
    path = root / "data" / "ledger" / "report_lint.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_block_mode_ledger_has_warn_row_but_not_error_row(tmp_path: Path, sent, monkeypatch):
    """기존 동작 보존 — block 모드는 발행을 그 자리에서 멈추므로(알림에 이미
    findings 가 담긴다) error 는 원장에 남기지 않는다. warn 만 남는다."""
    monkeypatch.setenv("REPORT_LINT_GATE", "block")
    model = _model_with_one_error_and_one_warn()
    with pytest.raises(RuntimeError):
        _lint_and_gate(model, tmp_path)
    rows = _ledger_rows(tmp_path)
    severities = [r["severity"] for r in rows]
    assert "warn" in severities
    assert "error" not in severities


def test_warn_mode_ledger_has_both_warn_and_error_rows(tmp_path: Path, sent, monkeypatch):
    """warn 모드는 빌드를 멈추지 않으므로, 나중에 감사할 수 있게 error 도
    원장에 남긴다(block 모드와의 유일한 원장 차이)."""
    monkeypatch.setenv("REPORT_LINT_GATE", "warn")
    model = _model_with_one_error_and_one_warn()
    _lint_and_gate(model, tmp_path)
    rows = _ledger_rows(tmp_path)
    severities = [r["severity"] for r in rows]
    assert "warn" in severities
    assert "error" in severities


def test_no_findings_at_all_is_silent_in_both_modes(tmp_path: Path, sent, monkeypatch):
    payload = {
        "schema": 1, "market": "US", "session_date": "2026-09-07",
        "generated_at": "2026-09-07T08:00:00+09:00",
        "missing": [],
        "auto_watch": "AUTO_WATCH: AAPL:NEWS",
        "symbols": [{"symbol": "AAPL", "name": "AAPL"}],
        "stance": {
            "label": STANCE_LABEL, "tier": "중립", "score": 0,
            "score100": 50, "line": "...",
        },
    }
    for mode in ("block", "warn"):
        monkeypatch.setenv("REPORT_LINT_GATE", mode)
        model = ReportModel(payload=dict(payload))
        _lint_and_gate(model, tmp_path)  # 예외 없음
    assert sent == []
    assert _ledger_rows(tmp_path) == []
