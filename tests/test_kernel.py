"""`quant/trade/strategy/kernel.py` 공용 커널 순수 함수 단위 테스트.

여러 전략 파일에 흩어져 있던 로컬 구현을 대체하는 함수들이므로, 각 테스트는
원래 로컬 구현이 지키던 계약(예: entry 유무로 "내 랏" 판정, 0=게이트 비활성)을
그대로 고정한다."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from quant.core.models import SignalAction
from quant.trade.strategy import kernel


def test_exit_signal_builds_full_exit():
    sig = kernel.exit_signal("gap_fade", "TQQQ", "손절")
    assert sig.strategy_id == "gap_fade"
    assert sig.symbol == "TQQQ"
    assert sig.action == SignalAction.EXIT_LONG
    assert sig.target_weight == 0.0
    assert sig.exit_fraction == 1.0
    assert sig.reason == "손절"


def test_parse_min_stop_bp_default():
    assert kernel.parse_min_stop_bp({}) == 40.0


def test_parse_min_stop_bp_custom():
    assert kernel.parse_min_stop_bp({"min_stop_bp": 25.0}) == 25.0


def test_parse_min_stop_bp_zero_allowed():
    assert kernel.parse_min_stop_bp({"min_stop_bp": 0.0}) == 0.0


def test_parse_min_stop_bp_negative_rejected():
    with pytest.raises(ValueError):
        kernel.parse_min_stop_bp({"min_stop_bp": -1.0})


def test_stop_bp_gate_ok_passes_above_threshold():
    assert kernel.stop_bp_gate_ok(50.0, 40.0) is True


def test_stop_bp_gate_ok_rejects_below_threshold():
    assert kernel.stop_bp_gate_ok(30.0, 40.0) is False


def test_stop_bp_gate_ok_disabled_when_min_is_zero():
    assert kernel.stop_bp_gate_ok(-5.0, 0.0) is True


def test_my_lot_returns_none_for_missing_symbol():
    assert kernel.my_lot({}, "TQQQ") is None


def test_my_lot_returns_none_for_foreign_empty_lot():
    assert kernel.my_lot({"TQQQ": {}}, "TQQQ") is None


def test_my_lot_returns_lot_with_entry():
    lot = {"entry": 10.0, "stop": 9.0}
    assert kernel.my_lot({"TQQQ": lot}, "TQQQ") == lot


def test_held_lot_returns_none_when_no_candidate_has_entry():
    lots = {"TQQQ": {}, "SQQQ": {}}
    assert kernel.held_lot(lots, ["TQQQ", "SQQQ"]) is None


def test_held_lot_returns_first_matching_candidate():
    lot = {"entry": 5.0}
    lots = {"TQQQ": {}, "SQQQ": lot}
    assert kernel.held_lot(lots, ["TQQQ", "SQQQ"]) == ("SQQQ", lot)


def test_should_flatten_calendar_none_minutes_is_false():
    assert kernel.should_flatten_calendar(None, 5.0, 10.0) is False


def test_should_flatten_calendar_continuous_close_is_false():
    assert kernel.should_flatten_calendar(0.0, 5.0, 10.0) is False


def test_should_flatten_calendar_within_window_is_true():
    assert kernel.should_flatten_calendar(12.0, 5.0, 10.0) is True


def test_should_flatten_calendar_outside_window_is_false():
    assert kernel.should_flatten_calendar(20.0, 5.0, 10.0) is False


def test_should_flatten_dual_true_via_calendar_leg():
    now = datetime(2026, 8, 31, 15, 25, tzinfo=ZoneInfo("Asia/Seoul"))
    assert kernel.should_flatten_dual("KR", now, 12.0, 5.0, 10.0) is True


def test_should_flatten_dual_true_via_wallclock_leg_when_calendar_false():
    # KR 연속 거래 종료 15:20. minutes_to_close(정규장 15:30 기준)는 아직 여유
    # 있어 캘린더 다리는 False지만, 15:20까지 8분 남아 cadence=5 를 빼면
    # flatten_minutes=10 미만이라 벽시계 다리가 True를 낸다.
    now = datetime(2026, 8, 31, 15, 12, tzinfo=ZoneInfo("Asia/Seoul"))
    assert kernel.should_flatten_dual("KR", now, 18.0, 5.0, 10.0) is True


def test_should_flatten_dual_false_when_neither_leg_fires():
    now = datetime(2026, 8, 31, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    assert kernel.should_flatten_dual("KR", now, 330.0, 5.0, 10.0) is False


def test_is_overnight_carry_true_for_stale_session():
    assert kernel.is_overnight_carry({"session": "2026-08-28"}, "2026-08-31") is True


def test_is_overnight_carry_false_for_same_day_session():
    assert kernel.is_overnight_carry({"session": "2026-08-31"}, "2026-08-31") is False


def test_is_overnight_carry_false_for_no_session_recorded():
    assert kernel.is_overnight_carry({}, "2026-08-31") is False


def test_session_rolled_true_on_new_day():
    assert kernel.session_rolled("2026-08-28", "2026-08-31") is True


def test_session_rolled_false_on_same_day():
    assert kernel.session_rolled("2026-08-31", "2026-08-31") is False


def test_session_rolled_true_when_no_prior_record():
    assert kernel.session_rolled(None, "2026-08-31") is True
