"""`cli spread-sample --extra-symbols` (2026-09-06 live-readiness §4).

`spread_sample_us.sh`가 레버리지 페어 앵커(TQQQ/SQQQ/SOXL/SOXS)를 워치리스트/
전략 기본 목록과 무관하게 강제하는 데 쓰는 플래그다 — letf_pair_sox처럼
top-level `symbols:`가 빈 전략은 그 기본 목록에 안 잡히므로, 이 플래그가
`--symbols`(명시 지정)이든 기본(워치리스트+전략 앵커)이든 항상 더해져야 한다.
네트워크는 절대 타지 않는다 — `sample_spread`/`build_toss_client`를 가짜로
갈아끼운다.
"""
from __future__ import annotations

import argparse

from quant.collect.spread import SpreadSample


def _args(**overrides):
    base = dict(
        market=None, symbols=None, extra_symbols=None,
        rounds=1, interval_seconds=1.0, root=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _patch_network(monkeypatch, captured: dict):
    def _fake_sample_spread(client, symbols, *, now, min_interval):
        captured["symbols"] = list(symbols)
        return SpreadSample(rows=[], dropped=[], empty=[], failed=[])

    monkeypatch.setattr("quant.collect.spread.sample_spread", _fake_sample_spread)
    monkeypatch.setattr("quant.apps.assembly.build_toss_client", lambda: object())


def test_extra_symbols_are_added_on_top_of_explicit_symbols(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    captured: dict = {}
    _patch_network(monkeypatch, captured)

    from quant.apps.cli import cmd_spread_sample

    cmd_spread_sample(_args(
        market="US", symbols=["TQQQ"], extra_symbols=["SOXL", "SOXS"], root=str(tmp_path),
    ))

    assert captured["symbols"] == ["TQQQ", "SOXL", "SOXS"]


def test_extra_symbols_are_added_on_top_of_the_default_watchlist(tmp_path, monkeypatch, capsys):
    """`--symbols` 없이 기본(워치리스트+전략) 경로를 타도 --extra-symbols는
    여전히 더해진다 — 실제 config/settings.yaml의 전략 앵커까지 섞인 기본
    목록 위에, 네 심볼이 (중복 없이) 얹혀야 한다."""
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    # 워치리스트 파일 자체가 없으면 FileWatchlistUniverse.refresh()가 빈 목록을
    # 준다 — 기본 목록은 실제 settings.yaml의 전략 symbols: 만 남는다.
    captured: dict = {}
    _patch_network(monkeypatch, captured)

    from quant.apps.cli import cmd_spread_sample

    cmd_spread_sample(_args(extra_symbols=["TQQQ", "SQQQ", "SOXL", "SOXS"], root=str(tmp_path)))

    symbols = captured["symbols"]
    assert {"TQQQ", "SQQQ", "SOXL", "SOXS"} <= set(symbols)
    assert len(symbols) == len(set(symbols)), "중복 없이 한 번씩만"


def test_extra_symbols_are_deduplicated_against_the_default_set(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    captured: dict = {}
    _patch_network(monkeypatch, captured)

    from quant.apps.cli import cmd_spread_sample

    cmd_spread_sample(_args(
        market="US", symbols=["TQQQ", "SOXL"], extra_symbols=["TQQQ", "SQQQ", "SOXL", "SOXS"],
        root=str(tmp_path),
    ))

    assert captured["symbols"] == ["TQQQ", "SOXL", "SQQQ", "SOXS"]


def test_extra_symbols_still_respects_the_market_filter(tmp_path, monkeypatch, capsys):
    """--market US 필터는 --extra-symbols에도 그대로 적용된다 — KR 심볼을
    실수로 넣어도 US 리포트에 섞이지 않는다."""
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    captured: dict = {}
    _patch_network(monkeypatch, captured)

    from quant.apps.cli import cmd_spread_sample

    cmd_spread_sample(_args(
        market="US", symbols=["TQQQ"], extra_symbols=["SOXL", "005930"], root=str(tmp_path),
    ))

    assert captured["symbols"] == ["TQQQ", "SOXL"]
