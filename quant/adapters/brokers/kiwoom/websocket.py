"""Kiwoom 실시간 웹소켓 피드 — Phase 3.

connect -> LOGIN -> REG(구독) -> REAL(체결가) 수신까지 구현한다. 스펙 근거는
docs/api/kiwoom/README.md "5. WebSocket 실시간 API" 절(공식 저장소 `ws_client.py`
+ `kiwoom_docs/실시간시세.md` 기준 [확인됨]). 실키가 아직 등록 전이라 실호출로
검증된 적은 없다 — 문서상 [확인됨] 항목만 하드코딩했고, 그 밖은 방어적으로 처리한다.

해외주식(TQQQ) 실시간시세가 이 경로로 실제 오는지는 [미확인] — 문서 상 미국주식
WebSocket 경로가 별도(`/api/us/websocket`)로 존재할 가능성이 있다
(docs/api/kiwoom/README.md 5.1/표 참고). 실키 발급 후 `kiwoom-probe`로 확인할 것.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from zoneinfo import ZoneInfo

import websockets

from quant.core.models import Quote

from .client import KiwoomError

logger = logging.getLogger(__name__)

# 실전/모의 접속 URL — docs/api/kiwoom/README.md 5.1 [확인됨]. 국내주식 경로만
# 하드코딩했다 (해외주식 경로는 [미확인]).
DEFAULT_WS_URL = "wss://mockapi.kiwoom.com:10000/api/dostk/websocket"

_KST = ZoneInfo("Asia/Seoul")

# 실시간 데이터 프레임(REAL, type="0B" 주식체결)의 FID 코드 — [확인됨, 이 두 개만].
# docs/api/kiwoom/README.md 5.5: 전체 FID 매핑표는 kiwoom_docs/실시간시세.md 원문에
# 더 있으나, 지금 필요한(체결가/체결시간) 것만 옮긴다 — 검증 안 된 필드로 파싱을
# 넓히지 않는다.
_FID_PRICE = "10"       # 현재가. 부호(+/-/공백)는 등락 방향 표시로 보임 — 절대값 사용 [미확인 해석]
_FID_TRADE_TIME = "20"  # 체결시간 HHMMSS (날짜 없음 — 오늘 날짜(KST)로 조립)

_REALTIME_TICK_TYPE = "0B"  # 주식체결 — 이 어댑터가 파싱하는 유일한 타입

# 연속 인증 실패가 이 횟수를 넘으면 WARNING이 아니라 ERROR로 승격하고, 로그에
# AUTH_ALERT_MARKER를 남긴다. server/scripts/watchdog.sh가 이 문자열을 grep해서
# 텔레그램으로 알린다 — "죽은 피드가 로그 벽 뒤에 조용히 숨는" 상황을 막는 장치.
AUTH_ALERT_THRESHOLD = 3
AUTH_ALERT_MARKER = "Kiwoom WS 인증 연속 실패"

# 인증 실패용 재접속 상한. 네트워크 단절(reconnect_max_delay=30초)과 달리, 토큰이
# 거부되는 상황은 30초마다 두드려 봐야 저절로 낫지 않는다 — 더 길게 벌린다.
DEFAULT_AUTH_RECONNECT_MAX_DELAY = 300.0


def _is_ping_message(msg: object) -> bool:
    """PING은 {"trnm": "PING", ...} 또는 순수 문자열 "PING"으로 온다 (문서 5.6)."""
    if isinstance(msg, str):
        return msg.strip().upper() == "PING"
    if isinstance(msg, dict):
        return str(msg.get("trnm", "")).upper() == "PING"
    return False


def _msg_trnm(msg: object) -> str:
    if isinstance(msg, dict):
        return str(msg.get("trnm", "")).upper()
    return ""


def _parse_price(values: dict) -> float | None:
    raw = values.get(_FID_PRICE)
    if raw is None:
        return None
    try:
        return abs(float(raw))
    except (TypeError, ValueError):
        return None


def _parse_trade_time(values: dict) -> datetime:
    """FID 20(HHMMSS)에는 날짜가 없다 — 오늘(KST) 날짜와 합성한다.

    [미확인/방어적 가정] 자정을 넘나드는 롤오버는 처리하지 않는다 — 국내 정규장
    (09:00~15:30)은 자정과 무관해 실질적 영향이 없다. 파싱 실패 시 수신 시각으로
    대체한다."""
    now_kst = datetime.now(_KST)
    raw = values.get(_FID_TRADE_TIME)
    if not raw:
        return now_kst
    try:
        t = datetime.strptime(str(raw), "%H%M%S").time()
        return datetime.combine(now_kst.date(), t, tzinfo=_KST)
    except ValueError:
        return now_kst


class KiwoomWSAuthError(KiwoomError):
    """LOGIN 프레임이 거부됨 — 토큰/자격증명 문제.

    네트워크 단절(`ConnectionError`, `websockets` 예외)과 **구분하기 위해서만**
    존재한다. 둘은 대응이 다르다: 네트워크는 짧게 재시도하면 낫고, 토큰 거부는
    같은 주기로 두드려도 낫지 않는다. 벤더 에러코드를 열거해 판별하지 않는다
    (미검증 스펙 위에 로직을 세우지 않는다 — brokers/CLAUDE.md) — LOGIN ack가
    실패로 왔다는 **위치**만으로 판별한다.
    """


@dataclass
class RealtimeHealth:
    """접속 상태 스냅샷 — MarketDataService.health()의 SourceHealth와 같은 패턴."""

    connected: bool = False
    reconnect_count: int = 0
    last_error: str | None = None
    last_tick_at: datetime | None = None
    consecutive_auth_failures: int = 0


class KiwoomRealtimeFeed:
    """비동기 실시간 시세 피드. connect() -> subscribe(symbols) -> run()으로 쓰거나,
    run()만 호출하면 생성자에 넘긴 symbols로 자동 구독 + 재연결 시 재구독한다.

    스레드 안전: quote()/health()는 락으로 보호된 dict/dataclass 스냅샷을 반환한다
    — run()이 별도 스레드에서 자기 이벤트 루프로 돌고, 엔진 루프가 다른 스레드에서
    quote()만 동기 호출하는 배치를 지원하기 위함.
    """

    def __init__(
        self,
        access_token: str | Callable[[], str],
        ws_url: str = DEFAULT_WS_URL,
        symbols: list[str] | None = None,
        types: list[str] | None = None,
        reconnect_initial_delay: float = 1.0,
        reconnect_max_delay: float = 30.0,
        auth_reconnect_max_delay: float = DEFAULT_AUTH_RECONNECT_MAX_DELAY,
        invalidate_token: Callable[[], None] | None = None,
    ) -> None:
        """`access_token`은 문자열이 아니라 **호출 가능한 토큰 발급자**를 넘기는 것이
        정상 사용법이다 (`KiwoomClient.access_token` 바운드 메서드). 매 접속마다
        호출된다.

        **다만 그것만으로는 폐기된 토큰이 교체되지 않는다** — `access_token()` 은
        로컬 시계 기준 만료 전이면 캐시를 그대로 돌려주기 때문이다. 키움은 새
        토큰을 발급하면 이전 토큰을 무효화하는 것으로 보이는데, 서버가 이미 폐기한
        토큰을 우리는 "아직 안 만료됐다"고 믿고 계속 재사용했다 — 웹소켓 LOGIN 이
        `[805004] Token이 유효하지 않습니다` 로 **9일간**(하루 243회) 거부됐고
        아무도 몰랐다(2026-08-11~20). 같은 키로 REST 는 683건 성공 중이었다.

        그래서 `invalidate_token` 을 함께 받는다 — 인증 실패 시 이걸 호출해
        캐시를 버려야 다음 접속이 **진짜로** 새 토큰을 받는다
        (`KiwoomClient.invalidate_token` 바운드 메서드).

        문자열도 받는다 — 테스트/일회성 프로브용이다. 문자열을 넘기면 그 토큰이
        만료되는 순간 재접속 루프가 영원히 실패한다(2026-08-11 실장애).
        """
        self._token_source = access_token
        self._invalidate_token = invalidate_token
        self.ws_url = ws_url
        self._symbols: list[str] = list(symbols or [])
        self._types: list[str] = list(types or [_REALTIME_TICK_TYPE])
        self._grp_no = "1"
        self._reconnect_initial_delay = reconnect_initial_delay
        self._reconnect_max_delay = reconnect_max_delay
        self._auth_reconnect_max_delay = auth_reconnect_max_delay
        self._reconnect_delay = reconnect_initial_delay
        self._current_max_delay = reconnect_max_delay

        self._ws = None
        self._stopped = False
        # run()이 시작될 때 채워진다 — 다른 스레드에서 resubscribe()를 걸 때
        # run_coroutine_threadsafe의 대상 루프로 쓴다.
        self._loop: asyncio.AbstractEventLoop | None = None

        self._lock = threading.Lock()
        self._quotes: dict[str, Quote] = {}
        self._health = RealtimeHealth()

        self._tick_events: dict[str, asyncio.Event] = {}

    # -------------------------------------------------------------- public 조회
    def quote(self, symbol: str) -> Quote | None:
        with self._lock:
            return self._quotes.get(symbol)

    def health(self) -> RealtimeHealth:
        with self._lock:
            return replace(self._health)

    # ------------------------------------------------------------------ 연결
    async def connect(self) -> None:
        self._ws = await websockets.connect(self.ws_url, ping_interval=None)
        await self._login()
        with self._lock:
            self._health.connected = True
            self._health.last_error = None
            self._health.consecutive_auth_failures = 0

    async def _resolve_token(self) -> str:
        """접속할 때마다 토큰을 새로 얻는다 — 캐시된 문자열을 재사용하지 않는다.

        토큰 발급자(`KiwoomClient.access_token`)는 **동기 HTTP**를 탈 수 있으므로
        스레드로 뺀다. 이 코루틴이 도는 이벤트 루프에는 PING echo 등 연결 유지에
        필요한 처리가 같이 걸려 있어서, 여기서 블로킹하면 안 된다.
        """
        source = self._token_source
        if callable(source):
            return await asyncio.to_thread(source)
        return source

    async def _login(self) -> None:
        """접속 직후 LOGIN 프레임 전송 → ack 대기 (문서 5.2 [확인됨, verbatim]).

        로그인 ack 전에 다른 메시지가 먼저 올 수 있다고 문서가 명시한다 — 순서를
        가정하지 않고 ack이 올 때까지 그 사이 메시지도 정상 처리(PING echo 등)한다.
        """
        assert self._ws is not None
        token = await self._resolve_token()
        await self._ws.send(json.dumps({"trnm": "LOGIN", "token": token}))
        ack = await self._recv_until(lambda m: _msg_trnm(m) == "LOGIN")
        code = ack.get("return_code")
        if code not in (None, 0):
            raise KiwoomWSAuthError(int(code), str(ack.get("return_msg", "")))

    async def subscribe(self, symbols: list[str], types: list[str] | None = None) -> None:
        """실시간 등록(REG, 문서 5.3 [확인됨, verbatim]). ack의 return_code로 성공을
        확인한다 — 실패해도 조용히 넘어가지 않는다.

        connect() 직후 dispatch_loop()가 시작되기 전에만 안전하게 쓸 수 있다 — ack를
        기다리며 직접 `_ws.recv()`를 걸기 때문에, dispatch_loop가 이미 같은 소켓을
        읽고 있으면 reader가 겹친다. dispatch_loop가 돌고 있는 동안 구독을 바꾸려면
        `resubscribe()`를 쓸 것."""
        assert self._ws is not None
        await self._send_reg(symbols, types)
        ack = await self._recv_until(lambda m: _msg_trnm(m) == "REG")
        code = ack.get("return_code")
        if code not in (None, 0):
            raise KiwoomError(int(code), str(ack.get("return_msg", "")))

    async def _send_reg(self, symbols: list[str], types: list[str] | None = None) -> None:
        """REG 프레임 전송만 하고 ack는 기다리지 않는다. subscribe()와 resubscribe()가
        공유하는 하위 로직 — ack를 누가 기다리는지(동기 vs dispatch_loop passive)만 다르다.

        문서 5.3: `refresh`: "1" = 기존 등록 유지(추가), "0" = 기존 등록 해지 후 신규
        등록만. 여기서는 항상 "1"(유지)을 쓴다 — 그래야 재구독이 기존 구독을
        지우지 않는다."""
        assert self._ws is not None
        self._symbols = list(symbols)
        self._types = list(types) if types else self._types
        payload = {
            "trnm": "REG",
            "grp_no": self._grp_no,
            "refresh": "1",
            "data": [{"item": self._symbols, "type": self._types}],
        }
        await self._ws.send(json.dumps(payload))

    def resubscribe(self, symbols: list[str], types: list[str] | None = None, timeout: float = 5.0) -> None:
        """구독 대상 심볼을 바꾼다 — 스레드 세이프, dispatch_loop가 돌고 있는 동안에도
        안전하게 부를 수 있다(subscribe()와 달리).

        run()은 보통 별도 스레드에서 자기 이벤트 루프로 돈다. 이 메서드는 다른
        스레드(엔진 루프 등)에서 호출되는 것을 전제로, REG 전송을 feed의 이벤트
        루프에 예약만 하고(`asyncio.run_coroutine_threadsafe`) 전송 완료까지만
        동기적으로 기다린다 — ack는 기다리지 않는다. ack를 여기서 또 기다리려면
        `_ws.recv()`를 걸어야 하는데, 그러면 dispatch_loop가 이미 걸어 둔 recv()와
        같은 소켓에 reader가 두 개 붙어 충돌한다. 성공/실패는 dispatch_loop가
        REG ack를 받는 대로 평소처럼 로그로 남긴다(`_handle_message`).

        `_symbols`/`_types`는 이 호출로 즉시 갱신되므로, 재접속 시 `run()`이 이
        새 목록으로 자동 재구독한다.

        run()이 아직 시작 전(이벤트 루프 없음)이면 RuntimeError.

        세션 롤 등으로 유니버스가 바뀔 때 쓰는 용도다 — 이 저장소에는 아직 어디서도
        호출을 배선하지 않았다(day-roll 배선은 app/loop.py 담당, 범위 밖). 사용 예:

            feed.resubscribe(["005930", "TQQQ"])  # 엔진 루프 스레드에서 직접 호출 가능
        """
        loop = self._loop
        if loop is None:
            raise RuntimeError("KiwoomRealtimeFeed.run()이 아직 시작되지 않음 — 이벤트 루프 없음")
        future = asyncio.run_coroutine_threadsafe(self._send_reg(symbols, types), loop)
        future.result(timeout=timeout)

    async def close(self) -> None:
        self._stopped = True
        if self._ws is not None:
            await self._ws.close()

    # -------------------------------------------------------------- 재연결 루프
    async def run(self) -> None:
        """connect -> (symbols 있으면) subscribe -> 수신 루프. 끊기면 지수 백오프로
        재접속 + 재등록을 반복한다. close()가 호출될 때까지 멈추지 않는다."""
        self._loop = asyncio.get_running_loop()  # resubscribe()가 threadsafe 호출 대상으로 쓴다
        while not self._stopped:
            try:
                await self.connect()
                if self._symbols:
                    await self.subscribe(self._symbols, self._types)
                self._reconnect_delay = self._reconnect_initial_delay
                await self._dispatch_loop()
                # 서버가 연결을 정상 종료해도 재접속 대상이다 — 예외로 통일해서
                # 아래 except 블록 하나로 처리한다.
                raise ConnectionError("Kiwoom WS 연결 종료")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                is_auth = isinstance(e, KiwoomWSAuthError)
                with self._lock:
                    self._health.connected = False
                    self._health.last_error = f"{type(e).__name__}: {e}"
                    self._health.reconnect_count += 1
                    if is_auth:
                        self._health.consecutive_auth_failures += 1
                    else:
                        self._health.consecutive_auth_failures = 0
                    auth_streak = self._health.consecutive_auth_failures
                if is_auth and self._invalidate_token is not None:
                    # 캐시를 버려야 다음 _resolve_token() 이 진짜로 새 토큰을 받는다.
                    # 이게 없으면 죽은 토큰으로 영원히 두드린다(위 docstring 참고).
                    try:
                        self._invalidate_token()
                    except Exception as e:  # noqa: BLE001 — 무효화 실패가 재접속을 막으면 안 된다
                        logger.warning("토큰 캐시 무효화 실패(무시): %s", e)
                # 인증 실패는 네트워크 단절보다 훨씬 길게 벌린다 — 30초마다 두드려도
                # 낫지 않는 종류의 고장이라, 같은 주기로 영원히 재시도하면 로그 벽만
                # 쌓이고 사람은 아무것도 눈치채지 못한다.
                self._current_max_delay = (
                    self._auth_reconnect_max_delay if is_auth else self._reconnect_max_delay
                )
                if not is_auth:
                    logger.warning(
                        "Kiwoom WS 연결 끊김(네트워크/서버), %.1fs 후 재접속: %s: %s",
                        self._reconnect_delay, type(e).__name__, e,
                    )
                elif auth_streak < AUTH_ALERT_THRESHOLD:
                    logger.warning(
                        "Kiwoom WS 인증 실패(토큰) %d회 연속, %.1fs 후 재접속(토큰 재발급): %s",
                        auth_streak, self._reconnect_delay, e,
                    )
                else:
                    logger.error(
                        "%s %d회 — 실시간 시세 중단 상태다. 재접속마다 토큰을 새로 "
                        "발급하는데도 거부되므로 만료가 아니라 자격증명/계정/호스트 "
                        "문제일 가능성이 높다(ws_url=%s). %.1fs 후 재접속: %s",
                        AUTH_ALERT_MARKER, auth_streak, self.ws_url, self._reconnect_delay, e,
                    )
            if self._stopped:
                break
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, self._current_max_delay)

    # ------------------------------------------------------------------ 수신
    async def _recv_until(self, predicate) -> dict:
        """predicate가 참인 메시지가 올 때까지 읽되, 그 사이 오는 메시지(PING 등)도
        정상적으로 처리한다."""
        assert self._ws is not None
        while True:
            raw = await self._ws.recv()
            msg = self._safe_parse(raw)
            if msg is None:
                continue
            if predicate(msg):
                return msg
            await self._handle_message(msg)

    async def _dispatch_loop(self) -> None:
        assert self._ws is not None
        async for raw in self._ws:
            msg = self._safe_parse(raw)
            if msg is None:
                continue
            await self._handle_message(msg)

    @staticmethod
    def _safe_parse(raw) -> dict | str | None:
        try:
            return json.loads(raw)
        except ValueError:
            logger.warning("Kiwoom WS 파싱 실패: %r", raw)
            return None

    async def _handle_message(self, msg: dict | str) -> None:
        if _is_ping_message(msg):
            # 문서 5.6: 애플리케이션 레벨 PING은 받은 그대로 echo해야 연결이 안 죽는다.
            assert self._ws is not None
            await self._ws.send(json.dumps(msg))
            return
        if not isinstance(msg, dict):
            logger.debug("Kiwoom WS 처리하지 않는 메시지: %r", msg)
            return
        trnm = _msg_trnm(msg)
        if trnm == "REAL":
            self._handle_real(msg)
        elif trnm in ("REG", "REMOVE"):
            code = msg.get("return_code")
            if code not in (None, 0):
                logger.warning("Kiwoom WS %s 실패: %s", trnm, msg.get("return_msg"))
        else:
            logger.debug("Kiwoom WS 처리하지 않는 메시지: %s", msg)

    def _handle_real(self, msg: dict) -> None:
        for item in msg.get("data", []):
            if not isinstance(item, dict) or item.get("type") != _REALTIME_TICK_TYPE:
                continue  # 0B(주식체결) 외 타입은 이 어댑터가 아직 파싱하지 않는다
            symbol = item.get("item")
            values = item.get("values")
            if not symbol or not isinstance(values, dict):
                continue
            price = _parse_price(values)
            if price is None:
                continue  # 알 수 없는/누락된 필드 — 조용히 스킵 (크래시 금지)
            quote = Quote(symbol=symbol, ts=_parse_trade_time(values), price=price)
            self._set_quote(symbol, quote)

    def _set_quote(self, symbol: str, quote: Quote) -> None:
        with self._lock:
            self._quotes[symbol] = quote
            self._health.last_tick_at = quote.ts
        ev = self._tick_events.get(symbol)
        if ev is not None:
            ev.set()

    # -------------------------------------------------------- 이벤트 기반 웨이크
    async def wait_for_tick(self, symbols: list[str], timeout: float | None = None) -> bool:
        """등록 심볼 중 하나라도 새 틱이 오면 즉시 True, timeout까지 없으면 False.

        루프가 `asyncio.sleep(poll_seconds)` 대신 쓸 수 있는 형태 — 단, 이 피드와
        같은 이벤트 루프에서 await해야 한다 (asyncio.Event는 스레드 세이프하지
        않다). run()을 별도 스레드에서 돌린다면 그 스레드의 루프에 대고
        `asyncio.run_coroutine_threadsafe(feed.wait_for_tick(symbols, timeout), feed_loop).result()`
        로 불러야 한다. 사용 예:

            async def poll():
                while True:
                    got_tick = await feed.wait_for_tick(["TQQQ"], timeout=10.0)
                    if not got_tick:
                        continue  # timeout — 평소처럼 폴링 주기로 넘어감
                    # 새 틱 도착 — 바로 사이클 실행

        app/loop.py에는 아직 배선하지 않았다 (범위 밖) — 여기 문서화만 해둔다.
        """
        if not symbols:
            return False
        events = [self._tick_events.setdefault(s, asyncio.Event()) for s in symbols]
        tasks = [asyncio.ensure_future(ev.wait()) for ev in events]
        done, pending = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if not done:
            return False
        for ev in events:
            if ev.is_set():
                ev.clear()
        return True
