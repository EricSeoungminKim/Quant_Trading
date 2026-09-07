"""2026-09-07 보안 감사 — tg_bridge.py 인증 경계의 추가 엣지 케이스.

`tests/test_tg_bridge_lanes.py`가 `is_allowed_chat`의 세 갈래(레거시 chat_id /
바인딩된 슈퍼그룹 / 오너 본인 발신)를 이미 촘촘히 덮는다. 이 파일은 그 위에서
감사 요청이 명시한 나머지 경계를 고정한다:

1. **채널 게시물(channel_post)** — 텔레그램은 채널에 올라온 글을 `update["message"]`
   가 아니라 `update["channel_post"]`로 보낸다. `is_allowed_chat`이 `message` 키만
   보므로 이미 안전하게 거부되지만, 그 사실이 회귀로 깨지지 않게 고정한다.
2. **수정된 메시지(edited_message)** — 마찬가지로 `message`가 아닌 별도 키다.
3. **바인딩된 그룹 안의 비오너 발신자가 /halt 같은 제어 명령을 실제로 실행할 수
   있다** — `is_allowed_chat` docstring이 명시한 오너의 의도적 완화("그 방에 있는
   사람은 신뢰한다")다. 버그가 아니라 정책이지만, 그 정책이 실제로 제어 명령
   경로까지 적용되는지(게이트만 통과하고 명령 자체는 막히지 않는지) 여기서
   확인한다 — 정책이 바뀌면 이 테스트가 먼저 깨져야 한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "server" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import tg_bridge  # noqa: E402


class _RecordingTelegramClient:
    def __init__(self):
        self.sent: list[tuple[int, str, int | None]] = []
        self.typed: list[int] = []

    def send_message(self, chat_id: int, text: str, message_thread_id=None) -> None:
        self.sent.append((chat_id, text, message_thread_id))

    def send_typing(self, chat_id: int) -> None:
        self.typed.append(chat_id)


class _FakeControl:
    def __init__(self):
        self.halted = False
        self.reason = None
        self.by = None

    def halt(self, reason: str, by: str) -> None:
        self.halted = True
        self.reason = reason
        self.by = by

    def resume(self) -> None:
        self.halted = False

    def is_halted(self) -> bool:
        return self.halted

    def halted_by(self):
        return self.by

    def halt_reason(self):
        return self.reason

    def request_flatten(self, scope: str) -> None:
        pass


class _FakeTossClient:
    def stock_info(self, symbol: str) -> dict:
        return {"name": "미검증"}


# ---------------------------------------------------------------------------
# 채널 게시물 / 수정된 메시지 — "message" 키가 없는 업데이트는 통째로 무시
# ---------------------------------------------------------------------------
def test_is_allowed_chat_rejects_update_with_no_message_key():
    update = {"update_id": 1, "channel_post": {"chat": {"id": 42}, "text": "/halt"}}
    assert tg_bridge.is_allowed_chat(update, allowed_chat_id=42) is False


def test_is_allowed_chat_rejects_edited_message():
    update = {"update_id": 1, "edited_message": {"chat": {"id": 42}, "text": "/halt"}}
    assert tg_bridge.is_allowed_chat(update, allowed_chat_id=42) is False


def test_process_update_ignores_channel_post_even_from_owners_channel(tmp_path):
    """채널 게시물은 chat.id가 오너의 허용 chat_id와 같아도(오너가 만든 채널)
    명령으로 처리되지 않는다 — 채널 게시물에는 신뢰 가능한 `from` 발신자가 없다."""
    tg = _RecordingTelegramClient()
    limiter = tg_bridge.RateLimiter()
    update = {
        "update_id": 1,
        "channel_post": {"chat": {"id": 42}, "text": "/halt 채널발 명령"},
    }

    tg_bridge.process_update(
        tg, 42, limiter, _FakeControl(), _FakeTossClient(), update,
        lanes_path=tmp_path / "tg_lanes.json",
    )

    assert tg.sent == []


def test_process_update_ignores_edited_message(tmp_path):
    tg = _RecordingTelegramClient()
    limiter = tg_bridge.RateLimiter()
    update = {
        "update_id": 1,
        "edited_message": {"chat": {"id": 42}, "text": "/halt 수정된 명령"},
    }

    tg_bridge.process_update(
        tg, 42, limiter, _FakeControl(), _FakeTossClient(), update,
        lanes_path=tmp_path / "tg_lanes.json",
    )

    assert tg.sent == []


# ---------------------------------------------------------------------------
# 바인딩된 그룹의 비오너 발신자 — /halt가 실제로 실행된다(오너의 의도적 완화)
# ---------------------------------------------------------------------------
def test_halt_from_non_owner_member_of_bound_group_actually_halts(tmp_path):
    """`is_allowed_chat`이 통과시키는 비오너 발신자가 실제로 /halt를 실행할 수
    있는지 명령 처리 경로까지 확인한다 — 게이트만 열려 있고 명령 핸들러가 별도로
    막는 게 아님을 고정한다(정책이 바뀌면 이 테스트가 먼저 깨져야 한다)."""
    lanes_path = tmp_path / "tg_lanes.json"
    tg_bridge.save_tg_lanes({"chat_id": -100999, "threads": {}}, lanes_path)

    tg = _RecordingTelegramClient()
    limiter = tg_bridge.RateLimiter()
    control = _FakeControl()
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": -100999},
            "from": {"id": 555},  # 오너(42)가 아닌 그룹 멤버
            "text": "/halt 비오너 발신",
        },
    }

    tg_bridge.process_update(
        tg, 42, limiter, control, _FakeTossClient(), update, lanes_path=lanes_path,
    )

    assert control.halted is True
    assert control.by == "manual"
    assert len(tg.sent) == 1
    assert tg.sent[0][0] == -100999


def test_halt_from_stranger_in_unbound_chat_is_rejected(tmp_path):
    """대조군 — 같은 /halt 라도 어디에도 바인딩되지 않은 낯선 채팅에서 오너가
    아닌 사람이 보내면 아예 처리되지 않아야 한다."""
    tg = _RecordingTelegramClient()
    limiter = tg_bridge.RateLimiter()
    control = _FakeControl()
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": -100111},
            "from": {"id": 555},
            "text": "/halt 낯선 채팅",
        },
    }

    tg_bridge.process_update(
        tg, 42, limiter, control, _FakeTossClient(), update,
        lanes_path=tmp_path / "tg_lanes.json",
    )

    assert control.halted is False
    assert tg.sent == []
