"""`report_cli._build_digest` 배선(서브프로젝트 I).

사건 클러스터링/분류 판단 자체는 `market_digest.build_digest` 가 하므로
(별도 유닛 테스트), 여기선 report_cli 가 스냅샷 뉴스 소스에서 올바른
`feeds` 를 뽑아 넘기는지만 검증한다.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from quant.apps import report_cli
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult

KST = ZoneInfo("Asia/Seoul")
_AT = datetime(2026, 8, 17, 8, 0, tzinfo=KST)


def _snap(results: dict | None = None) -> Snapshot:
    return Snapshot(SCHEMA_VERSION, "KR", date(2026, 8, 17), _AT, results or {})


def _news_result(feeds: dict) -> SourceResult:
    return SourceResult(
        key="news", ok=True, error=None, url="https://news.google.com",
        fetched_at=_AT, latency_ms=1, data={"feeds": feeds},
    )


def test_build_digest_uses_news_feeds_from_snapshot():
    feeds = {"한국경제_경제": [{"title": "삼성전자 실적 발표", "link": "https://a",
                              "published": None, "outlet": "한국경제"}]}
    snap = _snap(results={"news": _news_result(feeds)})

    digest = report_cli._build_digest(snap)

    assert digest["domestic"][0]["title"] == "삼성전자 실적 발표"
    assert digest["us_impact"] == []


def test_build_digest_missing_news_source_returns_empty_lists():
    snap = _snap(results={})

    digest = report_cli._build_digest(snap)

    assert digest == {"domestic": [], "us_impact": []}


def test_build_digest_failed_news_source_returns_empty_lists():
    failed = SourceResult(key="news", ok=False, error="boom", url="https://x",
                           fetched_at=_AT, latency_ms=1, data=None)
    snap = _snap(results={"news": failed})

    digest = report_cli._build_digest(snap)

    assert digest == {"domestic": [], "us_impact": []}


# ── quotes_lookup 계약(2026-09-07) — 레코드가 아니라 종가 float 를 넘긴다 ─────────
def test_close_lookup_returns_close_float_not_record():
    from quant.report.collect.tg_digest_section import _close_lookup

    lookup = _close_lookup({"005930": {"close": 71200, "date": "2026-09-05"}, "bad": "x", "nan": {"close": None}})
    assert lookup("005930") == 71200.0
    assert lookup("bad") is None
    assert lookup("nan") is None
    assert lookup("missing") is None


def test_close_lookup_feeds_verify_price_value_without_type_error():
    from quant.analyze.tg_digest import _verify_price_value
    from quant.report.collect.tg_digest_section import _close_lookup

    lookup = _close_lookup({"005930": {"close": 71200}})
    assert _verify_price_value(71000.0, "005930", lookup) == "✓"
    assert _verify_price_value(71000.0, "000660", lookup) == "미확인"
