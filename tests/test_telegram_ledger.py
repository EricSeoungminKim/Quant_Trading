"""보낸 텔레그램 메시지를 원장에 남기는가.

2026-08-19: 판단 워치독(ops_judge)이 "우리가 뭐라고 보냈는가"를 근거로 삼으려면
발송 기록이 있어야 한다. Bot API 의 getUpdates 는 봇에게 **온** 메시지만 주므로,
우리가 **보낸** 것은 우리가 남기지 않으면 어디에도 없다.

그날 실제로 "🎯 목표가 없음 (장 마감까지 보유)" 오문구를 사람이 텔레그램에서 읽고서야
발견했다 — 기계가 그걸 읽을 수 있어야 같은 종류를 자동으로 잡는다.
"""
from __future__ import annotations

import json

import pytest

from quant.adapters.notify import telegram as tg


def _read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_successful_send_is_recorded(tmp_path, monkeypatch):
    ledger = tmp_path / "data" / "ledger" / "notifications.jsonl"
    monkeypatch.setattr(tg, "_LEDGER_PATH", ledger)

    class _Resp:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(tg.httpx, "post", lambda *a, **k: _Resp())
    tg.TelegramNotifier("tok", "chat").send("🏢 현대해상 · 목표가 없음")

    rows = _read(ledger)
    assert len(rows) == 1
    assert rows[0]["ok"] is True
    assert "현대해상" in rows[0]["text"]
    assert rows[0]["ts"]


def test_failed_send_is_also_recorded_with_error(tmp_path, monkeypatch):
    """실패한 발송도 남아야 한다 — '보냈다고 생각했는데 안 갔다'가 가장 위험하다."""
    ledger = tmp_path / "data" / "ledger" / "notifications.jsonl"
    monkeypatch.setattr(tg, "_LEDGER_PATH", ledger)

    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(tg.httpx, "post", _boom)
    tg.TelegramNotifier("tok", "chat").send("실패할 메시지")

    rows = _read(ledger)
    assert len(rows) == 1
    assert rows[0]["ok"] is False
    assert "RuntimeError" in rows[0]["error"]


def test_ledger_failure_never_breaks_sending(tmp_path, monkeypatch):
    """기록이 알림을 막으면 본말전도다 — Notifier 프로토콜은 모든 예외를 삼킨다."""
    monkeypatch.setattr(tg, "_LEDGER_PATH", tmp_path / "nope" / "x.jsonl")

    class _Resp:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(tg.httpx, "post", lambda *a, **k: _Resp())
    monkeypatch.setattr(tg.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")))

    tg.TelegramNotifier("tok", "chat").send("보내야 한다")  # 예외가 새면 실패


def test_disabled_notifier_records_nothing(tmp_path, monkeypatch):
    ledger = tmp_path / "data" / "ledger" / "notifications.jsonl"
    monkeypatch.setattr(tg, "_LEDGER_PATH", ledger)
    tg.TelegramNotifier(None, None).send("no-op")
    assert not ledger.exists()
