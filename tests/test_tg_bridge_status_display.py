"""tg_bridge.py `/status` 명령의 보유종목 표시명 — 소유자 지적(2026-08-26):
"/status 라고 쳐도 이상하게 몇가지 종목들은 번호만 나오더라고. 한국주식 이름
확실하게 번호에서 이름으로 보일 수 있게."

`/status`는 그동안 `portfolio.json`의 심볼 코드를 이름 변환 없이 그대로
join해서 보여줬다(`/balance`·`/daily_record`는 진작 `_watchlist_names()` +
`_display_symbol()`로 이름을 붙이고 있었다) — 이 파일은 `/status`도 같은
폴백(관심종목 파일에 등록 시점에 적어둔 이름, 없으면 코드만)을 쓰는지 고정한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "server" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import tg_bridge  # noqa: E402
from quant.trade.control import TradingControl  # noqa: E402


def _control(tmp_path) -> TradingControl:
    return TradingControl(state_path=tmp_path / "control.json")


def _write_portfolio(tmp_path, positions: dict) -> Path:
    import json

    path = tmp_path / "portfolio.json"
    path.write_text(
        json.dumps({"cash": 1_000_000, "positions": positions}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _write_watchlist(tmp_path, entries: list[dict]) -> Path:
    path = tmp_path / "watchlist.yaml"
    tg_bridge.save_watchlist(entries, path=path)
    return path


def test_status_shows_watchlist_name_for_kr_symbol(tmp_path, monkeypatch):
    portfolio_path = _write_portfolio(tmp_path, {"088350": {"qty": 10}})
    monkeypatch.setattr(tg_bridge, "PORTFOLIO_STATE_PATH", portfolio_path)
    watchlist_path = _write_watchlist(
        tmp_path, [{"symbol": "088350", "name": "한화생명"}]
    )
    monkeypatch.setattr(tg_bridge, "WATCHLIST_PATH", watchlist_path)

    reply = tg_bridge.handle_control_command("/status", _control(tmp_path))

    assert "한화생명 (088350)" in reply
    assert "포지션: 088350" not in reply  # 코드만 노출되면 안 된다


def test_status_falls_back_to_code_when_no_watchlist_name(tmp_path, monkeypatch):
    """DART/KIND 등 어느 소스에도 이름이 없으면 코드만 — 0이나 "없음"으로 위장 금지."""
    portfolio_path = _write_portfolio(tmp_path, {"999999": {"qty": 5}})
    monkeypatch.setattr(tg_bridge, "PORTFOLIO_STATE_PATH", portfolio_path)
    watchlist_path = _write_watchlist(tmp_path, [])  # 이 종목은 관심종목에 없음
    monkeypatch.setattr(tg_bridge, "WATCHLIST_PATH", watchlist_path)

    reply = tg_bridge.handle_control_command("/status", _control(tmp_path))

    assert "포지션: 999999" in reply


def test_status_with_no_open_positions_shows_none(tmp_path, monkeypatch):
    portfolio_path = _write_portfolio(tmp_path, {})
    monkeypatch.setattr(tg_bridge, "PORTFOLIO_STATE_PATH", portfolio_path)
    monkeypatch.setattr(tg_bridge, "WATCHLIST_PATH", tmp_path / "watchlist.yaml")

    reply = tg_bridge.handle_control_command("/status", _control(tmp_path))

    assert "포지션: 없음" in reply
