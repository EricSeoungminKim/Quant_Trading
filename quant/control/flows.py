"""수급 원장 — 종목별 외국인·기관 순매수를 하루치씩 축적한다.

## 왜 긁지 않고 쌓는가

1년치 수급을 화면에 보여주려면 종목당 20페이지(네이버 프론드매매동향)를 넘겨야
한다. 토스는 주문 집행과 같은 경로라 요청을 늘리면 안 되고, 네이버도 예절
문제다(§핵심결정 2, `docs/superpowers/specs/2026-08-15-report-ui-design.md`).

대신 리포트 빌드가 이미 받아오는 네이버 10일 수급 스냅샷(`stock_detail.py` 의
`flow` — 필드는 `date`/`foreign_net`/`inst_net` 등)을 그날그날
`data/ledger/flows.jsonl` 에 적재한다. 표시 가능한 기간은 쌓인 만큼만 늘어난다.

**없는 기간은 0 이 아니다.** "10일 순매수 0" 과 "10일치가 아직 안 쌓였다"는
전혀 다른 사건이므로, `window_sums`/`coverage` 는 데이터가 없으면 `None`/`0`을
명시적으로 돌려주고 절대 패딩하지 않는다.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path


def load(path: Path) -> list[dict]:
    """원장 전체. 깨진 줄은 건너뛴다(selections.load 와 같은 관례).

    report_cli 가 기간 뷰(render.flow_periods)에 넘길 rows 를 여기서 읽는다
    (Task 6, §리포트 고도화 B).
    """
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("date") and row.get("symbol"):
            rows.append(row)
    return rows


def _rewrite(rows: list[dict], path: Path) -> None:
    """tmp 에 쓰고 os.replace 로 원자적 치환(selections.py 관례) — 쓰다 죽어도
    원본이 남는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


def append_flows(path: Path, rows: list[dict], today: str) -> int:
    """수급 스냅샷을 원장에 반영한다. 같은 (date, symbol) 은 최신 값으로 갱신한다.

    하루 두 번 빌드해도 행이 늘지 않는다(멱등) — 나중에 들어온 값이 이긴다.
    `today` 이후 날짜의 행은 버린다: 스크래핑 파싱 오류로 미래 날짜가 섞이면
    원장이 영구히 오염되므로(selections.py 상단의 2026-08-14 사고와 같은 종류의
    함정) 여기서 미리 막는다.
    """
    try:
        today_d = date.fromisoformat(today)
    except (TypeError, ValueError):
        return 0

    existing = {(r["date"], r["symbol"]): r for r in load(path)}
    written = 0
    for row in rows:
        d, symbol = row.get("date"), row.get("symbol")
        if not d or not symbol:
            continue
        try:
            if date.fromisoformat(d) > today_d:
                continue
        except ValueError:
            continue
        existing[(d, symbol)] = {
            "date": d,
            "symbol": symbol,
            "foreign_net": row.get("foreign_net"),
            "inst_net": row.get("inst_net"),
        }
        written += 1

    _rewrite(list(existing.values()), path)
    return written


def window_sums(rows: list[dict], symbol: str, days: int, today: str) -> dict | None:
    """최근 `days` 일(오늘 포함) 동안의 순매수 합계. 짧으면 있는 만큼 정직하게.

    데이터가 하루도 없으면 `None`(0 으로 위장하지 않는다). 있는 만큼만 더하고
    실제로 더한 일수를 `n_days` 로 같이 돌려준다 — 화면이 "10일" 이라고 주장하지
    않고 "N일치"라고 정직하게 표기할 수 있게.
    """
    try:
        today_d = date.fromisoformat(today)
    except (TypeError, ValueError):
        return None
    start = today_d - timedelta(days=days - 1)

    matched = []
    for r in rows:
        if r.get("symbol") != symbol:
            continue
        try:
            d = date.fromisoformat(r.get("date"))
        except (TypeError, ValueError):
            continue
        if start <= d <= today_d:
            matched.append(r)

    if not matched:
        return None

    return {
        "foreign": sum(r.get("foreign_net") or 0 for r in matched),
        "inst": sum(r.get("inst_net") or 0 for r in matched),
        "n_days": len(matched),
    }


def coverage(rows: list[dict]) -> dict:
    """원장에 실제로 쌓인 기간 — 화면의 "N일치 축적됨(YYYY-MM-DD 부터)" 표기용."""
    dates = sorted({r["date"] for r in rows if r.get("date")})
    if not dates:
        return {"first_date": None, "n_days": 0}
    return {"first_date": dates[0], "n_days": len(dates)}
