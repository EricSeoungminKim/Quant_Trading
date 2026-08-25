"""롤링 OOS(walk-forward) 안정성 하네스 — 전략 교체 시대의 상시 검증 도구.

**파라미터 자동 탐색을 하지 않는다.** `quant/research/walkforward.py`(optuna 기반
파라미터 탐색 + walk-forward, `optimize` CLI 서브커맨드)와는 목적이 다르다 — 이
모듈은 settings.yaml에 이미 정해진 **같은 설정**을 여러 시간 창에서 반복 실행해
"성과가 구간마다 안정적인가"만 본다. 이 저장소는 거버너 층 0에서 사이징·파라미터
자동화를 금지한다(루트 CLAUDE.md) — 이 모듈이 하는 일은 탐색이 아니라 관측이다.

## 오프셋 백테스트가 어떻게 가능한가

`run_backtest(days=..., end=...)`는 "`end` 이전 완성봉 중 마지막 `days`일치"를
리플레이한다(ADR-4, look-ahead 금지). `end`를 과거로 물리면 그 시점 기준의
`days`일 창을 얻는다 — 그래서 별도 엔진 수정 없이 `end`를 뒤로 옮겨가며 여러
과거 창을 뽑을 수 있다. stub 소스는 `end`가 가리키는 시점까지 합성 데이터를
자동으로 늘려 생성하므로(engine.run_backtest 참고) 임의 오프셋에서도 동작한다.
history 소스는 디스크에 있는 실데이터 범위를 벗어나면 그만큼 짧은 창이 된다 —
이는 데이터 가용성의 한계이지 이 모듈의 결함이 아니다.

`days`는 거래일(봉 개수) 기준이고 여기서 다루는 오프셋(`total_days`/`window_days`/
`step_days`)은 **달력일** 기준이다 — 두 축을 섞으면 안 되므로 fold마다
`end = anchor - Timedelta(days=offset)`로 달력일 오프셋만 시각 계산에 쓰고,
그 시각 이전 `window_days`만큼(거래일 개수 근사)을 `run_backtest(days=window_days,
end=end)`로 리플레이한다.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from quant.backtest.engine import run_backtest
from quant.backtest.fitness import evaluate


def rolling_windows(total_days: int, window_days: int, step_days: int) -> list[tuple[int, int]]:
    """`total_days`(과거 몇 일까지 볼지)를 `window_days`(창 크기)·`step_days`(창 간격)로
    나눈 (offset_days_ago_start, offset_days_ago_end) 목록 — 최신 창부터 과거 창 순.

    각 튜플의 두 값은 "오늘로부터 며칠 전"이다: `end`가 창의 최신 쪽(오늘에 더
    가까움, 작은 값), `start`가 창의 오래된 쪽(더 먼 과거, 큰 값)이다. 예:
    `rolling_windows(360, 90, 90)` → `[(90, 0), (180, 90), (270, 180), (360, 270)]`
    (겹치지 않는 4개 창, 최신 90일부터 역순으로).
    """
    windows: list[tuple[int, int]] = []
    end_offset = 0
    while end_offset + window_days <= total_days:
        start_offset = end_offset + window_days
        windows.append((start_offset, end_offset))
        end_offset += step_days
    return windows


def run_walkforward(
    strategy_id: str,
    total_days: int = 360,
    window_days: int = 90,
    step_days: int = 45,
    interval: str = "15m",
    source: str = "stub",
    symbols: list[str] | None = None,
    settings_path: str = "config/settings.yaml",
    anchor: datetime | pd.Timestamp | None = None,
) -> list[dict]:
    """`rolling_windows`가 뽑은 창마다 `run_backtest` → `fitness.evaluate`를 돌려
    fold별 결과 목록을 낸다. 파라미터는 전부 settings.yaml 그대로(param_overrides
    없음) — 창마다 바뀌는 것은 시간 구간뿐이다.

    `anchor`(기본 현재 시각)는 오프셋 계산의 기준점이다. 테스트가 결정론적으로
    돌게 하려면 고정된 `anchor`를 넘긴다."""
    anchor_ts = pd.Timestamp(anchor) if anchor is not None else pd.Timestamp.now()
    windows = rolling_windows(total_days, window_days, step_days)
    folds: list[dict] = []
    for start_offset, end_offset in windows:
        end_ts = anchor_ts - pd.Timedelta(days=end_offset)
        result = run_backtest(
            strategy_id=strategy_id, days=window_days, interval=interval, source=source,
            settings_path=settings_path, end=end_ts, symbols=symbols,
        )
        fit = evaluate(result)
        folds.append({
            "start_offset_days": start_offset,
            "end_offset_days": end_offset,
            "end": end_ts.date().isoformat(),
            **fit.to_dict(),
        })
    return folds


def stability_summary(fold_results: list[dict]) -> dict:
    """fold별 fitness dict(`net_bps`, `n_round_trips`, ...) → 안정성 요약.

    `verdict_hint`는 **판정이 아니라 힌트 문자열**이다 — 표본이 부족하면 그렇게
    말할 뿐, "채택/기각"을 대신 결정하지 않는다(사람이 읽고 판단한다)."""
    if not fold_results:
        return {
            "folds": 0, "n_positive": 0,
            "net_bps_median": None, "net_bps_min": None, "net_bps_max": None,
            "sufficient_folds": 0,
            "verdict_hint": "fold가 없음 — 표본 부족",
        }

    net_bps_values = sorted(f["net_bps"] for f in fold_results)
    n = len(net_bps_values)
    mid = n // 2
    median = net_bps_values[mid] if n % 2 else (net_bps_values[mid - 1] + net_bps_values[mid]) / 2
    n_positive = sum(1 for v in net_bps_values if v > 0)
    # 왕복 10건 미만인 fold는 그 fold 자체의 net_bps가 표본 부족으로 흔들릴 수
    # 있다(MIN_ROUND_TRIPS=30보다 낮은 문턱을 쓰는 이유: 여기서는 fold 전체를
    # 버리는 게 아니라 "이 fold를 얼마나 믿을지"의 참고 지표일 뿐이라 fitness.evaluate
    # 의 채택 기준(30)보다는 완화했다).
    sufficient_folds = sum(1 for f in fold_results if f.get("n_round_trips", 0) >= 10)

    if sufficient_folds < n:
        verdict_hint = (
            f"표본 부족: fold {n}개 중 {sufficient_folds}개만 왕복 10건 이상 — "
            "안정성 판단에 주의(추가 fold의 net_bps는 신뢰구간이 넓다)"
        )
    elif n_positive == n:
        verdict_hint = f"fold {n}개 전부 net_bps 양수 — 구간별로 안정적일 가능성"
    elif n_positive == 0:
        verdict_hint = f"fold {n}개 전부 net_bps 음수 — 엣지 없음 가능성"
    else:
        verdict_hint = f"fold {n}개 중 {n_positive}개만 net_bps 양수 — 구간별 성과가 불안정"

    return {
        "folds": n,
        "n_positive": n_positive,
        "net_bps_median": round(median, 3),
        "net_bps_min": round(net_bps_values[0], 3),
        "net_bps_max": round(net_bps_values[-1], 3),
        "sufficient_folds": sufficient_folds,
        "verdict_hint": verdict_hint,
    }
