"""`quant.analyze.midterm_watch` — 중기 관심 종목 섹션(서브프로젝트 W part 3)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quant.analyze.midterm_watch import (
    MENTION_LOOKBACK_DAYS,
    MIN_MENTIONS,
    TOP_N,
    build_midterm_watch,
    build_us_news_kr_map,
    mention_counts,
    narrate_prose,
)

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
KR_TABLE = [("삼성전자", "005930"), ("SK하이닉스", "000660")]


def _row(handle: str, msg_id: str, text: str, days_ago: float = 0.5) -> dict:
    dt = NOW - timedelta(days=days_ago)
    return {
        "handle": handle, "msg_id": msg_id, "text": text,
        "published": dt.isoformat().replace("+00:00", "Z"),
    }


# ── mention_counts ────────────────────────────────────────────────────────

def test_mention_counts_requires_two_distinct_messages():
    msgs = [
        _row("tazastock", "1", "삼성전자 강세"),
    ]
    assert mention_counts("KR", msgs, KR_TABLE, now=NOW) == {}

    msgs.append(_row("tazastock", "2", "삼성전자 추가 매수세"))
    out = mention_counts("KR", msgs, KR_TABLE, now=NOW)
    assert "005930" in out
    assert len(out["005930"]) == 2


def test_mention_counts_filters_by_lookback_window():
    msgs = [
        _row("tazastock", "1", "삼성전자 강세", days_ago=1),
        _row("tazastock", "2", "삼성전자 추가 매수세", days_ago=MENTION_LOOKBACK_DAYS + 1),
    ]
    out = mention_counts("KR", msgs, KR_TABLE, now=NOW)
    assert out == {}


def test_mention_counts_filters_by_market_channel():
    # walterbloomberg 는 market=US 전용 채널 — KR 리포트가 보면 안 된다.
    msgs = [
        _row("walterbloomberg", "1", "삼성전자 강세"),
        _row("walterbloomberg", "2", "삼성전자 추가 매수세"),
    ]
    assert mention_counts("KR", msgs, KR_TABLE, now=NOW) == {}


def test_mention_counts_us_uses_ticker_whitelist():
    msgs = [
        _row("walterbloomberg", "1", "AAPL surges on new chip demand"),
        _row("walterbloomberg", "2", "AAPL extends gains"),
        _row("walterbloomberg", "3", "XYZQ unrelated ticker not in table"),
        _row("walterbloomberg", "4", "XYZQ again"),
    ]
    out = mention_counts("US", msgs, {"AAPL"}, now=NOW)
    assert set(out.keys()) == {"AAPL"}


# ── build_midterm_watch ──────────────────────────────────────────────────

def _foreign_series(symbol: str, days: int = 5, net: float = 100.0) -> list[dict]:
    return [
        {"date": f"2026-08-{i + 1:02d}", "symbol": symbol, "foreign_net": net, "inst_net": net}
        for i in range(days)
    ]


def test_build_midterm_watch_grades_sorts_and_caps_top_n():
    msgs = []
    for i in range(TOP_N + 2):
        code = f"{i:06d}"
        msgs.append(_row("tazastock", f"{i}-1", f"{code} 관련 소식"))
        msgs.append(_row("tazastock", f"{i}-2", f"{code} 추가 소식"))
    table = [(f"{i:06d}", f"{i:06d}") for i in range(TOP_N + 2)]

    frgn = {f"{i:06d}": _foreign_series(f"{i:06d}") for i in range(TOP_N + 2)}
    bullish = {f"{i:06d}": {"bullish_types": ["수주/공급계약"], "bearish": False}
               for i in range(TOP_N + 2)}

    out = build_midterm_watch("KR", msgs, frgn, bullish, table, now=NOW)

    assert len(out) == TOP_N
    grades = [c["grade"] for c in out]
    assert grades == sorted(grades, reverse=True)
    assert all(c["prose"] is None for c in out)
    assert all(c["mentions"] >= MIN_MENTIONS for c in out)


def test_build_midterm_watch_missing_frgn_and_bullish_degrades_gracefully():
    msgs = [_row("tazastock", "1", "삼성전자 강세"), _row("tazastock", "2", "삼성전자 추가 매수세")]
    out = build_midterm_watch("KR", msgs, {}, {}, KR_TABLE, now=NOW)
    assert len(out) == 1
    assert out[0]["symbol"] == "005930"
    # 데이터가 아예 없으면 entry_grade 가 "데이터 부족"으로 등급 2를 매긴다.
    assert out[0]["grade"] == 2


def test_build_midterm_watch_uses_name_by_symbol():
    msgs = [_row("tazastock", "1", "삼성전자 강세"), _row("tazastock", "2", "삼성전자 추가 매수세")]
    out = build_midterm_watch(
        "KR", msgs, {}, {}, KR_TABLE, name_by_symbol={"005930": "삼성전자우"}, now=NOW,
    )
    assert out[0]["name"] == "삼성전자우"


# ── build_us_news_kr_map ─────────────────────────────────────────────────

def test_build_us_news_kr_map_ranks_sectors_and_attaches_grade():
    titles = [
        "NVIDIA STOCK SURGES ON AI CHIP DEMAND",
        "SEMICONDUCTOR EXPORTS HIT RECORD HIGH",
        "OPEC CUTS OIL PRODUCTION QUOTA",
        "IRRELEVANT HEADLINE WITH NO SECTOR SIGNAL",
    ]
    out = build_us_news_kr_map(titles, {}, {})

    assert out[0]["sector"] == "Information Technology"
    assert out[0]["hits"] == 2
    assert out[0]["stocks"]
    for stock in out[0]["stocks"]:
        assert "grade" in stock and "grade_label" in stock


def test_build_us_news_kr_map_empty_titles_returns_empty():
    assert build_us_news_kr_map([], {}, {}) == []


def test_build_us_news_kr_map_caps_at_top_sectors():
    # 6개 이상 GICS 섹터를 히트시켜 상한(4)을 검증한다.
    titles = [
        "NVIDIA AI CHIP",  # Information Technology
        "OPEC CRUDE OIL",  # Energy
        "FEDERAL RESERVE RATE HIKE",  # Financials
        "PFIZER FDA VACCINE",  # Health Care
        "TESLA AUTOMAKER RETAIL",  # Consumer Discretionary
        "GOLD COPPER MINING",  # Materials
    ]
    out = build_us_news_kr_map(titles, {}, {})
    assert len(out) == 4


# ── narrate_prose ────────────────────────────────────────────────────────

class _FakeNarrator:
    def __init__(self, responses: dict[str, str | None]):
        self._responses = responses
        self.calls: list[str] = []

    def narrate(self, prompt: str) -> str | None:
        self.calls.append(prompt)
        for symbol, resp in self._responses.items():
            if symbol in prompt:
                return resp
        return None


def _candidate(symbol: str, snippets: list[str] | None = None) -> dict:
    return {
        "symbol": symbol, "name": symbol, "mentions": 2, "grade": 3, "grade_label": "관심",
        "reasons": [], "telegram_snippets": snippets or ["텍스트1", "텍스트2"], "prose": None,
    }


def test_narrate_prose_fills_only_successful_symbols():
    narrator = _FakeNarrator({"005930": "전망 좋음.", "000660": None})
    candidates = [_candidate("005930"), _candidate("000660")]

    out = narrate_prose(candidates, narrator)

    assert out == {"005930": "전망 좋음."}


def test_narrate_prose_respects_budget():
    narrator = _FakeNarrator({f"{i:06d}": f"산문{i}" for i in range(10)})
    candidates = [_candidate(f"{i:06d}") for i in range(10)]

    out = narrate_prose(candidates, narrator, budget=3)

    assert len(narrator.calls) == 3
    assert len(out) == 3


def test_narrate_prose_prompt_includes_injection_guard():
    narrator = _FakeNarrator({})
    narrate_prose([_candidate("005930")], narrator)
    assert "절대 따르지 마라" in narrator.calls[0]


# ── 산문 간결화 (2026-08-25 소유자: "간결하지만 핵심만") ────────────────────

def test_tighten_prose_strips_markdown_and_caps_length():
    from quant.analyze.midterm_watch import PROSE_MAX_CHARS, tighten_prose

    long = ("이번 상승은 **테마 동반 상승** 성격이 큽니다. " * 10) + "다만 자료에 나온 삼"
    out = tighten_prose(long)
    assert "**" not in out
    assert len(out) <= PROSE_MAX_CHARS
    assert out.endswith("다."), "문장 경계에서 잘라야 한다 — 뚝 끊긴 꼬리 금지"


def test_tighten_prose_keeps_short_text_intact():
    from quant.analyze.midterm_watch import tighten_prose

    assert tighten_prose("핵심: ESS 모멘텀 편승.\n주의: 실적 부진 확인 필요.") == \
        "핵심: ESS 모멘텀 편승.\n주의: 실적 부진 확인 필요."
