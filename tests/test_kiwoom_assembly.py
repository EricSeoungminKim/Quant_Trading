"""build_kiwoom_realtime_route / _wait_for_connection 조립 단위 테스트.

전부 페이크만 쓴다 — 실 API 호출 없음(브로커/CLAUDE.md 금지사항). 연결 성공/실패
시나리오는 `_wait_for_connection`을 페이크 feed로 직접 검증하고(실 웹소켓 없이도
타이밍 로직만 떼어 테스트 가능), enabled=false/자격증명 없음 경로는
`build_kiwoom_realtime_route`를 직접 호출해 네트워크를 전혀 타지 않는 것까지 확인한다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from quant.apps.assembly import (
    _wait_for_connection,
    build_kiwoom_realtime_route,
    build_kiwoom_us_route,
)


class FakeClock:
    def now(self) -> datetime:
        return datetime(2024, 6, 3, 5, 0, tzinfo=timezone.utc)


class ScriptedFeed:
    """health() 호출마다 미리 정해둔 connected 값을 하나씩 소비한다 — 소진되면
    마지막 값을 계속 돌려준다. connect→subscribe 실패로 인한 flicker,
    안정적 연결/미연결을 모두 결정론적으로 재현하기 위함."""

    def __init__(self, connected_sequence: list[bool]):
        self._sequence = list(connected_sequence)
        self._i = 0

    def health(self) -> SimpleNamespace:
        val = self._sequence[min(self._i, len(self._sequence) - 1)]
        self._i += 1
        return SimpleNamespace(connected=val)


def _clean_kiwoom_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("KIWOOM_APP_KEY", "KIWOOM_SECRET_KEY", "KIWOOM_BASE_URL"):
        monkeypatch.delenv(key, raising=False)


# --------------------------------------------------------- _wait_for_connection

def test_wait_for_connection_returns_true_when_stably_connected():
    feed = ScriptedFeed([True] * 20)
    assert _wait_for_connection(feed, timeout=1.0, poll_interval=0.01, debounce=0.02) is True


def test_wait_for_connection_returns_false_when_never_connects():
    feed = ScriptedFeed([False] * 50)
    assert _wait_for_connection(feed, timeout=0.2, poll_interval=0.02, debounce=0.02) is False


def test_wait_for_connection_debounces_a_connect_then_immediately_fail_flicker():
    """connect()가 성공하자마자 subscribe()가 실패해 곧바로 connected=False로 돌아가는
    상황(구독 실패로 인한 flicker)을 "연결됨"으로 오판하면 안 된다."""
    # 처음 True 하나, 그 다음부터는 계속 False (flicker 후 재접속 루프)
    feed = ScriptedFeed([True, False, False, False, False, False, False, False, False, False, False])
    assert _wait_for_connection(feed, timeout=1.0, poll_interval=0.01, debounce=0.05) is False


# ------------------------------------------------------ build_kiwoom_realtime_route

def test_disabled_by_default_returns_none_and_touches_no_network(monkeypatch):
    """회귀 가드: enabled=false면 어떤 경로도 바뀌지 않는다."""
    _clean_kiwoom_env(monkeypatch)
    monkeypatch.setenv("KIWOOM_APP_KEY", "would-be-used-if-enabled")
    monkeypatch.setenv("KIWOOM_SECRET_KEY", "would-be-used-if-enabled")

    route = build_kiwoom_realtime_route(
        {"kiwoom": {"realtime": {"enabled": False}}}, ["TQQQ", "SQQQ"], FakeClock(),
    )

    assert route is None


def test_missing_kiwoom_config_key_returns_none(monkeypatch):
    _clean_kiwoom_env(monkeypatch)
    route = build_kiwoom_realtime_route({}, ["TQQQ", "SQQQ"], FakeClock())
    assert route is None


def test_empty_symbols_returns_none_even_if_enabled(monkeypatch):
    _clean_kiwoom_env(monkeypatch)
    monkeypatch.setenv("KIWOOM_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_SECRET_KEY", "s")

    route = build_kiwoom_realtime_route(
        {"kiwoom": {"realtime": {"enabled": True}}}, [], FakeClock(),
    )

    assert route is None


def test_enabled_without_credentials_warns_and_returns_none(monkeypatch, caplog):
    _clean_kiwoom_env(monkeypatch)  # KIWOOM_APP_KEY/SECRET_KEY 없음

    with caplog.at_level("WARNING"):
        route = build_kiwoom_realtime_route(
            {"kiwoom": {"realtime": {"enabled": True}}}, ["TQQQ"], FakeClock(),
        )

    assert route is None
    assert any("KIWOOM_APP_KEY" in rec.message for rec in caplog.records)


def test_all_us_symbols_returns_none_without_touching_network(monkeypatch, caplog):
    """2026-08-29 결함 2 회귀 가드: 유니버스가 전부 US 심볼이면 자격증명이 있어도
    웹소켓 라우트를 아예 만들지 않는다(빈 REG를 보낼 이유가 없다) — 토큰 발급도
    시도하지 않는다."""
    _clean_kiwoom_env(monkeypatch)
    monkeypatch.setenv("KIWOOM_APP_KEY", "would-be-used-if-any-kr-symbols")
    monkeypatch.setenv("KIWOOM_SECRET_KEY", "would-be-used-if-any-kr-symbols")

    from quant.adapters.brokers.kiwoom.client import KiwoomClient

    def _boom(self):
        raise AssertionError("US 심볼만 있는데 토큰 발급을 시도했다")

    monkeypatch.setattr(KiwoomClient, "access_token", _boom)

    with caplog.at_level("WARNING"):
        route = build_kiwoom_realtime_route(
            {"kiwoom": {"realtime": {"enabled": True}}}, ["TQQQ", "SQQQ"], FakeClock(),
        )

    assert route is None
    assert any("실시간 구독에서 제외" in rec.message for rec in caplog.records)


def test_mixed_symbols_subscribes_kr_only(monkeypatch, caplog):
    """결함 2 핵심 회귀: KR+US가 섞인 유니버스에서 웹소켓에는 KR 심볼만 실린다.
    US 심볼은 로그로만 남고 REG/route.symbols에는 등장하지 않는다."""
    _clean_kiwoom_env(monkeypatch)
    monkeypatch.setenv("KIWOOM_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_SECRET_KEY", "s")

    from quant.adapters.brokers.kiwoom.client import KiwoomClient

    monkeypatch.setattr(KiwoomClient, "access_token", lambda self: "tok")

    captured = {}

    class FakeFeed:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def health(self):
            return SimpleNamespace(connected=True)

    monkeypatch.setattr(
        "quant.adapters.brokers.kiwoom.websocket.KiwoomRealtimeFeed", FakeFeed,
    )
    # build_kiwoom_realtime_route는 KiwoomRealtimeFeed를 함수 내부에서 다시
    # import하므로, 모듈 속성 패치 + threading.Thread를 막아 백그라운드 스레드가
    # 실제 이벤트 루프를 돌리지 않게 한다(FakeFeed는 asyncio 코루틴이 없다).
    monkeypatch.setattr("quant.apps.assembly.threading.Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    # _wait_for_connection의 debounce(기본 1.0초) 실제 대기를 없애 테스트를 빠르게 한다
    # — FakeFeed는 항상 connected=True이므로 debounce 값 자체는 이 테스트와 무관하다.
    monkeypatch.setattr("quant.apps.assembly.time.sleep", lambda s: None)

    with caplog.at_level("WARNING"):
        route = build_kiwoom_realtime_route(
            {"kiwoom": {"realtime": {"enabled": True}}},
            ["005930", "TQQQ", "000660", "SQQQ"],
            FakeClock(),
        )

    assert route is not None
    assert route.symbols == frozenset({"005930", "000660"})
    assert captured["symbols"] == ["005930", "000660"]
    assert any(
        "실시간 구독에서 제외" in rec.message and "TQQQ" in rec.message and "SQQQ" in rec.message
        for rec in caplog.records
    )


# --------------------------------------------------------- build_kiwoom_us_route

def _clean_kiwoom_global_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("KIWOOM_GLOBAL_APP_KEY", "KIWOOM_GLOBAL_SECRET_KEY", "KIWOOM_GLOBAL_BASE_URL"):
        monkeypatch.delenv(key, raising=False)


def test_us_route_returns_none_when_no_us_symbols_and_touches_no_network(monkeypatch):
    """회귀 가드: KR 심볼만 있으면 자격증명이 있어도 네트워크를 아예 타지 않는다."""
    _clean_kiwoom_global_env(monkeypatch)
    monkeypatch.setenv("KIWOOM_GLOBAL_APP_KEY", "would-be-used-if-there-were-us-symbols")
    monkeypatch.setenv("KIWOOM_GLOBAL_SECRET_KEY", "would-be-used-if-there-were-us-symbols")

    route = build_kiwoom_us_route({}, ["005930", "000660"], FakeClock())

    assert route is None


def test_us_route_returns_none_with_empty_symbol_list(monkeypatch):
    _clean_kiwoom_global_env(monkeypatch)
    monkeypatch.setenv("KIWOOM_GLOBAL_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_GLOBAL_SECRET_KEY", "s")

    route = build_kiwoom_us_route({}, [], FakeClock())

    assert route is None


def test_us_route_returns_none_without_credentials(monkeypatch, caplog):
    """글로벌 키가 없으면 조용히 Toss 단독으로 넘어간다 — 라우트를 등록하지 않는다."""
    _clean_kiwoom_global_env(monkeypatch)  # KIWOOM_GLOBAL_APP_KEY/SECRET_KEY 없음

    with caplog.at_level("INFO"):
        route = build_kiwoom_us_route({}, ["TQQQ", "SQQQ"], FakeClock())

    assert route is None
    assert any("KIWOOM_GLOBAL_APP_KEY" in rec.message for rec in caplog.records)


def test_us_route_returns_none_when_token_fetch_fails(monkeypatch, caplog):
    """자격증명은 있지만 토큰 발급이 실패하면(네트워크 오류 등) 라우트를 등록하지 않는다."""
    _clean_kiwoom_global_env(monkeypatch)
    monkeypatch.setenv("KIWOOM_GLOBAL_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_GLOBAL_SECRET_KEY", "s")

    from quant.adapters.brokers.kiwoom.client import KiwoomClient

    def _boom(self):
        raise RuntimeError("token endpoint unreachable")

    monkeypatch.setattr(KiwoomClient, "access_token", _boom)

    with caplog.at_level("WARNING"):
        route = build_kiwoom_us_route({}, ["TQQQ"], FakeClock())

    assert route is None
    assert any("토큰 발급 실패" in rec.message for rec in caplog.records)
