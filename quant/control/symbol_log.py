"""종목 점수 일일 원장 — 조사한 것을 버리지 않는다 (2026-08-26 소유자 지시).

> "당일이 될 때마다 조사하고 버리는 게 아니라, 이미 공들여 서치하고 파악한
> 종목들에 대한 점수와 투자의견들을 숫자화해서 기록하는 거지. 다음날 리포트를
> 만들거나 종목을 선정할 때 좋은 흐름을 이어오던 주식들을 참고하고, 미래에는
> 과거의 우리 기록 vs 실제 움직임을 보면서 시스템이 잘 파악했는지 본다."

기존 원장과의 역할 구분(겹침 아님):
- selections/judgments 원장 — **후보로 뽑힌** 종목의 판단 기록(outcomes 가 채점).
- 이 원장(`symbol_scores.jsonl`) — 그날 **조사된 전 종목**의 수치 스냅샷.
  후보가 안 됐어도 기록한다 — "그날 버려진 조사"가 다음날의 연속성 근거이자
  미래의 적중률 검증 표본이 된다.

행 스키마(하루 1행/시장/종목, 같은 키 재기록 시 마지막이 이긴다):
  {date, market, symbol, name, score, label, today_articles, streak_days,
   change_pct, factors: [키...], recorded_at}

순수 조립 + append-only 파일 I/O(제어 평면 원장 관례).
"""
from __future__ import annotations

import json
import logging
from datetime import date as dtdate, datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATH = "data/ledger/symbol_scores.jsonl"


def build_score_rows(
    day: dtdate,
    market: str,
    cont: dict[str, dict],
    scores: dict[str, dict],
    sym_quotes: dict | None = None,
    names: dict[str, str] | None = None,
) -> list[dict]:
    """그날 조사된 종목(cont 전체) → 원장 행. 순수 함수.

    값이 없는 축은 **키를 생략**한다(None 을 0으로 위장하지 않는다 —
    selections 원장과 같은 원칙)."""
    names = names or {}
    sym_quotes = sym_quotes or {}
    rows: list[dict] = []
    for symbol, c in sorted(cont.items()):
        row: dict = {
            "date": day.isoformat(),
            "market": market,
            "symbol": symbol,
        }
        name = names.get(symbol)
        if name:
            row["name"] = name
        s = scores.get(symbol) or {}
        if s.get("score") is not None:
            row["score"] = s["score"]
        if s.get("label"):
            row["label"] = s["label"]
        if s.get("factors"):
            row["factors"] = [f.get("key") for f in s["factors"] if f.get("key")]
        for key in ("today_articles", "streak_days"):
            if c.get(key) is not None:
                row[key] = c[key]
        q = sym_quotes.get(symbol)
        change = (q or {}).get("change_pct") if isinstance(q, dict) else None
        if change is not None:
            row["change_pct"] = change
        rows.append(row)
    return rows


def append_scores(rows: list[dict], path: Path | str = DEFAULT_PATH) -> int:
    """append-only 기록. 같은 (date, market, symbol)이 이미 있으면 **건너뛴다**
    (재실행 멱등 — 아침 리포트를 손으로 다시 돌려도 원장이 불지 않는다)."""
    if not rows:
        return 0
    p = Path(path)
    existing: set[tuple] = set()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                existing.add((r.get("date"), r.get("market"), r.get("symbol")))
            except ValueError:
                continue
    p.parent.mkdir(parents=True, exist_ok=True)
    added = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with p.open("a", encoding="utf-8") as f:
        for row in rows:
            key = (row.get("date"), row.get("market"), row.get("symbol"))
            if key in existing:
                continue
            existing.add(key)
            f.write(json.dumps({**row, "recorded_at": now}, ensure_ascii=False) + "\n")
            added += 1
    return added


def load_scores(path: Path | str = DEFAULT_PATH, days: int | None = None,
                today: dtdate | None = None) -> list[dict]:
    """원장 로드(깨진 줄 건너뜀). `days`를 주면 `today` 기준 최근 N일만."""
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict] = []
    cutoff = None
    if days is not None and today is not None:
        from datetime import timedelta

        cutoff = (today - timedelta(days=days)).isoformat()
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if cutoff is not None and (r.get("date") or "") < cutoff:
            continue
        rows.append(r)
    return rows


def hot_streak_symbols(
    rows: list[dict], today: dtdate, market: str = "KR",
    min_days: int = 2, min_score: float = 2.0, lookback_days: int = 4,
) -> list[str]:
    """"좋은 흐름을 이어오던 주식" — 최근 `lookback_days`일 중 서로 다른
    `min_days`일 이상 score >= `min_score` 였던 종목(오늘 제외), 최다 일수 순.

    다음날 아침 후보 유니버스에 합류시키는 용도다(extra_watch). 문턱은
    score_symbol 의 요인 가점 체계(요인당 ±1) 기준 "가점 2개 이상" [미검증
    초기값] — 이 축의 실효는 outcomes/주간 리뷰가 판정한다."""
    from collections import Counter
    from datetime import timedelta

    cutoff = (today - timedelta(days=lookback_days)).isoformat()
    today_iso = today.isoformat()
    counts: Counter = Counter()
    for r in rows:
        d = r.get("date") or ""
        if not (cutoff <= d < today_iso):
            continue
        if (r.get("market") or "KR") != market:
            continue
        if (r.get("score") or 0) >= min_score:
            counts[(r["symbol"])] += 1
    return [sym for sym, n in counts.most_common() if n >= min_days]


def accuracy_join(
    rows: list[dict],
    next_day_change: dict[tuple[str, str], float],
) -> dict | None:
    """기록한 점수 vs 다음 거래일 실제 등락 — "시스템이 잘 파악했는가"의 숫자.

    `next_day_change`: {(date, symbol): 다음 거래일 등락%} — 가격 조회는 호출부
    몫(순수 유지). 매칭되는 표본만 쓴다(없는 날을 지어내지 않는다).

    반환: 점수 구간별 {n, avg_next_pct, hit_rate(다음날 양봉 비율)}.
    표본 0이면 None.
    """
    buckets = {"score>=3": [], "score=2": [], "score<=1": []}
    for r in rows:
        key = (r.get("date"), r.get("symbol"))
        nxt = next_day_change.get(key)
        if nxt is None or r.get("score") is None:
            continue
        s = r["score"]
        b = "score>=3" if s >= 3 else ("score=2" if s == 2 else "score<=1")
        buckets[b].append(float(nxt))
    out = {}
    total = 0
    for name, vals in buckets.items():
        if not vals:
            continue
        total += len(vals)
        out[name] = {
            "n": len(vals),
            "avg_next_pct": round(sum(vals) / len(vals), 3),
            "hit_rate": round(sum(1 for v in vals if v > 0) / len(vals), 3),
        }
    return out or None
