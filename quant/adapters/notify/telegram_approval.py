"""신규 진입 승인 봇 — 인라인 버튼으로 사용자 승인을 받는 텔레그램 어댑터.

notify/telegram.py의 TelegramNotifier와 같은 규칙을 따른다: 모든 예외를 안에서
삼키고(거래 루프에 네트워크 실패가 새어나가면 안 된다), 연속 실패 시 서킷브레이커로
일정 시간 조용해진다. 다만 성격이 다르다 — 알림은 실패해도 거래가 그대로 진행되지만,
**이 봇의 실패는 신규 진입이 일어나지 않는다는 뜻이다(fail-closed)**. 그래서 실패를
'없는 값'이 아니라 None(=전송 실패)으로 정확히 돌려주고, 호출부가 진입을 취소한다.
청산은 이 봇을 아예 거치지 않으므로 영향받지 않는다.

**토큰 제약(중요)**: 이 봇은 TELEGRAM_BOT_TOKEN(알림 봇)을 쓴다. 이 값은
TELEGRAM_BRIDGE_BOT_TOKEN(Claude 브리지 봇)과 **반드시 다른 토큰**이어야 한다.
getUpdates의 offset 확인(confirm)은 봇 토큰 단위로 전역이라, 같은 토큰을 두
폴러가 읽으면 먼저 읽은 쪽이 상대의 업데이트까지 확정해버려 서로의 콜백을 훔친다.
그러면 사용자가 누른 승인 버튼이 조용히 사라진다. from_env()는 두 토큰이 같으면
기동 시 경고를 남긴다.

검증된 Bot API 계약(공식 문서 기준):
- callback_data는 **1~64바이트**. 그래서 요청 ID만 담는다: "ok:<id>" / "no:<id>".
- answerCallbackQuery는 **필수**다 — "necessary to react by calling
  answerCallbackQuery even if no notification to the user is needed". 안 부르면
  사용자 화면에서 버튼 로딩 스피너가 계속 돈다.
- getUpdates의 offset은 **max(update_id) + 1** — "An update is considered
  confirmed as soon as getUpdates is called with an offset higher than its
  update_id". 확정하지 않으면 같은 콜백을 매 사이클 다시 받는다.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:  # 런타임 임포트 금지 — 어댑터가 app/을 끌고 들어오면 의존 방향이 뒤집힌다
    from quant.core.models import ApprovalRequest

logger = logging.getLogger(__name__)

_API_URL = "https://api.telegram.org/bot{token}/{method}"
_MAX_LEN = 4096              # Telegram sendMessage text limit
_CALLBACK_DATA_MAX_BYTES = 64  # Bot API 하드 제한 (1~64 bytes)
_FAILURE_LIMIT = 5           # consecutive failures before muting
_MUTE_SECONDS = 10 * 60      # mute duration once the breaker trips
_TIMEOUT_SECONDS = 3.0       # 거래 루프 안에서 도는 호출이라 짧게 — 장기 폴링 금지


class TelegramApprovalBot:
    """token/chat_id가 falsy면 self.enabled=False, 모든 호출이 no-op(전송 실패로 취급)."""

    def __init__(self, token: str | None, chat_id: str | None) -> None:
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)
        # getUpdates offset. 0으로 시작하면 첫 폴에서 밀린 업데이트를 전부 받지만,
        # 그 안의 낡은 요청 ID는 게이트에서 이미 종결 상태라 no-op이 된다(멱등).
        self._offset = 0
        self._consecutive_failures = 0
        self._muted_until = 0.0

    @classmethod
    def from_env(cls) -> "TelegramApprovalBot":
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        bridge_token = os.environ.get("TELEGRAM_BRIDGE_BOT_TOKEN")
        if token and bridge_token and token == bridge_token:
            logger.warning(
                "TELEGRAM_BOT_TOKEN과 TELEGRAM_BRIDGE_BOT_TOKEN이 같다 — 두 getUpdates "
                "폴러가 같은 봇을 읽으면 서로의 업데이트를 확정해 승인 버튼이 조용히 "
                "사라진다. 승인용 봇은 반드시 별도 토큰이어야 한다."
            )
        return cls(token, os.environ.get("TELEGRAM_CHAT_ID"))

    # ------------------------------------------------------------ 내부 HTTP
    def _call(self, method: str, payload: dict) -> dict | None:
        """Bot API 호출. 실패는 전부 삼키고 None. 연속 실패 시 10분 뮤트."""
        if not self.enabled:
            return None
        now = time.time()
        if now < self._muted_until:
            return None
        try:
            resp = httpx.post(
                _API_URL.format(token=self.token, method=method),
                json=payload,
                timeout=_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"telegram ok=false: {data.get('description')}")
            self._consecutive_failures = 0
            return data
        except Exception as e:
            self._consecutive_failures += 1
            logger.warning("Telegram %s 실패 (연속 %d회): %s: %s",
                           method, self._consecutive_failures, type(e).__name__, e)
            if self._consecutive_failures >= _FAILURE_LIMIT:
                self._muted_until = now + _MUTE_SECONDS
                self._consecutive_failures = 0
                logger.warning("Telegram %d회 연속 실패 — %d분간 승인 봇 중단 (그동안 신규 진입 없음)",
                               _FAILURE_LIMIT, _MUTE_SECONDS // 60)
            return None

    # ------------------------------------------------------------ 공개 API
    def send_request(self, req: ApprovalRequest, expire_seconds: float) -> int | None:
        """승인 요청 메시지 전송. 성공 시 message_id, 실패 시 None(=진입하지 않음).

        expire_seconds를 함께 받는 이유: 만료 시각은 요청 레코드가 아니라 설정에서
        나온다(config가 단일 진실 소스). 사용자가 "언제까지 유효한가"를 모른 채
        버튼을 누르면 만료된 신호를 승인하려다 취소당하는 경험을 반복하게 된다."""
        ok_data = f"ok:{req.id}"
        no_data = f"no:{req.id}"
        for data in (ok_data, no_data):
            if not 1 <= len(data.encode("utf-8")) <= _CALLBACK_DATA_MAX_BYTES:
                # 여기 걸리면 요청 ID 생성 규칙이 깨진 것이다. 잘린 callback_data로
                # 보내면 사용자가 누른 버튼이 엉뚱한 요청을 가리킬 수 있다 — 그럴
                # 바엔 진입하지 않는다.
                logger.error("callback_data 길이 위반(%d바이트) — 승인 요청 취소: %s",
                             len(data.encode("utf-8")), data)
                return None
        payload = {
            "chat_id": self.chat_id,
            "text": self._format_request(req, expire_seconds),
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "✅ 수락", "callback_data": ok_data},
                    {"text": "❌ 거절", "callback_data": no_data},
                ]]
            },
        }
        data = self._call("sendMessage", payload)
        if data is None:
            return None
        message_id = data.get("result", {}).get("message_id")
        return message_id if isinstance(message_id, int) else None

    def poll_decisions(self) -> list[tuple[str, bool]]:
        """확정되지 않은 콜백을 읽어 (req_id, approved) 목록으로 돌려준다.

        offset은 인스턴스에 유지한다. 실패하면 빈 리스트 — 다음 사이클에 같은
        업데이트를 다시 받게 되므로 결정이 유실되지는 않는다."""
        if not self.enabled:
            return []
        data = self._call("getUpdates", {
            "offset": self._offset,
            "timeout": 0,  # 장기 폴링 금지 — 거래 사이클을 붙잡으면 안 된다
            "allowed_updates": ["callback_query"],
        })
        if data is None:
            return []
        out: list[tuple[str, bool]] = []
        for update in data.get("result", []) or []:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._offset = max(self._offset, update_id + 1)
            callback = update.get("callback_query")
            if not callback:
                continue
            raw = callback.get("data") or ""
            approved = raw.startswith("ok:")
            if not approved and not raw.startswith("no:"):
                self.answer(callback.get("id"), "알 수 없는 요청")
                continue
            # answerCallbackQuery는 결과와 무관하게 항상 먼저 부른다 — 안 부르면
            # 사용자 쪽 버튼이 계속 로딩 상태로 남는다.
            self.answer(callback.get("id"), "수락 처리 중" if approved else "거절 처리")
            out.append((raw[3:], approved))
        return out

    def answer(self, callback_query_id: str | None, text: str) -> None:
        if not callback_query_id:
            return
        self._call("answerCallbackQuery", {
            "callback_query_id": callback_query_id,
            "text": text[:200],  # Bot API: 0~200자
        })

    def finalize(self, message_id: int | None, text: str) -> None:
        """버튼을 제거하고 결과 텍스트로 갱신한다 — 같은 요청을 다시 누를 수 없게.

        editMessageReplyMarkup을 reply_markup 없이 부르면 인라인 키보드가 사라진다.
        멱등성의 최종 방어선은 ApprovalGate.decide()지 이 버튼 제거가 아니다 —
        네트워크가 실패해 버튼이 남아 있어도 두 번 실행되지는 않는다."""
        if message_id is None:
            return
        self._call("editMessageReplyMarkup", {"chat_id": self.chat_id, "message_id": message_id})
        if len(text) > _MAX_LEN:
            text = text[: _MAX_LEN - 1] + "…"
        self._call("editMessageText", {
            "chat_id": self.chat_id, "message_id": message_id, "text": text,
        })

    # ------------------------------------------------------------ 포매팅
    @staticmethod
    def _format_request(req: ApprovalRequest, expire_seconds: float) -> str:
        """통화 기호는 붙이지 않는다 — 종목의 표시 통화(USD/KRW)는 이 계층이 모른다."""
        expires_at = datetime.fromtimestamp(req.created_at + expire_seconds)
        lines = [
            "[승인 요청] 신규 진입",
            f"종목: {req.symbol} ({req.action})",
            f"수량: {req.qty:g} @ {req.price:,.2f} (명목 {req.qty * req.price:,.2f})",
            f"전략: {req.strategy_id}",
        ]
        if req.stop is not None:
            lines.append(f"손절: {req.stop:,.2f}" + (f" / 목표: {req.target:,.2f}" if req.target else ""))
        if req.reason:
            lines.append(f"사유: {req.reason}")
        lines.append(f"만료: {expires_at:%H:%M:%S} ({expire_seconds:.0f}초 후)")
        lines.append(f"요청 ID: {req.id}")
        text = "\n".join(lines)
        return text if len(text) <= _MAX_LEN else text[: _MAX_LEN - 1] + "…"
