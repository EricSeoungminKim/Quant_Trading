"""전략별 기본 param space — CLI(`quant.apps.cli optimize`)가 --param-space 없이
호출될 때 쓰는 기본값. 형식은 optimize._sample_params가 읽는 것과 동일한 데이터
구조라 그대로 YAML로 옮겨 --param-space로 오버라이드할 수 있다."""
from __future__ import annotations

DEFAULT_PARAM_SPACES: dict[str, dict] = {
    "donchian": {
        "lookback_bars": {"type": "int", "low": 20, "high": 60, "step": 5},
        "stop_min_bps": {"type": "float", "low": 0, "high": 30, "step": 5},
        "allow_same_day_reentry": {"type": "categorical", "choices": [True, False]},
    },
    # ORB는 논문(SSRN 4416622/4729284)이 규칙과 값을 명시한 전략이라, 탐색 대상은
    # 논문이 실제로 변형해 본 축으로만 좁힌다 — 발표 규격을 임의 파라미터로 흩뜨리면
    # 재현 검증이 아니라 새 전략 탐색이 된다.
    # 탐색 범위는 **비용 역학**에서 나왔다(2026-08-07 전구간 측정). 왕복 1회당
    # 명목 대비 총수익이 왕복 비용(키움 7bp x2 = 14bp)을 넘어야 이기는데:
    #  - 손절 폭: 논문 기본값(ATR x0.05, 총수익 2.4bp)은 너무 타이트하다. 넓힐수록
    #    R배수당 명목 수익이 커지다가 ~x0.35에서 8.2bp로 정점을 찍고 다시 준다.
    #  - 도지 임계: 논문이 "open ~ close"라고만 쓰고 값을 정의하지 않은 자리다.
    #    0.5~0.8%에서 23~27bp로 정점을 찍고 1.25% 이상에서 급락한다(표본 고갈).
    # **주의**: 이 범위는 전구간을 본 뒤 정해졌으므로 그 자체가 선택 편의를 담는다.
    # 여기서 나온 최적값은 walk-forward의 out-of-sample 구간으로만 판정해야 한다.
    "orb": {
        "stop_mode": {"type": "categorical", "choices": ["or_extreme", "atr_pct"]},
        "atr_stop_mult": {"type": "float", "low": 0.10, "high": 1.00, "step": 0.05},
        "doji_threshold_pct": {"type": "float", "low": 0.0, "high": 1.2, "step": 0.1},
        "profit_target_r": {"type": "categorical", "choices": [None, 10.0]},
        "relative_volume_min": {"type": "categorical", "choices": [None, 1.0]},
    },
}
