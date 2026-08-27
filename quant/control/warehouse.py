"""아티팩트 → 분석 저장소 적재.

**아티팩트가 진실, DB 는 색인이다.** 여기서 하는 일은 옮기기뿐이고, 원본
JSONL 은 그대로 남는다. DB 를 통째로 날려도 재적재하면 복구된다.

이 모듈이 푸는 문제 하나: "이 체결로 이어진 기사가 뭐였나". 지금은 두 저장소의
JSONL 을 손으로 조인해야 하고, 그게 개선 루프의 핵심 질문이다.

행 변환기(`article_row`/`trade_row`/`selection_row`)는 순수 함수다 — DB 없이
전부 테스트된다. I/O 는 `ingest_*` 만 한다.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from quant.adapters.db import upsert_sql

log = logging.getLogger(__name__)

_MARKETS = ("KR", "US")


def _utc_naive(iso: str | None) -> datetime | None:
    """ISO 문자열 → UTC naive datetime. MySQL DATETIME 에 타임존이 없어서다.

    **naive 를 로컬시각으로 오해하지 않도록 전부 UTC 로 통일한다.** 이 저장소는
    이미 타임존 때문에 9시간이 어긋난 적이 있다(2026-08-13 볼트 렌더).
    """
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def link_sha(link_key: str) -> str:
    return hashlib.sha256(link_key.encode("utf-8")).hexdigest()


def fill_sha(rec: dict) -> str:
    """체결 한 줄의 내용 해시.

    원장에 id 가 없어 자연키가 없다. `(ts, 전략, 심볼, 방향)` 은 같은 초에 두 번
    체결되면 충돌한다. 내용 해시를 쓰면 같은 파일을 몇 번 적재해도 행이 늘지 않는다.
    """
    canon = json.dumps(rec, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ── 순수 행 변환기 ────────────────────────────────────────────────────────

ARTICLE_COLS = ["market", "link_sha", "link", "title", "outlet", "published",
                "published_known", "first_seen", "last_seen", "seen_count", "collect_date"]
# 충돌 시 갱신할 것만. first_seen 은 최초값을 지킨다 — 처음 본 시각이 바뀌면
# "언제부터 돌던 기사인가"를 잃는다.
ARTICLE_UPDATE = ["last_seen", "seen_count", "title", "outlet"]


def article_row(rec: dict, market: str, collect_date: date) -> tuple | None:
    """뉴스 저장소 한 줄 → article 행. 링크나 제목이 없으면 None."""
    key = rec.get("key") or rec.get("link")
    if not key or not (rec.get("title") or "").strip():
        return None
    first = _utc_naive(rec.get("first_seen"))
    last = _utc_naive(rec.get("last_seen")) or first
    if first is None:
        return None
    return (
        market,
        link_sha(str(key)),
        str(rec.get("link") or key)[:1024],
        str(rec["title"]).strip()[:512],
        str(rec.get("outlet") or "")[:128],
        _utc_naive(rec.get("published")),
        1 if rec.get("published_known") else 0,
        first,
        last,
        int(rec.get("seen_count") or 1),
        collect_date,
    )


TRADE_COLS = ["fill_sha", "ts", "trade_date", "strategy_id", "symbol", "market",
              "side", "qty", "price", "fee", "realized_pnl", "reason"]


def trade_row(rec: dict) -> tuple | None:
    """체결 원장 한 줄 → trade 행. ts/symbol/side 가 없으면 None."""
    ts = _utc_naive(rec.get("ts"))
    symbol = str(rec.get("symbol") or "").strip()
    side = str(rec.get("side") or "").strip().lower()
    if ts is None or not symbol or side not in ("buy", "sell"):
        return None
    market = str(rec.get("market") or "").strip().upper()
    if market not in _MARKETS:
        market = "KR" if (symbol.isdigit() and len(symbol) == 6) else "US"
    return (
        fill_sha(rec),
        ts,
        ts.date(),
        str(rec.get("strategy_id") or rec.get("strategy") or "unknown")[:64],
        symbol[:16],
        market,
        side,
        float(rec.get("qty") or 0),
        float(rec.get("price") or 0),
        float(rec.get("fee") or 0),
        float(rec.get("realized_pnl") or 0),
        (str(rec["reason"])[:255] if rec.get("reason") else None),
    )


SELECTION_COLS = ["session_date", "market", "symbol", "name", "is_candidate",
                  "ai_score100", "trending_score100", "trending_label",
                  "news_articles_today", "news_streak_days", "in_ranking",
                  "ranking_bullish", "best_board_rank", "n_boards",
                  "relative_volume", "foreign_buy_streak", "inst_buy_streak",
                  "upside_pct", "close_price", "change_pct", "origin", "params"]
# 같은 날 리포트를 다시 돌리면 최신 판단으로 덮는다 — 선정은 그날의 최종 판단이다.
SELECTION_UPDATE = [c for c in SELECTION_COLS if c not in ("session_date", "market", "symbol")]


def _i(v) -> int | None:
    return None if v is None else int(v)


def _f(v) -> float | None:
    return None if v is None else float(v)


def _b(v) -> int | None:
    return None if v is None else (1 if v else 0)


def selection_row(rec: dict) -> tuple | None:
    """선정 원장 한 줄 → selection 행.

    점수는 전부 NULL 허용이다 — "0점"과 "계산 못 했다"를 섞으면 나중에 어느 쪽
    근거로 뽑혔는지 알 수 없다(`selections._attributes` 와 같은 원칙).
    """
    symbol = str(rec.get("symbol") or "").strip()
    session_date = rec.get("date")
    market = str(rec.get("market") or "").strip().upper()
    if not symbol or not session_date or market not in _MARKETS:
        return None
    return (
        session_date,
        market,
        symbol[:16],
        (str(rec["name"])[:128] if rec.get("name") else None),
        1 if rec.get("is_candidate") else 0,
        _i(rec.get("ai_score100")),
        _i(rec.get("trending_score100")),
        (str(rec["trending_label"])[:32] if rec.get("trending_label") else None),
        _i(rec.get("news_articles_today")),
        _i(rec.get("news_streak_days")),
        _b(rec.get("in_ranking")),
        _b(rec.get("ranking_bullish")),
        _i(rec.get("best_board_rank")),
        _i(rec.get("n_boards")),
        _f(rec.get("relative_volume")),
        _i(rec.get("foreign_buy_streak")),
        _i(rec.get("inst_buy_streak")),
        _f(rec.get("upside_pct")),
        _f(rec.get("close")),
        _f(rec.get("change_pct")),
        (str(rec["origin"])[:16] if rec.get("origin") else None),
        json.dumps(rec.get("params") or {}, ensure_ascii=False, sort_keys=True),
    )


# ── I/O ───────────────────────────────────────────────────────────────────

def read_jsonl(path: Path) -> list[dict]:
    """망가진 줄은 건너뛴다 — 한 줄 때문에 적재 전체가 멈추면 안 된다."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _executemany(conn, table: str, cols: list[str], update: list[str],
                 rows: list[tuple]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(upsert_sql(table, cols, update), rows)
    conn.commit()
    return len(rows)


def ingest_articles(conn, root: Path, market: str,
                    symbols_of: Callable[[str], Iterable[str]] | None = None,
                    since: date | None = None) -> dict:
    """`data/news/{market}/*.jsonl` 을 적재. 종목 태깅은 주입받는다.

    `symbols_of` 는 **이 시장 전용** 제목→종목코드 추출기다(KR 과 US 는 사전도
    매칭 규칙도 다르다). 추출기를 인자로 받는 이유: 이 모듈이 분석 평면을 직접
    알 필요가 없고, 그래야 DB 없이도 순수 테스트가 된다.

    `since`: 그 날짜 **이상**의 파일만 적재(None = 전체 = 백필 경로).
    **왜 필요한가** — 원래 매 실행이 전 이력을 다시 적재했다. upsert 라 결과는
    옳지만 비용이 이력에 비례해 늘어 2026-08-15 부터 systemd 예산(45분)을 넘겼고,
    **2주간 매 회차가 강제 종료**됐다(DB 는 8/24 에서 멈췄는데 감시는 "기록 없음"만
    반복했다 — 조용한 실패). 파일이 진실 원본이고 upsert 는 멱등이라 최근 며칠만
    다시 훑으면 충분하다.
    """
    day_dir = root / "data" / "news" / market
    total = tagged = 0
    for path in sorted(day_dir.glob("*.jsonl")):
        try:
            collect_date = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if since is not None and collect_date < since:
            continue
        recs = read_jsonl(path)
        rows = [r for r in (article_row(x, market, collect_date) for x in recs) if r]
        total += _executemany(conn, "article", ARTICLE_COLS, ARTICLE_UPDATE, rows)
        if symbols_of:
            tagged += _tag_symbols(conn, market, recs, symbols_of)
    return {"articles": total, "tagged": tagged}


def _tag_symbols(conn, market: str, recs: list[dict],
                 symbols_of: Callable[[str], Iterable[str]]) -> int:
    """기사↔종목 연결. 기사 id 는 링크 해시로 되찾는다."""
    pairs: list[tuple] = []
    for rec in recs:
        key = rec.get("key") or rec.get("link")
        title = (rec.get("title") or "").strip()
        if not key or not title:
            continue
        syms = {str(s)[:16] for s in symbols_of(title) if s}
        if not syms:
            continue
        pairs.append((link_sha(str(key)), sorted(syms)))
    if not pairs:
        return 0
    with conn.cursor() as cur:
        shas = [p[0] for p in pairs]
        marks = ", ".join(["%s"] * len(shas))
        cur.execute(
            f"SELECT link_sha, id FROM article WHERE market=%s AND link_sha IN ({marks})",
            (market, *shas),
        )
        id_of = {row[0]: row[1] for row in cur.fetchall()}
        rows = [(id_of[sha], sym) for sha, syms in pairs if sha in id_of for sym in syms]
        if rows:
            cur.executemany(
                "INSERT IGNORE INTO article_symbol (article_id, symbol) VALUES (%s, %s)",
                rows,
            )
    conn.commit()
    return len(rows)


def ingest_trades(conn, root: Path) -> dict:
    recs = read_jsonl(root / "data" / "state" / "trades.jsonl")
    rows = [r for r in (trade_row(x) for x in recs) if r]
    # 체결은 불변이다 — 충돌하면 아무것도 갱신하지 않는다(같은 내용이므로).
    return {"trades": _executemany(conn, "trade", TRADE_COLS, [], rows)}


def ingest_selections(conn, root: Path) -> dict:
    total = 0
    for path in sorted((root / "data" / "ledger").glob("selections*.jsonl")):
        rows = [r for r in (selection_row(x) for x in read_jsonl(path)) if r]
        total += _executemany(conn, "selection", SELECTION_COLS, SELECTION_UPDATE, rows)
    return {"selections": total}


def ingest_judgments(conn, root: Path) -> dict:
    """`data/ledger/judgments.jsonl` → judgment 테이블 (Phase 7.3).

    판단은 **불변**이라 충돌 시 아무것도 갱신하지 않는다(`INSERT IGNORE`) — 같은
    생산자가 같은 입력을 같은 날 두 번 봐도 판단은 하나다.
    """
    from quant.control.judgment import JUDGMENT_COLS, JUDGMENT_UPDATE

    recs = read_jsonl(root / "data" / "ledger" / "judgments.jsonl")
    rows = [
        tuple(r.get(c) for c in JUDGMENT_COLS)
        for r in recs
        if r.get("producer") and r.get("symbol") and r.get("session_date")
    ]
    return {"judgments": _executemany(conn, "judgment", JUDGMENT_COLS,
                                     JUDGMENT_UPDATE, rows)}


FORWARD_COLS = ["market", "symbol", "session_date", "horizon_days", "return_bps", "asof_date"]
# 같은 (종목, 날짜, 지평)을 다시 계산하면 최신 값으로 덮는다 — 기준가·시세가 정정될
# 수 있고, 그때 옛 값을 남겨두면 리더보드가 두 값을 동시에 본다.
FORWARD_UPDATE = ["return_bps", "asof_date"]


def forward_rows_from_selections(recs: list[dict]) -> list[tuple]:
    """선정 원장의 `outcome_dN_bps` 를 지평별 행으로 편다 (Phase 7.2).

    **지평별로 행을 나누는 이유**: D+1 은 채웠는데 D+20 은 아직인 상태가 정상이다.
    한 행에 열 3개로 두면 "아직 안 채움"과 "0"이 섞이기 쉽다.
    """
    from quant.control.judgment import HOLD_HORIZONS

    out: list[tuple] = []
    for r in recs:
        market = str(r.get("market") or "").strip().upper()
        symbol = str(r.get("symbol") or "").strip()
        day = r.get("date")
        if market not in _MARKETS or not symbol or not day:
            continue
        for h in HOLD_HORIZONS:
            bps = r.get(f"outcome_d{h}_bps")
            asof = r.get(f"outcome_d{h}_asof")
            if bps is None or not asof:
                continue   # 아직 안 채워진 지평은 행을 만들지 않는다
            out.append((market, symbol, day, h, float(bps), asof))
    return out


def ingest_forward_returns(conn, root: Path) -> dict:
    recs = read_jsonl(root / "data" / "ledger" / "selections.jsonl")
    rows = forward_rows_from_selections(recs)
    return {"forward_returns": _executemany(conn, "forward_return", FORWARD_COLS,
                                            FORWARD_UPDATE, rows)}


def ingest_all(conn, root: Path,
               tagger_for: Callable[[str], Callable[[str], Iterable[str]] | None] | None = None,
               since: date | None = None) -> dict:
    """전 시장·전 아티팩트 적재.

    `tagger_for(market)` 가 그 시장의 추출기를 돌려준다(없으면 None → 태깅 생략).
    시장마다 사전이 달라 하나로 못 쓴다 — KR 은 상장사 한글명, US 는 NASDAQ
    SymDir 티커다.

    `since` 는 기사 적재 창만 좁힌다(사유는 `ingest_articles`). 체결·선정·판단·
    전방수익률 원장은 파일 하나짜리라 훑는 비용이 작고, 창을 두면 지연 도착분
    (outcomes 유예 2거래일 등)을 놓칠 수 있어 전체를 유지한다.
    """
    stats: dict = {}
    for market in _MARKETS:
        tagger = None
        if tagger_for is not None:
            try:
                tagger = tagger_for(market)
            except Exception as e:  # 사전 로딩 실패가 적재를 막지 않는다
                log.warning("%s 종목 태깅 생략: %s: %s", market, type(e).__name__, e)
        for k, v in ingest_articles(conn, root, market, tagger, since=since).items():
            stats[k] = stats.get(k, 0) + v
    stats.update(ingest_trades(conn, root))
    stats.update(ingest_selections(conn, root))
    stats.update(ingest_judgments(conn, root))
    stats.update(ingest_forward_returns(conn, root))
    return stats
