"""접힘 요약줄(mod-tldr) 완전성 — 2026-09-07 소유자 폰 피드백 대응.

"리포트에 접어두기로 정보를 깔끔하게 정리한 건 좋은데, 펼쳐보기 전 상태에서도
헤드라인 정도는 정보가 보여야 한다. 지금처럼 '.'만 있으면 눌러야 하는지도
모른다." 실측(오늘 KR 리포트, 모바일 390px)해 보니 원인은 두 갈래였다.

1. 요약줄(.mod-tldr) 자체가 아예 없는 섹션 — "주도 섹터"(sector_daily)와
   "리포트 정확도"(report_accuracy)는 `summarize_*` 함수가 없어 제목+부제만
   보였다(부제도 "거래대금 · 외국인 순매수 · 5일 추이"처럼 항목 이름일 뿐
   숫자가 없었다). → `summarize_sector_daily`/`summarize_report_accuracy` 신설.
2. 접기/펼치기 화살표(`::after`)가 회색 10px "▸" 하나뿐이라 실기기에서 점
   하나로 보였다 — 텍스트("펼치기"/"접기") + 강조색 + 자체 줄로 키운다.

이 파일은 (1)의 순수 함수 단위 테스트 + 렌더 결과 검증, (2)의 CSS 산출물
검증, 그리고 실제 리포트 픽스처 3종 전체를 스캔해 "class=mod인 모든 details
의 summary는 빈 요약줄이 아니다"를 회귀 방지로 고정한다.
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime
from pathlib import Path

from quant.analyze.render import (
    render,
    summarize_money_flow,
    summarize_report_accuracy,
    summarize_sector_daily,
    summarize_us_kr_bridge,
    summarize_us_wrap,
)
from quant.collect.contracts import SCHEMA_VERSION, Snapshot
from quant.core.report_clock import KST

_AT = datetime(2026, 9, 7, 8, 0, tzinfo=KST)


def _snap(market: str = "KR") -> Snapshot:
    return Snapshot(SCHEMA_VERSION, market, date(2026, 9, 7), _AT, {})


def _cont():
    return {
        "005930": {
            "name": "삼성전자", "days": 3, "articles": 5, "today_articles": 4,
            "streak_days": 3, "is_new": False, "history": [True] * 10, "titles": [],
        }
    }


# ── 1a. summarize_sector_daily (신설) ───────────────────────────────────


def test_summarize_sector_daily_shows_count_and_top_sector():
    out = summarize_sector_daily({
        "date": "2026-09-05",
        "sectors": [
            {"rank": 1, "sector": "반도체와반도체장비", "trend": "▲", "foreign_net": 12000,
             "top_members": [{"code": "005930", "name": "삼성전자"}]},
            {"rank": 2, "sector": "화학", "trend": "▽", "foreign_net": -500,
             "top_members": []},
        ],
    })
    assert out == "2개 업종 · 거래대금 1위 반도체와반도체장비"


def test_summarize_sector_daily_missing_flag_says_no_data():
    assert summarize_sector_daily({"missing": True}) == "데이터 없음"


def test_summarize_sector_daily_empty_sectors_says_no_data():
    assert summarize_sector_daily({"date": "2026-09-05", "sectors": []}) == "데이터 없음"


def test_summarize_sector_daily_none_when_absent():
    assert summarize_sector_daily(None) is None
    assert summarize_sector_daily({}) is None


# ── 1b. summarize_report_accuracy (신설) ────────────────────────────────


def test_summarize_report_accuracy_shows_claim_count_when_measured():
    out = summarize_report_accuracy({"measured": True, "n_claims": 12, "min_n": 20})
    assert out == "청구 12건 채점"


def test_summarize_report_accuracy_none_when_not_measured():
    assert summarize_report_accuracy({"measured": False, "n_claims": 0}) is None


def test_summarize_report_accuracy_none_when_absent():
    assert summarize_report_accuracy(None) is None


# ── 1c. 기존 함수 3종 — 데이터는 있지만 라벨이 전부 빠졌을 때 폴백 ───────
#    (섹션 자체는 `{% if x %}`로 열리는데 내부 라벨이 없으면 예전엔 빈 요약줄
#    이었다 — "데이터 없음"으로 막는다.)


def test_summarize_money_flow_falls_back_when_labels_missing():
    assert summarize_money_flow({"flow": {}, "cash": {}}) == "데이터 없음"


def test_summarize_us_wrap_falls_back_when_tone_missing():
    assert summarize_us_wrap({"date": "2026-09-05"}) == "데이터 없음"


def test_summarize_us_kr_bridge_falls_back_when_all_fields_missing():
    assert summarize_us_kr_bridge({"date": "2026-09-05"}) == "데이터 없음"


# ── 2. 렌더 결과 — 새로 생긴 두 섹션이 실제로 접힘줄을 낸다 ──────────────


def _sector_daily_view():
    return {
        "date": "2026-09-05",
        "sectors": [
            {"rank": 1, "sector": "반도체와반도체장비", "trend": "▲", "foreign_net": 12000,
             "top_members": [{"code": "005930", "name": "삼성전자"}]},
        ],
    }


def _report_accuracy_measured():
    empty_h = {"n": 0, "measured": False}
    return {
        "measured": True, "as_of": "2026-09-01", "n_claims": 12, "min_n": 20,
        "direction": {h: dict(empty_h) for h in (1, 3, 5)},
        "regime_direction": {h: dict(empty_h) for h in (1, 3, 5)},
        "candidates": {h: dict(empty_h) for h in (1, 3, 5)},
        "ic": {h: dict(empty_h) for h in (1, 3, 5)},
        "trending_ic": {h: dict(empty_h) for h in (1, 3, 5)},
    }


def test_sector_daily_section_summary_has_tldr_line():
    html = render(_snap(), _cont(), sector_daily=_sector_daily_view())
    assert '<summary>주도 섹터' in html
    assert '<span class="mod-tldr">1개 업종 · 거래대금 1위 반도체와반도체장비</span>' in html


def test_report_accuracy_section_summary_has_tldr_line():
    html = render(_snap(), _cont(), report_accuracy=_report_accuracy_measured())
    assert '<summary>리포트 정확도' in html
    assert '<span class="mod-tldr">청구 12건 채점</span>' in html


def test_us_channel_empty_sections_show_absent_marker_in_summary_not_just_body():
    """usnews_headlines/us_news_kr_view 가 없을 때(채널 재편으로 상시 발생) 이전엔
    본문("채널 없음 — 등록된...")에만 있고 접힌 <summary> 자체엔 아무 사실도
    없었다 — 이제 접힌 채로도 "채널 없음"이 보인다."""
    html = render(_snap(), _cont(), usnews_headlines=None, us_news_kr_view=None)
    assert (
        '<summary>🇺🇸 실시간 헤드라인<span class="sub">텔레그램 미국 뉴스 채널</span>'
        '<span class="mod-tldr">채널 없음</span></summary>'
    ) in html
    assert (
        '<summary>🇺🇸→🇰🇷 미국발 섹터 수혜주<span class="sub">텔레그램 미국 헤드라인 · '
        '섹터별 국내 수혜주</span><span class="mod-tldr">채널 없음</span></summary>'
    ) in html


# ── 3. 어포던스(펼치기/접기) — CSS 산출물 검증 ───────────────────────────


def test_affordance_shows_text_next_to_chevron_for_both_states():
    html = render(_snap(), _cont())
    assert '.mod > summary::after{content:"펼치기 ▸"' in html
    assert 'details.mod[open] > summary::after{content:"접기 ▾"}' in html


def test_summary_row_has_44px_tap_target():
    html = render(_snap(), _cont())
    assert ".mod > summary{cursor:pointer;list-style:none;min-height:44px}" in html


# ── 4. 실제 리포트 픽스처 전체 스캔 — 모든 <details class="mod"> summary는
#    빈 요약줄이 아니다(회귀 방지) ───────────────────────────────────────

sys.path.insert(0, str(Path(__file__).parent / "report" / "fixtures"))
from report_fixtures import (  # noqa: E402 — sys.path 조작 뒤 임포트
    kr_normal_fixture,
    thin_day_fixture,
    us_normal_fixture,
)

from quant.report.render.html import write_open_report  # noqa: E402

_SUMMARY_RE = re.compile(r'<details class="mod"[^>]*>\s*<summary>(.*?)</summary>', re.DOTALL)

_FIXTURES = {
    "kr_normal": kr_normal_fixture,
    "us_normal": us_normal_fixture,
    "thin_day": thin_day_fixture,
}


def _render_fixture(name: str, tmp_path: Path) -> str:
    model, snap = _FIXTURES[name]()
    hp, _jp, _cp = write_open_report(model, snap, tmp_path)
    return hp.read_text(encoding="utf-8")


def test_every_collapsible_mod_summary_has_nonempty_fact_line_kr(tmp_path):
    html = _render_fixture("kr_normal", tmp_path)
    matches = _SUMMARY_RE.findall(html)
    assert matches, "픽스처에 접힘 섹션(<details class=\"mod\">)이 하나도 안 잡혔다"
    for summary_html in matches:
        assert 'class="mod-tldr"' in summary_html, (
            f"요약줄 없는 <summary> 발견: {summary_html[:120]!r}"
        )


def test_every_collapsible_mod_summary_has_nonempty_fact_line_us(tmp_path):
    html = _render_fixture("us_normal", tmp_path)
    matches = _SUMMARY_RE.findall(html)
    assert matches
    for summary_html in matches:
        assert 'class="mod-tldr"' in summary_html, (
            f"요약줄 없는 <summary> 발견: {summary_html[:120]!r}"
        )


def test_every_collapsible_mod_summary_has_nonempty_fact_line_thin_day(tmp_path):
    """후보 1건뿐인 조용한 날 — 대부분 섹션이 비어 아예 안 그려지지만, 그래도
    실제로 그려진 섹션은 전부 요약줄이 있어야 한다."""
    html = _render_fixture("thin_day", tmp_path)
    matches = _SUMMARY_RE.findall(html)
    assert matches
    for summary_html in matches:
        assert 'class="mod-tldr"' in summary_html, (
            f"요약줄 없는 <summary> 발견: {summary_html[:120]!r}"
        )
