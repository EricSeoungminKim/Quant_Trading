"""Telegram 알림 어댑터 — domain.interfaces.Notifier 구현체.

best-effort 전용: 네트워크 실패가 거래 루프에 영향을 주면 안 된다 (Notifier 프로토콜
규칙 — 모든 예외는 이 안에서 삼킨다). TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID가 비어 있으면
disabled(모든 호출이 no-op) — 봇 등록 전에도 안전하게 배포 가능.

Ported from stock-algo-trade/engine/notify/telegram.py (circuit breaker, 4096자
truncate). 포트폴리오 포맷팅 메서드(notify_fill 등)는 이식하지 않는다 — 이 리포는
아직 그 도메인 타입이 없고, Notifier 프로토콜은 send()만 요구한다.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_LEN = 4096              # Telegram sendMessage text limit
_FAILURE_LIMIT = 5           # consecutive failures before muting
_MUTE_SECONDS = 10 * 60      # mute duration once the breaker trips
# 보낸 메시지 원장. Bot API 의 getUpdates 는 봇에게 **온** 메시지만 주므로,
# 우리가 **보낸** 것은 우리가 남기지 않으면 어디에도 없다. 판단 워치독
# (quant/control/ops_judge.py)이 "우리가 뭐라고 보냈는가"를 근거로 삼으려면
# 이 원장이 있어야 한다 — 2026-08-19 소유자 요구("텔레그램에 와 있는 문제들을
# 보고 점검")의 전제다. 실제로 그날 "장 마감까지 보유" 오문구를 사람이 읽고서야
# 발견했다.
_LEDGER_PATH = Path("data/ledger/notifications.jsonl")


class TelegramNotifier:
    """token/chat_id가 falsy면 self.enabled=False, send()는 no-op."""

    def __init__(self, token: str | None, chat_id: str | None) -> None:
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)
        self._consecutive_failures = 0
        self._muted_until = 0.0

    @classmethod
    def from_env(cls) -> "TelegramNotifier":
        return cls(os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"))

    def send(self, text: str) -> None:
        """POST to Telegram. Any exception -> one WARNING line, swallowed.
        After 5 straight failures, mute sends for 10 minutes (time-based),
        then retry naturally on the next call — no unbounded retries."""
        if not self.enabled:
            return
        now = time.time()
        if now < self._muted_until:
            return
        if len(text) > _MAX_LEN:
            text = text[: _MAX_LEN - 1] + "…"
        try:
            resp = httpx.post(
                _SEND_URL.format(token=self.token),
                json={"chat_id": self.chat_id, "text": text},
                timeout=3.0,
            )
            resp.raise_for_status()
            self._consecutive_failures = 0
            self._record(text, ok=True)
        except Exception as e:
            self._consecutive_failures += 1
            self._record(text, ok=False, error=f"{type(e).__name__}: {e}")
            logger.warning("Telegram 전송 실패 (연속 %d회): %s: %s",
                            self._consecutive_failures, type(e).__name__, e)
            if self._consecutive_failures >= _FAILURE_LIMIT:
                self._muted_until = now + _MUTE_SECONDS
                self._consecutive_failures = 0
                logger.warning("Telegram %d회 연속 실패 — %d분간 알림 중단",
                                _FAILURE_LIMIT, _MUTE_SECONDS // 60)

    def _record(self, text: str, *, ok: bool, error: str | None = None) -> None:
        """보낸 메시지를 원장에 남긴다. **실패해도 삼킨다** — 기록이 알림을 막으면
        본말전도다(Notifier 프로토콜 규칙: 모든 예외는 이 안에서 삼킨다)."""
        try:
            row = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "ok": ok,
                "text": text,
            }
            if error:
                row["error"] = error
            _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _LEDGER_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            logger.debug("알림 원장 기록 실패(무시): %s", e)
