"""토스 랭킹 거래대금의 과거 이력 — "지금이 평소보다 몰리는가"의 베이스라인.

이미 저장된 과거 스냅샷(`data/snapshots/{market}/*.json`)만 읽는다. `quant/analyze/delta.py`의
`previous_snapshot`과 같은 패턴이다 — 새 네트워크 호출이 없고, 스냅샷 디렉터리
내용이 그대로면 항상 같은 값이 나온다(재현성 유지).

거래대금 보드만 베이스라인으로 쓴다. 다른 보드(상승률/하락률)는 등락률이라 그
자체가 이미 상대값이고, 거래대금만 "이 종목에 오늘 얼마가 몰렸나"를 절대
금액(원)으로 준다. 종목이 그날 거래대금 보드 10위 밖이면 그 날은 이력에서
빠진다 — 없는 날을 0으로 채우면 베이스라인이 실제보다 낮게 왜곡된다.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

LOOKBACK_DAYS = 10
BASELINE_BOARD = "거래대금"


def load_trading_amount_history(
    market: str,
    symbol: str,
    before: date,
    snap_root: Path,
    lookback: int = LOOKBACK_DAYS,
) -> list[int]:
    """symbol이 거래대금 보드에 올랐던 과거(= before 이전) 날짜들의 거래대금 목록.

    스냅샷 파일이 없거나(주말 등) 그날 토스 랭킹이 실패했으면 그냥 건너뛴다 —
    결측을 0으로 채우지 않는다.
    """
    from quant.collect.snapshot import load_snapshot

    amounts: list[int] = []
    for back in range(1, lookback + 1):
        p = snap_root / market / f"{(before - timedelta(days=back)).isoformat()}.json"
        if not p.exists():
            continue
        try:
            snap = load_snapshot(p)
        except Exception:
            continue
        r = snap.results.get("toss_rankings")
        if r is None or not r.ok or not r.data:
            continue
        for item in r.data.get("boards", {}).get(BASELINE_BOARD, []):
            if item["symbol"] == symbol:
                amounts.append(item["trading_amount"])
                break
    return amounts
