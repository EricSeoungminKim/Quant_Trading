"""전 세션 대비 변화. 리포트가 하루하루 따로 놀지 않게 하는 최소 장치.

직전 스냅샷을 며칠까지 거슬러 찾는다 — 주말·공휴일·장애로 하루 걸러도 비교가
끊기지 않아야 한다.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from quant.collect.contracts import Snapshot

LOOKBACK_DAYS = 10
_ACTORS = ("개인", "외국인", "기관계")


def _ok(snap: Snapshot | None, key: str) -> dict | None:
    if snap is None:
        return None
    r = snap.results.get(key)
    return r.data if r is not None and r.ok and r.data else None


def compare(current: Snapshot, previous: Snapshot | None) -> dict:
    quotes: dict[str, dict] = {}
    cur_q, prev_q = _ok(current, "market"), _ok(previous, "market")
    if cur_q and prev_q:
        for sym, q in cur_q.get("quotes", {}).items():
            p = prev_q.get("quotes", {}).get(sym)
            if not p or not p.get("close"):
                continue
            quotes[sym] = {
                "label": q.get("label", sym),
                "close": q["close"],
                "prev_close": p["close"],
                "pct": (q["close"] / p["close"] - 1) * 100,
            }

    flow: dict[str, dict] = {}
    cur_f, prev_f = _ok(current, "kospi_flow"), _ok(previous, "kospi_flow")
    if cur_f and prev_f and cur_f.get("rows") and prev_f.get("rows"):
        c_row, p_row = cur_f["rows"][0], prev_f["rows"][0]
        # 같은 날짜면 비교할 게 없다 — 네이버는 전 거래일까지만 확정치를 준다
        if c_row.get("date") != p_row.get("date"):
            for actor in _ACTORS:
                if actor not in c_row or actor not in p_row:
                    continue
                c, p = c_row[actor], p_row[actor]
                flow[actor] = {
                    "current": c,
                    "previous": p,
                    "flipped": (c > 0) != (p > 0),
                }
    return {"quotes": quotes, "flow": flow}


def previous_snapshot(market: str, session_date: date, root: Path) -> Snapshot | None:
    from quant.collect.snapshot import load_snapshot

    for back in range(1, LOOKBACK_DAYS + 1):
        p = root / market / f"{(session_date - timedelta(days=back)).isoformat()}.json"
        if p.exists():
            return load_snapshot(p)
    return None
