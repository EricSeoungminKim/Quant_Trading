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
