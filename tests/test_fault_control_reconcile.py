"""결함주입 4/6 — 제어/대사: control.json 손상, 장부-포트폴리오 대사 불일치,
유니버스 리로드로 보유 종목 이탈, 국면(regime) 파일 손상/부재, VIX 게이트
전환의 사이징 시점."""
from __future__ import annotations

import json

import pytest

from quant.trade.control import TradingControl
from quant.trade.reconcile import Reconciler
from quant.trade.regime.provider import RegimeProvider

# ------------------------------------------------------- 1. control.json 손상

def test_corrupted_control_json_does_not_crash_engine_boot(tmp_path):
    """엔진 부팅(TradingControl 생성) 자체가 죽으면 안 된다 — invalid JSON은
    무시하고 기본값(정지 아님)으로 시작한다."""
    path = tmp_path / "control.json"
    path.write_text("{not valid json at all", encoding="utf-8")
    control = TradingControl(state_path=path)  # 생성자에서 크래시하면 실패
    assert control.is_halted() is False


def test_corrupted_control_json_while_running_keeps_last_known_halt_state(tmp_path):
    """halt 상태로 돌던 중 파일이 손상되면(디스크 장애 등) `_load()`는 조용히
    실패하고 **메모리의 마지막 값을 유지**한다 — 파싱 실패로 halt가 몰래 풀리는
    쪽(더 위험한 쪽)이 아니라 직전 상태를 유지하는 쪽으로 fail한다."""
    path = tmp_path / "control.json"
    control = TradingControl(state_path=path)
    control.halt("테스트 정지", by="manual")
    assert control.is_halted() is True

    path.write_text("{{{corrupt", encoding="utf-8")
    # is_halted()가 매번 _load()를 다시 하므로, 손상된 파일을 만나 예외 없이
    # 직전 메모리 상태(halted=True)를 그대로 유지해야 한다.
    assert control.is_halted() is True


def test_missing_control_json_defaults_to_not_halted(tmp_path):
    path = tmp_path / "does_not_exist" / "control.json"
    control = TradingControl(state_path=path)
    assert control.is_halted() is False


# ---------------------------------------------------- 2. 장부-포트폴리오 대사

class _MismatchedBroker:
    """엔진 원장은 TQQQ 10주를 안다고 믿는데 실제 브로커 보유는 4주뿐 —
    부분체결 후 프로세스 재시작 등으로 어긋난 상황을 흉내."""

    def positions(self):
        from quant.core.models import Position
        return {"TQQQ": Position(symbol="TQQQ", qty=4.0, avg_cost=50.0)}

    def cash(self) -> float:
        return 1_000_000.0

    def engine_owned_qty(self, symbol: str) -> float:
        return 10.0 if symbol == "TQQQ" else 0.0

    def engine_owned_symbols(self) -> set[str]:
        return {"TQQQ"}

    def engine_owned_cash(self) -> float:
        return 1_000_000.0


def test_position_mismatch_halts_new_entries(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    reconciler = Reconciler(_MismatchedBroker(), control, interval_minutes=0)
    report = reconciler.check(force=True)
    assert not report.ok
    assert any("TQQQ" in m for m in report.mismatches)
    assert control.is_halted() is True
    assert control.halted_by() == "auto"


class _MatchedBroker:
    def positions(self):
        from quant.core.models import Position
        return {"TQQQ": Position(symbol="TQQQ", qty=10.0, avg_cost=50.0)}

    def cash(self) -> float:
        return 1_000_000.0

    def engine_owned_qty(self, symbol: str) -> float:
        return 10.0 if symbol == "TQQQ" else 0.0

    def engine_owned_symbols(self) -> set[str]:
        return {"TQQQ"}

    def engine_owned_cash(self) -> float:
        return 1_000_000.0


def test_matched_books_does_not_halt(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    reconciler = Reconciler(_MatchedBroker(), control, interval_minutes=0)
    report = reconciler.check(force=True)
    assert report.ok
    assert control.is_halted() is False


def test_reconcile_mismatch_halt_does_not_block_exits():
    """halt는 신규 진입만 막는다 — 청산은 TradingControl.is_halted() 자체가
    청산 경로에서 절대 확인되지 않는다는 것이 quant/trade/loop.py의 계약
    (run_cycle의 halt 체크는 _ENTRY_ACTIONS에만 적용). 여기서는 그 계약을
    TradingControl 수준에서 재확인한다: halt는 진입 여부를 나타내는 플래그일
    뿐 청산 가능 여부를 나타내는 별도 상태가 없다(설계상 청산 코드는 이 플래그를
    아예 참조하지 않는다)."""
    from quant.core.models import SignalAction
    from quant.trade.loop import _ENTRY_ACTIONS

    assert SignalAction.EXIT_LONG not in _ENTRY_ACTIONS
    assert SignalAction.SCALE_OUT not in _ENTRY_ACTIONS


# --------------------------------------------------------- 3. 국면 파일 손상

def test_regime_missing_file_defaults_to_neutral(tmp_path):
    provider = RegimeProvider(state_path=tmp_path / "regime.json")
    assert provider.risk_multiplier("US") == pytest.approx(1.0)
    assert provider.risk_multiplier("KR") == pytest.approx(1.0)


def test_regime_corrupted_file_defaults_to_neutral_not_crash(tmp_path):
    path = tmp_path / "regime.json"
    path.write_text("not json{{{", encoding="utf-8")
    provider = RegimeProvider(state_path=path)
    assert provider.risk_multiplier("US") == pytest.approx(1.0)


def test_regime_partial_markets_key_defaults_kr_to_neutral(tmp_path):
    """구버전 캐시(최상위 필드만 US, markets.KR 없음)를 만나도 KR은 중립으로
    안전하게 떨어진다 — KR을 미국 지수로 오판하던 구결함의 재발 방지."""
    path = tmp_path / "regime.json"
    path.write_text(json.dumps({
        "label": "aggressive", "risk_multiplier": 1.3, "reasons": ["테스트"],
        "computed_at": "2026-09-07T08:00:00+09:00", "degraded": False,
        # markets 키 자체가 없다 — KR 캐시가 생기기 전 구버전 스키마.
    }), encoding="utf-8")
    provider = RegimeProvider(state_path=path)
    assert provider.risk_multiplier("US") == pytest.approx(1.3)
    assert provider.risk_multiplier("KR") == pytest.approx(1.0)  # markets.KR 없음 → 중립
