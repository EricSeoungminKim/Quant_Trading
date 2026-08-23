"""`render_close`/`write_close_html`/`write_close_machine` — 마감 포지션
리포트(서브프로젝트 R) 템플릿 배선. 3섹션(장중 신규 호재/당일 수급/마감
포지션 후보) 렌더 + 데이터 없을 때의 결측 표시를 확인한다.
"""
from __future__ import annotations

from datetime import date, datetime

from quant.core.report_clock import KST
from quant.collect.contracts import SCHEMA_VERSION, Snapshot
from quant.analyze.render import render_close, write_close_html, write_close_machine

_AT = datetime(2026, 8, 17, 13, 40, tzinfo=KST)


def _snap(market: str = "KR") -> Snapshot:
    return Snapshot(SCHEMA_VERSION, market, date(2026, 8, 17), _AT, {})


def _news_view() -> list[dict]:
    return [
        {
            "title": "삼성전자 대규모 수주 계약 체결", "link": "https://a/1",
            "outlet": "매체A", "dup_count": 2, "published_hhmm": "12:30",
            "bullish_types": ["수주/공급계약"], "bullish_tier": 2, "bearish": False,
        },
        {
            "title": "어닝쇼크에 목표가 하향", "link": "https://a/2",
            "outlet": "매체B", "dup_count": 1, "published_hhmm": "13:10",
            "bullish_types": [], "bullish_tier": 0, "bearish": True,
        },
    ]


def _flow_view() -> dict:
    return {"foreign_net": 1200, "institution_net": -300, "individual_net": -900}


def _ranking_view() -> dict:
    return {
        "거래대금": [{"rank": 1, "symbol": "005930", "name": "삼성전자", "change_pct": 1.2}],
        "상승률": [{"rank": 1, "symbol": "000660", "name": "SK하이닉스", "change_pct": 8.5}],
    }


def _intraday_view() -> list[dict]:
    return [{
        "symbol": "005930", "name": "삼성전자", "score100": 72,
        "factors": [
            {"name": "호재 뉴스", "pts": 15, "max": 40, "evidence": "호재: 수주 (+15)"},
            {"name": "외국인 추세", "pts": 30, "max": 30, "evidence": "foreign_score_v2 20/20 (+30)"},
        ],
        "foreign_label": "매수 시그널(재유입)",
        "grade": "단타 진입",
        "grade_reasons": ["종목 호재 뉴스"],
    }]


def test_render_close_includes_market_and_session_date():
    html = render_close(_snap(), _news_view(), _flow_view(), _ranking_view(), _intraday_view())

    assert "한국장 마감 포지션 리포트" in html
    assert "2026-08-17" in html


def test_render_close_news_section_shows_bullish_marker_and_bearish_veto():
    html = render_close(_snap(), _news_view(), {}, {}, [])

    assert "삼성전자 대규모 수주 계약 체결" in html
    assert "수주/공급계약" in html
    assert "악재 거부권" in html


def test_render_close_news_section_empty_shows_placeholder():
    html = render_close(_snap(), [], {}, {}, [])

    assert "아침 리포트 이후 신규 뉴스 없음" in html


def test_render_close_flow_section_shows_provisional_label_and_values():
    html = render_close(_snap(), [], _flow_view(), {}, [])

    assert "잠정" in html
    # eok() 포맷터가 값을 억원 단위 문자열로 바꾼다 — 정확한 포맷보다
    # "결측으로 빠지지 않았다"만 확인한다(포맷 자체는 units.py 테스트 소관).
    assert "결측" not in html.split("당일 수급")[1].split("</section>")[0]


def test_render_close_flow_section_shows_missing_when_no_data():
    html = render_close(_snap(), [], {}, {}, [])

    section = html.split("당일 수급")[1].split("</section>")[0]
    assert "시장 잠정 수급 결측" in section


def test_render_close_ranking_boards_present():
    html = render_close(_snap(), [], {}, _ranking_view(), [])

    assert "거래대금" in html
    assert "상승률" in html
    assert "삼성전자" in html
    assert "SK하이닉스" in html


def test_render_close_intraday_section_shows_top_candidates():
    html = render_close(_snap(), [], {}, {}, _intraday_view())

    assert "마감 포지션 후보" in html
    assert "72" in html
    assert "매수 시그널(재유입)" in html


def test_render_close_intraday_section_empty_shows_placeholder():
    html = render_close(_snap(), [], {}, {}, [])

    assert "마감 포지션 후보 없음" in html


def test_render_close_intraday_section_shows_grade_badge_and_reason_chip():
    """report.html.j2 의 "당일 단타 후보"와 동일한 3단계 등급 배지
    (`data-sg`, scalp_grade.py) — 마감판도 같은 마크업을 쓴다."""
    html = render_close(_snap(), [], {}, {}, _intraday_view())

    assert 'data-sg="단타 진입"' in html
    assert "종목 호재 뉴스" in html


def test_render_close_footer_disclaimer_present():
    html = render_close(_snap(), [], {}, {}, [])

    assert "14:00~15:30 마감 포지션 참고용" in html
    assert "판단은 본인 몫" in html


def test_render_close_does_not_include_heavy_sections_from_main_template():
    """무거운 섹션(Exec Summary/업종 히트맵/유튜브·블로그 브리핑)은 이 템플릿
    자체에 없다 — 마크업 문자열 자체가 등장하지 않아야 한다."""
    html = render_close(_snap(), _news_view(), _flow_view(), _ranking_view(), _intraday_view())

    assert "Executive Summary" not in html
    assert "업종상위" not in html


def test_write_close_html_and_machine_write_distinct_files(tmp_path):
    snap = _snap()

    hp = write_close_html(snap, tmp_path, _news_view(), _flow_view(), _ranking_view(), _intraday_view())
    jp = write_close_machine({"schema": 1, "session": "close"}, snap, tmp_path)

    assert hp.name == "KR_close_report.html"
    assert jp.name == "KR_close_engine.json"
    assert hp.parent == jp.parent == tmp_path / "2026" / "08" / "17"
    assert hp.exists() and jp.exists()


def test_write_close_html_defaults_are_safe_with_no_data(tmp_path):
    snap = _snap()

    hp = write_close_html(snap, tmp_path)

    assert hp.exists()
    html = hp.read_text(encoding="utf-8")
    assert "아침 리포트 이후 신규 뉴스 없음" in html
    assert "마감 포지션 후보 없음" in html


# ── 텔레그램 인사이트 (서브프로젝트 S part 2) ──────────────────────────────
# 오후엔 장중 시황방(tazastock/mootda) 가치가 특히 크다는 사용자 지시로
# 마감판에도 싣는다 — 단, narrator 요약은 없다(완전 무LLM 계약).


def _telegram_view():
    return [
        {
            "handle": "pikachu_aje", "분류": "전력 섹터", "tier": "sector",
            "msgs": [
                {"text": "전력 테마 강세", "link": "https://t.me/pikachu_aje/10", "published_hhmm": "09:10"},
            ],
            "error": None,
        },
        {
            "handle": "tazastock", "분류": "장중 시황·국내 시황", "tier": "news",
            "msgs": [], "error": "no preview (프리뷰 비활성)",
        },
    ]


def test_render_close_telegram_section_renders_channel_card():
    html = render_close(_snap(), [], {}, {}, [], _telegram_view())
    zone = html[html.index("텔레그램 인사이트"):]
    zone = zone[:zone.index("</section>")]
    assert "pikachu_aje" in zone
    assert "전력 섹터" in zone
    assert 'href="https://t.me/pikachu_aje/10"' in zone


def test_render_close_telegram_section_honestly_labels_unavailable_preview():
    html = render_close(_snap(), [], {}, {}, [], _telegram_view())
    zone = html[html.index("텔레그램 인사이트"):]
    zone = zone[:zone.index("</section>")]
    assert "프리뷰 미제공" in zone


def test_render_close_telegram_section_omitted_when_empty():
    html = render_close(_snap(), [], {}, {}, [], [])
    assert "텔레그램 인사이트" not in html


def test_render_close_telegram_section_omitted_when_none():
    html = render_close(_snap(), [], {}, {}, [])
    assert "텔레그램 인사이트" not in html


def test_write_close_html_accepts_telegram_view(tmp_path):
    snap = _snap()
    hp = write_close_html(snap, tmp_path, telegram_view_kr=_telegram_view())
    html = hp.read_text(encoding="utf-8")
    assert "pikachu_aje" in html


def _telegram_view_with_photo():
    return [
        {
            "handle": "pikachu_aje", "분류": "전력 섹터", "tier": "sector",
            "msgs": [
                {"text": "전력 설비 사진", "link": "https://t.me/pikachu_aje/11",
                 "published_hhmm": "09:10", "image_count": 1,
                 "first_image_url": "https://cdn5.telesco.pe/a.jpg"},
            ],
            "error": None,
        },
    ]


def test_render_close_telegram_section_shows_photo_chip_deterministically():
    """마감판도 사진 개수 칩은 싣는다 — 결정론적(LLM 없음)이라 완전 무LLM
    계약을 깨지 않는다."""
    html = render_close(_snap(), [], {}, {}, [], _telegram_view_with_photo())
    zone = html[html.index("텔레그램 인사이트"):]
    zone = zone[:zone.index("</section>")]
    assert "🖼 사진 1" in zone


def test_render_close_telegram_section_never_shows_ai_interpretation():
    """마감판은 완전 무LLM 계약이다 — render_close 에는 telegram_image_desc
    파라미터 자체가 없다(AI 해석을 실을 수 있는 경로가 아예 없다)."""
    html = render_close(_snap(), [], {}, {}, [], _telegram_view_with_photo())
    assert '<span class="chip on">AI 해석</span>' not in html


def test_render_close_telegram_section_does_not_hotlink_cdn_image_url():
    html = render_close(_snap(), [], {}, {}, [], _telegram_view_with_photo())
    assert "cdn5.telesco.pe" not in html
