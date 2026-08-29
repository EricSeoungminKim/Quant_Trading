"""`cli peek` — 읽기 전용 시세 조회 진입점(2026-08-30, LLM 판단 프로세스용).

argparse 배선 + 정상/실패 경로가 JSON으로 출력되는지, `--n` 상한이 지켜지는지를
확인한다. 네트워크 호출 없음 — `TossDataFeed`를 페이크로 대체한다(cmd_peek는
`build_market_data`가 아니라 `TossDataFeed`를 직접 쓴다 — Kiwoom 실시간 웹소켓
세션을 라이브 엔진과 다투지 않기 위해서다, cmd_peek docstring 참고).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import pandas as pd
import pytest

from quant.apps import cli
from quant.core.models import Quote


class FakeTossDataFeed:
    def __init__(self, client, symbols=None):
        self.symbols = symbols

    def quote(self, symbol):
        return Quote(symbol=symbol, ts=datetime(2026, 8, 30, tzinfo=timezone.utc), price=123.45)

    def history(self, symbol, interval, n):
        idx = pd.date_range("2026-08-29", periods=n, freq="5min", tz=timezone.utc)
        return pd.DataFrame({
            "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
            "close": [100.5] * n, "volume": [1000.0] * n,
        }, index=idx)


class FailingTossDataFeed(FakeTossDataFeed):
    def history(self, symbol, interval, n):
        raise RuntimeError("boom")


def _args(**overrides):
    base = dict(symbol="005930", interval="5m", n=40)
    base.update(overrides)
    return type("Args", (), base)()


# ---------------------------------------------------------------------------
# argparse 배선
# ---------------------------------------------------------------------------


def test_peek_wires_defaults(monkeypatch):
    captured: dict = {}

    def fake_cmd(args):
        captured.update(symbol=args.symbol, interval=args.interval, n=args.n)

    monkeypatch.setattr(cli, "cmd_peek", fake_cmd)
    monkeypatch.setattr(sys, "argv", ["quant", "peek", "--symbol", "005930"])

    cli.main()

    assert captured == {"symbol": "005930", "interval": "5m", "n": 40}


def test_peek_wires_explicit_args(monkeypatch):
    captured: dict = {}

    def fake_cmd(args):
        captured.update(symbol=args.symbol, interval=args.interval, n=args.n)

    monkeypatch.setattr(cli, "cmd_peek", fake_cmd)
    monkeypatch.setattr(sys, "argv", [
        "quant", "peek", "--symbol", "TQQQ", "--interval", "1m", "--n", "10",
    ])

    cli.main()

    assert captured == {"symbol": "TQQQ", "interval": "1m", "n": 10}


# ---------------------------------------------------------------------------
# cmd_peek — 동작
# ---------------------------------------------------------------------------


def test_peek_prints_quote_and_bars(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: None)
    monkeypatch.setattr("quant.apps.assembly.build_toss_client", lambda: object())
    monkeypatch.setattr("quant.adapters.brokers.toss.datafeed.TossDataFeed", FakeTossDataFeed)

    cli.cmd_peek(_args(n=3))

    out = json.loads(capsys.readouterr().out)
    assert out["symbol"] == "005930"
    assert out["interval"] == "5m"
    assert out["quote"] == {"price": 123.45, "ts": "2026-08-30T00:00:00+00:00"}
    assert len(out["bars"]) == 3
    assert out["bars"][0]["close"] == 100.5


def test_peek_caps_n_at_max(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: None)
    monkeypatch.setattr("quant.apps.assembly.build_toss_client", lambda: object())
    monkeypatch.setattr("quant.adapters.brokers.toss.datafeed.TossDataFeed", FakeTossDataFeed)

    cli.cmd_peek(_args(n=10_000))

    out = json.loads(capsys.readouterr().out)
    assert len(out["bars"]) == cli._PEEK_MAX_N


def test_peek_rejects_non_positive_n(monkeypatch):
    monkeypatch.setattr(cli, "load_settings", lambda: None)

    with pytest.raises(SystemExit):
        cli.cmd_peek(_args(n=0))


def test_peek_missing_credentials_exits(monkeypatch):
    from quant.apps.assembly import MissingCredentials

    def boom():
        raise MissingCredentials("no creds")

    monkeypatch.setattr(cli, "load_settings", lambda: None)
    monkeypatch.setattr("quant.apps.assembly.build_toss_client", boom)

    with pytest.raises(SystemExit):
        cli.cmd_peek(_args())


def test_peek_history_failure_surfaces_as_json_error_field(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: None)
    monkeypatch.setattr("quant.apps.assembly.build_toss_client", lambda: object())
    monkeypatch.setattr("quant.adapters.brokers.toss.datafeed.TossDataFeed", FailingTossDataFeed)

    cli.cmd_peek(_args())

    out = json.loads(capsys.readouterr().out)
    assert out["bars"] is None
    assert "boom" in out["bars_error"]
    # quote는 history 실패와 무관하게 정상 조회됐어야 한다.
    assert out["quote"]["price"] == 123.45
