"""2026-09-08~11 발행 중단 회귀: 표시용 급락 경고는 NEWS 거부권이 아니다."""
from datetime import date, datetime

import pytest

from quant.analyze.render import machine_payload
from quant.collect.contracts import SCHEMA_VERSION, Snapshot
from quant.core.report_clock import KST
from quant.report.lint import ERROR, lint_report


def _payload(market, symbol, name, change, titles):
    snap = Snapshot(SCHEMA_VERSION, market, date(2026, 9, 11),
                    datetime(2026, 9, 11, 8, tzinfo=KST), {})
    cont = {symbol: {"name": name, "today_articles": 3, "streak_days": 2,
                     "days": 2, "is_new": False, "ranking_bullish": True,
                     "titles": [{"title": title} for title in titles]}}
    return machine_payload(snap, cont, {}, [], sym_quotes={symbol: {"change_pct": change}})


@pytest.mark.parametrize("market,symbol,name,change", [
    ("KR", "267250", "HD현대", -3.7),
    ("KR", "032790", "엠젠솔루션", -7.5),
    ("KR", "200670", "휴메딕스", -3.2),
    ("US", "AA", "Alcoa", -4.8),
    ("US", "LAKE", "Lakeland Industries", -12.9),
])
def test_price_warning_preserves_news_candidate_without_false_lint_error(market, symbol, name, change):
    # 운영 로그의 심볼·등락률. 언급/제목은 거부권 차이를 고정하는 합성 테스트 입력이다.
    payload = _payload(market, symbol, name, change, [f"{name} 신규 사업 발표"])
    entry = payload["symbols"][0]
    assert entry["bearish_markers"] == [f"직전 세션 급락 {change:+.1f}%"]
    assert entry["news_bearish_markers"] == []
    assert "NEWS" in payload["auto_watch"] and "RANK" not in payload["auto_watch"]
    assert not [f for f in lint_report(payload) if f.severity == ERROR and f.section == "candidates"]


def test_real_headline_marker_still_blocks_news_and_lint_catches_forced_contradiction():
    payload = _payload("KR", "267250", "HD현대", -3.7, ["HD현대 목표가 하향"])
    assert payload["symbols"][0]["news_bearish_markers"] == ["목표가 하향"]
    assert "267250:NEWS" not in payload["auto_watch"]
    payload["auto_watch"] = "AUTO_WATCH: 267250:NEWS"
    assert any(f.severity == ERROR and "매수 후보/매수 유의 모순" in f.message
               for f in lint_report(payload))


def test_legacy_payload_with_only_merged_markers_keeps_conservative_gate():
    payload = _payload("KR", "267250", "HD현대", -3.7, ["HD현대 신규 사업 발표"])
    del payload["symbols"][0]["news_bearish_markers"]
    assert any(f.severity == ERROR and f.section == "candidates" for f in lint_report(payload))
