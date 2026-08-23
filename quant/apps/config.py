"""설정 로더: config/settings.yaml + .env/.env.local. 핫 리로드 지원.

시크릿은 .env(.local)에서, 전략 파라미터는 settings.yaml에서 — 원칙 5 (README).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

DEFAULT_SETTINGS_PATH = "config/settings.yaml"


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Settings:
    """settings.yaml을 감싸는 얇은 typed 접근자. 원본 dict는 .raw로 접근 가능."""

    def __init__(self, raw: dict[str, Any], path: Path):
        self.raw = raw
        self.path = path
        self._mtime = path.stat().st_mtime

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
    def notifications(self) -> dict:
        return self.raw.get("notifications", {})

    @property
    def banker(self) -> dict:
        return self.raw.get("banker", {})

    @property
    def poll_seconds(self) -> float:
        return self.engine.get("poll_seconds", 10)

    def reload_if_changed(self) -> bool:
        """yaml mtime이 바뀌었으면 다시 읽어 raw를 교체한다. 바뀌었으면 True를 반환."""
        mtime = self.path.stat().st_mtime
        if mtime == self._mtime:
            return False
        self.raw = _read_yaml(self.path)
        self._mtime = mtime
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
    return Settings(_read_yaml(path), path)
