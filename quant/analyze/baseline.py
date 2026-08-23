"""baseline.py — 검증된 채점기(watch_scorer TREND 프로필)의 적용 범위 확장.

watch_scorer._trend_score를 그대로 감싸 유니버스 전체 채점에 재사용한다. 배점은
여기서 다시 정의하지 않는다 — watch_scorer가 유일한 정의처다.

핵심 계약: 채점 불가(행 부족·컬럼 결측·예외)는 **None**을 반환한다. 0은 절대
반환하지 않는다 — 0은 "최하위 평가"가 되어 채점 루프의 IC(정보계수)를 오염시킨다."""
from __future__ import annotations

from datetime import date

import pandas as pd

from quant.analyze.watch_scorer import _MIN_ROWS, _trend_score

_REQUIRED_COLUMNS = ("open", "close", "volume")  # _trend_score가 실제로 쓰는 컬럼


def _as_date(ts) -> date:
    return ts.date() if hasattr(ts, "date") else ts


def baseline_score(ohlcv: pd.DataFrame, today: date | None = None) -> int | None:
    """일봉 OHLCV DataFrame(시간 오름차순)을 watch_scorer의 TREND 프로필로 채점한다.

    당일(및 미래) 미완성 봉은 채점 전 제거한다 — watch_scorer.score_symbol의 방어와
    동일 규칙(그 함수 428-432행): 채점 시점에 오늘 행이 거래량 0/일부로 끼어 있으면
    지표가 붕괴한다.

    채점 불가(행 부족·컬럼 결측·예외)는 None. 0을 반환하지 않는다."""
    if ohlcv is None or not len(ohlcv):
        return None
    if not set(_REQUIRED_COLUMNS).issubset(ohlcv.columns):
        return None

    today = today or date.today()
    dates = [_as_date(ts) for ts in ohlcv.index]
    completed = [d < today for d in dates]
    if not all(completed):
        ohlcv = ohlcv[completed]

    if len(ohlcv) < _MIN_ROWS:
        return None

    try:
        score, _reasons, _breakdown = _trend_score(ohlcv)
    except Exception:  # noqa: BLE001 — 채점 불가는 None, 조용한 0 금지
        return None
    return int(score)
