"""외국인 수급 원장(서브프로젝트 I) — 종목별 외국인·기관 일별 순매수 시계열.

`quant/control/flows.py`(5일/기간 합계 뷰용)와 같은 관례(멱등 upsert per
(date, symbol), tmp 파일에 쓰고 `os.replace` 로 원자적 치환)를 따르되, 이
원장은 `quant.analyze.foreign_trend.classify()` 전용 입력이다 — 그 분석기는
날짜 오름차순 시계열에서 유입→이탈→재유입 반전을 판정해야 하므로
`load_series` 가 최신순이 아니라 오름차순으로 돌려준다.

**사용자 원칙**: "한국주식은 기관 매수세는 노이즈 — 메인 리서치와 비리 없는
매수는 오로지 외국인 매수 추세." 기관 데이터(`inst_net`)는 여기서도 같이
쌓지만(표시용), 라벨 판단에는 쓰지 않는다 — 판단은 `foreign_trend.py` 의 몫.
"""
from __future__ import annotations

import json
from pathlib import Path


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
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
    """tmp 에 쓰고 os.replace 로 원자적 치환(flows.py/selections.py 관례) —
    쓰다 죽어도 원본이 남는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


def append_daily(rows: list[dict], path: Path) -> int:
    """일별 외국인/기관 순매수 행을 원장에 반영한다. (date, symbol) 은 최신
    값으로 갱신한다(멱등) — 하루 두 번 리포트를 빌드해도 행이 늘지 않는다
    (`flows.append_flows` 와 동일한 upsert 방식).

    `date`/`symbol` 이 없는 행은 건너뛴다.
    """
    existing = {(r["date"], r["symbol"]): r for r in _load(path)}
    written = 0
    for row in rows:
        d, symbol = row.get("date"), row.get("symbol")
        if not d or not symbol:
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


def load_series(path: Path, symbol: str, days: int = 20) -> list[dict]:
    """`symbol` 의 최근 `days` 일 시계열을 날짜 오름차순으로 돌려준다.

    `classify()` 가 과거→최근 순으로 유입/이탈 반전을 읽어야 하므로, 원장
    적재 순서와 무관하게 항상 날짜 오름차순으로 정렬해 최근 `days` 개만
    남긴다. 데이터가 `days` 보다 짧으면 있는 만큼만 정직하게 돌려준다.
    """
    rows = sorted(
        (r for r in _load(path) if r.get("symbol") == symbol),
        key=lambda r: r["date"],
    )
    return rows[-days:] if days else rows
