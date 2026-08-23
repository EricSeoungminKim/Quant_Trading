"""TelegramNotifier: disabled no-op, 4096자 truncation, circuit breaker."""
from __future__ import annotations

import time
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
