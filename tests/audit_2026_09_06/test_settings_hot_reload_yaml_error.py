"""2026-09-06 안정성 감사 — P0. `quant.apps.config.Settings.reload_if_changed()`가
YAML 파싱 실패를 전혀 잡지 않는다.

배경: `quant/trade/loop.py`의 `run_paper_loop`(메인 `while True:` 루프, 사이클마다
`settings.reload_if_changed()`를 호출 — 2026-09-06 감사 시점 기준 그 줄 번호는
loop.py:2179)와 `quant/apps/cli.py`의 `cmd_paper`(→ `args.func(args)`, `main()` 끝)
사이 어디에도 이 예외를 잡는 try/except가 없다. `config/settings.yaml`이 핫 리로드
경계(mtime 변경)에서 문법 오류 상태로 걸리면(동시 편집 중 저장, git checkout 도중
읽힘, 수동 편집 실수 등 — 실제로 이 저장소는 오늘도 여러 워커가 settings.yaml을
동시에 고치고 있었다) `yaml.YAMLError`가 이 루프 밖으로 그대로 터져 `quant.apps.cli
paper` 프로세스 전체가 죽는다.

`server/systemd/quant-engine.service`는 `Restart=always`/`RestartSec=30`이므로
재시작 자체는 일어나지만, `load_settings()`가 부팅 시점에도 같은 파일을 다시 읽으므로
파일이 고쳐지기 전까지는 매 30초 동일한 예외로 죽는 크래시 루프가 된다. 그 동안
엔진 프로세스 자체가 없으므로 `quant/trade/loop.py:_intraday_hard_stop_check`의
-5% 하드레일을 포함해 **모든 손절 판정이 정지**한다 — 열린 포지션이 있으면 무방비
상태로 방치된다.

**2026-09-06 수정 완료**: `Settings.reload_if_changed()`가 이제 `_read_merged()`
호출을 try/except(yaml.YAMLError, OSError)로 감싸 실패 시 ERROR 로그 한 줄만 남기고
`self.raw`를 그대로 유지한다(`quant/trade/risk/manager.py`의 `_load_day_state`가
이미 쓰는 "복원 실패는 조용히 기본값/직전값 유지" 패턴과 동일). 아래 첫 테스트가
그 바람직한 계약을 고정한다(과거엔 xfail이었다 — 이제 통과한다). "오늘은 예외가
샌다"를 고정하던 특성화 테스트는 그 버그 자체가 없어졌으므로 제거했다."""
from __future__ import annotations

import os
import time

import pytest

from quant.apps.config import Settings, _read_merged

_GOOD_YAML = "engine:\n  poll_seconds: 5\nstrategies: {}\n"
# 흔한 실수 하나 — 리스트를 닫는 대괄호를 빼먹음 (동시 편집 중 저장 시 실제로 나는 모양).
_BROKEN_YAML = "engine:\n  poll_seconds: 5\nrisk:\n  overnight_strategies: [close_bet\n"


def _touch_future(path) -> None:
    """mtime을 확실히 미래로 밀어 `reload_if_changed()`가 변경을 감지하게 한다
    (일부 파일시스템의 mtime 해상도가 초 단위라 `time.sleep()`만으로는 불안정하다)."""
    future = time.time() + 5
    os.utime(path, (future, future))


def test_reload_if_changed_should_survive_malformed_yaml(tmp_path):
    """바람직한 계약(2026-09-06 수정 완료): 리로드 실패는 마지막으로 성공한 설정을
    유지한 채 조용히 넘어가야 한다 — 엔진 하트비트/거래는 계속돼야 한다."""
    path = tmp_path / "settings.yaml"
    path.write_text(_GOOD_YAML, encoding="utf-8")
    settings = Settings(_read_merged(path), path)
    assert settings.raw["engine"]["poll_seconds"] == 5

    path.write_text(_BROKEN_YAML, encoding="utf-8")
    _touch_future(path)

    changed = settings.reload_if_changed()  # 더 이상 raise하지 않는다

    assert changed is False
    # 살아남았다면 마지막으로 성공한 설정이 그대로여야 한다(깨진 값으로 부분 반영 금지).
    assert settings.raw["engine"]["poll_seconds"] == 5
    assert "overnight_strategies" not in settings.raw.get("risk", {})


def test_reload_if_changed_recovers_once_file_is_fixed_again(tmp_path):
    """valid → invalid → valid: 깨진 파일이 다시 정상으로 고쳐지면 다음 mtime
    변경에서 정상적으로 반영돼야 한다 — 실패가 그 뒤의 정상 리로드까지 막으면
    안 된다."""
    path = tmp_path / "settings.yaml"
    path.write_text(_GOOD_YAML, encoding="utf-8")
    settings = Settings(_read_merged(path), path)
    assert settings.raw["engine"]["poll_seconds"] == 5

    path.write_text(_BROKEN_YAML, encoding="utf-8")
    _touch_future(path)
    assert settings.reload_if_changed() is False
    assert settings.raw["engine"]["poll_seconds"] == 5  # 여전히 마지막 정상값

    fixed_yaml = "engine:\n  poll_seconds: 7\nstrategies: {}\n"
    path.write_text(fixed_yaml, encoding="utf-8")
    future = time.time() + 10
    os.utime(path, (future, future))

    assert settings.reload_if_changed() is True
    assert settings.raw["engine"]["poll_seconds"] == 7  # 고쳐진 새 값이 반영됨


def test_reload_if_changed_calls_on_error_exactly_once_per_broken_mtime(tmp_path):
    """실패 시 on_error 콜백이 정확히 한 번 불려야 한다(ops 알림 스팸 방지) —
    같은 깨진 mtime에 대해 reload_if_changed()를 다시 불러도(변경 없음) 재호출되지
    않는다."""
    path = tmp_path / "settings.yaml"
    path.write_text(_GOOD_YAML, encoding="utf-8")
    settings = Settings(_read_merged(path), path)

    path.write_text(_BROKEN_YAML, encoding="utf-8")
    _touch_future(path)

    errors: list[Exception] = []
    assert settings.reload_if_changed(on_error=errors.append) is False
    assert len(errors) == 1

    # mtime이 그대로면(파일이 다시 안 바뀌면) 재시도도, 재알림도 없다.
    assert settings.reload_if_changed(on_error=errors.append) is False
    assert len(errors) == 1


def test_reload_if_changed_on_error_callback_failure_does_not_propagate(tmp_path):
    """on_error 콜백 자체가 실패해도(알림 전송 오류 등) 리로드 실패 처리에
    영향을 주면 안 된다 — settings.raw는 여전히 마지막 정상값을 유지해야 한다."""
    path = tmp_path / "settings.yaml"
    path.write_text(_GOOD_YAML, encoding="utf-8")
    settings = Settings(_read_merged(path), path)

    path.write_text(_BROKEN_YAML, encoding="utf-8")
    _touch_future(path)

    def _boom(_e: Exception) -> None:
        raise RuntimeError("텔레그램 전송 실패")

    assert settings.reload_if_changed(on_error=_boom) is False
    assert settings.raw["engine"]["poll_seconds"] == 5
