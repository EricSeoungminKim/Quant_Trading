"""`cli naver-fundamentals`/`cli dart-fundamentals` — 새로 배선한 진입점.

`naver_quant.py`/`dart_financials.py`의 `fetch_and_persist`는 오랫동안 호출부가
0건이었다(만든 것 != 배선된 것). 이 테스트는 (1) argparse가 올바른 기본값으로
커맨드를 연결하는지, (2) `dart-fundamentals`의 종목 목록 결정 우선순위
(--symbols > watchlist.yaml KR 종목 > 대상 없음)가 맞는지, (3) 실패(대상 없음 /
개별 종목 에러)가 조용히 삼켜지지 않고 출력되는지를 확인한다.
"""
from __future__ import annotations

import sys

import pytest
import yaml

from quant.apps import cli


# ---------------------------------------------------------------------------
# argparse 배선
# ---------------------------------------------------------------------------


def test_naver_fundamentals_wires_root_default(monkeypatch):
    captured: dict = {}

    def fake_cmd(args):
        captured["root"] = args.root

    monkeypatch.setattr(cli, "cmd_naver_fundamentals", fake_cmd)
    monkeypatch.setattr(sys, "argv", ["quant", "naver-fundamentals"])

    cli.main()

    assert captured["root"] == "."


def test_dart_fundamentals_wires_defaults(monkeypatch):
    captured: dict = {}

    def fake_cmd(args):
        captured.update(
            root=args.root, symbols=args.symbols,
            bsns_year=args.bsns_year, reprt_code=args.reprt_code,
        )

    monkeypatch.setattr(cli, "cmd_dart_fundamentals", fake_cmd)
    monkeypatch.setattr(sys, "argv", ["quant", "dart-fundamentals"])

    cli.main()

    assert captured == {
        "root": ".", "symbols": None, "bsns_year": None, "reprt_code": "11011",
    }


def test_dart_fundamentals_wires_explicit_args(monkeypatch):
    captured: dict = {}

    def fake_cmd(args):
        captured.update(
            symbols=args.symbols, bsns_year=args.bsns_year, reprt_code=args.reprt_code,
        )

    monkeypatch.setattr(cli, "cmd_dart_fundamentals", fake_cmd)
    monkeypatch.setattr(sys, "argv", [
        "quant", "dart-fundamentals",
        "--symbols", "005930 000660", "--bsns-year", "2024", "--reprt-code", "11012",
    ])

    cli.main()

    assert captured == {
        "symbols": "005930 000660", "bsns_year": "2024", "reprt_code": "11012",
    }


# ---------------------------------------------------------------------------
# _kr_watchlist_symbols — 대상 종목 기본값 산출
# ---------------------------------------------------------------------------


def test_kr_watchlist_symbols_filters_kr_only(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "watchlist.yaml").write_text(
        yaml.dump({"symbols": [
            {"symbol": "005930", "name": "삼성전자"},
            {"symbol": "TQQQ", "name": "TQQQ"},
            "000660",  # 문자열 항목도 지원
        ]}),
        encoding="utf-8",
    )

    out = cli._kr_watchlist_symbols(tmp_path)

    assert out == ["005930", "000660"]


def test_kr_watchlist_symbols_missing_file_returns_empty(tmp_path):
    assert cli._kr_watchlist_symbols(tmp_path) == []


def test_kr_watchlist_symbols_broken_yaml_returns_empty(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "watchlist.yaml").write_text("not: valid: yaml: [", encoding="utf-8")

    assert cli._kr_watchlist_symbols(tmp_path) == []


# ---------------------------------------------------------------------------
# cmd_dart_fundamentals — 종목 목록 결정 + 실패 표면화
# ---------------------------------------------------------------------------


def _args(**overrides):
    base = dict(root=".", symbols=None, bsns_year=None, reprt_code="11011")
    base.update(overrides)
    return type("Args", (), base)()


def test_dart_fundamentals_prefers_explicit_symbols_over_watchlist(tmp_path, monkeypatch, capsys):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "watchlist.yaml").write_text(
        yaml.dump({"symbols": [{"symbol": "999999"}]}), encoding="utf-8",
    )

    captured: dict = {}

    def fake_fetch_and_persist(stock_codes, bsns_year, reprt_code, root, **kwargs):
        captured["stock_codes"] = stock_codes
        return {"requested": len(stock_codes), "added": 0, "errors": []}

    monkeypatch.setattr(
        "quant.collect.sources.dart_financials.fetch_and_persist", fake_fetch_and_persist,
    )

    cli.cmd_dart_fundamentals(_args(root=str(tmp_path), symbols="005930 000660"))

    assert captured["stock_codes"] == ["005930", "000660"]


def test_dart_fundamentals_falls_back_to_kr_watchlist(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "watchlist.yaml").write_text(
        yaml.dump({"symbols": [{"symbol": "005930"}, {"symbol": "TQQQ"}]}), encoding="utf-8",
    )

    captured: dict = {}

    def fake_fetch_and_persist(stock_codes, bsns_year, reprt_code, root, **kwargs):
        captured["stock_codes"] = stock_codes
        return {"requested": len(stock_codes), "added": 0, "errors": []}

    monkeypatch.setattr(
        "quant.collect.sources.dart_financials.fetch_and_persist", fake_fetch_and_persist,
    )

    cli.cmd_dart_fundamentals(_args(root=str(tmp_path)))

    assert captured["stock_codes"] == ["005930"]


def test_dart_fundamentals_no_target_prints_message_and_skips_fetch(tmp_path, monkeypatch, capsys):
    called = False

    def fake_fetch_and_persist(*args, **kwargs):
        nonlocal called
        called = True
        return {"requested": 0, "added": 0, "errors": []}

    monkeypatch.setattr(
        "quant.collect.sources.dart_financials.fetch_and_persist", fake_fetch_and_persist,
    )

    cli.cmd_dart_fundamentals(_args(root=str(tmp_path)))

    assert not called
    assert "대상 종목 없음" in capsys.readouterr().out


def test_dart_fundamentals_prints_per_symbol_errors(tmp_path, monkeypatch, capsys):
    def fake_fetch_and_persist(stock_codes, bsns_year, reprt_code, root, **kwargs):
        return {
            "requested": len(stock_codes), "added": 0,
            "errors": ["999999: corp_code 매핑 없음"],
        }

    monkeypatch.setattr(
        "quant.collect.sources.dart_financials.fetch_and_persist", fake_fetch_and_persist,
    )

    cli.cmd_dart_fundamentals(_args(root=str(tmp_path), symbols="999999"))

    out = capsys.readouterr().out
    assert "오류 1건" in out
    assert "999999: corp_code 매핑 없음" in out


@pytest.mark.parametrize(
    "now_year,now_month,expected",
    [
        (2026, 8, "2025"),   # 4월 이후 — 작년도 사업보고서가 이미 제출됨
        (2026, 4, "2025"),   # 경계값: 4월
        (2026, 3, "2024"),   # 경계값: 3월 — 아직 작년도 사업보고서 전이라 재작년도로
        (2026, 1, "2024"),
    ],
)
def test_dart_fundamentals_bsns_year_default_by_month(
    tmp_path, monkeypatch, now_year, now_month, expected,
):
    import datetime as _dt

    class _FixedDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime(now_year, now_month, 15)

    # `cmd_dart_fundamentals`가 함수 안에서 `from datetime import datetime`으로 매번
    # 새로 임포트하므로, `datetime` 모듈의 `datetime` 속성 자체를 갈아끼워야 그 다음
    # 임포트가 페이크를 집는다(모듈 레벨 이름을 캐시해 두고 patch하는 방식은 안 통함).
    monkeypatch.setattr("datetime.datetime", _FixedDatetime)

    captured: dict = {}

    def fake_fetch_and_persist(stock_codes, bsns_year, reprt_code, root, **kwargs):
        captured["bsns_year"] = bsns_year
        return {"requested": len(stock_codes), "added": 0, "errors": []}

    monkeypatch.setattr(
        "quant.collect.sources.dart_financials.fetch_and_persist", fake_fetch_and_persist,
    )

    cli.cmd_dart_fundamentals(_args(root=str(tmp_path), symbols="005930"))

    assert captured["bsns_year"] == expected


def test_dart_fundamentals_explicit_bsns_year_overrides_default(tmp_path, monkeypatch):
    captured: dict = {}

    def fake_fetch_and_persist(stock_codes, bsns_year, reprt_code, root, **kwargs):
        captured["bsns_year"] = bsns_year
        return {"requested": len(stock_codes), "added": 0, "errors": []}

    monkeypatch.setattr(
        "quant.collect.sources.dart_financials.fetch_and_persist", fake_fetch_and_persist,
    )

    cli.cmd_dart_fundamentals(_args(root=str(tmp_path), symbols="005930", bsns_year="2019"))

    assert captured["bsns_year"] == "2019"


# ---------------------------------------------------------------------------
# cmd_naver_fundamentals
# ---------------------------------------------------------------------------


def test_naver_fundamentals_calls_fetch_and_persist_and_prints_summary(tmp_path, monkeypatch, capsys):
    def fake_fetch_and_persist(root, **kwargs):
        assert str(root) == str(tmp_path)
        return {"fetched": 100, "added": 7, "date": "2026-08-19"}

    monkeypatch.setattr(
        "quant.collect.sources.naver_quant.fetch_and_persist", fake_fetch_and_persist,
    )

    cli.cmd_naver_fundamentals(_args(root=str(tmp_path)))

    out = capsys.readouterr().out
    assert "조회 100건" in out
    assert "신규 적재 7건" in out
    assert "2026-08-19" in out
