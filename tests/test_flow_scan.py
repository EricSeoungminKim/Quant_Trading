"""장중 거래대금 발굴(quant.analyze.flow_scan) 테스트.

`flow_candidates`는 순수 함수라 오프라인으로 전부 검증한다. CLI 출력 계약
(`FLOW: ...` 한 줄 / 무출력)은 `cmd_flow_scan`을 가짜 Toss 클라이언트로 배선해
검증한다 — 진짜 네트워크는 절대 타지 않는다."""
from __future__ import annotations

import argparse

from quant.analyze.flow_scan import flow_candidates


def _row(symbol: str) -> dict:
    return {"rank": 1, "symbol": symbol, "currency": "KRW",
            "price": {"lastPrice": "1000"}, "tradingAmount": "1000000"}


# --------------------------------------------------------------- flow_candidates


def test_excludes_existing_symbols():
    rows = [_row("005930"), _row("000660"), _row("035420")]
    out = flow_candidates(rows, existing={"005930"}, market="KR")
    assert out == ["000660", "035420"]


def test_filters_by_market_shape_kr():
    # US 티커(AAPL)가 KR 랭킹 응답에 섞여 들어와도 KR 형태(6자리 숫자)가 아니면 제외.
    rows = [_row("005930"), _row("AAPL"), _row("12345"), _row("0059301")]
    out = flow_candidates(rows, existing=set(), market="KR")
    assert out == ["005930"]


def test_filters_by_market_shape_us():
    rows = [_row("AAPL"), _row("005930"), _row("TQQQ"), _row("TOOLONG1")]
    out = flow_candidates(rows, existing=set(), market="US")
    assert out == ["AAPL", "TQQQ"]


def test_top_truncates_and_preserves_ranking_order():
    rows = [_row(s) for s in ["000660", "005930", "035420", "051910", "005380"]]
    out = flow_candidates(rows, existing=set(), market="KR", top=3)
    assert out == ["000660", "005930", "035420"]


def test_empty_rankings_returns_empty_list():
    assert flow_candidates([], existing=set(), market="KR") == []


def test_deduplicates_repeated_symbols():
    rows = [_row("005930"), _row("005930"), _row("000660")]
    out = flow_candidates(rows, existing=set(), market="KR")
    assert out == ["005930", "000660"]


def test_empty_symbol_row_skipped():
    rows = [{"rank": 1, "symbol": "", "currency": "KRW"}, _row("005930")]
    out = flow_candidates(rows, existing=set(), market="KR")
    assert out == ["005930"]


# --------------------------------------------------------------- cmd_flow_scan (CLI 배선)


class _FakeStrategiesSettings:
    """cmd_flow_scan이 쓰는 `settings.strategies`만 흉내 낸다."""

    strategies = {"donchian": {"symbols": ["TQQQ", "SQQQ"]}}


class _FakeTossClient:
    def __init__(self, rows):
        self._rows = rows

    def rankings(self, *, type, market_country, duration, count):
        return {"rankings": self._rows[:count]}


def _cli_args(root, market="KR", top=30):
    return argparse.Namespace(market=market, top=top, root=str(root))


def test_cmd_flow_scan_prints_flow_line(tmp_path, monkeypatch, capsys):
    from quant.apps import cli

    monkeypatch.setattr(cli, "load_settings", lambda: _FakeStrategiesSettings())
    monkeypatch.setattr("quant.apps.assembly.build_toss_client",
                         lambda: _FakeTossClient([_row("000660"), _row("035420")]))

    cli.cmd_flow_scan(_cli_args(tmp_path))

    out = capsys.readouterr().out.strip()
    assert out == "FLOW: 000660 035420"


def test_cmd_flow_scan_silent_when_no_new_candidates(tmp_path, monkeypatch, capsys):
    from quant.apps import cli

    monkeypatch.setattr(cli, "load_settings", lambda: _FakeStrategiesSettings())
    # 랭킹 전량이 이미 앵커(TQQQ)와 겹치는 경우를 흉내 — 여기선 형태가 안 맞는
    # 행만 줘서 전부 필터링되는 경로를 검증한다.
    monkeypatch.setattr("quant.apps.assembly.build_toss_client",
                         lambda: _FakeTossClient([_row("AAPL")]))  # KR인데 US 형태

    cli.cmd_flow_scan(_cli_args(tmp_path, market="KR"))

    out = capsys.readouterr().out
    assert out == ""


def test_cmd_flow_scan_missing_credentials_exits_2(tmp_path, monkeypatch, capsys):
    from quant.apps import cli
    from quant.apps.assembly import MissingCredentials

    monkeypatch.setattr(cli, "load_settings", lambda: _FakeStrategiesSettings())

    def _raise():
        raise MissingCredentials("no creds")

    monkeypatch.setattr("quant.apps.assembly.build_toss_client", _raise)

    import pytest
    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_flow_scan(_cli_args(tmp_path))
    assert exc_info.value.code == 2
