"""tg_bridge.py `/rest` `/live` `/flatten day` — 2026-09-01 소유자 지시.

owner: 텔레그램으로 매매를 정지/재개하면서 REST(수동 정지)와 회로차단기 자동
중단을 구분해서 보고 싶고, 단타 포지션만 즉시 청산하는 명령을 원한다.

`/halt` `/resume` `/flatten`은 그대로 두고 `/rest`(=/halt) `/live`(=/resume)를
별칭으로 추가한다 — 내부적으로 같은 TradingControl 경로를 탄다. `/flatten day`
(`/단타청산`)는 scope="day"로 request_flatten을 호출한다(실제 청산 대상 필터링은
quant/trade/loop.py의 `_flatten_all`이 처리 — tests/test_strategy_lots.py 참고,
여기서는 tg_bridge가 올바른 scope를 넘기는지만 본다).
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "server" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import tg_bridge  # noqa: E402
from quant.trade.control import TradingControl  # noqa: E402


def _control(tmp_path) -> TradingControl:
    return TradingControl(state_path=tmp_path / "control.json")


# ============================================================= /rest, /live: /halt, /resume 별칭


def test_rest_halts_with_manual_origin(tmp_path):
    control = _control(tmp_path)
    reply = tg_bridge.handle_control_command("/rest 병원 진료", control)

    assert control.is_halted() is True
    assert control.halted_by() == "manual"
    assert control.halt_reason() == "병원 진료"
    assert "REST" in reply
    assert "신규 진입 중단" in reply and "청산은 계속" in reply


def test_rest_without_reason_uses_default(tmp_path):
    control = _control(tmp_path)
    tg_bridge.handle_control_command("/rest", control)
    assert control.halt_reason() == "수동 중단(텔레그램)"


def test_live_resumes_same_as_resume(tmp_path):
    control = _control(tmp_path)
    control.halt("정지", by="manual")

    reply = tg_bridge.handle_control_command("/live", control)

    assert control.is_halted() is False
    assert "재개" in reply


def test_halt_alias_still_works_and_marks_manual(tmp_path):
    """기존 /halt 명령이 REST 별칭 도입 후에도 그대로 동작한다."""
    control = _control(tmp_path)
    tg_bridge.handle_control_command("/halt 점검", control)
    assert control.is_halted() is True
    assert control.halted_by() == "manual"
    assert control.halt_reason() == "점검"


def test_resume_alias_still_works(tmp_path):
    control = _control(tmp_path)
    control.halt("정지")
    tg_bridge.handle_control_command("/resume", control)
    assert control.is_halted() is False


# ============================================================= /status: REST vs 자동 중단 구분


def test_status_shows_live_when_not_halted(tmp_path, monkeypatch):
    monkeypatch.setattr(tg_bridge, "PORTFOLIO_STATE_PATH", tmp_path / "no_portfolio.json")
    monkeypatch.setattr(tg_bridge, "WATCHLIST_PATH", tmp_path / "none.yaml")
    control = _control(tmp_path)

    reply = tg_bridge.handle_control_command("/status", control)

    assert "LIVE" in reply
    assert "REST" not in reply
    assert "자동 중단" not in reply


def test_status_shows_rest_for_manual_halt(tmp_path, monkeypatch):
    monkeypatch.setattr(tg_bridge, "PORTFOLIO_STATE_PATH", tmp_path / "no_portfolio.json")
    monkeypatch.setattr(tg_bridge, "WATCHLIST_PATH", tmp_path / "none.yaml")
    control = _control(tmp_path)
    control.halt("비행기 탑승", by="manual")

    reply = tg_bridge.handle_control_command("/status", control)

    assert "REST" in reply
    assert "비행기 탑승" in reply
    assert "자동 중단" not in reply


def test_status_shows_auto_halt_distinctly_from_manual_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(tg_bridge, "PORTFOLIO_STATE_PATH", tmp_path / "no_portfolio.json")
    monkeypatch.setattr(tg_bridge, "WATCHLIST_PATH", tmp_path / "none.yaml")
    control = _control(tmp_path)
    control.halt("연속 3회 사이클 실패 — 자동 정지", by="auto")

    reply = tg_bridge.handle_control_command("/status", control)

    assert "자동 중단" in reply
    assert "REST" not in reply
    assert "연속 3회 사이클 실패" in reply


# ============================================================= /flatten day, /단타청산


def test_flatten_day_requests_day_scope(tmp_path):
    control = _control(tmp_path)
    reply = tg_bridge.handle_control_command("/flatten day", control)

    assert control.consume_flatten_scope() == "day"
    assert "단타" in reply
    assert "오버나이트" in reply


def test_flatten_korean_alias_requests_day_scope(tmp_path):
    control = _control(tmp_path)
    tg_bridge.handle_control_command("/단타청산", control)
    assert control.consume_flatten_scope() == "day"


def test_plain_flatten_still_requests_all_scope(tmp_path):
    """기존 /flatten(전량)이 새 scope 개념 도입 후에도 그대로 동작한다."""
    control = _control(tmp_path)
    reply = tg_bridge.handle_control_command("/flatten", control)

    assert control.consume_flatten_scope() == "all"
    assert "전량" in reply


def test_flatten_all_explicit_alias(tmp_path):
    control = _control(tmp_path)
    tg_bridge.handle_control_command("/flatten all", control)
    assert control.consume_flatten_scope() == "all"


def test_watchlist_names_falls_back_to_engine_symbol_cache(tmp_path, monkeypatch):
    """보유 중인데 유니버스에서 빠진 종목의 이름이 사라지던 결함(2026-09-02 실측).

    /status·/balance 가 삼성전자를 "005930" 코드로만 보여줬다 — 이식으로 넘겨받은
    보유분이라 그날 관심종목 후보에서 빠졌기 때문. 엔진 캐시(symbol_names.json)는
    이름을 알고 있었다. 관심종목 이름이 우선하고, 없으면 캐시가 메운다.
    """
    import json as _json
    import tg_bridge

    cache = tmp_path / "symbol_names.json"
    cache.write_text(_json.dumps({"005930": "삼성전자", "066570": "엘지전자"}), encoding="utf-8")
    monkeypatch.setattr(tg_bridge, "SYMBOL_NAMES_PATH", cache)

    wl = tmp_path / "watchlist.yaml"
    wl.write_text("symbols:\n  - symbol: '066570'\n    name: LG전자\n", encoding="utf-8")

    names = tg_bridge._watchlist_names(wl)
    assert names["005930"] == "삼성전자"   # 캐시가 메운다
    assert names["066570"] == "LG전자"     # 관심종목 표기가 우선

    # 캐시가 없어도 기존 동작 그대로(관심종목 이름만) — 하위호환
    monkeypatch.setattr(tg_bridge, "SYMBOL_NAMES_PATH", tmp_path / "missing.json")
    assert tg_bridge._watchlist_names(wl) == {"066570": "LG전자"}
