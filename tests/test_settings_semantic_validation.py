"""2026-09-07 보안/견고성 감사 — settings.yaml 핫 리로드 가드는 `yaml.YAMLError`
(문법 오류)만 잡는다(2026-09-06 P0-1). **문법적으로는 유효하지만 의미가 깨진**
파일 — 음수 `capital_fraction`, 숫자 자리에 문자열 — 은 전부 그대로 파싱에
성공해 `reload_if_changed()`가 조용히 반영해버렸다.

이 파일은 `quant.apps.config._validate_semantics` + 그것을 `reload_if_changed`/
`load_settings`에 배선한 가드를 고정한다: 핫 리로드는 검증 실패 시 마지막으로
성공한 설정을 유지(엔진은 죽지 않는다, `test_settings_hot_reload_yaml_error.py`와
같은 계약), 부팅(`load_settings`)은 검증 실패 시 기동을 거부(raise) — 깨진 설정으로
기동하는 것보다 안전하다는 기존 원칙 그대로.

**`strategies:` 키 부재와 `governor.protected_strategies`의 참조 무결성은
의도적으로 검사하지 않는다** — `_validate_semantics`의 docstring 참고. 둘 다
처음엔 검사했지만 `tests/test_backtest_intrabar.py`처럼 `config/settings.yaml`을
복사해 `strategies`만 단일 프로브로 갈아끼우는 정당한 테스트 패턴을 오탐으로
깨뜨렸다(2026-09-07 실측, 도입 직후 12개 회귀). `protected_strategies`가 실제
`config/settings.yaml`과 맞는지는 `tests/test_governor_wiring.py::
test_protected_strategies_are_known_settings_yaml_strategies`(정적 검사)가 잡는다.
"""
from __future__ import annotations

import os
import time

import pytest

from quant.apps.config import (
    Settings,
    SettingsValidationError,
    _read_merged,
    _validate_semantics,
    load_settings,
)

_GOOD_YAML = (
    "engine:\n  poll_seconds: 5\n"
    "governor:\n  protected_strategies: [scalp_1m]\n"
    "risk:\n  per_strategy_initial_krw: 10000000\n  per_strategy_initial_usd: 10000\n"
    "strategies:\n"
    "  scalp_1m:\n    capital_fraction:\n      KR: 0.03\n      US: 0.03\n"
)


def _touch_future(path) -> None:
    future = time.time() + 5
    os.utime(path, (future, future))


# ---------------------------------------------------------------------------
# _validate_semantics — 순수 함수 단위 테스트
# ---------------------------------------------------------------------------
def test_valid_config_has_no_errors():
    assert _validate_semantics(
        {
            "strategies": {"a": {"capital_fraction": {"KR": 0.1, "US": 0.0}}},
            "governor": {"protected_strategies": ["a"]},
            "risk": {"per_strategy_initial_krw": 1, "per_strategy_initial_usd": 1},
            "engine": {"poll_seconds": 5},
        }
    ) == []


def test_missing_strategies_key_is_not_rejected():
    """의도적으로 검사하지 않는다 — `Settings.strategies`가 이미 `.get(key, {})`로
    부재를 안전하게 흡수하고(전략 0개 == 매매 안 함, 위험한 방향이 아니다),
    이 자리에 강제하면 `strategies`와 무관한 설정만 다루는 다른 테스트/구성
    (`tests/test_governor_wiring.py`의 오버레이 병합 테스트 등)을 깨뜨린다."""
    errors = _validate_semantics({"engine": {"poll_seconds": 5}})
    assert errors == []


def test_strategies_wrong_type_is_still_rejected():
    """부재는 허용하지만 **있는데 dict가 아닌 것**은 여전히 잡는다 — 그건
    어느 테스트 패턴과도 충돌하지 않는 명백한 오류다."""
    errors = _validate_semantics({"strategies": "oops"})
    assert any("strategies" in e for e in errors)


def test_negative_capital_fraction_scalar_is_rejected():
    errors = _validate_semantics({"strategies": {"a": {"capital_fraction": -0.1}}})
    assert any("capital_fraction" in e and "음수" in e for e in errors)


def test_negative_capital_fraction_per_market_is_rejected():
    errors = _validate_semantics(
        {"strategies": {"a": {"capital_fraction": {"KR": -0.05, "US": 0.1}}}}
    )
    assert any("capital_fraction.KR" in e for e in errors)


def test_string_capital_fraction_is_rejected():
    errors = _validate_semantics({"strategies": {"a": {"capital_fraction": "0.1"}}})
    assert any("숫자가 아니다" in e for e in errors)


def test_protected_strategies_referencing_unknown_id_is_not_rejected():
    """의도적으로 검사하지 않는다(모듈독스트링 참고) — 정적 검사
    (`test_governor_wiring.py::test_protected_strategies_are_known_settings_yaml_strategies`)
    가 커밋된 실제 파일에 대해서만 이걸 지킨다."""
    errors = _validate_semantics(
        {"strategies": {"a": {}}, "governor": {"protected_strategies": ["oopsy_typo"]}}
    )
    assert errors == []


def test_string_per_strategy_initial_krw_is_rejected():
    errors = _validate_semantics(
        {"strategies": {"a": {}}, "risk": {"per_strategy_initial_krw": "많이"}}
    )
    assert any("per_strategy_initial_krw" in e for e in errors)


def test_zero_or_negative_per_strategy_initial_is_rejected():
    errors = _validate_semantics(
        {"strategies": {"a": {}}, "risk": {"per_strategy_initial_usd": 0}}
    )
    assert any("per_strategy_initial_usd" in e for e in errors)


def test_string_poll_seconds_is_rejected():
    errors = _validate_semantics({"strategies": {"a": {}}, "engine": {"poll_seconds": "열"}})
    assert any("poll_seconds" in e for e in errors)


# ---------------------------------------------------------------------------
# reload_if_changed — 핫 리로드 경계: 검증 실패는 이전 설정을 지킨다
# ---------------------------------------------------------------------------
def test_reload_if_changed_rejects_negative_capital_fraction(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(_GOOD_YAML, encoding="utf-8")
    settings = Settings(_read_merged(path), path)
    assert settings.raw["strategies"]["scalp_1m"]["capital_fraction"]["KR"] == 0.03

    broken = _GOOD_YAML.replace("KR: 0.03", "KR: -0.03")
    path.write_text(broken, encoding="utf-8")
    _touch_future(path)

    changed = settings.reload_if_changed()

    assert changed is False
    assert settings.raw["strategies"]["scalp_1m"]["capital_fraction"]["KR"] == 0.03


def test_reload_if_changed_accepts_reduced_strategies_map(tmp_path):
    """`governor.protected_strategies`가 새 `strategies:`에 없는 id를 가리켜도
    핫 리로드는 반영한다(의도적 비검사, 모듈독스트링 참고) — 이 자리에서
    막으면 `strategies`를 의도적으로 줄이는 정당한 배포/실험 절차까지 막는다."""
    path = tmp_path / "settings.yaml"
    path.write_text(_GOOD_YAML, encoding="utf-8")
    settings = Settings(_read_merged(path), path)

    reduced = _GOOD_YAML.replace(
        "strategies:\n  scalp_1m:\n    capital_fraction:\n      KR: 0.03\n      US: 0.03\n",
        "strategies:\n  other_strategy:\n    capital_fraction:\n      KR: 0.03\n      US: 0.03\n",
    )
    path.write_text(reduced, encoding="utf-8")
    _touch_future(path)

    assert settings.reload_if_changed() is True
    assert "scalp_1m" not in settings.raw["strategies"]
    assert settings.raw["governor"]["protected_strategies"] == ["scalp_1m"]


def test_reload_if_changed_rejects_string_where_float_expected(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(_GOOD_YAML, encoding="utf-8")
    settings = Settings(_read_merged(path), path)

    broken = _GOOD_YAML.replace("poll_seconds: 5", 'poll_seconds: "다섯"')
    path.write_text(broken, encoding="utf-8")
    _touch_future(path)

    assert settings.reload_if_changed() is False
    assert settings.raw["engine"]["poll_seconds"] == 5


def test_reload_if_changed_calls_on_error_for_semantic_failure(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(_GOOD_YAML, encoding="utf-8")
    settings = Settings(_read_merged(path), path)

    broken = _GOOD_YAML.replace("KR: 0.03", "KR: -0.03")
    path.write_text(broken, encoding="utf-8")
    _touch_future(path)

    errors: list[Exception] = []
    assert settings.reload_if_changed(on_error=errors.append) is False
    assert len(errors) == 1
    assert isinstance(errors[0], SettingsValidationError)


def test_reload_recovers_once_semantic_error_is_fixed(tmp_path):
    """invalid → valid: 의미 오류가 고쳐지면 다음 mtime 변경에 정상 반영돼야 한다."""
    path = tmp_path / "settings.yaml"
    path.write_text(_GOOD_YAML, encoding="utf-8")
    settings = Settings(_read_merged(path), path)

    broken = _GOOD_YAML.replace("KR: 0.03", "KR: -0.03")
    path.write_text(broken, encoding="utf-8")
    _touch_future(path)
    assert settings.reload_if_changed() is False

    fixed = _GOOD_YAML.replace("KR: 0.03", "KR: 0.09")
    path.write_text(fixed, encoding="utf-8")
    future = time.time() + 10
    os.utime(path, (future, future))

    assert settings.reload_if_changed() is True
    assert settings.raw["strategies"]["scalp_1m"]["capital_fraction"]["KR"] == 0.09


# ---------------------------------------------------------------------------
# load_settings — 부팅 경계: 검증 실패는 기동을 거부한다(마지막 정상값이 없다)
# ---------------------------------------------------------------------------
def test_load_settings_refuses_to_boot_on_negative_capital_fraction(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    (tmp_path / ".env.local").write_text("", encoding="utf-8")
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(_GOOD_YAML.replace("KR: 0.03", "KR: -0.03"), encoding="utf-8")

    with pytest.raises(SettingsValidationError):
        load_settings(str(settings_path))


def test_load_settings_boots_fine_on_valid_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    (tmp_path / ".env.local").write_text("", encoding="utf-8")
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(_GOOD_YAML, encoding="utf-8")

    settings = load_settings(str(settings_path))
    assert settings.raw["strategies"]["scalp_1m"]["capital_fraction"]["KR"] == 0.03
