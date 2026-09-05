"""tg_bridge.py의 텔레그램 포럼 토픽 레인(2026-09-05) — /here, /lanes,
토픽 안 답장, 슈퍼그룹 게이트.

server/scripts/tg_bridge.py는 패키지가 아닌 독립 스크립트라 sys.path에 그
디렉토리를 얹어 직접 import한다. 텔레그램/Toss API는 전부 mock — 실 네트워크
호출 없음.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "server" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import tg_bridge  # noqa: E402
from quant.core import tglanes  # noqa: E402


def _msg(
    chat_id: int = 111, thread_id: int | None = None, text: str = "",
    from_id: int | None = None,
) -> dict:
    chat: dict = {"id": chat_id}
    message: dict = {"chat": chat, "text": text}
    if thread_id is not None:
        message["message_thread_id"] = thread_id
    if from_id is not None:
        message["from"] = {"id": from_id}
    return message


def _update(
    chat_id: int = 111, thread_id: int | None = None, text: str = "", update_id: int = 1,
    from_id: int | None = None,
) -> dict:
    return {"update_id": update_id, "message": _msg(chat_id, thread_id, text, from_id)}


class RecordingTelegramClient:
    def __init__(self):
        self.sent: list[tuple[int, str, int | None]] = []
        self.typed: list[int] = []

    def send_message(self, chat_id: int, text: str, message_thread_id=None) -> None:
        self.sent.append((chat_id, text, message_thread_id))

    def send_typing(self, chat_id: int) -> None:
        self.typed.append(chat_id)


class FakeControl:
    def is_halted(self):
        return False


class FakeTossClient:
    def stock_info(self, symbol: str) -> dict:
        return {"name": "미검증"}


# ---------------------------------------------------------------------------
# _match_lane — 영문 id / 대소문자 무관 / 한국어 표시명
# ---------------------------------------------------------------------------
def test_match_lane_by_id():
    assert tg_bridge._match_lane("trades") == "trades"


def test_match_lane_case_insensitive():
    assert tg_bridge._match_lane("TRADES") == "trades"
    assert tg_bridge._match_lane("Ops") == "ops"


def test_match_lane_by_korean_display_name():
    assert tg_bridge._match_lane("매매") == "trades"
    assert tg_bridge._match_lane("제어실") == "control"
    assert tg_bridge._match_lane("브리핑") == "briefs"
    assert tg_bridge._match_lane("채널 인텔") == "intel"
    assert tg_bridge._match_lane("운영") == "ops"


def test_match_lane_unknown_returns_none():
    assert tg_bridge._match_lane("존재안함") is None
    assert tg_bridge._match_lane("") is None


# ---------------------------------------------------------------------------
# load_tg_lanes / save_tg_lanes
# ---------------------------------------------------------------------------
def test_load_tg_lanes_missing_file_returns_empty_dict(tmp_path):
    assert tg_bridge.load_tg_lanes(tmp_path / "missing.json") == {}


def test_load_tg_lanes_corrupt_file_returns_empty_dict(tmp_path):
    path = tmp_path / "tg_lanes.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert tg_bridge.load_tg_lanes(path) == {}


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "tg_lanes.json"
    mapping = {"chat_id": -100123, "threads": {"trades": 42}}
    tg_bridge.save_tg_lanes(mapping, path)
    assert tg_bridge.load_tg_lanes(path) == mapping


# ---------------------------------------------------------------------------
# handle_here — 바인딩
# ---------------------------------------------------------------------------
def test_here_binds_topic_to_lane(tmp_path):
    path = tmp_path / "tg_lanes.json"
    reply = tg_bridge.handle_here("매매", _msg(chat_id=-100999, thread_id=7), path)
    assert "매매" in reply
    saved = tg_bridge.load_tg_lanes(path)
    assert saved["chat_id"] == -100999
    assert saved["threads"]["trades"] == 7
    assert "bound_at" in saved and "trades" in saved["bound_at"]


def test_here_without_thread_id_is_rejected(tmp_path):
    """포럼 토픽이 아닌 채팅(예: 일반 그룹/DM)에서는 바인딩할 스레드가 없다."""
    path = tmp_path / "tg_lanes.json"
    reply = tg_bridge.handle_here("매매", _msg(chat_id=-100999, thread_id=None), path)
    assert "포럼 토픽이 아닙니다" in reply
    assert tg_bridge.load_tg_lanes(path) == {}


def test_here_unknown_lane_is_rejected(tmp_path):
    path = tmp_path / "tg_lanes.json"
    reply = tg_bridge.handle_here("없는레인", _msg(chat_id=-100999, thread_id=7), path)
    assert "알 수 없는 레인" in reply
    assert tg_bridge.load_tg_lanes(path) == {}


def test_here_alone_lists_bindings_when_empty(tmp_path):
    path = tmp_path / "tg_lanes.json"
    reply = tg_bridge.handle_here("", _msg(chat_id=-100999, thread_id=7), path)
    assert "바인딩된 레인 없음" in reply


def test_here_alone_lists_current_bindings(tmp_path):
    path = tmp_path / "tg_lanes.json"
    tg_bridge.handle_here("매매", _msg(chat_id=-100999, thread_id=7), path)
    tg_bridge.handle_here("운영", _msg(chat_id=-100999, thread_id=9), path)
    reply = tg_bridge.handle_here("", _msg(chat_id=-100999, thread_id=None), path)
    assert "topic 7" in reply
    assert "topic 9" in reply
    assert "미바인딩" in reply  # control/briefs/intel은 아직 안 묶임


def test_here_can_rebind_a_different_lane_without_losing_others(tmp_path):
    path = tmp_path / "tg_lanes.json"
    tg_bridge.handle_here("매매", _msg(chat_id=-100999, thread_id=7), path)
    tg_bridge.handle_here("운영", _msg(chat_id=-100999, thread_id=9), path)
    saved = tg_bridge.load_tg_lanes(path)
    assert saved["threads"] == {"trades": 7, "ops": 9}


def test_here_rebinding_same_lane_overwrites_thread(tmp_path):
    path = tmp_path / "tg_lanes.json"
    tg_bridge.handle_here("매매", _msg(chat_id=-100999, thread_id=7), path)
    tg_bridge.handle_here("매매", _msg(chat_id=-100999, thread_id=77), path)
    saved = tg_bridge.load_tg_lanes(path)
    assert saved["threads"]["trades"] == 77


def test_concurrent_here_calls_lose_nothing(tmp_path):
    """flock이 read-modify-write 전체를 배타 구간으로 만든다(관심종목 파일과 같은
    패턴 — tests/test_tg_bridge_watchlist.py::test_concurrent_watch_calls_lose_nothing)."""
    import concurrent.futures as cf

    path = tmp_path / "tg_lanes.json"
    lanes = list(tglanes.LANES)  # 5개
    with cf.ThreadPoolExecutor(5) as ex:
        list(ex.map(
            lambda i: tg_bridge.handle_here(lanes[i], _msg(chat_id=-1, thread_id=100 + i), path),
            range(len(lanes)),
        ))
    saved = tg_bridge.load_tg_lanes(path)
    assert set(saved["threads"]) == set(lanes)


# ---------------------------------------------------------------------------
# format_lanes_table / handle_lanes_command
# ---------------------------------------------------------------------------
def test_lanes_table_lists_all_five_lanes_with_descriptions(tmp_path):
    text = tg_bridge.format_lanes_table(tmp_path / "missing.json")
    for lane_id, (emoji, name) in tglanes.LANES.items():
        assert name in text
        assert tg_bridge.LANE_DESCRIPTIONS[lane_id] in text


def test_lanes_table_marks_bound_lanes(tmp_path):
    path = tmp_path / "tg_lanes.json"
    tg_bridge.handle_here("매매", _msg(chat_id=-1, thread_id=7), path)
    text = tg_bridge.format_lanes_table(path)
    lines = {l.strip() for l in text.splitlines()}
    assert any(l.startswith("✅") and "매매" in l for l in lines)
    assert any(l.startswith("⬜") and "운영" in l for l in lines)


def test_handle_lanes_command_non_command_returns_none():
    assert tg_bridge.handle_lanes_command("그냥 안부", _msg()) is None


def test_handle_lanes_command_lanes_alias():
    assert tg_bridge.handle_lanes_command("/레인", _msg()) is not None
    assert tg_bridge.handle_lanes_command("/lanes", _msg()) is not None


def test_handle_lanes_command_here_dispatches(tmp_path):
    path = tmp_path / "tg_lanes.json"
    reply = tg_bridge.handle_lanes_command("/here 매매", _msg(chat_id=-1, thread_id=5), path)
    assert reply is not None and "매매" in reply
    assert tg_bridge.load_tg_lanes(path)["threads"]["trades"] == 5


# ---------------------------------------------------------------------------
# is_allowed_chat — 레거시 chat_id 또는 바인딩된 슈퍼그룹 chat_id
# ---------------------------------------------------------------------------
def test_is_allowed_chat_legacy_id():
    assert tg_bridge.is_allowed_chat(_update(chat_id=42), 42) is True


def test_is_allowed_chat_rejects_unknown_chat_with_no_binding():
    assert tg_bridge.is_allowed_chat(_update(chat_id=999), 42) is False


def test_is_allowed_chat_accepts_bound_supergroup():
    assert tg_bridge.is_allowed_chat(_update(chat_id=-100999), 42, bound_chat_id=-100999) is True


def test_is_allowed_chat_rejects_chat_that_is_neither():
    assert tg_bridge.is_allowed_chat(_update(chat_id=-100111), 42, bound_chat_id=-100999) is False


# ── 부트스트랩: 오너 본인의 발신자 id (2026-09-05) ───────────────────────────
# TELEGRAM_BRIDGE_CHAT_ID는 오너와의 1:1 개인 채팅이라 chat_id == 오너의
# user id다. 그래서 어느 채팅에서 왔든 message.from.id가 그 값과 같으면
# 오너 본인으로 보고 허용한다 — .env.local을 건드리지 않고도 새 슈퍼그룹의
# 새 토픽에서 곧바로 /here를 쳐서 최초 바인딩을 만들 수 있어야 하기 때문이다.

def test_is_allowed_chat_owner_user_id_in_foreign_chat_is_allowed():
    """오너(user id 42)가 아직 어디에도 안 묶인 새 슈퍼그룹에서 보낸 메시지."""
    update = _update(chat_id=-100999, from_id=42)
    assert tg_bridge.is_allowed_chat(update, 42) is True


def test_is_allowed_chat_other_user_id_in_foreign_chat_is_rejected():
    """같은 새 슈퍼그룹이라도 오너가 아닌 사람이 보내면 거부한다."""
    update = _update(chat_id=-100999, from_id=555)
    assert tg_bridge.is_allowed_chat(update, 42) is False


def test_is_allowed_chat_other_user_in_bound_supergroup_is_still_allowed():
    """일단 바인딩된 슈퍼그룹 안에서는 (오너가 아닌) 다른 멤버도 여전히
    허용된다 — chat 규칙(2번)이 우선 적용되는, 오너가 명시적으로 선택한
    완화다(크론 알림 답장·다른 식구의 조회 같은 용례)."""
    update = _update(chat_id=-100999, from_id=555)
    assert tg_bridge.is_allowed_chat(update, 42, bound_chat_id=-100999) is True


# ---------------------------------------------------------------------------
# process_update — 답장은 명령이 온 토픽으로, 슈퍼그룹은 바인딩되면 통과
# ---------------------------------------------------------------------------
def test_process_update_replies_in_the_same_topic(monkeypatch, tmp_path):
    monkeypatch.setattr(tg_bridge, "handle_control_command", lambda *a, **k: "재개됨(LIVE)")
    tg = RecordingTelegramClient()
    limiter = tg_bridge.RateLimiter()
    update = _update(chat_id=42, thread_id=7, text="/resume")

    tg_bridge.process_update(tg, 42, limiter, FakeControl(), FakeTossClient(), update,
                              lanes_path=tmp_path / "tg_lanes.json")

    assert tg.sent == [(42, "재개됨(LIVE)", 7)]


def test_process_update_legacy_chat_reply_has_no_thread(monkeypatch, tmp_path):
    monkeypatch.setattr(tg_bridge, "handle_control_command", lambda *a, **k: "재개됨(LIVE)")
    tg = RecordingTelegramClient()
    limiter = tg_bridge.RateLimiter()
    update = _update(chat_id=42, thread_id=None, text="/resume")

    tg_bridge.process_update(tg, 42, limiter, FakeControl(), FakeTossClient(), update,
                              lanes_path=tmp_path / "tg_lanes.json")

    assert tg.sent == [(42, "재개됨(LIVE)", None)]


def test_process_update_here_command_binds_and_replies_in_thread(tmp_path):
    """실제 부트스트랩 경로 재현(2026-09-05): 오너의 개인 채팅 chat_id(42, ==
    오너의 user id)는 `.env.local`을 그대로 둔다 — `/here`는 오너가 방금 만든
    새 슈퍼그룹(chat_id=-100999, 아직 아무 데도 안 묶임)의 새 토픽에서
    `from.id=42`(오너 본인)로 보낸 메시지라 곧바로 통과한다."""
    lanes_path = tmp_path / "tg_lanes.json"
    tg = RecordingTelegramClient()
    limiter = tg_bridge.RateLimiter()
    update = _update(chat_id=-100999, thread_id=7, text="/here 매매", from_id=42)

    tg_bridge.process_update(tg, 42, limiter, FakeControl(), FakeTossClient(), update,
                              lanes_path=lanes_path)

    assert len(tg.sent) == 1
    chat_id, text, thread_id = tg.sent[0]
    assert "매매" in text
    assert thread_id == 7
    assert tg_bridge.load_tg_lanes(lanes_path)["chat_id"] == -100999
    assert tg_bridge.load_tg_lanes(lanes_path)["threads"]["trades"] == 7


def test_process_update_here_command_from_non_owner_in_foreign_chat_is_ignored(tmp_path):
    """다른 사람이 아직 안 묶인 낯선 채팅에서 `/here`를 쳐도 조용히 무시된다 —
    오너 본인(from.id==42)이 아니면 최초 바인딩을 만들 수 없다."""
    lanes_path = tmp_path / "tg_lanes.json"
    tg = RecordingTelegramClient()
    limiter = tg_bridge.RateLimiter()
    update = _update(chat_id=-100999, thread_id=7, text="/here 매매", from_id=555)

    tg_bridge.process_update(tg, 42, limiter, FakeControl(), FakeTossClient(), update,
                              lanes_path=lanes_path)

    assert tg.sent == []
    assert tg_bridge.load_tg_lanes(lanes_path) == {}


def test_process_update_accepts_supergroup_once_bound(tmp_path):
    """`/here`로 한 번 바인딩되고 나면, 그 슈퍼그룹은 legacy chat_id와 별개로
    계속 허용된다(같은 chat_id 안의 다른 토픽에서 온 이후 명령들)."""
    lanes_path = tmp_path / "tg_lanes.json"
    tg_bridge.save_tg_lanes({"chat_id": -100999, "threads": {"trades": 7}}, lanes_path)

    tg = RecordingTelegramClient()
    limiter = tg_bridge.RateLimiter()
    update = _update(chat_id=-100999, thread_id=9, text="/watchlist")

    tg_bridge.process_update(tg, 42, limiter, FakeControl(), FakeTossClient(), update,
                              lanes_path=lanes_path)

    assert len(tg.sent) == 1  # 무시되지 않고 응답을 받았다
    assert tg.sent[0][2] == 9  # 그 토픽으로 답장


def test_process_update_rejects_chat_that_is_neither_legacy_nor_bound(tmp_path):
    lanes_path = tmp_path / "tg_lanes.json"
    tg_bridge.save_tg_lanes({"chat_id": -100999, "threads": {"trades": 7}}, lanes_path)

    tg = RecordingTelegramClient()
    limiter = tg_bridge.RateLimiter()
    update = _update(chat_id=-100111, thread_id=9, text="/watchlist")

    tg_bridge.process_update(tg, 42, limiter, FakeControl(), FakeTossClient(), update,
                              lanes_path=lanes_path)

    assert tg.sent == []  # 조용히 무시


def test_process_update_forwarded_post_replies_in_its_own_topic(monkeypatch, tmp_path):
    from quant.collect.sources import telegram_channels

    handle = telegram_channels.CHANNELS[0]["handle"]
    tg = RecordingTelegramClient()
    limiter = tg_bridge.RateLimiter()
    ledger_path = tmp_path / "telegram_msgs.jsonl"
    monkeypatch.setattr(tg_bridge, "TELEGRAM_LEDGER_PATH", ledger_path)

    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 42},
            "message_thread_id": 3,
            "text": "포워딩된 본문",
            "forward_origin": {"type": "channel", "chat": {"username": handle}, "message_id": 5, "date": 1},
        },
    }
    tg_bridge.process_update(tg, 42, limiter, FakeControl(), FakeTossClient(), update,
                              lanes_path=tmp_path / "tg_lanes.json")

    assert len(tg.sent) == 1
    assert tg.sent[0][2] == 3
