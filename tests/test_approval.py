"""ApprovalGate(상태 기계·영속화·멱등)와 TelegramApprovalBot(Bot API 계약).

승인 게이트의 **의미론**(청산은 승인을 거치지 않는다, fail-closed, 가격 드리프트
재검증)은 여기가 아니라 tests/e2e/test_approval_gate.py에서 loop.py를 실제로
구동해 검증한다 — 이 파일은 저장소 그 자체와 텔레그램 어댑터만 다룬다.
실제 네트워크는 절대 호출하지 않는다(httpx.post를 전부 패치).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from quant.trade.approval import ApprovalGate, ApprovalRequest
from quant.adapters.notify.telegram_approval import TelegramApprovalBot


def _create(gate: ApprovalGate, symbol: str = "TQQQ", now: float | None = None) -> ApprovalRequest:
    return gate.create(
        strategy_id="donchian", symbol=symbol, action="enter_long",
        qty=3.0, price=100.0, reason="돌파", target_weight=1.0, now=now,
    )


# --------------------------------------------------------------------- 상태 기계

def test_create_starts_pending(tmp_path):
    gate = ApprovalGate(state_path=tmp_path / "approvals.json")
    req = _create(gate)
    assert req.status == "pending"
    assert [r.id for r in gate.pending()] == [req.id]


def test_approve_then_take_marks_executed_once(tmp_path):
    """멱등의 핵심: take_approved()는 one-shot이다 — 두 번 불러도 두 번 사지 않는다."""
    gate = ApprovalGate(state_path=tmp_path / "approvals.json")
    req = _create(gate)
    assert gate.decide(req.id, True) is not None

    taken = gate.take_approved()
    assert [r.id for r in taken] == [req.id]
    assert gate.take_approved() == []
    assert gate.get(req.id).status == "executed"


def test_double_decision_is_noop(tmp_path):
    """사용자가 버튼을 두 번 눌러도 두 번째 콜백은 None — 두 번 사지 않는다."""
    gate = ApprovalGate(state_path=tmp_path / "approvals.json")
    req = _create(gate)
    assert gate.decide(req.id, True) is not None
    assert gate.decide(req.id, True) is None
    assert gate.decide(req.id, False) is None  # 승인 뒤 거절도 무시
    assert len(gate.take_approved()) == 1


def test_reject_never_becomes_executable(tmp_path):
    gate = ApprovalGate(state_path=tmp_path / "approvals.json")
    req = _create(gate)
    decided = gate.decide(req.id, False)
    assert decided.status == "rejected"
    assert gate.take_approved() == []


def test_decide_on_unknown_id_is_noop(tmp_path):
    gate = ApprovalGate(state_path=tmp_path / "approvals.json")
    assert gate.decide("deadbeef", True) is None


def test_expired_request_cannot_be_approved(tmp_path):
    """만료 뒤 도착한 승인 콜백은 no-op이어야 한다 — 만료된 신호는 다른 거래다."""
    gate = ApprovalGate(state_path=tmp_path / "approvals.json")
    req = _create(gate, now=1000.0)

    expired = gate.expire_stale(expire_seconds=300, now=1400.0)
    assert [r.id for r in expired] == [req.id]

    assert gate.decide(req.id, True) is None
    assert gate.take_approved() == []
    assert gate.get(req.id).status == "expired"


def test_expire_stale_leaves_fresh_requests_alone(tmp_path):
    gate = ApprovalGate(state_path=tmp_path / "approvals.json")
    old = _create(gate, symbol="TQQQ", now=1000.0)
    fresh = _create(gate, symbol="SQQQ", now=1350.0)

    expired = gate.expire_stale(expire_seconds=300, now=1400.0)

    assert [r.id for r in expired] == [old.id]
    assert [r.id for r in gate.pending()] == [fresh.id]


def test_expire_marks_single_pending_and_is_idempotent(tmp_path):
    gate = ApprovalGate(state_path=tmp_path / "approvals.json")
    req = _create(gate)
    assert gate.expire(req.id).status == "expired"
    assert gate.expire(req.id) is None


def test_set_message_id_persists(tmp_path):
    gate = ApprovalGate(state_path=tmp_path / "approvals.json")
    req = _create(gate)
    gate.set_message_id(req.id, 4242)
    assert gate.get(req.id).message_id == 4242


def test_prune_removes_only_settled_old_records(tmp_path):
    gate = ApprovalGate(state_path=tmp_path / "approvals.json")
    settled = _create(gate, symbol="TQQQ", now=1000.0)
    still_pending = _create(gate, symbol="SQQQ", now=1000.0)
    gate.decide(settled.id, False, now=1000.0)

    removed = gate.prune(keep_seconds=86400, now=1000.0 + 90000)

    assert removed == 1
    # pending은 아무리 오래돼도 지우지 않는다 — 지우면 사용자 버튼이 조용한 no-op이 된다
    assert [r.id for r in gate.all()] == [still_pending.id]


# --------------------------------------------------------------------- 영속화

def test_state_survives_restart_and_expiry_uses_stored_created_at(tmp_path):
    """엔진이 재시작해도 만료 시계가 리셋되면 안 된다 — created_at은 디스크에서 온다."""
    path = tmp_path / "approvals.json"
    first = ApprovalGate(state_path=path)
    req = _create(first, now=1000.0)

    restarted = ApprovalGate(state_path=path)
    assert [r.id for r in restarted.pending()] == [req.id]
    assert restarted.get(req.id).created_at == 1000.0

    expired = restarted.expire_stale(expire_seconds=300, now=1400.0)
    assert [r.id for r in expired] == [req.id]


def test_approved_but_unconsumed_request_survives_restart(tmp_path):
    path = tmp_path / "approvals.json"
    first = ApprovalGate(state_path=path)
    req = _create(first)
    first.decide(req.id, True)

    restarted = ApprovalGate(state_path=path)
    assert [r.id for r in restarted.take_approved()] == [req.id]
    assert ApprovalGate(state_path=path).take_approved() == []


def test_missing_state_file_is_treated_as_empty(tmp_path):
    gate = ApprovalGate(state_path=tmp_path / "nope" / "approvals.json")
    assert gate.pending() == []
    assert gate.take_approved() == []


def test_unknown_fields_in_state_file_are_ignored(tmp_path):
    """스키마가 늘어난 뒤 남은 낡은 레코드가 기동을 막으면 안 된다."""
    path = tmp_path / "approvals.json"
    path.write_text(
        '{"requests": [{"id": "abc", "strategy_id": "s", "symbol": "TQQQ",'
        ' "action": "enter_long", "qty": 1, "price": 10, "reason": "",'
        ' "created_at": 1.0, "legacy_field": 123}]}',
        encoding="utf-8",
    )
    gate = ApprovalGate(state_path=path)
    assert [r.id for r in gate.pending()] == ["abc"]


# --------------------------------------------------------------------- 텔레그램 어댑터

def _ok(result) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"ok": True, "result": result}
    return resp


def test_callback_data_fits_telegram_64_byte_limit(tmp_path):
    """callback_data는 1~64바이트 하드 제한 — 넘으면 사용자가 누른 버튼이 엉뚱한
    요청을 가리킬 수 있다."""
    gate = ApprovalGate(state_path=tmp_path / "approvals.json")
    req = _create(gate)
    bot = TelegramApprovalBot("token", "chat")

    with patch("quant.adapters.notify.telegram_approval.httpx.post",
               return_value=_ok({"message_id": 7})) as mock_post:
        assert bot.send_request(req, expire_seconds=300) == 7

    markup = mock_post.call_args.kwargs["json"]["reply_markup"]
    buttons = markup["inline_keyboard"][0]
    assert len(buttons) == 2
    for button in buttons:
        assert 1 <= len(button["callback_data"].encode("utf-8")) <= 64
    assert {b["callback_data"] for b in buttons} == {f"ok:{req.id}", f"no:{req.id}"}


def test_send_request_returns_none_on_network_failure(tmp_path):
    """전송 실패는 None — 호출부가 이걸 보고 진입을 취소한다(fail-closed)."""
    gate = ApprovalGate(state_path=tmp_path / "approvals.json")
    req = _create(gate)
    bot = TelegramApprovalBot("token", "chat")
    with patch("quant.adapters.notify.telegram_approval.httpx.post", side_effect=Exception("boom")):
        assert bot.send_request(req, expire_seconds=300) is None


def test_disabled_bot_never_touches_network(tmp_path):
    gate = ApprovalGate(state_path=tmp_path / "approvals.json")
    req = _create(gate)
    bot = TelegramApprovalBot(None, "chat")
    assert bot.enabled is False
    with patch("quant.adapters.notify.telegram_approval.httpx.post") as mock_post:
        assert bot.send_request(req, expire_seconds=300) is None
        assert bot.poll_decisions() == []
        mock_post.assert_not_called()


def test_poll_decisions_parses_callbacks_and_answers_every_one():
    """answerCallbackQuery는 필수다 — 안 부르면 사용자 화면의 버튼이 계속 로딩 상태로 남는다."""
    bot = TelegramApprovalBot("token", "chat")
    updates = _ok([
        {"update_id": 10, "callback_query": {"id": "cb1", "data": "ok:abc123"}},
        {"update_id": 11, "callback_query": {"id": "cb2", "data": "no:def456"}},
    ])
    with patch("quant.adapters.notify.telegram_approval.httpx.post",
               side_effect=[updates, _ok(True), _ok(True)]) as mock_post:
        assert bot.poll_decisions() == [("abc123", True), ("def456", False)]

    methods = [call.args[0] for call in mock_post.call_args_list]
    assert methods[0].endswith("/getUpdates")
    assert [m.rsplit("/", 1)[-1] for m in methods[1:]] == ["answerCallbackQuery", "answerCallbackQuery"]


def test_poll_decisions_advances_offset_to_max_update_id_plus_one():
    """offset = max(update_id)+1. 확정하지 않으면 같은 콜백을 매 사이클 다시 받는다."""
    bot = TelegramApprovalBot("token", "chat")
    first = _ok([{"update_id": 41, "callback_query": {"id": "cb", "data": "ok:abc"}}])
    with patch("quant.adapters.notify.telegram_approval.httpx.post",
               side_effect=[first, _ok(True)]) as mock_post:
        bot.poll_decisions()
    assert mock_post.call_args_list[0].kwargs["json"]["offset"] == 0

    with patch("quant.adapters.notify.telegram_approval.httpx.post",
               return_value=_ok([])) as mock_post:
        bot.poll_decisions()
    assert mock_post.call_args.kwargs["json"]["offset"] == 42


def test_poll_decisions_filters_to_callback_query_updates():
    bot = TelegramApprovalBot("token", "chat")
    with patch("quant.adapters.notify.telegram_approval.httpx.post",
               return_value=_ok([])) as mock_post:
        bot.poll_decisions()
    assert mock_post.call_args.kwargs["json"]["allowed_updates"] == ["callback_query"]


def test_poll_decisions_swallows_network_errors():
    bot = TelegramApprovalBot("token", "chat")
    with patch("quant.adapters.notify.telegram_approval.httpx.post", side_effect=Exception("boom")):
        assert bot.poll_decisions() == []


def test_finalize_removes_buttons_then_edits_text():
    bot = TelegramApprovalBot("token", "chat")
    with patch("quant.adapters.notify.telegram_approval.httpx.post",
               return_value=_ok(True)) as mock_post:
        bot.finalize(99, "수락됨")

    methods = [call.args[0].rsplit("/", 1)[-1] for call in mock_post.call_args_list]
    assert methods == ["editMessageReplyMarkup", "editMessageText"]
    # reply_markup을 생략해야 인라인 키보드가 사라진다 — 남아 있으면 재클릭을 유도한다
    assert "reply_markup" not in mock_post.call_args_list[0].kwargs["json"]


def test_from_env_warns_when_sharing_bridge_token(monkeypatch, caplog):
    """같은 토큰을 두 getUpdates 폴러가 읽으면 서로의 업데이트를 훔친다."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "same-token")
    monkeypatch.setenv("TELEGRAM_BRIDGE_BOT_TOKEN", "same-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    with caplog.at_level("WARNING", logger="quant.adapters.notify.telegram_approval"):
        TelegramApprovalBot.from_env()
    assert any("BRIDGE" in r.message for r in caplog.records)


def test_from_env_silent_when_tokens_differ(monkeypatch, caplog):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "notify-token")
    monkeypatch.setenv("TELEGRAM_BRIDGE_BOT_TOKEN", "bridge-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    with caplog.at_level("WARNING", logger="quant.adapters.notify.telegram_approval"):
        bot = TelegramApprovalBot.from_env()
    assert bot.enabled is True
    assert not caplog.records
