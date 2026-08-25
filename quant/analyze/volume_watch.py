"""최근 며칠 거래대금 상위에 반복 등장한 종목 — "오늘 뉴스는 없지만 최근 계속
돈이 몰린" 종목을 관심 리스트에 붙잡아 두기 위한 신호 (2026-08-25, 소유자 지시).

이미 저장된 과거 스냅샷(`data/snapshots/{market}/*.json`)만 읽는다 — `rank_history.py`와
같은 패턴이다. 새 네트워크 호출이 없고, 스냅샷 디렉터리 내용이 그대로면 항상 같은
값이 나온다(재현성 유지).
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

BOARD = "거래대금"


def recurring_volume_symbols(
    snap_root: Path,
    market: str,
    today: date,
    days: int = 5,
    min_appearances: int = 2,
    board_top: int = 10,
) -> list[str]:
    """최근 `days`일(오늘 제외) 거래대금 보드 상위 `board_top`에 `min_appearances`회
    이상 등장한 6자리 KR 종목코드를, 등장 횟수 내림차순으로 반환.

    스냅샷 파일이 없거나(주말 등) 그날 토스 랭킹이 실패했으면 그냥 건너뛴다 —
    결측을 예외로 올리지 않는다(`load_trading_amount_history`와 같은 관례).
    """
    from quant.collect.snapshot import load_snapshot

    counts: dict[str, int] = {}
    for back in range(1, days + 1):
        p = snap_root / market / f"{(today - timedelta(days=back)).isoformat()}.json"
        if not p.exists():
            continue
        try:
            snap = load_snapshot(p)
        except Exception:
            continue
        r = snap.results.get("toss_rankings")
        if r is None or not r.ok or not r.data:
            continue
        seen_today: set[str] = set()
        for item in r.data.get("boards", {}).get(BOARD, [])[:board_top]:
            symbol = item.get("symbol")
            if symbol and len(symbol) == 6 and symbol.isdigit():
                seen_today.add(symbol)
        for symbol in seen_today:
            counts[symbol] = counts.get(symbol, 0) + 1

    return sorted(
        (s for s, n in counts.items() if n >= min_appearances),
        key=lambda s: (-counts[s], s),
    )
