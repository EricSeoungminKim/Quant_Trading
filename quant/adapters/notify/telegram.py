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

from quant.core import tglanes

logger = logging.getLogger(__name__)

_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_LEN = 4096              # Telegram sendMessage text limit
_FAILURE_LIMIT = 5           # consecutive failures before muting
_MUTE_SECONDS = 10 * 60      # mute duration once the breaker trips
# 레인(포럼 토픽) 매핑 — 브리지의 `/here`가 쓰고(server/scripts/tg_bridge.py),
# 여기서는 읽기만 한다. 매핑이 없거나 이 레인이 아직 안 묶였으면 `tglanes.resolve`
# 가 레거시 `chat_id`로 폴백한다(quant/core/tglanes.py 모듈독스트링 참고).
_LANES_PATH = Path("data/state/tg_lanes.json")
# 보낸 메시지 원장. Bot API 의 getUpdates 는 봇에게 **온** 메시지만 주므로,
# 우리가 **보낸** 것은 우리가 남기지 않으면 어디에도 없다. 판단 워치독
# (quant/control/ops_judge.py)이 "우리가 뭐라고 보냈는가"를 근거로 삼으려면
# 이 원장이 있어야 한다 — 2026-08-19 소유자 요구("텔레그램에 와 있는 문제들을
# 보고 점검")의 전제다. 실제로 그날 "장 마감까지 보유" 오문구를 사람이 읽고서야
# 발견했다.
_LEDGER_PATH = Path("data/ledger/notifications.jsonl")


class TelegramNotifier:
    """token/chat_id가 falsy면 self.enabled=False, send()는 no-op."""

    def __init__(
        self, token: str | None, chat_id: str | None, lanes_path: Path | None = None,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)
        self._consecutive_failures = 0
        self._muted_until = 0.0
        # 테스트가 매핑 파일 위치를 주입할 수 있게 — 실제 경로 기본값은 _LANES_PATH.
        self.lanes_path = lanes_path or _LANES_PATH

    @classmethod
    def from_env(cls) -> "TelegramNotifier":
        return cls(os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"))

    def _load_lane_mapping(self) -> dict | None:
        """`data/state/tg_lanes.json`을 best-effort로 읽는다. 없거나 깨졌으면
        `None` — `tglanes.resolve`가 그걸 "아직 바인딩 안 됨"으로 취급해 레거시
        채팅으로 폴백한다(알림이 이 파일 하나 때문에 죽으면 안 된다)."""
        try:
            return json.loads(self.lanes_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def send(self, text: str, lane: str | None = None) -> None:
        """POST to Telegram. Any exception -> one WARNING line, swallowed.
        After 5 straight failures, mute sends for 10 minutes (time-based),
        then retry naturally on the next call — no unbounded retries.

        parse_mode=HTML(2026-09-04, L1 서식)로 먼저 보낸다 — 엔진 메시지가
        `quant.core.tgfmt`로 만든 `<b>`/`<code>` 태그를 담고 있어서다. 텔레그램이
        태그 불균형·미지원 엔티티를 400으로 거부하면(예: 이스케이프를 놓친
        `&`/`<`/`>`), **그 한 통만** parse_mode 없이 평문으로 즉시 재시도한다 —
        서식 버그 하나가 알림 자체를 삼키면 손절/체결 알림 유실로 직결된다.

        `lane`(2026-09-05, 포럼 토픽 레인)을 주면 `data/state/tg_lanes.json`
        매핑으로 그 레인의 `(chat_id, message_thread_id)`를 찾는다 — 레인이
        아직 안 묶였으면 기존과 동일하게 `self.chat_id`로 폴백하되, **다른
        레인이라도 하나 이상 바인딩된 뒤**라면(`tglanes.is_bound`) 그 레거시
        채팅이 여러 레인이 섞이는 방이 되므로 한 줄 헤더(이모지+이름)를 붙인다
        (토픽 안으로 실제로 라우팅됐으면 헤더는 붙이지 않는다 — 탭 자체가
        정체성이다)."""
        if not self.enabled:
            return
        now = time.time()
        if now < self._muted_until:
            return
        chat_id: object = self.chat_id
        thread_id: int | None = None
        if lane is not None:
            mapping = self._load_lane_mapping()
            chat_id, thread_id = tglanes.resolve(lane, mapping, self.chat_id)
            if thread_id is None and tglanes.is_bound(mapping):
                text = f"{tglanes.header(lane)}\n{text}"
        if len(text) > _MAX_LEN:
            text = text[: _MAX_LEN - 1] + "…"
        payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        try:
            resp = self._post_with_timeout_retry(payload)
            if resp.status_code == 400:
                # HTML 파싱 실패로 추정 — parse_mode 없이 평문 폴백 1회.
                # 다른 400(chat 없음 등)이면 이 재시도도 400을 받고 그대로
                # 아래 raise_for_status()가 예외로 떨어진다(폴백이 실패를 숨기지
                # 않는다).
                logger.warning("Telegram HTML 파싱 실패(400) — 평문 폴백 재시도")
                plain_payload = {k: v for k, v in payload.items() if k != "parse_mode"}
                resp = self._post_with_timeout_retry(plain_payload)
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

    def _post_with_timeout_retry(self, payload: dict):
        """sendMessage 1회 POST — 타임아웃이면 **딱 1회** 즉시 재시도(2026-08-24,
        8-21 포지션 현황이 ReadTimeout 한 번에 영구 유실된 실측). 무한 재시도는
        금지(뮤트 회로차단기가 폭주 방지 담당이고, 이 재시도는 그 계약을 흔들지
        않는다). 4xx 등 다른 예외는 재시도해도 같은 답이라 재시도하지 않는다."""
        try:
            return httpx.post(_SEND_URL.format(token=self.token), json=payload, timeout=3.0)
        except httpx.TimeoutException:
            return httpx.post(_SEND_URL.format(token=self.token), json=payload, timeout=3.0)

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
