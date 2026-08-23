"""종목 언급 원장 + 연속성 지표.

수집물을 일회성으로 버리지 않는다 — 매일 종목별 뉴스 등장을 append-only 원장에
쌓고, 거기서 "며칠 연속 등장했나"를 센다.

**판정은 LLM이, 누적은 코드가.** 이 모듈은 "호재인가"를 판단하지 않는다. 며칠
연속 나왔고 몇 건이나 나왔는지만 센다. 호재/악재 판정은 Phase 2에서 LLM이 붙되,
가중치를 곱해 conviction을 만드는 계산은 계속 여기 남는다 — 그래야 같은 원장에서
같은 숫자가 재현되고 나중에 채점할 수 있다.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from quant.collect.contracts import Snapshot
from quant.analyze.entities import extract, extract_us

STREAK_WINDOW = 10

# 감쇠 가중 뉴스 축(2026-08-18, SK하이닉스 15점 사건 재점검) — 당일/전일/전전일.
# `continuity()`의 `titles` 필드는 `date == 오늘`만 골라 만들어져, 직전
# 개장일(주말 포함)에 이미 터진 호재가 다음 채점에서 "호재 마커 없음"으로
# 잡혔다(`quant.analyze.bullish_markers.classify_titles_dated`가 이 값을
# 쓴다). 재료의 신선도(당일 기사가 더 강한 신호)와 주말·전일 축적(며칠 전
# 기사도 완전히 버리면 안 된다)을 절충한다.
RECENT_TITLES_DECAY: tuple[float, ...] = (1.0, 0.6, 0.3)


def collect_mentions(
    snap: Snapshot, table: list[tuple[str, str]], *, market: str | None = None
) -> list[dict]:
    """스냅샷의 뉴스 섹션에서 종목 언급을 뽑는다.

    시장별로 추출기가 다르다 — KR은 한글 상장사 사전(`extract`), US는 S&P500
    사전(`extract_us`, 티커 오탐 방어 규칙이 별도로 있다). `market` 을 안 주면
    스냅샷 자체의 시장을 따른다.
    """
    news = snap.results.get("news")
    if news is None or not news.ok or not news.data:
        return []
    resolved_market = market if market is not None else snap.market
    extractor = extract_us if resolved_market == "US" else extract
    rows: list[dict] = []
    for feed, items in news.data.get("feeds", {}).items():
        for item in items:
            for hit in extractor(item["title"], table):
                rows.append({
                    "date": snap.session_date.isoformat(),
                    "symbol": hit["symbol"],
                    "name": hit["name"],
                    "title": item["title"],
                    "link": item["link"],
                    "feed": feed,
                    "outlet": item.get("outlet", ""),
                    # 시장을 반드시 남긴다 — 없으면 US 리포트가 KR 종목을
                    # 집어온다(실측 2026-08-12: US 리포트 상위에 삼성전자·
                    # SK하이닉스가 올라왔다). 원장은 하나지만 집계는 시장별이다.
                    "market": market or snap.market,
                })
    return rows


def load_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def append_ledger(rows: list[dict], path: Path) -> int:
    """(symbol, link) 중복은 건너뛴다 — 같은 기사를 두 번 수집해도 부풀지 않는다."""
    existing = {(r["symbol"], r["link"]) for r in load_ledger(path)}
    fresh = [r for r in rows if (r["symbol"], r["link"]) not in existing]
    if fresh:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for r in fresh:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(fresh)


def _recent_session_dates(ledger: list[dict], today: date, n: int) -> list[date]:
    """원장에 실제로 존재하는(=수집이 실행됐던) 날짜 중 오늘 이하 최근 `n`개
    (내림차순, 오늘 포함).

    원장의 각 행은 기사의 실제 발행일이 아니라 **수집이 돈 날**로 찍힌다
    (`collect_mentions`의 `"date": snap.session_date`) — 수집이 개장일에만
    도는 한, 원장에 실제로 존재하는 날짜 자체가 곧 "최근 개장일" 목록이다.
    주말·휴장으로 수집이 비면 그 날짜는 원장에 아예 없으므로 자동으로
    빠진다 — `quant.analyze.opendays`처럼 별도 공휴일표 없이도 맞는다."""
    seen = {r["date"] for r in ledger}
    dates = sorted(
        (d for s in seen if (d := date.fromisoformat(s)) <= today), reverse=True
    )
    return dates[:n]


def continuity(
    ledger: list[dict],
    today: date,
    lookback: int = STREAK_WINDOW,
    market: str | None = None,
) -> dict[str, dict]:
    """오늘 등장한 종목의 누적 언급·연속 일수·신규 여부·등장 이력.

    `market` 을 주면 그 시장 행만 센다. 원장은 두 시장이 공유하므로 필터가
    없으면 US 리포트에 KR 종목이 섞인다. `market` 필드가 없는 오래된 행은
    KR 로 본다 — US 추출이 붙기 전 기록은 전부 한국 종목이었다.
    """
    if market is not None:
        ledger = [r for r in ledger if (r.get("market") or "KR") == market]
    cutoff = today - timedelta(days=lookback)
    today_iso = today.isoformat()

    session_dates = _recent_session_dates(ledger, today, len(RECENT_TITLES_DECAY))
    weight_by_iso = {
        d.isoformat(): w for d, w in zip(session_dates, RECENT_TITLES_DECAY)
    }

    windowed: dict[str, set[date]] = defaultdict(set)
    ever: dict[str, set[date]] = defaultdict(set)
    counts: dict[str, int] = defaultdict(int)
    for r in ledger:
        d = date.fromisoformat(r["date"])
        ever[r["symbol"]].add(d)
        if cutoff <= d <= today:
            windowed[r["symbol"]].add(d)
            counts[r["symbol"]] += 1

    out: dict[str, dict] = {}
    for symbol, dates in windowed.items():
        if today not in dates:
            continue  # 오늘 안 나온 종목은 오늘 리포트의 관심사가 아니다
        streak, cursor = 0, today
        while cursor in dates:
            streak += 1
            cursor -= timedelta(days=1)
        today_rows = [
            r for r in ledger if r["symbol"] == symbol and r["date"] == today_iso
        ]
        # 최근 3개장일(감쇠 가중) — 호재 마커 판정용. `titles`(아래, 오늘치만)
        # 와 별개 필드다: 카드 상단 "오늘의 기사"는 여전히 오늘치만 보여줘야
        # 하지만(render.bearish_markers), 호재 마커 채점은 하루 전 재료도
        # 가중치를 낮춰서라도 봐야 한다(위 RECENT_TITLES_DECAY 절).
        recent_rows = [
            r for r in ledger if r["symbol"] == symbol and r["date"] in weight_by_iso
        ]
        out[symbol] = {
            "name": today_rows[0]["name"] if today_rows else symbol,
            "days": len(dates),
            "articles": counts[symbol],
            "today_articles": len(today_rows),
            "streak_days": streak,
            "is_new": ever[symbol] == {today},
            "history": [
                (today - timedelta(days=lookback - 1 - i)) in dates
                for i in range(lookback)
            ],
            "titles": [
                {"title": r["title"], "link": r["link"], "feed": r["feed"]}
                for r in today_rows
            ],
            "recent_titles": [
                {
                    "title": r["title"], "link": r["link"], "feed": r["feed"],
                    "weight": weight_by_iso[r["date"]],
                }
                for r in recent_rows
            ],
        }
    return out


def mark_origin(
    cont: dict[str, dict],
    ranked_symbols: set[str],
    bullish_symbols: set[str] | None = None,
) -> dict[str, dict]:
    """뉴스로 잡힌 종목 중 거래 랭킹에도 있는지, 그게 매수세 근거인지 표시한다.

    `origin`: cont 는 전부 뉴스 추출로 채워지므로 기본이 "news" 다. 랭킹에도
    있으면 "both" — 이야기도 돌고 돈도 몰린 곳이라 가장 강한 신호로 본다.
    "ranking" 단독(랭킹에만 있고 뉴스에 전혀 안 잡힌 종목)은 여기 안 나온다
    — cont 자체가 뉴스 원장에서 나오기 때문이다. 그런 종목은 seeded_news 가
    별도로 다룬다.

    이 필드가 3층 채점의 입력이 된다: "뉴스만" vs "랭킹만" vs "둘 다" 중
    어느 근거가 실제로 맞았는지는 값이 몇 주 쌓여야 답이 나온다 — 지금은
    기록만 시작한다.

    `ranking_bullish`: 그 랭킹 편입이 **매수세** 근거인가. `bullish_symbols` 를
    주면 그 집합만 True 다(안 주면 `in_ranking` 과 같다).

    **왜 둘로 나눴나 (2026-08-13).** 랭킹 보드에는 하락률도 있다. `in_ranking` 만
    보면 폭락 중인 종목이 `RANK` 태그를 달고, 그게 엔진 어휘의 `TREND` 로 번역돼
    롱 온리 전략의 진입 우선순위를 차지한다 — 실제로 펄어비스가 하락률 보드에
    오른 채 08:42 자동 편입됐고 그날 손실로 청산됐다.
    그렇다고 `in_ranking` 자체를 좁히면 위 3층 채점 원장이 "랭킹에 있었다"는
    **원사실**을 잃는다. 원장은 사실을 적고, 판정은 별도 필드로 한다.
    """
    bullish = ranked_symbols if bullish_symbols is None else bullish_symbols
    return {
        symbol: {**c, "origin": "both" if symbol in ranked_symbols else "news",
                 "in_ranking": symbol in ranked_symbols,
                 "ranking_bullish": symbol in bullish}
        for symbol, c in cont.items()
    }


def rank(cont: dict[str, dict], limit: int = 12) -> list[tuple[str, dict]]:
    """노출이 많고 연속된 종목이 위로. 아무거나 나열하면 단타 리포트로 의미가 없다."""
    return sorted(
        cont.items(),
        key=lambda kv: (-kv[1]["today_articles"], -kv[1]["streak_days"], kv[0]),
    )[:limit]
