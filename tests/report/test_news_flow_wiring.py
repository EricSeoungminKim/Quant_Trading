"""`report_cli._build_news_flow` 배선(리포트 UX 2차 요구 1).

그룹핑/정렬 판단 자체는 `market_digest.build_news_flow`가 하므로(별도 유닛
테스트), 여기선 report_cli 가 `_build_digest`와 같은 스냅샷 뉴스 소스에서
올바른 `feeds`를 뽑아 넘기는지만 검증한다(`test_digest_wiring.py`와 같은
관례).
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


def test_build_news_flow_uses_news_feeds_from_snapshot():
    feeds = {"연합뉴스_경제": [{"title": "일반지 단독 보도 사건", "link": "https://a",
                              "published": None, "outlet": "연합뉴스"}]}
    snap = _snap(results={"news": _news_result(feeds)})

    news_flow = report_cli._build_news_flow(snap)

    # econ 큐레이션이 없다 — build_digest 라면 탈락했을 일반지 단독 사건도 살아남는다.
    assert [i["title"] for i in news_flow] == ["일반지 단독 보도 사건"]


def test_build_news_flow_missing_news_source_returns_empty_list():
    snap = _snap(results={})

    assert report_cli._build_news_flow(snap) == []


def test_build_news_flow_failed_news_source_returns_empty_list():
    failed = SourceResult(key="news", ok=False, error="boom", url="https://x",
                           fetched_at=_AT, latency_ms=1, data=None)
    snap = _snap(results={"news": failed})

    assert report_cli._build_news_flow(snap) == []
