"""TelegramNotifier: disabled no-op, 4096자 truncation, circuit breaker."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from quant.adapters.notify.telegram import TelegramNotifier


def _ok_response() -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    return resp


def test_disabled_when_token_missing_is_noop():
    notifier = TelegramNotifier(None, "chat-id")
    assert notifier.enabled is False
    with patch("quant.adapters.notify.telegram.httpx.post") as mock_post:
        notifier.send("hello")
        mock_post.assert_not_called()


def test_disabled_when_chat_id_missing_is_noop():
    notifier = TelegramNotifier("token", None)
    assert notifier.enabled is False
    with patch("quant.adapters.notify.telegram.httpx.post") as mock_post:
        notifier.send("hello")
        mock_post.assert_not_called()


def test_truncates_at_4096_chars():
    notifier = TelegramNotifier("token", "chat-id")
    long_text = "a" * 5000
    with patch("quant.adapters.notify.telegram.httpx.post", return_value=_ok_response()) as mock_post:
        notifier.send(long_text)
    sent_text = mock_post.call_args.kwargs["json"]["text"]
    assert len(sent_text) == 4096
    assert sent_text.endswith("…")


def test_short_text_not_truncated():
    notifier = TelegramNotifier("token", "chat-id")
    with patch("quant.adapters.notify.telegram.httpx.post", return_value=_ok_response()) as mock_post:
        notifier.send("short message")
    sent_text = mock_post.call_args.kwargs["json"]["text"]
    assert sent_text == "short message"


def test_circuit_breaker_mutes_after_5_consecutive_failures():
    notifier = TelegramNotifier("token", "chat-id")
    with patch("quant.adapters.notify.telegram.httpx.post", side_effect=Exception("boom")) as mock_post:
        for _ in range(5):
            notifier.send("x")
        assert mock_post.call_count == 5
        assert notifier._muted_until > time.time()

        # 6th call: muted, must not hit the network again
        notifier.send("x")
        assert mock_post.call_count == 5


def test_failure_counter_resets_on_success():
    notifier = TelegramNotifier("token", "chat-id")
    with patch("quant.adapters.notify.telegram.httpx.post") as mock_post:
        mock_post.side_effect = [Exception("boom"), Exception("boom"), _ok_response()]
        notifier.send("x")
        notifier.send("x")
        assert notifier._consecutive_failures == 2
        notifier.send("x")
        assert notifier._consecutive_failures == 0


def test_send_never_raises_on_network_error():
    notifier = TelegramNotifier("token", "chat-id")
    with patch("quant.adapters.notify.telegram.httpx.post", side_effect=Exception("boom")):
        notifier.send("x")  # must not raise


# ── 타임아웃 1회 재시도 (2026-08-24) ─────────────────────────────────────────
# 실측: 8-21 포지션 현황 메시지가 ReadTimeout 한 번에 영구 유실됐다(발송 원장
# ok=False 1건). 타임아웃은 대부분 일시적이다 — **딱 1회** 즉시 재시도한다.
# 무한 재시도는 금지(뮤트 회로차단기와 충돌), 타임아웃 외 예외(4xx 등)는
# 재시도해도 같은 답이므로 재시도하지 않는다.

def test_timeout_retries_once_and_succeeds():
    n = TelegramNotifier("tkn", "chat")
    with patch(
        "quant.adapters.notify.telegram.httpx.post",
        side_effect=[httpx.ReadTimeout("t"), _ok_response()],
    ) as mock_post:
        n.send("hello")
    assert mock_post.call_count == 2
    assert n._consecutive_failures == 0


def test_timeout_retry_fails_counts_one_failure():
    n = TelegramNotifier("tkn", "chat")
    with patch(
        "quant.adapters.notify.telegram.httpx.post",
        side_effect=[httpx.ReadTimeout("t"), httpx.ReadTimeout("t")],
    ) as mock_post:
        n.send("hello")
    assert mock_post.call_count == 2  # 1회만 재시도 — 무한 아님
    assert n._consecutive_failures == 1


def test_non_timeout_error_is_not_retried():
    n = TelegramNotifier("tkn", "chat")
    with patch(
        "quant.adapters.notify.telegram.httpx.post",
        side_effect=Exception("400 Bad Request"),
    ) as mock_post:
        n.send("hello")
    assert mock_post.call_count == 1


# ── parse_mode=HTML + 평문 폴백 (2026-09-04, L1 서식) ──────────────────────────
# 엔진 메시지가 quant.core.tgfmt로 만든 <b>/<code> 태그를 담는다 — 이스케이프를
# 놓친 태그 하나가 400을 유발해도 알림 자체는 나가야 한다(서식 버그가 손절
# 알림을 삼키면 안 된다).

def _bad_request_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 400
    resp.raise_for_status.return_value = None
    return resp


def test_send_uses_html_parse_mode_by_default():
    n = TelegramNotifier("tkn", "chat")
    with patch(
        "quant.adapters.notify.telegram.httpx.post", return_value=_ok_response(),
    ) as mock_post:
        n.send("<b>제목</b>")
    assert mock_post.call_count == 1
    assert mock_post.call_args.kwargs["json"]["parse_mode"] == "HTML"
    assert mock_post.call_args.kwargs["json"]["text"] == "<b>제목</b>"


def test_html_parse_failure_falls_back_to_plain_text():
    n = TelegramNotifier("tkn", "chat")
    with patch(
        "quant.adapters.notify.telegram.httpx.post",
        side_effect=[_bad_request_response(), _ok_response()],
    ) as mock_post:
        n.send("<b>깨진 태그")
    assert mock_post.call_count == 2
    first_json = mock_post.call_args_list[0].kwargs["json"]
    second_json = mock_post.call_args_list[1].kwargs["json"]
    assert first_json["parse_mode"] == "HTML"
    assert "parse_mode" not in second_json
    assert second_json["text"] == "<b>깨진 태그"
    # 폴백이 성공했으므로 알림 유실이 아니다 — 실패로 집계하지 않는다.
    assert n._consecutive_failures == 0


def test_html_parse_failure_then_plain_http_error_counts_as_failure():
    n = TelegramNotifier("tkn", "chat")
    real_bad = MagicMock()
    real_bad.status_code = 400
    real_bad.raise_for_status.side_effect = httpx.HTTPStatusError(
        "400", request=MagicMock(), response=real_bad,
    )
    with patch(
        "quant.adapters.notify.telegram.httpx.post",
        side_effect=[real_bad, real_bad],
    ) as mock_post:
        n.send("<b>깨진 태그")
    assert mock_post.call_count == 2
    assert n._consecutive_failures == 1


# ── 레인(포럼 토픽) 라우팅 (2026-09-05) ────────────────────────────────────────
# data/state/tg_lanes.json 매핑에 따라 message_thread_id 를 붙이거나(바인딩됨),
# 레거시 chat_id 로 폴백하며 헤더를 붙인다(다른 레인이라도 이미 바인딩된 뒤).


def _lanes_file(tmp_path: Path, mapping: dict | None) -> Path:
    path = tmp_path / "tg_lanes.json"
    if mapping is not None:
        path.write_text(json.dumps(mapping), encoding="utf-8")
    return path


def test_send_without_lane_is_unchanged(tmp_path: Path):
    """lane 을 안 주면 기존 동작 그대로 — message_thread_id 도, 헤더도 없다."""
    n = TelegramNotifier("tkn", "chat", lanes_path=_lanes_file(tmp_path, {"chat_id": 111, "threads": {"trades": 42}}))
    with patch("quant.adapters.notify.telegram.httpx.post", return_value=_ok_response()) as mock_post:
        n.send("hello")
    payload = mock_post.call_args.kwargs["json"]
    assert payload["chat_id"] == "chat"
    assert payload["text"] == "hello"
    assert "message_thread_id" not in payload


def test_send_with_bound_lane_routes_to_thread_no_header(tmp_path: Path):
    mapping = {"chat_id": 111, "threads": {"trades": 42}}
    n = TelegramNotifier("tkn", "chat", lanes_path=_lanes_file(tmp_path, mapping))
    with patch("quant.adapters.notify.telegram.httpx.post", return_value=_ok_response()) as mock_post:
        n.send("체결 알림", lane="trades")
    payload = mock_post.call_args.kwargs["json"]
    assert payload["chat_id"] == 111
    assert payload["message_thread_id"] == 42
    assert payload["text"] == "체결 알림"  # 토픽 안에서는 헤더를 붙이지 않는다


def test_send_with_unbound_lane_falls_back_before_any_binding(tmp_path: Path):
    """매핑 파일 자체가 없으면(마이그레이션 이전) 완전히 기존과 동일 — 헤더도 없다."""
    n = TelegramNotifier("tkn", "chat", lanes_path=tmp_path / "missing.json")
    with patch("quant.adapters.notify.telegram.httpx.post", return_value=_ok_response()) as mock_post:
        n.send("체결 알림", lane="trades")
    payload = mock_post.call_args.kwargs["json"]
    assert payload["chat_id"] == "chat"
    assert "message_thread_id" not in payload
    assert payload["text"] == "체결 알림"


def test_send_with_unbound_lane_after_some_binding_gets_header(tmp_path: Path):
    """"briefs"는 아직 안 묶였지만 "trades"는 묶여 있다 — 레거시로 떨어지는
    briefs 메시지는 헤더로 자기가 누군지 밝혀야 한다(섞인 방)."""
    mapping = {"chat_id": 111, "threads": {"trades": 42}}
    n = TelegramNotifier("tkn", "chat", lanes_path=_lanes_file(tmp_path, mapping))
    with patch("quant.adapters.notify.telegram.httpx.post", return_value=_ok_response()) as mock_post:
        n.send("아침 브리핑", lane="briefs")
    payload = mock_post.call_args.kwargs["json"]
    assert payload["chat_id"] == "chat"
    assert "message_thread_id" not in payload
    assert payload["text"] == "📰 브리핑\n아침 브리핑"


def test_send_lane_html_fallback_preserves_thread_id(tmp_path: Path):
    """HTML 파싱 실패 → 평문 재시도에도 message_thread_id 는 그대로 남는다."""
    mapping = {"chat_id": 111, "threads": {"ops": 7}}
    n = TelegramNotifier("tkn", "chat", lanes_path=_lanes_file(tmp_path, mapping))
    with patch(
        "quant.adapters.notify.telegram.httpx.post",
        side_effect=[_bad_request_response(), _ok_response()],
    ) as mock_post:
        n.send("<b>깨진 태그", lane="ops")
    assert mock_post.call_count == 2
    first_json = mock_post.call_args_list[0].kwargs["json"]
    second_json = mock_post.call_args_list[1].kwargs["json"]
    assert first_json["message_thread_id"] == 7
    assert second_json["message_thread_id"] == 7
    assert "parse_mode" not in second_json
