"""TradingControl: halt/resume/flatten 영속화 + 재시작 후 상태 유지.

halt는 신규 진입만 막고 청산은 막지 않는다는 의미론은 여기서가 아니라
loop.py를 구동하는 tests/test_loop_resilience.py에서 검증한다 — 이 파일은
TradingControl 그 자체(디스크 영속화, one-shot flatten)만 다룬다."""
from __future__ import annotations

from quant.trade.control import TradingControl


def test_default_state_is_not_halted_and_no_flatten(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    assert control.is_halted() is False
    assert control.halt_reason() == ""
    assert control.consume_flatten() is False


def test_halt_sets_reason(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    control.halt("긴급 점검")
    assert control.is_halted() is True
    assert control.halt_reason() == "긴급 점검"


def test_resume_clears_halt(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    control.halt("일시 중단")
    control.resume()
    assert control.is_halted() is False
    assert control.halt_reason() == ""


def test_request_flatten_is_one_shot(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    assert control.consume_flatten() is False  # 요청 전엔 항상 False

    control.request_flatten()
    assert control.consume_flatten() is True  # 첫 소비: True
    assert control.consume_flatten() is False  # 두번째 소비: 이미 클리어됨


def test_state_survives_simulated_restart(tmp_path):
    """엔진 프로세스가 죽었다 재시작해도(=새 TradingControl 인스턴스가 같은 파일을
    읽어도) halt 상태가 유지돼야 한다 — 그게 이 클래스를 디스크에 영속화하는 이유다."""
    path = tmp_path / "control.json"
    first = TradingControl(state_path=path)
    first.halt("재시작 테스트")

    restarted = TradingControl(state_path=path)
    assert restarted.is_halted() is True
    assert restarted.halt_reason() == "재시작 테스트"


def test_flatten_request_survives_restart_but_stays_one_shot(tmp_path):
    path = tmp_path / "control.json"
    first = TradingControl(state_path=path)
    first.request_flatten()

    restarted = TradingControl(state_path=path)
    assert restarted.consume_flatten() is True

    # 소비된 뒤 다시 재시작해도 이미 클리어된 상태가 유지된다(재청산 반복 안 함)
    restarted_again = TradingControl(state_path=path)
    assert restarted_again.consume_flatten() is False


def test_halt_reason_updates_on_repeated_halt(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    control.halt("사유1")
    control.halt("사유2")
    assert control.halt_reason() == "사유2"


def test_missing_state_file_is_treated_as_default(tmp_path):
    """control.json이 아예 없는 최초 기동 상태 — 에러 없이 기본값(정상)으로 취급."""
    control = TradingControl(state_path=tmp_path / "does_not_exist" / "control.json")
    assert control.is_halted() is False
    assert control.consume_flatten() is False


# ============================================================= halted_by: 수동 REST vs 자동 중단


def test_halt_defaults_to_manual(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    control.halt("사유")
    assert control.halted_by() == "manual"


def test_halt_by_auto_marks_circuit_breaker_origin(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    control.halt("연속 실패", by="auto")
    assert control.is_halted() is True
    assert control.halted_by() == "auto"


def test_resume_resets_halted_by_to_manual(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    control.halt("자동 정지", by="auto")
    control.resume()
    control.halt("다시 수동")
    assert control.halted_by() == "manual"


def test_halted_by_survives_restart(tmp_path):
    path = tmp_path / "control.json"
    first = TradingControl(state_path=path)
    first.halt("자동 중단", by="auto")

    restarted = TradingControl(state_path=path)
    assert restarted.halted_by() == "auto"
    assert restarted.halt_reason() == "자동 중단"


def test_old_schema_without_halted_by_or_flatten_scope_defaults_safely(tmp_path):
    """control.json 하위호환 — 이 필드들이 생기기 전에 쓰인 구 스키마(halted만
    있고 halted_by/flatten_scope가 없는 파일)를 에러 없이 읽고 안전한 기본값을
    쓴다."""
    import json

    path = tmp_path / "control.json"
    path.write_text(
        json.dumps({"halted": True, "halt_reason": "레거시 정지", "flatten_requested": False}),
        encoding="utf-8",
    )

    control = TradingControl(state_path=path)
    assert control.is_halted() is True
    assert control.halt_reason() == "레거시 정지"
    assert control.halted_by() == "manual"  # 필드 부재 시 기본값
    assert control.consume_flatten() is False


# ============================================================= flatten scope: all vs day


def test_request_flatten_defaults_to_all_scope(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    control.request_flatten()
    assert control.consume_flatten_scope() == "all"


def test_request_flatten_day_scope_round_trips(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    control.request_flatten("day")
    assert control.consume_flatten_scope() == "day"
    # one-shot — 두번째 소비는 빈 문자열(요청 없음)
    assert control.consume_flatten_scope() == ""


def test_consume_flatten_bool_wrapper_still_works_for_day_scope(tmp_path):
    """하위호환 bool 버전은 scope와 무관하게 요청 여부만 True/False로 반환한다."""
    control = TradingControl(state_path=tmp_path / "control.json")
    control.request_flatten("day")
    assert control.consume_flatten() is True
    assert control.consume_flatten() is False


def test_flatten_scope_survives_restart(tmp_path):
    path = tmp_path / "control.json"
    first = TradingControl(state_path=path)
    first.request_flatten("day")

    restarted = TradingControl(state_path=path)
    assert restarted.consume_flatten_scope() == "day"


# ================================================= pending_flatten: 장 마감 재시도 대기열


def test_no_pending_flatten_by_default(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    assert control.pending_flatten() is None


def test_set_pending_flatten_persists_scope_and_symbols(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    control.set_pending_flatten("all", ["TQQQ", "005930"])

    pending = control.pending_flatten()
    assert pending is not None
    assert pending["scope"] == "all"
    assert pending["symbols"] == ["005930", "TQQQ"]  # 정렬됨
    assert pending["requested_at"]  # 비어있지 않은 타임스탬프


def test_set_pending_flatten_with_no_symbols_is_a_noop(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    control.set_pending_flatten("all", [])
    assert control.pending_flatten() is None


def test_pending_flatten_survives_restart(tmp_path):
    """엔진 재시작(=새 TradingControl 인스턴스)에도 대기 목록이 살아남아야
    한다 — 이 결함의 핵심 요구사항(디스크 영속화)."""
    path = tmp_path / "control.json"
    first = TradingControl(state_path=path)
    first.set_pending_flatten("day", ["TQQQ"])

    restarted = TradingControl(state_path=path)
    pending = restarted.pending_flatten()
    assert pending is not None
    assert pending["scope"] == "day"
    assert pending["symbols"] == ["TQQQ"]


def test_set_pending_flatten_merges_same_scope_and_keeps_original_timestamp(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    control.set_pending_flatten("all", ["TQQQ"])
    first_ts = control.pending_flatten()["requested_at"]

    control.set_pending_flatten("all", ["SQQQ"])
    pending = control.pending_flatten()
    assert pending["symbols"] == ["SQQQ", "TQQQ"]
    assert pending["requested_at"] == first_ts


def test_set_pending_flatten_different_scope_replaces_previous_record(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    control.set_pending_flatten("day", ["TQQQ"])
    control.set_pending_flatten("all", ["SQQQ"])

    pending = control.pending_flatten()
    assert pending["scope"] == "all"
    assert pending["symbols"] == ["SQQQ"]


def test_clear_pending_flatten_removes_only_done_symbols(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    control.set_pending_flatten("all", ["TQQQ", "SQQQ"])

    control.clear_pending_flatten(["TQQQ"])
    pending = control.pending_flatten()
    assert pending is not None
    assert pending["symbols"] == ["SQQQ"]


def test_clear_pending_flatten_clears_record_when_all_symbols_done(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    control.set_pending_flatten("all", ["TQQQ"])

    control.clear_pending_flatten(["TQQQ"])
    assert control.pending_flatten() is None


def test_clear_pending_flatten_with_no_pending_record_is_a_noop(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    control.clear_pending_flatten(["TQQQ"])  # 예외 없이 조용히 무시
    assert control.pending_flatten() is None


def test_old_schema_without_pending_flatten_defaults_to_none(tmp_path):
    """이 필드가 생기기 전 control.json — 하위호환."""
    import json

    path = tmp_path / "control.json"
    path.write_text(json.dumps({"halted": False}), encoding="utf-8")

    control = TradingControl(state_path=path)
    assert control.pending_flatten() is None

