"""중기 관심 종목 섹션(서브프로젝트 W part 3) — `render`/`render_close` 배선.

Jinja 블록 자체가 문법 오류 없이 실제로 렌더되는지(빈 리스트로는 `{% if %}`가
분기를 건너뛰어 검증되지 않는다) + 등급 배지·핵심 필드가 HTML에 나타나는지만
본다. 후보 선정·등급 산정 로직은 `tests/test_midterm_watch.py`의 몫이다.
"""
from __future__ import annotations

from datetime import date, datetime

from quant.core.report_clock import KST
from quant.collect.contracts import SCHEMA_VERSION, Snapshot
from quant.analyze.render import render, render_close

_AT = datetime(2026, 8, 17, 8, 0, tzinfo=KST)


def _snap(market: str = "KR") -> Snapshot:
    return Snapshot(SCHEMA_VERSION, market, date(2026, 8, 17), _AT, {})


def _midterm_view() -> list[dict]:
    return [{
        "symbol": "005930", "name": "삼성전자", "mentions": 3, "grade": 5,
        "grade_label": "적극 매수", "reasons": ["호재 마커 1건", "외국인+기관 쌍끌이(최근일)"],
        "telegram_snippets": ["삼성전자 강세 지속", "외국인 매수세 유입"],
        "prose": "반도체 업황 개선으로 긍정적 흐름이 이어질 전망이다.",
    }]


def _us_news_kr_view() -> list[dict]:
    return [{
        "sector": "Information Technology", "hits": 2,
        "sample_headlines": ["NVIDIA STOCK SURGES ON AI CHIP DEMAND"],
        "stocks": [{
            "symbol": "005930", "name": "삼성전자", "why": "반도체·AI 밸류체인 대장주",
            "grade": 3, "grade_label": "관심", "reasons": ["최근 순매수 전환(유입 시작)"],
        }],
    }]


def _usnews_headlines() -> list[dict]:
    return [{"text": "FED SIGNALS RATE HIKE", "published_hhmm": "13:05"}]


# ── report.html.j2 (render) ──────────────────────────────────────────────

def test_render_midterm_section_shows_grade_badge_and_prose():
    html = render(_snap(), midterm_view=_midterm_view())

    assert "중기 관심 종목" in html
    assert "삼성전자" in html
    assert 'data-g="5"' in html
    assert "적극 매수" in html
    assert "반도체 업황 개선으로 긍정적 흐름이 이어질 전망이다." in html
    assert "언급 3건" in html


def test_render_midterm_section_omitted_when_empty():
    html = render(_snap())
    assert "중기 관심 종목" not in html


def test_render_us_news_kr_section_shows_sector_and_beneficiary():
    html = render(_snap(), us_news_kr_view=_us_news_kr_view())

    assert "미국발 섹터 수혜주" in html
    assert "Information Technology" in html
    assert "NVIDIA STOCK SURGES ON AI CHIP DEMAND" in html
    assert 'data-g="3"' in html


def test_render_usnews_headlines_section_shows_text_and_time():
    html = render(_snap("US"), usnews_headlines=_usnews_headlines())

    assert "실시간 헤드라인" in html
    assert "FED SIGNALS RATE HIKE" in html
    assert "13:05" in html


# ── close_report.html.j2 (render_close) ──────────────────────────────────

def test_render_close_midterm_section_shows_grade_badge():
    html = render_close(_snap(), midterm_view=_midterm_view())

    assert "중기 관심 종목" in html
    assert 'data-g="5"' in html
    assert "producer=midterm_watch_v1_close" in html


def test_render_close_us_news_kr_section_shows_sector():
    html = render_close(_snap(), us_news_kr_view=_us_news_kr_view())
    assert "미국발 섹터 수혜주" in html
    assert "Information Technology" in html


def test_render_close_usnews_headlines_section_shows_text():
    html = render_close(_snap("US"), usnews_headlines=_usnews_headlines())
    assert "FED SIGNALS RATE HIKE" in html


# ── 🌙 어젯밤 미국장 → 오늘 한국장 브리지 카드 (2026-08-21) ────────────────

def _bridge_view() -> dict:
    return {
        "us_sectors": [
            {"ticker": "XLV", "name": "헬스케어", "change_pct": 3.5},
            {"ticker": "XLF", "name": "금융", "change_pct": -1.1},
        ],
        "up_count": 1, "down_count": 1, "tone": "혼조",
        "focus": [{
            "us_name": "헬스케어", "us_ticker": "XLV", "us_change_pct": 3.5,
            "kr_sectors": ["제약", "생물공학"],
            "stocks": [{"code": "207940", "name": "삼성바이오로직스",
                        "change_pct": 4.2, "kr_sector": "생물공학"}],
        }],
    }


def test_render_us_kr_bridge_card():
    html = render(_snap("KR"), {}, {}, {}, "", us_kr_bridge=_bridge_view())
    assert "어젯밤 미국장" in html
    assert "삼성바이오로직스" in html
    assert "제약 · 생물공학" in html
    assert "XLV" in html


def test_render_without_bridge_omits_card():
    html = render(_snap("KR"), {}, {}, {}, "")
    assert "어젯밤 미국장" not in html
