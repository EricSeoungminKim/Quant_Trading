"""설정 로더: config/settings.yaml + .env/.env.local. 핫 리로드 지원.

시크릿은 .env(.local)에서, 전략 파라미터는 settings.yaml에서 — 원칙 5 (README).
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS_PATH = "config/settings.yaml"

# 오버레이 파일명. settings.yaml과 같은 디렉터리에 둔다 — 배포 경로가 바뀌어도
# 같이 따라간다. **왜 settings.yaml을 직접 안 쓰는가(거버너 배선, 2026-08):**
# settings.yaml은 사람이 쓴 수백 줄의 '왜' 주석이 자산이다. 자동 반영 코드가
# yaml을 파싱해서 다시 쓰면 그 주석이 전부 날아간다. 그래서 기계가 소유하는
# 작고 주석 없는 오버레이 파일을 따로 두고 settings.yaml 위에 깊은 병합한다 —
# 되돌리기는 그 파일에서 키를 지우는 것뿐이고, 권한 경계가 파일 단위로 명확하다.
AUTO_PARAMS_FILENAME = "auto_params.yaml"


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """base 위에 overlay를 재귀적으로 얹는다. dict는 병합, 그 외(스칼라·리스트)는
    overlay가 이긴다. 둘 다 건드리지 않고 새 dict를 반환한다(순수 함수)."""
    merged = dict(base)
    for key, value in overlay.items():
        base_value = merged.get(key)
        if isinstance(base_value, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(base_value, value)
        else:
            merged[key] = value
    return merged


def _read_merged(path: Path) -> dict[str, Any]:
    """settings.yaml을 읽고, 옆에 auto_params.yaml이 있으면 깊은 병합한다.
    없으면 기존과 100% 동일하게 settings.yaml만 반환한다."""
    raw = _read_yaml(path)
    overlay_path = path.parent / AUTO_PARAMS_FILENAME
    if overlay_path.exists():
        raw = _deep_merge(raw, _read_yaml(overlay_path))
    return raw


class Settings:
    """settings.yaml(+auto_params.yaml 오버레이)을 감싸는 얇은 typed 접근자.
    병합된 dict는 .raw로 접근 가능."""

    def __init__(self, raw: dict[str, Any], path: Path):
        self.raw = raw
        self.path = path
        self._overlay_path = path.parent / AUTO_PARAMS_FILENAME
        self._mtime = path.stat().st_mtime
        self._overlay_mtime = (
            self._overlay_path.stat().st_mtime if self._overlay_path.exists() else None
        )

    @property
    def engine(self) -> dict:
        return self.raw.get("engine", {})

    @property
    def universe(self) -> dict:
        return self.raw.get("universe", {})

    @property
    def strategies(self) -> dict:
        return self.raw.get("strategies", {})

    @property
    def risk(self) -> dict:
        return self.raw.get("risk", {})

    @property
    def execution(self) -> dict:
        return self.raw.get("execution", {})

    @property
    def poll_seconds(self) -> float:
        return self.engine.get("poll_seconds", 10)

    def reload_if_changed(self, on_error: Callable[[Exception], None] | None = None) -> bool:
        """settings.yaml **또는** auto_params.yaml의 mtime이 바뀌었으면 다시 읽어
        raw를 교체한다(둘을 병합). 오버레이만 바뀌어도 핫 리로드돼야 거버너가
        반영한 값이 다음 폴링에 바로 먹힌다. 바뀌었으면 True를 반환.

        **2026-09-06 안정성 감사 P0-1 수정**: 파싱/검증 실패(`yaml.YAMLError`,
        `OSError` — 동시 편집 중 저장, git checkout 도중 읽힘 등으로 settings.yaml이
        일시적으로 깨진 상태)는 예전엔 그대로 위로 새어나가 `run_paper_loop`의
        `while True:`를 통째로 죽였다. systemd `Restart=always`가 재시작해도
        `load_settings()`가 부팅 시점에 같은 깨진 파일을 다시 읽으므로 파일이
        고쳐지기 전까지 30초 간격 크래시 루프가 되고, 그 동안 엔진 프로세스
        자체가 없어 `_intraday_hard_stop_check`의 −5% 백스톱마저 정지한다.

        이제는 `quant/trade/risk/manager.py`의 `_load_day_state`(복원 실패는
        조용히 마지막 상태 유지)와 같은 패턴을 쓴다: 실패하면 ERROR 로그 한 줄만
        남기고 `self.raw`(마지막으로 성공한 설정)를 그대로 유지한 채 False를
        반환한다 — 엔진은 죽지 않고 예전 설정으로 계속 돈다.

        **재시도 시점**: 실패해도 `self._mtime`/`self._overlay_mtime`은 이번에
        읽은(깨진) mtime으로 갱신한다 — 그래야 같은 깨진 파일을 매 사이클
        재시도하며 ERROR 로그를 스팸하지 않는다("re-check on next mtime change").
        파일이 다시 저장되어 mtime이 또 바뀌면(고쳐졌든 여전히 깨졌든) 다음
        호출에서 다시 시도한다.

        `on_error`는 옵션 콜백(예: 텔레그램 ops 알림) — 실패했을 때만, 예외
        객체와 함께 정확히 한 번 불린다. 콜백 자체가 실패해도(예: 알림 전송
        오류) 삼켜서 리로드 실패 처리에 영향을 주지 않는다."""
        mtime = self.path.stat().st_mtime
        overlay_mtime = (
            self._overlay_path.stat().st_mtime if self._overlay_path.exists() else None
        )
        if mtime == self._mtime and overlay_mtime == self._overlay_mtime:
            return False
        try:
            new_raw = _read_merged(self.path)
        except (yaml.YAMLError, OSError) as e:
            logger.error(
                "settings.yaml 리로드 실패 — 마지막으로 성공한 설정을 유지합니다: %s: %s",
                type(e).__name__, e,
            )
            # 같은 깨진 파일에 매 사이클 재시도하지 않도록 mtime은 갱신한다 —
            # 다음 실제 변경(파일이 다시 저장됨)에만 재시도한다.
            self._mtime = mtime
            self._overlay_mtime = overlay_mtime
            if on_error is not None:
                try:
                    on_error(e)
                except Exception:  # noqa: BLE001 — 알림 실패가 리로드 실패 처리를 막으면 안 된다
                    logger.exception("settings.yaml 리로드 실패 알림 콜백 자체가 실패함(무시)")
            return False
        self.raw = new_raw
        self._mtime = mtime
        self._overlay_mtime = overlay_mtime
        return True


def load_settings(settings_path: str = DEFAULT_SETTINGS_PATH) -> Settings:
    """환경변수 로드 후 settings.yaml 파싱. 우선순위: **프로세스 환경 > .env.local > .env**

    `load_dotenv(".env.local", override=True)`만 쓰면 파일이 **실제 프로세스
    환경변수까지** 덮어쓴다. 그러면 `MODE=paper python -m quant.apps.cli paper`가
    조용히 무시되고 `.env.local`의 값이 이긴다 — 운영자가 안전을 위해 명시적으로
    건 설정이 사라지는 방향이라 위험하다(실측: `MODE=live`로 실행해도 `MODE=paper`로
    해석됐다). `MODE`는 `TossBroker.place_order`의 실주문 게이트이자 승인 게이트의
    활성 조건이므로, 이 값의 해석이 애매하면 안 된다.

    그래서 파일을 읽기 전 프로세스 환경을 스냅샷해 두고, 로드 후 그 키들을 되돌린다.
    결과적으로 명시적으로 준 환경변수가 항상 최종 승자이고, 파일끼리는 기존대로
    `.env.local`이 `.env`를 이긴다.
    """
    explicit = dict(os.environ)  # 파일 로드 전에 이미 있던 값 = 사람이 명시적으로 준 값
    load_dotenv(".env")
    load_dotenv(".env.local", override=True)
    os.environ.update(explicit)
    path = Path(settings_path)
    try:
        raw = _read_merged(path)
    except (yaml.YAMLError, OSError) as e:
        # 부팅 시점 실패는 핫 리로드(reload_if_changed)와 다르다 — 유지할 "마지막
        # 성공한 설정"이 아예 없으므로 여기는 진짜 치명적이다. 조용히 기본값으로
        # 넘어가지 않고 원인을 분명히 남긴 뒤 그대로 올린다(프로세스는 죽어야
        # 한다 — 깨진 설정으로 기동하는 것보다 안전하다).
        logger.error(
            "settings.yaml 로드 실패(기동 불가) — %s: %s: %s", path, type(e).__name__, e,
        )
        raise
    return Settings(raw, path)
