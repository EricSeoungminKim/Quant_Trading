"""파라미터 최적화 + walk-forward 검증 레이어.

라이브 엔진(app/, execution/, brokers/, risk/, strategies/)을 건드리지 않고
quant.backtest.run_backtest를 감싸 파라미터를 탐색한다. optuna/quantstats는
선택 의존성(pyproject.toml [dependency-groups] research)이며 각 함수 내부에서
지연 임포트한다 — 이 패키지를 import하는 것 자체는 그 두 패키지 설치 여부와
무관하게 항상 안전하다(paper/live 코드 경로는 애초에 이 패키지를 import하지 않는다).
"""
from __future__ import annotations

from quant.research.optimize import OptimizeResult, Trial, optimize
from quant.research.walkforward import (
    Window,
    WalkForwardResult,
    WindowResult,
    split_windows,
    walk_forward,
)

__all__ = [
    "OptimizeResult",
    "Trial",
    "optimize",
    "Window",
    "WalkForwardResult",
    "WindowResult",
    "split_windows",
    "walk_forward",
]
