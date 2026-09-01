"""섹션 접힘 구조(details/summary) + 요약줄 조립 + 확률 엔진 v2 폴백.

소유자 지시(2026-09-02): 섹션이 많아진 리포트를 details/summary 로 접되,
접힌 채로도 그 섹션의 결론이 한 줄(요약줄)로 읽혀야 한다. 이 파일은 세 가지를
검증한다.

1. 요약줄 조립 함수(render.py, `summarize_*`) — 순수 함수 단위 테스트.
2. details 구조 — 핵심 3개(지수 전망·돈의 흐름·오늘의 후보)는 `open`,
   나머지는 접혀 있다. 정보는 소실되지 않는다(펼쳐지는 내용은 여전히 DOM에
   있다 — 문자열 검색으로 확인).
3. 지수별 전망 확률 v2 필드(up_prob/down_prob/...) 유무에 따른 폴백 — 필드가
   있으면 쌍 확률+게이지를, 없으면 기존 단일 상승확률 문구를 그대로 보여준다.
"""
from __future__ import annotations

from datetime import date, datetime

from quant.analyze.render import (
    render,
    summarize_candidates,
    summarize_carried_candidates,
    summarize_digest,
    summarize_foreign_view,
    summarize_holiday,
    summarize_index_outlook,
    summarize_intraday_view,
    summarize_money_flow,
    summarize_news_flow,
    summarize_sector_view,
    summarize_top_movers,
    summarize_us_kr_bridge,
    summarize_us_wrap,
)
from quant.collect.contracts import SCHEMA_VERSION, Snapshot
from quant.core.report_clock import KST

_AT = datetime(2026, 8, 31, 8, 0, tzinfo=KST)


def _snap(market: str = "KR") -> Snapshot:
    return Snapshot(SCHEMA_VERSION, market, date(2026, 8, 31), _AT, {})


def _cont():
    return {
        "005930": {
            "name": "삼성전자", "days": 3, "articles": 5, "today_articles": 4,
            "streak_days": 3, "is_new": False, "history": [True] * 10, "titles": [],
        }
    }


# ── 1. 요약줄 조립 함수(순수 함수) ──────────────────────────────────────


def test_summarize_index_outlook_uses_up_prob_when_present():
    out = summarize_index_outlook({
        "kospi": {"up_prob": 0.60, "probability": {"prob": None}, "score100": None, "label": None},
        "kosdaq": {"up_prob": 0.53, "probability": {"prob": None}, "score100": None, "label": None},
    })
    assert out == "코스피 상승 60% · 코스닥 상승 53%"


def test_summarize_index_outlook_falls_back_to_probability_prob():
    out = summarize_index_outlook({
        "kospi": {"probability": {"prob": 0.62}, "score100": 83, "label": "강한 상승 신호"},
    })
    assert out == "코스피 상승 62%"


def test_summarize_index_outlook_falls_back_to_score_label_when_no_probability():
    out = summarize_index_outlook({
        "kospi": {"probability": {"prob": None}, "score100": 83, "label": "강한 상승 신호"},
    })
    assert out == "코스피 강한 상승 신호"


def test_summarize_index_outlook_falls_back_to_insufficient_basis():
    out = summarize_index_outlook({
        "kospi": {"probability": {"prob": None}, "score100": None, "label": None},
    })
    assert out == "코스피 판단 근거 부족"


def test_summarize_index_outlook_none_when_absent():
    assert summarize_index_outlook(None) is None
    assert summarize_index_outlook({}) is None


def test_summarize_money_flow_combines_flow_and_cash_labels():
    out = summarize_money_flow({
        "flow": {"label": "긴축 부담 — 채권·주식 동반 이탈"},
        "cash": {"label": "VIX 안정 — 현금이 위험자산에 머문다"},
    })
    assert out == "긴축 부담 — 채권·주식 동반 이탈 · VIX 안정 — 현금이 위험자산에 머문다"


def test_summarize_money_flow_none_when_absent():
    assert summarize_money_flow(None) is None


def test_summarize_candidates_counts_and_names_top():
    ranked = [
        ("005930", {"name": "삼성전자", "today_articles": 4}),
        ("000660", {"name": "SK하이닉스", "today_articles": 2}),
    ]
    assert summarize_candidates(ranked) == "2종목 · 노출 1위 삼성전자(오늘 4건)"


def test_summarize_candidates_empty_says_no_candidates():
    assert summarize_candidates([]) == "오늘 뉴스에서 추출된 후보 없음"


def test_summarize_holiday_prefers_top_theme():
    out = summarize_holiday({"gap_days": 2, "theme_freq": [{"theme": "반도체", "count": 3}]})
    assert out == "휴장 2일 · 상위 테마 반도체(3건)"


def test_summarize_holiday_falls_back_without_themes():
    assert summarize_holiday({"gap_days": 2, "theme_freq": []}) == "휴장 2일 종합"


def test_summarize_digest_counts_domestic_and_us():
    out = summarize_digest({"domestic": [{}, {}], "us_impact": [{}]})
    assert out == "국내 2건 · 미국발 1건"


def test_summarize_digest_none_when_both_empty():
    assert summarize_digest({"domestic": [], "us_impact": []}) is None


def test_summarize_news_flow_counts_events():
    assert summarize_news_flow([{}, {}, {}]) == "전 매체 사건 단위 3건"


def test_summarize_us_wrap_combines_tone_and_counts():
    out = summarize_us_wrap({"tone": "상승 우위", "up_count": 7, "down_count": 4})
    assert out == "상승 우위 · 7↑ 4↓"


def test_summarize_us_kr_bridge_combines_tone_counts_and_focus():
    out = summarize_us_kr_bridge({
        "tone": "상승 우위", "up_count": 8, "down_count": 3, "focus": [{}, {}],
    })
    assert out == "상승 우위 · 8↑ 3↓ · 연동 업종 2개"


def test_summarize_top_movers_shows_top_sector_and_theme():
    out = summarize_top_movers({
        "sectors": [{"name": "반도체", "change_pct": 5.0}],
        "themes": [{"name": "정유", "change_pct": 6.48}],
    })
    assert out == "업종 1위 반도체 +5.0% · 테마 1위 정유 +6.5%"


def test_summarize_sector_view_shows_top_mover():
    out = summarize_sector_view([
        {"name": "반도체와반도체장비", "change_pct": 3.5},
        {"name": "화학", "change_pct": -1.2},
    ])
    assert out == "2개 업종 · 최고 반도체와반도체장비 +3.5%"


def test_summarize_foreign_view_counts_buy_signal_rows():
    out = summarize_foreign_view({
        "sectors": [{
            "name": "반도체", "rows": [
                {"label": "매수 시그널(재유입)"},
                {"label": "이탈 추세(부분 매도/중립 고려)"},
            ],
        }],
    })
    assert out == "1개 업종 · 매수 시그널 1건"


def test_summarize_intraday_view_shows_top_item():
    out = summarize_intraday_view([
        {"name": "삼성전자", "grade": "단타 적극 진입", "score100": 72},
    ])
    assert out == "1종목 · 1위 삼성전자 단타 적극 진입 72점"


def test_summarize_carried_candidates_counts_symbols_and_sectors():
    carried = [{"symbol": "005930", "sector": "반도체"}, {"symbol": "000660", "sector": "반도체"}]
    by_sector = [{"name": "반도체", "tiles": carried}]
    assert summarize_carried_candidates(carried, by_sector) == "2종목 · 1개 업종"


def test_summarize_carried_candidates_none_when_empty():
    assert summarize_carried_candidates([], []) is None


# ── 2. details 구조 — 핵심 3개는 open, 나머지는 닫힘 ────────────────────


def _index_outlook_v1():
    return {
        "kospi": {
            "score": 2, "span": 3, "score100": 83, "label": "강한 상승 신호",
            "positives": ["KOSPI +1.20%"], "negatives": [],
            "probability": {"prob": 0.62, "n": 145, "reason": None, "method": "경험적 빈도"},
            "proxy_symbol": "069500",
        },
    }


def _money_flow_view():
    return {
        "series": {"vix": {"label": "VIX", "value": 14.0, "chg_5d": -0.5, "direction_5d": "↓"}},
        "flow": {"label": "완화 국면", "reasons": []},
        "cash": {"label": "VIX 안정 — 현금이 위험자산에 머문다", "reasons": []},
        "prose": None, "fallback_text": "완화 국면 · VIX 안정 — 현금이 위험자산에 머문다",
    }


def test_core_three_sections_render_as_open_details():
    """핵심 3개(지수 전망·돈의 흐름·오늘의 후보)는 접힘 상태가 아니라 펼쳐서 나온다."""
    html = render(
        _snap(), _cont(),
        index_outlook=_index_outlook_v1(), money_flow=_money_flow_view(),
    )
    assert '<details class="mod" open>\n  <summary>지수별 전망' in html
    assert '<details class="mod" open>\n  <summary>돈의 흐름' in html
    assert '<details class="mod" open>\n  <summary>뉴스 노출 상위 종목' in html


def test_non_core_sections_render_as_closed_details():
    """핵심 3개가 아닌 섹션(예: 오늘의 뉴스 흐름)은 기본 접힘 — open 속성이 없다."""
    news_flow = [{"title": "사건", "link": "https://a", "outlet": "한국경제", "dup_count": 1}]
    html = render(_snap(), _cont(), news_flow=news_flow)
    assert '<details class="mod">\n  <summary>오늘의 뉴스 흐름' in html
    # 콘텐츠는 접혀 있어도 DOM 에는 그대로 있다 — 정보 소실이 아니다.
    assert "사건" in html


def test_collapsed_section_summary_line_shown_without_needing_to_open():
    """접힌 섹션도 <summary> 안에 결론 한 줄(mod-tldr)이 있어 오해가 없다."""
    news_flow = [{"title": "사건1", "link": "https://a", "outlet": "한국경제", "dup_count": 1}]
    html = render(_snap(), _cont(), news_flow=news_flow)
    assert '<span class="mod-tldr">전 매체 사건 단위 1건</span>' in html


def test_money_flow_section_tldr_matches_flow_and_cash_labels():
    html = render(_snap(), _cont(), money_flow=_money_flow_view())
    assert '<span class="mod-tldr">완화 국면 · VIX 안정 — 현금이 위험자산에 머문다</span>' in html


def test_information_not_lost_when_section_collapsed():
    """접힌 섹션이라도 전체 콘텐츠는 렌더된 HTML 문자열 안에 그대로 있다
    (details 콘텐츠는 CSS display:none 으로 숨겨질 뿐 DOM/문자열에선 안 지워진다)."""
    us_wrap = {
        "date": "2026-08-30", "tone": "상승 우위", "up_count": 7, "down_count": 4,
        "indices": [{"symbol": "^GSPC", "label": "S&P500 지수", "change_pct": 0.5}],
    }
    html = render(_snap(), _cont(), us_wrap=us_wrap)
    assert '<details class="mod">\n  <summary>🌆 전일 미국장 마감 요약' in html
    assert "^GSPC" in html  # 접혀 있어도 콘텐츠는 HTML 문자열 안에 그대로 남는다


# ── 3. 확률 엔진 v2 필드 유/무 폴백 ──────────────────────────────────────


def test_index_outlook_v1_probability_unchanged_without_v2_fields():
    """up_prob 가 없으면(구 스키마) 기존 단일 상승확률 문구가 그대로 나온다 —
    병렬 워커의 확률 엔진 v2 가 아직 배선되지 않은 상태와 같다."""
    html = render(_snap(), _cont(), index_outlook=_index_outlook_v1())
    assert "62%" in html
    assert 'class="prob-pair"' not in html
    assert 'class="prob-gauge"' not in html


def test_index_outlook_v2_fields_render_probability_pair_and_gauge():
    """up_prob/down_prob 가 있으면 상승·하락 확률 쌍 + 게이지로 바뀐다.

    factors 스키마는 실제 배선(quant.analyze.index_outlook.shrinkage_probability,
    커밋 b4fce3f)과 동일하게 {"name", "state", "contribution"} 형태로 맞춘다.
    """
    outlook = {
        "kospi": {
            "score": 2, "span": 3, "score100": 83, "label": "강한 상승 신호",
            "positives": [], "negatives": [],
            "probability": {"prob": 0.62, "n": 145, "reason": None, "method": "구 방식"},
            "up_prob": 0.58, "down_prob": 0.42, "n_samples": 200, "shrinkage": 0.15,
            "method": "베이지안 축소 추정", "brier_vs_base": 0.03,
            "factors": [
                {"name": "전일 등락 부호", "state": "하락", "contribution": "+2.3%p"},
                {"name": "5일 추세 부호", "state": "상승", "contribution": "-1.1%p"},
            ],
            "proxy_symbol": "069500",
        },
    }
    html = render(_snap(), _cont(), index_outlook=outlook)
    assert 'class="prob-pair"' in html
    assert 'class="prob-gauge"' in html
    assert "58%" in html and "42%" in html
    assert "베이지안 축소 추정" in html
    assert "표본 200개" in html
    assert "전일 등락 부호 하락 (+2.3%p)" in html


def test_index_outlook_v2_missing_down_prob_field_key_falls_back():
    """up_prob 만 있고 값이 None 이면(필드는 생겼지만 계산 실패) 폴백 경로로 간다."""
    outlook = {
        "kospi": {
            "score": 0, "span": 0, "score100": None, "label": None,
            "positives": [], "negatives": [],
            "probability": {"prob": None, "n": 0, "method": None, "reason": "표본 부족"},
            "up_prob": None, "down_prob": None,
            "proxy_symbol": "069500",
        },
    }
    html = render(_snap(), _cont(), index_outlook=outlook)
    assert 'class="prob-pair"' not in html
    assert "표본 부족" in html


# ── 4. 인쇄 시 강제 펼침 CSS ─────────────────────────────────────────────


def test_print_media_query_forces_details_open():
    html = render(_snap(), _cont())
    assert "details:not([open]) > *:not(summary){display:block !important}" in html
