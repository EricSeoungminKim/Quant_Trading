from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from quant.collect.sources.kr_flow import (
    million_won_to_trillion,
    parse_freesis_main,
)

FIXTURE = Path(__file__).parent / "fixtures" / "freesis_main_20260812.html"


@pytest.fixture
def parsed():
    return parse_freesis_main(FIXTURE.read_text(encoding="utf-8"), today=date(2026, 8, 12))


def test_million_won_to_trillion():
    assert million_won_to_trillion(1_000_000) == 1.0
    assert million_won_to_trillion(97_928_935) == pytest.approx(97.928935)


def test_parses_known_funding_items(parsed):
    assert set(parsed) == {"투자자예탁금", "신용융자잔고", "CMA잔고"}


def test_투자자예탁금_matches_known_values(parsed):
    r = parsed["투자자예탁금"]
    assert r["value"] == pytest.approx(97.93, abs=0.01)
    assert r["unit"] == "조원"
    assert r["change"] == pytest.approx(-2.79, abs=0.01)
    assert r["change_pct"] == pytest.approx(-2.77)
    assert r["as_of"] == "2026-08-11"


def test_신용융자잔고_positive_change_has_no_explicit_sign_in_source(parsed):
    # 소스 HTML의 num2a(증가)는 텍스트에 부호가 없다 — 파서가 양수로 처리하는지 확인.
    r = parsed["신용융자잔고"]
    assert r["change"] == pytest.approx(0.01, abs=0.01)
    assert r["change_pct"] == pytest.approx(0.05)


def test_cma잔고_matches_known_values(parsed):
    r = parsed["CMA잔고"]
    assert r["value"] == pytest.approx(104.47, abs=0.01)
    assert r["change"] == pytest.approx(-0.04, abs=0.01)
    assert r["change_pct"] == pytest.approx(-0.04)


def test_non_million_won_widgets_are_excluded(parsed):
    # KOSPI지수(P), 국고채/회사채(%), 펀드 순자산(억원)은 백만원 단위가 아니므로 제외된다.
    assert "KOSPI지수" not in parsed
    assert "국고채(3년)" not in parsed


def test_date_year_rollover_when_source_date_is_in_future():
    # 오늘이 새해 초인데 위젯이 아직 작년 12월 데이터를 보여주는 경우 — 작년으로 보정.
    html = (
        '<dt class="chart-name"><a href="#">투자자예탁금</a></dt>'
        '<dd class="etc"><span class="dan">백만원</span> | <span class="date">12/31</span></dd>'
        '<dd class="chart-num"><span class="num1">1,000,000</span>'
        '<span class="num2a">1,000</span><span class="num3">0.1%</span></dd>'
    )
    parsed = parse_freesis_main(html, today=date(2026, 1, 2))
    assert parsed["투자자예탁금"]["as_of"] == "2025-12-31"


def test_parse_freesis_main_empty_html_returns_empty_dict():
    assert parse_freesis_main("<html></html>", today=date(2026, 8, 12)) == {}


def test_fetch_kr_flow_one_source_failure_does_not_kill_the_other(parsed):
    """FREESIS 파싱이 실패해도(funding={}) vkospi 가 있으면 예외가 나지 않는다.

    현재 vkospi 는 항상 None 이므로, funding 파싱이 완전히 비면(예: FREESIS가 죽으면)
    fetch_kr_flow 전체가 raise 해야 한다 — 이 계약을 mock 으로 검증한다.
    """
    from quant.collect.sources.kr_flow import fetch_kr_flow

    with patch("quant.collect.sources.kr_flow.client") as mock_client:
        mock_client.side_effect = RuntimeError("network down")
        with pytest.raises(ValueError, match="한국 수급 지표"):
            fetch_kr_flow()


@pytest.mark.live
def test_live_fetch_kr_flow_fills_at_least_one_item():
    from quant.collect.sources.kr_flow import fetch_kr_flow

    d = fetch_kr_flow()
    assert d["funding"] or d["vkospi"] is not None
