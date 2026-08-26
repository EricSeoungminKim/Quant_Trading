"""KiwoomRealtimeFeed 테스트. 전부 로컬 페이크 웹소켓 서버(websockets.serve on
127.0.0.1)만 쓴다 — 실 키움 서버 호출 없음. pytest-asyncio 미설치라 각 테스트는
asyncio.run()으로 직접 이벤트 루프를 돈다 (tests/test_loop_resilience.py와 동일 패턴)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from datetime import datetime, timezone

import pytest
import websockets

from quant.adapters.brokers.kiwoom.client import KiwoomError
from quant.adapters.brokers.kiwoom.websocket import (
    AUTH_ALERT_MARKER,
    AUTH_ALERT_THRESHOLD,
    KiwoomRealtimeFeed,
)
from quant.core.models import Quote


async def _drain(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_login_handshake_sends_token_and_waits_for_ack():
    received = {}

    async def handler(ws):
        received["login"] = json.loads(await ws.recv())
        await ws.send(json.dumps({"trnm": "LOGIN", "return_code": 0, "return_msg": ""}))
        await ws.wait_closed()

    async def run():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            feed = KiwoomRealtimeFeed(access_token="tok123", ws_url=f"ws://127.0.0.1:{port}")
            await feed.connect()
            assert feed.health().connected is True
            await feed.close()

    asyncio.run(run())
    assert received["login"] == {"trnm": "LOGIN", "token": "tok123"}


def test_login_failure_raises_kiwoom_error():
    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"trnm": "LOGIN", "return_code": 3, "return_msg": "토큰 만료"}))
        await ws.wait_closed()

    async def run():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            feed = KiwoomRealtimeFeed(access_token="bad", ws_url=f"ws://127.0.0.1:{port}")
            try:
                await feed.connect()
                raised = None
            except KiwoomError as e:
                raised = e
            assert raised is not None
            assert raised.code == 3

    asyncio.run(run())


def test_subscribe_sends_reg_frame_and_waits_for_ack():
    received = {}

    async def handler(ws):
        await ws.recv()  # login
        await ws.send(json.dumps({"trnm": "LOGIN", "return_code": 0}))
        received["reg"] = json.loads(await ws.recv())
        await ws.send(json.dumps({"trnm": "REG", "return_code": 0, "return_msg": ""}))
        await ws.wait_closed()

    async def run():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            feed = KiwoomRealtimeFeed(access_token="tok", ws_url=f"ws://127.0.0.1:{port}")
            await feed.connect()
            await feed.subscribe(["005930"])
            await feed.close()

    asyncio.run(run())
    assert received["reg"] == {
        "trnm": "REG",
        "grp_no": "1",
        "refresh": "1",
        "data": [{"item": ["005930"], "type": ["0B"]}],
    }


def test_subscribe_failure_raises_kiwoom_error():
    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"trnm": "LOGIN", "return_code": 0}))
        await ws.recv()
        await ws.send(json.dumps({"trnm": "REG", "return_code": 5, "return_msg": "구독 실패"}))
        await ws.wait_closed()

    async def run():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            feed = KiwoomRealtimeFeed(access_token="tok", ws_url=f"ws://127.0.0.1:{port}")
            await feed.connect()
            try:
                await feed.subscribe(["005930"])
                raised = None
            except KiwoomError as e:
                raised = e
            assert raised is not None
            assert raised.code == 5

    asyncio.run(run())


def test_tick_updates_quote_cache_and_concurrent_reads_dont_crash():
    """수신 틱이 quote()에 반영되고, 다른 스레드에서 동시에 quote()를 읽어도 죽지 않는다."""
    async def handler(ws):
        await ws.recv()  # login
        await ws.send(json.dumps({"trnm": "LOGIN", "return_code": 0}))
        await ws.recv()  # reg
        await ws.send(json.dumps({"trnm": "REG", "return_code": 0}))
        await ws.send(json.dumps({
            "trnm": "REAL",
            "data": [{
                "type": "0B", "name": "주식체결", "item": "005930",
                "values": {"20": "101530", "10": "-71500"},
            }],
        }))
        await ws.wait_closed()

    async def run():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            feed = KiwoomRealtimeFeed(access_token="tok", ws_url=f"ws://127.0.0.1:{port}")
            await feed.connect()
            await feed.subscribe(["005930"])

            stop = threading.Event()
            errors = []

            def reader() -> None:
                while not stop.is_set():
                    try:
                        feed.quote("005930")
                    except Exception as e:  # pragma: no cover - 실패하면 테스트가 잡아낸다
                        errors.append(e)

            reader_thread = threading.Thread(target=reader, daemon=True)
            reader_thread.start()

            dispatch_task = asyncio.create_task(feed._dispatch_loop())
            for _ in range(100):
                if feed.quote("005930") is not None:
                    break
                await asyncio.sleep(0.02)

            stop.set()
            reader_thread.join(timeout=1)
            await _drain(dispatch_task)
            await feed.close()

            assert not errors
            quote = feed.quote("005930")
            assert quote is not None
            assert quote.symbol == "005930"
            assert quote.price == 71500.0  # 부호 접두사는 방향 표시 — 절대값으로 저장
            assert feed.health().last_tick_at is not None

    asyncio.run(run())


def test_non_tick_realtime_types_are_ignored_without_crash():
    """0B(주식체결) 외 타입(예: 00 주문체결)은 아직 파싱 대상이 아니다 — 조용히 스킵."""
    feed = KiwoomRealtimeFeed(access_token="tok")
    feed._handle_real({
        "trnm": "REAL",
        "data": [{"type": "00", "name": "주문체결", "item": "005930", "values": {"910": "71500"}}],
    })
    assert feed.quote("005930") is None


def test_reconnect_after_disconnect_reregisters():
    connections = 0
    reg_count = 0
    done = threading.Event()

    async def handler(ws):
        nonlocal connections, reg_count
        connections += 1
        await ws.recv()  # login
        await ws.send(json.dumps({"trnm": "LOGIN", "return_code": 0}))
        await ws.recv()  # reg
        reg_count += 1
        await ws.send(json.dumps({"trnm": "REG", "return_code": 0}))
        if connections == 1:
            await ws.close()  # 첫 연결은 바로 끊어서 재접속을 유도
        else:
            done.set()
            await ws.wait_closed()

    async def run():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            feed = KiwoomRealtimeFeed(
                access_token="tok", ws_url=f"ws://127.0.0.1:{port}",
                symbols=["005930"], reconnect_initial_delay=0.02, reconnect_max_delay=0.02,
            )
            run_task = asyncio.create_task(feed.run())
            for _ in range(200):
                if done.is_set():
                    break
                await asyncio.sleep(0.02)

            assert done.is_set(), "재접속이 시간 내에 일어나지 않음"
            assert connections >= 2
            assert reg_count >= 2  # 재접속마다 재구독(REG)했다
            health = feed.health()
            assert health.reconnect_count >= 1

            await feed.close()
            await _drain(run_task)

    asyncio.run(run())


def test_wait_for_tick_fires_immediately_on_tick_and_false_on_timeout():
    async def run():
        feed = KiwoomRealtimeFeed(access_token="tok")

        async def emit_soon() -> None:
            await asyncio.sleep(0.05)
            feed._set_quote("005930", Quote(symbol="005930", ts=datetime.now(timezone.utc), price=100.0))

        emit_task = asyncio.create_task(emit_soon())
        got = await feed.wait_for_tick(["005930"], timeout=2.0)
        assert got is True
        await _drain(emit_task)

        got_timeout = await feed.wait_for_tick(["005930"], timeout=0.05)
        assert got_timeout is False

    asyncio.run(run())


def test_ping_is_echoed_verbatim():
    echoed = {}
    ping_obj = {"trnm": "PING", "id": "abc123"}

    async def handler(ws):
        await ws.recv()  # login
        await ws.send(json.dumps({"trnm": "LOGIN", "return_code": 0}))
        await ws.send(json.dumps(ping_obj))
        echoed["msg"] = json.loads(await ws.recv())
        await ws.close()

    async def run():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            feed = KiwoomRealtimeFeed(access_token="tok", ws_url=f"ws://127.0.0.1:{port}")
            await feed.connect()
            dispatch_task = asyncio.create_task(feed._dispatch_loop())
            for _ in range(100):
                if "msg" in echoed:
                    break
                await asyncio.sleep(0.02)
            await _drain(dispatch_task)
            await feed.close()

    asyncio.run(run())
    assert echoed["msg"] == ping_obj


def test_resubscribe_sends_new_reg_frame_from_another_thread_while_dispatch_loop_runs():
    """resubscribe()는 dispatch_loop가 이미 소켓을 읽고 있는 동안(run() 정상 동작 중)
    다른 스레드에서 불려도 안전해야 한다 — subscribe()를 그대로 재사용하면 recv()가
    dispatch_loop와 겹쳐 충돌한다(그래서 ack를 기다리지 않는 _send_reg 경로를 쓴다).
    ack 자체는 dispatch_loop의 REG 핸들러가 조용히 처리한다.

    run()은 프로덕션과 동일하게 **진짜 별도 OS 스레드에서 자기 이벤트 루프로** 돈다
    (asyncio.run(feed.run())) — 테스트 코루틴과 같은 루프에 task로 얹으면 resubscribe()가
    내부에서 거는 future.result()가 그 루프 자체를 막아 데드락이 난다."""
    reg_frames = []
    got_second_reg = threading.Event()

    async def handler(ws):
        await ws.recv()  # login
        await ws.send(json.dumps({"trnm": "LOGIN", "return_code": 0}))
        reg_frames.append(json.loads(await ws.recv()))  # 최초 REG(생성자 symbols)
        await ws.send(json.dumps({"trnm": "REG", "return_code": 0}))
        second = json.loads(await ws.recv())  # resubscribe()가 보낸 REG
        reg_frames.append(second)
        await ws.send(json.dumps({"trnm": "REG", "return_code": 0}))
        got_second_reg.set()
        await ws.wait_closed()

    async def run():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            feed = KiwoomRealtimeFeed(
                access_token="tok", ws_url=f"ws://127.0.0.1:{port}", symbols=["005930"],
            )

            feed_thread = threading.Thread(
                target=lambda: asyncio.run(feed.run()), name="kiwoom-feed-test", daemon=True,
            )
            feed_thread.start()

            # feed 스레드가 자기 루프를 세팅하고 연결/구독을 마칠 시간을 준다.
            for _ in range(200):
                if feed._loop is not None and feed.health().connected:
                    break
                await asyncio.sleep(0.02)
            assert feed.health().connected is True

            errors = []
            try:
                feed.resubscribe(["TQQQ", "SQQQ"])
            except Exception as e:  # pragma: no cover - 실패하면 테스트가 잡아낸다
                errors.append(e)

            for _ in range(100):
                if got_second_reg.is_set():
                    break
                await asyncio.sleep(0.02)

            assert not errors
            assert got_second_reg.is_set(), "resubscribe()가 보낸 REG를 서버가 못 받음"
            assert feed._symbols == ["TQQQ", "SQQQ"], "재접속 시 새 목록으로 재구독되도록 _symbols가 갱신돼야 한다"

            feed._stopped = True  # 백그라운드 스레드가 재접속을 멈추도록만 신호(close()는 다른 루프 소속이라 여기서 못 씀)

    asyncio.run(run())
    assert reg_frames[0]["data"][0]["item"] == ["005930"]
    assert reg_frames[1]["data"][0]["item"] == ["TQQQ", "SQQQ"]
    assert reg_frames[1]["refresh"] == "1"


def test_resubscribe_before_run_raises_runtime_error():
    """run()이 시작되기 전(이벤트 루프 없음)에는 조용히 무시하지 않고 명확히 실패한다."""
    feed = KiwoomRealtimeFeed(access_token="tok")
    with pytest.raises(RuntimeError):
        feed.resubscribe(["005930"])


def test_malformed_and_unexpected_messages_do_not_crash():
    feed = KiwoomRealtimeFeed(access_token="tok")
    assert feed._safe_parse("not-json{{{") is None

    async def run():
        await feed._handle_message({"trnm": "UNKNOWN_TYPE", "foo": "bar"})
        await feed._handle_message("just a random string")
        await feed._handle_message({"trnm": "REAL", "data": [{"type": "0B", "item": "005930", "values": {}}]})
        await feed._handle_message({"trnm": "REAL", "data": "not-a-list-of-dicts"})

    asyncio.run(run())
    assert feed.quote("005930") is None


# ---------------------------------------------------------------- 토큰 재발급
# 2026-08-11~13 실장애 회귀 테스트. EC2에서 20시간 넘게 30초마다 반복된
# `[805004] 토큰 인증에 실패했습니다`의 원인: 피드가 조립 시점에 토큰 문자열을
# **한 번 캡처**하고 재접속마다 그대로 다시 보냈다. 키움 토큰은 24시간짜리라
# 만료되는 순간 서버가 소켓을 끊고, 이후 모든 재접속이 죽은 토큰으로 거부된다
# — 프로세스를 재시작하기 전까지 절대 스스로 회복하지 못한다.


def test_reconnect_reissues_token_instead_of_reusing_expired_one():
    """재접속은 토큰을 **다시 발급받아야** 한다 (만료/폐기 자가 회복).

    가짜 토큰 소스는 호출될 때마다 새 토큰을 발급하고 이전 토큰을 폐기한다
    (키움 실동작과 같은 가정). 가짜 서버는 현재 유효한 토큰만 LOGIN을 통과시키고,
    첫 접속 뒤에는 토큰을 만료시킨 채 소켓을 끊는다. 토큰을 캐시해 재사용하는
    구현은 여기서 영원히 805004를 맞고 두 번째 LOGIN에 도달하지 못한다.
    """
    valid = {"token": None}
    issued: list[str] = []

    def token_source() -> str:
        token = f"tok-{len(issued) + 1}"
        issued.append(token)
        valid["token"] = token  # 재발급 시 이전 토큰은 폐기된다
        return token

    seen_logins: list[str] = []

    async def handler(ws):
        login = json.loads(await ws.recv())
        seen_logins.append(login["token"])
        if login["token"] != valid["token"]:
            await ws.send(json.dumps({
                "trnm": "LOGIN", "return_code": 805004,
                "return_msg": "토큰 인증에 실패했습니다. 접속을 종료합니다",
            }))
            return  # 서버가 끊는다 — 실서버와 동일
        await ws.send(json.dumps({"trnm": "LOGIN", "return_code": 0}))
        if len(seen_logins) == 1:
            valid["token"] = None  # 첫 세션 도중 토큰 만료
            return
        await ws.wait_closed()

    async def run():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            feed = KiwoomRealtimeFeed(
                access_token=token_source, ws_url=f"ws://127.0.0.1:{port}",
                reconnect_initial_delay=0.01, reconnect_max_delay=0.05,
            )
            task = asyncio.create_task(feed.run())
            for _ in range(250):
                await asyncio.sleep(0.02)
                if len(seen_logins) >= 2 and feed.health().connected:
                    break
            await feed.close()
            await _drain(task)

    asyncio.run(run())
    assert seen_logins[0] == "tok-1"
    assert len(seen_logins) >= 2, "재접속 자체가 일어나지 않았다"
    assert seen_logins[1] == "tok-2", (
        f"재접속이 만료된 토큰을 재사용했다: {seen_logins[:2]} — 토큰을 새로 발급해야 한다"
    )


def test_auth_failure_is_distinguishable_from_network_drop_and_backs_off_further(caplog):
    """토큰 인증 실패는 (1) 로그에서 네트워크 단절과 구분되고, (2) 연속되면
    같은 주기로 영원히 재시도하지 않고 별도의 더 긴 백오프로 확대되며,
    (3) 임계 횟수를 넘으면 ERROR 한 줄로 승격된다 — 워치독이 이 줄을 본다."""
    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({
            "trnm": "LOGIN", "return_code": 805004,
            "return_msg": "토큰 인증에 실패했습니다. 접속을 종료합니다",
        }))

    async def run():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            feed = KiwoomRealtimeFeed(
                access_token="always-bad", ws_url=f"ws://127.0.0.1:{port}",
                reconnect_initial_delay=0.01, reconnect_max_delay=0.02,
                auth_reconnect_max_delay=0.5,
            )
            task = asyncio.create_task(feed.run())
            # 조건 기반 대기 — 고정 반복(250×0.02s≈5초 예산)은 전체 스위트가
            # 병렬로 돌 때 CPU 경합으로 백오프 수면·스케줄링이 늘어지면 임계
            # 도달 전에 소진돼 간헐 실패했다(2026-08-25~26 3회 관측, 격리 실행은
            # 항상 통과). 벽시계 데드라인을 넉넉히 두되 조건이 차면 즉시 빠지므로
            # 정상 실행 시간은 그대로다(~2초).
            deadline = asyncio.get_running_loop().time() + 30.0
            while asyncio.get_running_loop().time() < deadline:
                if feed.health().consecutive_auth_failures >= AUTH_ALERT_THRESHOLD:
                    break
                await asyncio.sleep(0.02)
            await feed.close()
            await _drain(task)
            return feed

    with caplog.at_level(logging.WARNING, logger="quant.adapters.brokers.kiwoom.websocket"):
        feed = asyncio.run(run())

    assert feed.health().consecutive_auth_failures >= AUTH_ALERT_THRESHOLD
    messages = [r.getMessage() for r in caplog.records]
    assert any("인증 실패(토큰)" in m for m in messages), messages
    assert not any("연결 끊김(네트워크" in m for m in messages), (
        "인증 실패를 네트워크 단절로 오분류했다"
    )
    escalated = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert escalated, "연속 인증 실패가 ERROR로 승격되지 않았다"
    assert any(AUTH_ALERT_MARKER in r.getMessage() for r in escalated), (
        "워치독이 grep할 마커가 ERROR 로그에 없다"
    )
    # 인증 실패 백오프는 네트워크용 상한(0.02s)이 아니라 인증용 상한으로 커져야 한다
    assert feed._reconnect_delay > 0.02
