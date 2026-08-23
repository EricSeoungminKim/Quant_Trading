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
