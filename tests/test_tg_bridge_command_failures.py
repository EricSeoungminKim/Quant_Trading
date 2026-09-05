"""명령 처리 실패 시 **사용자에게 응답한다** — 2026-08-26 감사 수리.

감사 발견: `/halt` `/resume` `/flatten`(거래 제어)과 `/watch` `/unwatch`
`/watchlist-reset`(파일 잠금·저장 I/O)에서 예외가 나면, 바깥 메인 루프의 광역
try/except 가 로그만 남기고 **사용자는 완전한 침묵**을 받았다. 위험 명령에서
"성공했는지 실패했는지 모르는" 상태가 가장 나쁘다 — 특히 /halt 는 사용자가
멈췄다고 믿고 자리를 뜰 수 있다.

계약: 실패해도 원 예외는 로그에 남기고(삼키지 않는다), 사용자에게는 실패와
"상태가 불확실하니 /status 로 확인" 안내를 보낸다. 실패 알림 발송이 또 실패해도
거기서 멈춘다(무한 재귀 금지).
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "server" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import tg_bridge  # noqa: E402


class _Tg:
    def __init__(self, fail_send: bool = False):
        self.sent: list[tuple[int, str]] = []
        self._fail = fail_send

    def send_message(self, chat_id: int, text: str, message_thread_id=None) -> None:
        if self._fail:
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, text))


def _update(text: str, chat_id: int = 7) -> dict:
    return {"message": {"chat": {"id": chat_id}, "text": text}}


def _run(monkeypatch, tg, text, *, control_raises=None, watch_raises=None,
         control_reply=None, watch_reply=None):
    def _control(_text, _control, _client):
        if control_raises:
            raise control_raises
        return control_reply

    def _watch(_text, _client):
        if watch_raises:
            raise watch_raises
        return watch_reply

    monkeypatch.setattr(tg_bridge, "handle_control_command", _control)
    monkeypatch.setattr(tg_bridge, "handle_watchlist_command", _watch)
    monkeypatch.setattr(tg_bridge, "is_allowed_chat", lambda *_a, **_k: True)
    tg_bridge.process_update(tg, 7, None, None, None, _update(text))


def test_control_command_failure_tells_the_user(monkeypatch):
    """/halt 가 터지면 침묵하지 않는다 — 멈췄다고 믿게 두는 게 가장 위험하다."""
    tg = _Tg()
    _run(monkeypatch, tg, "/halt", control_raises=OSError("disk full"))

    assert len(tg.sent) == 1
    _, msg = tg.sent[0]
    assert "/halt" in msg and "실패" in msg and "OSError" in msg
    assert "/status" in msg, "상태를 직접 확인할 방법을 알려준다"


def test_watchlist_command_failure_tells_the_user(monkeypatch):
    tg = _Tg()
    _run(monkeypatch, tg, "/watch 005930", watch_raises=RuntimeError("flock 실패"))

    assert len(tg.sent) == 1
    _, msg = tg.sent[0]
    assert "/watch" in msg and "005930" not in msg, "명령 이름만 쓴다(인자 제외)"
    assert "RuntimeError" in msg


def test_failure_notice_failing_does_not_raise(monkeypatch):
    """실패 알림 발송이 또 실패해도 거기서 멈춘다(무한 재귀 금지)."""
    tg = _Tg(fail_send=True)
    _run(monkeypatch, tg, "/flatten", control_raises=ValueError("boom"))
    assert tg.sent == []


def test_successful_command_replies_normally(monkeypatch):
    """정상 경로는 종전과 같다 — 수리가 성공 응답을 건드리지 않는다."""
    tg = _Tg()
    _run(monkeypatch, tg, "/halt", control_reply="⏸ 신규 진입 중단")

    assert tg.sent == [(7, "⏸ 신규 진입 중단")]


def test_command_name_helper_drops_arguments():
    assert tg_bridge._command_name("/watch 005930 000660") == "/watch"
    assert tg_bridge._command_name("   ") == "명령"
