"""tg_bridge.py의 포워드된 채널 게시물 처리(2026-09-05, "포워딩 우회").

오너 결정: 웹 프리뷰가 본문을 못 주는 채널(clawnewssummary, text_not_supported —
`quant/collect/sources/telegram_channels.py` 모듈독스트링 참고)은 오너가 폰에서
직접 봇 채팅으로 포워딩한다. 텔레그램/Toss API는 전부 mock — 실 네트워크 호출 없음.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "server" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import tg_bridge  # noqa: E402
from quant.collect.sources.telegram_channels import load_ledger  # noqa: E402


class RecordingTelegramClient:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.typed: list[int] = []

    def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    def send_typing(self, chat_id: int) -> None:
        self.typed.append(chat_id)


class FakeControl:
    def is_halted(self):
        return False


class FakeTossClient:
    def stock_info(self, symbol: str) -> dict:
        return {"name": "미검증"}


def _new_api_forward_update(username: str, message_id: int, date: int = 1700000000,
                             text: str | None = "속보: 금리 동결", caption: str | None = None,
                             chat_id: int = 42, update_id: int = 1) -> dict:
    message: dict = {
        "chat": {"id": chat_id},
        "forward_origin": {"type": "channel", "chat": {"username": username},
                            "message_id": message_id, "date": date},
    }
    if text is not None:
        message["text"] = text
    if caption is not None:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}


def _legacy_forward_update(username: str, message_id: int, date: int = 1700000000,
                            text: str = "속보: 금리 동결", chat_id: int = 42, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "text": text,
            "forward_from_chat": {"type": "channel", "username": username},
            "forward_from_message_id": message_id,
            "forward_date": date,
        },
    }


# ---------------------------------------------------------------------------
# _forward_channel_origin — 신형/구형 Bot API 파싱
# ---------------------------------------------------------------------------


def test_forward_channel_origin_parses_new_bot_api_shape():
    message = _new_api_forward_update("clawnewssummary", 555)["message"]
    origin = tg_bridge._forward_channel_origin(message)
    assert origin == {"username": "clawnewssummary", "message_id": "555", "date": 1700000000}


def test_forward_channel_origin_parses_legacy_shape():
    message = _legacy_forward_update("clawnewssummary", 777)["message"]
    origin = tg_bridge._forward_channel_origin(message)
    assert origin == {"username": "clawnewssummary", "message_id": "777", "date": 1700000000}


def test_forward_channel_origin_none_for_non_forwarded_message():
    assert tg_bridge._forward_channel_origin({"text": "그냥 대화"}) is None


def test_forward_channel_origin_none_for_forward_from_user():
    message = {"text": "안녕", "forward_origin": {"type": "user", "sender_user": {"id": 1}}}
    assert tg_bridge._forward_channel_origin(message) is None


def test_forward_channel_origin_username_none_for_private_channel():
    message = {"text": "x", "forward_origin": {"type": "channel", "chat": {}, "message_id": 1}}
    origin = tg_bridge._forward_channel_origin(message)
    assert origin == {"username": None, "message_id": "1", "date": None}


# ---------------------------------------------------------------------------
# _match_forward_channel_handle — 대소문자 무관
# ---------------------------------------------------------------------------


def test_match_forward_channel_handle_is_case_insensitive():
    assert tg_bridge._match_forward_channel_handle("ClawNewsSummary") == "clawnewssummary"
    assert tg_bridge._match_forward_channel_handle("SAMSUNG_GLOBAL_AI_SW") == "Samsung_Global_AI_SW"


def test_match_forward_channel_handle_unknown_returns_none():
    assert tg_bridge._match_forward_channel_handle("some_random_channel") is None


# ---------------------------------------------------------------------------
# _handle_forwarded_channel_post — 저장 + 응답
# ---------------------------------------------------------------------------


def test_forwarded_post_stored_once(tmp_path):
    path = tmp_path / "telegram_msgs.jsonl"
    tg = RecordingTelegramClient()
    message = _new_api_forward_update("clawnewssummary", 100, text="속보: 반도체 수급 개선")["message"]

    tg_bridge._handle_forwarded_channel_post(tg, 42, message, path=path)

    rows = load_ledger(path)
    assert len(rows) == 1
    assert rows[0]["handle"] == "clawnewssummary"
    assert rows[0]["msg_id"] == "100"
    assert rows[0]["text"] == "속보: 반도체 수급 개선"
    assert rows[0]["published"] == "2023-11-14T22:13:20+00:00"
    assert tg.sent == [(42, "📥 clawnewssummary 저장 (1)")]


def test_reforward_is_deduped_and_reports_zero(tmp_path):
    path = tmp_path / "telegram_msgs.jsonl"
    tg = RecordingTelegramClient()
    message = _new_api_forward_update("clawnewssummary", 100)["message"]

    tg_bridge._handle_forwarded_channel_post(tg, 42, message, path=path)
    tg_bridge._handle_forwarded_channel_post(tg, 42, message, path=path)

    rows = load_ledger(path)
    assert len(rows) == 1  # 중복 없음
    assert tg.sent[0][1] == "📥 clawnewssummary 저장 (1)"
    assert tg.sent[1][1] == "📥 clawnewssummary 저장 (0)"


def test_non_registry_origin_ignored_silently(tmp_path):
    path = tmp_path / "telegram_msgs.jsonl"
    tg = RecordingTelegramClient()
    message = _new_api_forward_update("some_random_channel", 1)["message"]

    tg_bridge._handle_forwarded_channel_post(tg, 42, message, path=path)

    assert not path.exists()
    assert tg.sent == []  # 조용히 무시 — 답장 없음


def test_forward_with_unknown_username_ignored_silently(tmp_path):
    path = tmp_path / "telegram_msgs.jsonl"
    tg = RecordingTelegramClient()
    message = {
        "chat": {"id": 42},
        "forward_origin": {"type": "channel", "chat": {}, "message_id": 5},
    }

    tg_bridge._handle_forwarded_channel_post(tg, 42, message, path=path)

    assert not path.exists()
    assert tg.sent == []


def test_photo_forward_stores_caption_as_text(tmp_path):
    path = tmp_path / "telegram_msgs.jsonl"
    tg = RecordingTelegramClient()
    message = _new_api_forward_update(
        "clawnewssummary", 200, text=None, caption="사진 캡션: 코스피 상승 마감",
    )["message"]

    tg_bridge._handle_forwarded_channel_post(tg, 42, message, path=path)

    rows = load_ledger(path)
    assert rows[0]["text"] == "사진 캡션: 코스피 상승 마감"


def test_legacy_forward_shape_also_stored(tmp_path):
    path = tmp_path / "telegram_msgs.jsonl"
    tg = RecordingTelegramClient()
    message = _legacy_forward_update("clawnewssummary", 300)["message"]

    tg_bridge._handle_forwarded_channel_post(tg, 42, message, path=path)

    rows = load_ledger(path)
    assert rows[0]["msg_id"] == "300"


def test_forward_row_schema_matches_append_ledger_convention(tmp_path):
    """append_ledger가 기대하는 스키마(handle/msg_id/text/published/links/images)
    그대로여야 telegram_channels.load_window/기존 소비자가 그대로 읽는다."""
    path = tmp_path / "telegram_msgs.jsonl"
    tg = RecordingTelegramClient()
    message = _new_api_forward_update("clawnewssummary", 400)["message"]

    tg_bridge._handle_forwarded_channel_post(tg, 42, message, path=path)

    raw = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert set(raw.keys()) == {"handle", "msg_id", "text", "published", "links", "images"}
    assert raw["links"] == []
    assert raw["images"] == []


# ---------------------------------------------------------------------------
# process_update — 포워드는 명령/일반 대화 라우팅을 우회한다
# ---------------------------------------------------------------------------


def test_process_update_routes_forwarded_post_and_skips_command_handling(tmp_path, monkeypatch):
    ledger_path = tmp_path / "telegram_msgs.jsonl"
    monkeypatch.setattr(tg_bridge, "TELEGRAM_LEDGER_PATH", ledger_path)

    def _boom(*args, **kwargs):
        raise AssertionError("포워드된 메시지는 claude 서브프로세스를 호출하면 안 된다")

    monkeypatch.setattr(tg_bridge, "run_claude", _boom)
    tg = RecordingTelegramClient()
    limiter = tg_bridge.RateLimiter()
    control = FakeControl()
    toss = FakeTossClient()
    update = _new_api_forward_update("clawnewssummary", 999, text="속보")

    tg_bridge.process_update(tg, 42, limiter, control, toss, update)

    assert tg.sent == [(42, "📥 clawnewssummary 저장 (1)")]
    rows = load_ledger(ledger_path)
    assert len(rows) == 1


def test_process_update_forwarded_message_with_slash_text_not_treated_as_command(tmp_path, monkeypatch):
    """포워드된 메시지의 본문이 우연히 '/'로 시작해도 명령으로 처리하면 안 된다."""
    ledger_path = tmp_path / "telegram_msgs.jsonl"
    monkeypatch.setattr(tg_bridge, "TELEGRAM_LEDGER_PATH", ledger_path)
    watch_path = tmp_path / "watchlist.yaml"
    monkeypatch.setattr(tg_bridge, "WATCHLIST_PATH", watch_path)

    tg = RecordingTelegramClient()
    limiter = tg_bridge.RateLimiter()
    control = FakeControl()
    toss = FakeTossClient()
    update = _new_api_forward_update("clawnewssummary", 1000, text="/watch AAPL 처럼 보이는 뉴스 문구")

    tg_bridge.process_update(tg, 42, limiter, control, toss, update)

    assert not watch_path.exists()  # 관심종목 명령으로 처리되지 않았다
    assert tg.sent == [(42, "📥 clawnewssummary 저장 (1)")]


def test_process_update_non_forwarded_command_unaffected(tmp_path, monkeypatch):
    watch_path = tmp_path / "watchlist.yaml"
    monkeypatch.setattr(tg_bridge, "WATCHLIST_PATH", watch_path)
    tg = RecordingTelegramClient()
    limiter = tg_bridge.RateLimiter()
    control = FakeControl()
    toss = FakeTossClient()
    update = {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/watch AAPL"}}

    tg_bridge.process_update(tg, 42, limiter, control, toss, update)

    entries = tg_bridge.load_watchlist(watch_path)
    assert entries[0]["symbol"] == "AAPL"


def test_process_update_non_forwarded_chat_unaffected(monkeypatch):
    monkeypatch.setattr(tg_bridge, "run_claude", lambda prompt, cwd=None: (True, "안녕하세요"))
    tg = RecordingTelegramClient()
    limiter = tg_bridge.RateLimiter()
    control = FakeControl()
    toss = FakeTossClient()
    update = {"update_id": 1, "message": {"chat": {"id": 42}, "text": "오늘 시장 어때?"}}

    tg_bridge.process_update(tg, 42, limiter, control, toss, update)

    assert tg.sent == [(42, "안녕하세요")]
